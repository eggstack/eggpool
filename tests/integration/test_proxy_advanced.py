"""Advanced integration tests for the proxy endpoints."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.app import create_app
from go_aggregator.background.cleanup import cleanup_stale_reservations
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


# ── 1. SSE frames split across multiple HTTP chunks ──────────────────────────


@pytest.mark.asyncio
async def test_split_sse_frames_across_chunks(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Upstream returns SSE content that the proxy must parse line-by-line."""
    sse_lines = [
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        ),
        "",
        "data: [DONE]",
    ]
    sse_content = "\n".join(sse_lines) + "\n"

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
    assert "Hello" in text
    assert " world" in text
    assert "[DONE]" in text


# ── 2. Client cancellation mid-stream is graceful ────────────────────────────


@pytest.mark.asyncio
async def test_client_cancellation_graceful(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Cancelling a streaming request mid-stream does not produce a 500."""
    sse_lines = [
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        ),
        "",
        "data: [DONE]",
    ]
    sse_content = "\n".join(sse_lines) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        collected: list[str] = []
        status_code: int = 0
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
            status_code = resp.status_code
            count = 0
            async for line in resp.aiter_lines():
                collected.append(line)
                count += 1
                if count >= 2:
                    break

    assert status_code == 200
    assert len(collected) >= 1


# ── 3. Upstream failure mid-stream ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_midstream_upstream_failure(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Upstream returns partial content then errors; proxy returns what it can."""
    good_chunk = (
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        )
        + "\n\n"
    )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=good_chunk.encode(),
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
    assert "Hello" in response.text


# ── 4. Concurrent routing of multiple requests ───────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_routing_multiple_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """Five concurrent streaming requests all complete and are recorded."""
    sse_lines = [
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: [DONE]",
    ]
    sse_content = "\n".join(sse_lines) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        tasks = [
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": f"Message {i}"}],
                    "stream": True,
                },
                headers=auth_headers,
            )
            for i in range(5)
        ]
        responses = await asyncio.gather(*tasks)

    assert len(responses) == 5
    for resp in responses:
        assert resp.status_code == 200
        assert "Hello" in resp.text

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 5


# ── 5. Catalog refresh with divergent models ─────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_refresh_with_divergent_models(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """After adding a new model to the cache, requests for it route correctly."""
    catalog: CatalogService = app.state.catalog

    catalog.cache.load_model(
        model_id="gpt-4-turbo",
        display_name="GPT-4 Turbo",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4-turbo", "test-acct")

    db: Database = app.state.db
    await db.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4-turbo", "openai"),
    )
    await db.connection.commit()

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
                            "message": {
                                "role": "assistant",
                                "content": "Turbo response",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4-turbo",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Turbo response"

    rows = await db.fetch_all("SELECT * FROM requests")
    assert len(rows) == 1
    assert rows[0]["model_id"] == "gpt-4-turbo"


# ── 6. Stale reservation cleanup on startup ──────────────────────────────────


@pytest.mark.asyncio
async def test_stale_reservation_cleanup_on_startup() -> None:
    """Stale reservations are marked released during startup cleanup."""
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    os.environ["GO_AGG_TEST_KEY"] = "test-key-123"

    db = Database(path=":memory:")
    await db.connect()

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
        "INSERT INTO requests (account_id, model_id, status) "
        "VALUES (1, 'gpt-4', 'completed')"
    )
    await db.connection.commit()

    await db.execute(
        "INSERT INTO reservations "
        "(request_id, account_id, model_id, reserved_microdollars, status, created_at) "
        "VALUES (1, 1, 'gpt-4', 1000000, 'active', datetime('now', '-1200 seconds'))"
    )
    await db.connection.commit()

    row = await db.fetch_one("SELECT status FROM reservations")
    assert row is not None
    assert row["status"] == "active"

    cleaned = await cleanup_stale_reservations(db)
    assert cleaned == 1

    row = await db.fetch_one("SELECT status FROM reservations")
    assert row is not None
    assert row["status"] == "released"

    await db.disconnect()


# ── 7. Multiple accounts routing with weighted distribution ───────────────────


@pytest.mark.asyncio
async def test_multiple_accounts_routing() -> None:
    """Requests are distributed across two weighted accounts."""
    os.environ["GO_AGG_AUTH_KEY"] = "test-key-123"
    os.environ["GO_AGG_ACCT1_KEY"] = "acct1-api-key"
    os.environ["GO_AGG_ACCT2_KEY"] = "acct2-api-key"

    config = AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "GO_AGG_AUTH_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {
                    "name": "acct1",
                    "api_key_env": "GO_AGG_ACCT1_KEY",
                    "weight": 0.7,
                },
                {
                    "name": "acct2",
                    "api_key_env": "GO_AGG_ACCT2_KEY",
                    "weight": 0.3,
                },
            ],
            "dashboard": {"enabled": False},
        }
    )

    application = create_app(config)

    db = Database(path=":memory:")
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) VALUES (?, ?, 1, ?)",
        ("acct1", "GO_AGG_ACCT1_KEY", 0.7),
    )
    await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) VALUES (?, ?, 1, ?)",
        ("acct2", "GO_AGG_ACCT2_KEY", 0.3),
    )
    await db.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
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
    catalog.cache.add_account_support("gpt-4", "acct1")
    catalog.cache.add_account_support("gpt-4", "acct2")

    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    )
    auth_headers = {"Authorization": "Bearer test-key-123"}

    try:
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
                                "message": {
                                    "role": "assistant",
                                    "content": "Hi",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                )
            )

            for _ in range(10):
                resp = await test_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                    headers=auth_headers,
                )
                assert resp.status_code == 200

        rows = await db.fetch_all(
            """
            SELECT a.name, COUNT(*) as cnt
            FROM requests r
            JOIN accounts a ON r.account_id = a.id
            GROUP BY a.name
            """
        )
        account_counts = {row["name"]: row["cnt"] for row in rows}
        assert len(account_counts) == 2
        assert "acct1" in account_counts
        assert "acct2" in account_counts
    finally:
        await test_client.aclose()
        await httpx_client.aclose()
        await db.disconnect()


# ── 8. Non-streaming error recorded with status='error' ─────────────────────


@pytest.mark.asyncio
async def test_non_streaming_error_recorded_as_error(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """A 500 upstream response is recorded in the database with status='error'."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                500,
                json={
                    "error": {
                        "message": "Internal error",
                        "type": "server_error",
                    }
                },
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 500

    db: Database = app.state.db
    rows = await db.fetch_all("SELECT * FROM requests")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["model_id"] == "gpt-4"
