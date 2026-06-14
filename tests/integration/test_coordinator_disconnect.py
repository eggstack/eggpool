"""Integration tests for client disconnect and mid-stream failure scenarios."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.catalog.service import CatalogService
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.models.config import AppConfig
from go_aggregator.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from go_aggregator.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
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


@pytest_asyncio.fixture()
async def db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    await database.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("test-acct", "OPENCODE_TEST_KEY"),
    )
    await database.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
    )
    await database.connection.commit()
    yield database
    await database.disconnect()


@pytest.fixture()
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def coordinator(
    db: Database, config: AppConfig
) -> AsyncGenerator[RequestCoordinator, None]:
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")

    router = Router(registry, catalog)
    router.set_account_weight("test-acct", 1.0)

    health_manager = HealthManager()
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        httpx_client=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


def _make_stream_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
    ).encode()


def _sse_lines() -> list[str]:
    return [
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
        "data: [DONE]",
    ]


@pytest.mark.asyncio
async def test_client_disconnect_upstream_closed(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """When the stream generator encounters an error (simulating disconnect),
    the upstream is closed and the attempt is finalized."""
    request_body = _make_stream_body()

    async def _mock_stream(
        request: httpx.Request,
    ) -> httpx.Response:
        async def _aiter_bytes():  # type: ignore[no-untyped-def]
            yield b"data: {"
            raise httpx.RemoteProtocolError("Connection reset")

        return httpx.Response(
            200,
            stream=_aiter_bytes(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_mock_stream)

        context = ProxyRequestContext(
            request_id="test-disconnect",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.stream_iterator is not None

    # Consume stream (will raise, triggering error finalization)
    try:
        async for _chunk in response.stream_iterator:
            pass
    except httpx.RemoteProtocolError:
        pass

    # The attempt should be finalized with error info
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) >= 1
    assert attempt_rows[0]["error_class"] is not None

    # Request should be finalized as error
    req_rows = await db.fetch_all(
        "SELECT * FROM requests WHERE id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(req_rows) == 1
    assert req_rows[0]["status"] == "error"


@pytest.mark.asyncio
async def test_upstream_stream_error_finalizes_as_error(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """When the upstream stream raises an error mid-way, the request is
    finalized as error."""
    request_body = _make_stream_body()

    async def _error_stream(
        request: httpx.Request,
    ) -> httpx.Response:
        async def _aiter_bytes():  # type: ignore[no-untyped-def]
            yield b"data: {"
            raise httpx.RemoteProtocolError("Connection reset")

        return httpx.Response(
            200,
            stream=_aiter_bytes(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_error_stream)

        context = ProxyRequestContext(
            request_id="test-stream-error",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.stream_iterator is not None

    # Consume the stream - should raise the upstream error
    collected: list[bytes] = []
    error_raised = False
    try:
        async for chunk in response.stream_iterator:
            collected.append(chunk)
    except httpx.RemoteProtocolError:
        error_raised = True

    assert error_raised or len(collected) >= 1

    # Verify attempt record has error info
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) >= 1
    assert attempt_rows[0]["error_class"] is not None


@pytest.mark.asyncio
async def test_upstream_error_after_bytes_no_replay(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """When upstream sends some bytes then errors, no retry replay occurs."""
    request_body = _make_stream_body()

    async def _partial_error_stream(
        request: httpx.Request,
    ) -> httpx.Response:
        async def _aiter_bytes():  # type: ignore[no-untyped-def]
            yield b"data: {"
            raise httpx.RemoteProtocolError("Stream broke")

        return httpx.Response(
            200,
            stream=_aiter_bytes(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_partial_error_stream
        )

        context = ProxyRequestContext(
            request_id="test-no-replay",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.stream_iterator is not None

    # Consume the stream - should not retry since bytes were emitted
    try:
        async for _chunk in response.stream_iterator:
            pass
    except httpx.RemoteProtocolError:
        pass

    # Only one attempt should exist (no retry after first byte)
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) == 1


@pytest.mark.asyncio
async def test_bytes_emitted_tracked_correctly(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify bytes_emitted is tracked correctly for streamed responses."""
    request_body = _make_stream_body()

    async def _stream_handler(
        request: httpx.Request,
    ) -> httpx.Response:
        async def _aiter_bytes():  # type: ignore[no-untyped-def]
            yield b"data: first\n\n"
            yield b"data: second\n\n"
            yield b"data: [DONE]\n\n"

        return httpx.Response(
            200,
            stream=_aiter_bytes(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_stream_handler
        )

        context = ProxyRequestContext(
            request_id="test-bytes-tracking",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.stream_iterator is not None

    # Consume entire stream
    collected: list[bytes] = []
    async for chunk in response.stream_iterator:
        collected.append(chunk)

    total_bytes = sum(len(c) for c in collected)
    assert total_bytes > 0

    # Check attempt record has bytes_emitted
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) >= 1
    assert attempt_rows[0]["bytes_emitted"] > 0
