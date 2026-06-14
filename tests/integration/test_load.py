"""Load and soak tests for concurrent and long-running proxy behavior."""

from __future__ import annotations

import asyncio
import os
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
from go_aggregator.models.config import AppConfig
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

    await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("test-acct", "OPENCODE_TEST_KEY"),
    )
    await db.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
    )
    await db.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("claude-3", "anthropic"),
    )
    await db.connection.commit()

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
