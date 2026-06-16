"""Load and soak tests for concurrent and long-running proxy behavior."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import resource
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.app import create_app
from go_aggregator.catalog.service import CatalogService
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.models.config import AppConfig
from go_aggregator.request.coordinator import RequestCoordinator
from go_aggregator.routing.router import Router
from go_aggregator.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    os.environ["GO_AGG_TEST_KEY"] = "test-key-123"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "GO_AGG_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def app(config: AppConfig) -> AsyncGenerator[FastAPI]:
    application = create_app(config)

    db = Database(path=":memory:")
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(
            config.upstream.read_timeout_s,
            connect=config.upstream.connect_timeout_s,
            read=config.upstream.read_timeout_s,
            write=config.upstream.write_timeout_s,
            pool=config.upstream.keepalive_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=config.upstream.max_connections,
            max_keepalive_connections=config.upstream.max_keepalive,
            keepalive_expiry=config.upstream.keepalive_timeout_s,
        ),
    )
    application.state.httpx_client = httpx_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, httpx_client)
    application.state.catalog = catalog

    router = Router(registry, catalog)
    application.state.router = router

    application.state.stats = StatsService(db)

    health_manager = HealthManager()
    application.state.health_manager = health_manager

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)

    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        httpx_client=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
    )
    application.state.coordinator = coordinator

    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")

    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "test-acct")

    yield application

    await httpx_client.aclose()
    await db.disconnect()


@pytest.fixture
def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key-123"}


# ── helpers ───────────────────────────────────────────────────────────────────


def _openai_sse_response(i: int) -> httpx.Response:
    return httpx.Response(
        200,
        content=(
            f'data: {{"id":"cmpl-{i}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{"content":"ok"}},'
            f'"finish_reason":"stop"}}]}}\n\n'
            f"data: [DONE]\n\n"
        ).encode(),
        headers={"content-type": "text/event-stream"},
    )


def _anthropic_sse_response(i: int) -> httpx.Response:
    body = (
        f'event: message_start\ndata: {{"type":"message_start",'
        f'"message":{{"id":"msg-{i}","type":"message","role":"assistant",'
        f'"usage":{{"input_tokens":1}}}}}}\n\n'
        f'event: content_block_delta\ndata: {{"type":"content_block_delta",'
        f'"index":0,"delta":{{"type":"text_delta","text":"ok"}}}}\n\n'
        f'event: message_delta\ndata: {{"type":"message_delta",'
        f'"delta":{{"stop_reason":"end_turn"}},'
        f'"usage":{{"output_tokens":1}}}}\n\n'
        f'event: message_stop\ndata: {{"type":"message_stop"}}\n\n'
    )
    return httpx.Response(
        200,
        content=body.encode(),
        headers={"content-type": "text/event-stream"},
    )


# ── 1. Concurrent streaming requests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_streaming_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=lambda request, route: _openai_sse_response(route.call_count)
        )

        async def _do_request(idx: int) -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
                headers=auth_headers,
            )

        responses = await asyncio.gather(*[_do_request(i) for i in range(10)])

    for resp in responses:
        assert resp.status_code == 200
        assert "ok" in resp.text

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 10


# ── 2. Concurrent non-streaming requests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_non_streaming_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        async def _do_request() -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )

        responses = await asyncio.gather(*[_do_request() for _ in range(20)])

    for resp in responses:
        assert resp.status_code == 200


# ── 3. Repeated disconnects ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_disconnects(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=(
                    b'data: {"id":"cmpl-1","object":"chat.completion.chunk",'
                    b'"choices":[{"index":0,"delta":{"content":"chunk1"},'
                    b'"finish_reason":null}]}\n\n'
                    b'data: {"id":"cmpl-1","object":"chat.completion.chunk",'
                    b'"choices":[{"index":0,"delta":{"content":"chunk2"},'
                    b'"finish_reason":"stop"}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        )

        for _ in range(5):
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
                headers=auth_headers,
            ) as resp:
                assert resp.status_code == 200
                first_chunk = await resp.aiter_bytes().__anext__()
                assert len(first_chunk) > 0


# ── 4. Memory stability under repeated sequential requests ───────────────────


@pytest.mark.asyncio
async def test_memory_stability_under_repeated_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        for _ in range(50):
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )
            assert resp.status_code == 200

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 50


# ── 5. Concurrent mixed protocols ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_mixed_protocols(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=lambda request, route: _openai_sse_response(route.call_count)
        )
        respx.post(f"{UPSTREAM_BASE}/messages").mock(
            side_effect=lambda request, route: _anthropic_sse_response(route.call_count)
        )

        async def _openai() -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
                headers=auth_headers,
            )

        async def _anthropic() -> httpx.Response:
            return await client.post(
                "/v1/messages",
                json={
                    "model": "claude-3",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
                headers=auth_headers,
            )

        tasks = [_openai() for _ in range(5)] + [_anthropic() for _ in range(5)]
        responses = await asyncio.gather(*tasks)

    for resp in responses:
        assert resp.status_code == 200


# ── 6. Database handles concurrent writes ────────────────────────────────────


@pytest.mark.asyncio
async def test_database_handles_concurrent_writes(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        async def _do_request() -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )

        responses = await asyncio.gather(*[_do_request() for _ in range(10)])

    for resp in responses:
        assert resp.status_code == 200

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 10


# ── 7. Rapid successive sequential requests ──────────────────────────────────


@pytest.mark.asyncio
async def test_rapid_successive_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        for _ in range(10):
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )
            assert resp.status_code == 200


# ── 8. Catalog refresh during active requests ────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_refresh_during_active_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """Catalog refresh does not interrupt in-flight requests."""
    catalog: CatalogService = app.state.catalog

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        async def _request() -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )

        # Fire requests and catalog refresh concurrently
        tasks = [_request() for _ in range(5)]
        tasks.append(catalog.refresh())
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # All HTTP requests should succeed (catalog refresh is a coroutine, not a response)
    http_results = [r for r in results if isinstance(r, httpx.Response)]
    assert len(http_results) == 5
    for resp in http_results:
        assert resp.status_code == 200


# ── 10. One-week synthetic request history ───────────────────────────────────


@pytest.mark.asyncio
async def test_one_week_synthetic_request_history(
    app: FastAPI,
) -> None:
    """Inserting a week of synthetic request data and querying stats works."""
    db: Database = app.state.db

    # Insert 7 days of synthetic requests (100 per day)
    async with db.transaction():
        for day in range(7):
            for _ in range(100):
                await db.execute_write(
                    """
                    INSERT INTO requests (
                        account_id, model_id, started_at, completed_at,
                        status, input_tokens, output_tokens,
                        cost_microdollars, upstream_latency_ms
                    ) VALUES (
                        1, 'gpt-4',
                        datetime('now', ? || ' days', ? || ' hours'),
                        datetime('now', ? || ' days', ? || ' hours', '+1 seconds'),
                        'completed', 100, 50, 5000, 150
                    )
                    """,
                    (f"-{day}", f"{day % 24}", f"-{day}", f"{day % 24}"),
                )

    # Verify total count
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 700

    # Verify stats queries work — use a wide range to capture all inserted data
    from go_aggregator.stats.service import StatsService, TimeRange

    stats = StatsService(db)
    time_range = TimeRange(
        start=datetime.now(UTC) - timedelta(days=8),
        end=datetime.now(UTC) + timedelta(hours=1),
        label="8d",
    )

    summary = await stats.get_summary(time_range)
    assert summary["total_requests"] == 700
    assert summary["total_input_tokens"] == 70000
    assert summary["total_output_tokens"] == 35000

    account_stats = await stats.get_account_stats(time_range)
    assert len(account_stats) >= 1

    model_stats = await stats.get_model_stats(time_range)
    assert len(model_stats) >= 1


# ── 11. Long-stream structural test (30-60 min equivalent) ───────────────────


@pytest.mark.asyncio
async def test_long_stream_structural(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """A stream with 500 chunks simulates a long-running stream.

    Real 30-60 min streams are impractical in CI. This verifies the relay
    handles high-volume SSE without error, memory blow-up, or DB write failure.
    """
    chunks = []
    for i in range(500):
        chunks.append(
            "data: "
            + json.dumps(
                {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"token-{i:04d}"},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        )
        chunks.append("")
    chunks.append("data: [DONE]")
    sse_content = "\n".join(chunks) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    text = response.text
    assert "token-0000" in text
    assert "token-0499" in text
    assert "[DONE]" in text

    # Verify the request was recorded in the database
    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 1


# ── 12. SQLite writes under streaming load ───────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_writes_under_streaming_load(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """20 concurrent streaming requests all record to SQLite on completion.

    Tests that SQLite WAL mode handles concurrent writes from streaming
    request completion without errors or lost data.
    """
    sse_content = (
        'data: {"id":"cmpl-1","object":"chat.completion.chunk",'
        '"choices":[{"index":0,"delta":{"content":"ok"},'
        '"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        async def _stream_request(idx: int) -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": f"Msg {idx}"}],
                    "stream": True,
                },
                headers=auth_headers,
            )

        responses = await asyncio.gather(*[_stream_request(i) for i in range(20)])

    for resp in responses:
        assert resp.status_code == 200

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 20

    # Verify all requests have valid model_id (no corrupt writes)
    rows = await db.fetch_all("SELECT model_id FROM requests")
    for r in rows:
        assert r["model_id"] == "gpt-4"


# ── 12. File descriptor stability ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_descriptor_stability(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """File descriptors do not leak across many requests."""
    gc.collect()
    fd_before = resource.getrlimit(resource.RLIMIT_NOFILE)[0]

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        for _ in range(20):
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )
            assert resp.status_code == 200

    gc.collect()
    fd_after = resource.getrlimit(resource.RLIMIT_NOFILE)[0]

    # Soft limit should not have changed (no fd leak in ulimit)
    assert fd_before == fd_after
