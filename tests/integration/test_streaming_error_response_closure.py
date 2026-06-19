"""Integration tests for streaming pre-body response closure (Phase 14)."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.routing.router import Router

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
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
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
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


def _make_stream_body(request_id: str) -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": f"Message {request_id}"}],
            "stream": True,
        }
    ).encode()


async def _error_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        500,
        json={"error": {"message": "Internal server error", "type": "server_error"}},
    )


@pytest.mark.asyncio
async def test_streaming_error_response_aclose_called(
    coordinator: RequestCoordinator,
) -> None:
    """When a streaming request gets status >= 400, response.aclose() is called."""
    close_called = False

    async def _tracking_send(
        request: httpx.Request,
        *,
        stream: bool = False,
    ) -> httpx.Response:
        nonlocal close_called
        resp = httpx.Response(
            500,
            json={
                "error": {
                    "message": "Internal server error",
                    "type": "server_error",
                }
            },
        )

        class _TrackableResponse:
            def __init__(self, r: httpx.Response) -> None:
                self._real = r

            @property
            def status_code(self) -> int:
                return self._real.status_code

            @property
            def headers(self) -> httpx.Headers:
                return self._real.headers

            @property
            def content(self) -> bytes:
                return self._real.content

            async def aread(self) -> bytes:
                return await self._real.aread()

            async def aclose(self) -> None:
                nonlocal close_called
                close_called = True
                await self._real.aclose()

        return _TrackableResponse(resp)  # type: ignore[return-value]

    with patch.object(coordinator._client, "send", _tracking_send):
        context = ProxyRequestContext(
            request_id="closure-test-001",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=_make_stream_body("closure-test"),
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 500
    assert response.body is not None
    assert close_called, "response.aclose() was not called for error response"


@pytest.mark.asyncio
async def test_streaming_500_exhausted_returns_error_response(
    coordinator: RequestCoordinator,
) -> None:
    """Streaming 500 errors exhaust retries and return an error response."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_error_handler)

        context = ProxyRequestContext(
            request_id="closure-exhaust-001",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=_make_stream_body("exhaust-test"),
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 500
    assert response.body is not None
    error_body = json.loads(response.body)
    assert "error" in error_body


@pytest.mark.asyncio
async def test_streaming_prebody_error_headers_are_filtered(
    coordinator: RequestCoordinator,
) -> None:
    """Streaming pre-body errors should not leak hop-by-hop headers."""

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                500,
                content=b'{"error":{"message":"fail"}}',
                headers={
                    "connection": "keep-alive",
                    "transfer-encoding": "chunked",
                    "x-request-id": "up-123",
                },
            )
        )

        context = ProxyRequestContext(
            request_id="closure-header-filter-001",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=_make_stream_body("header-filter-test"),
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 500
    headers_dict = dict(response.headers)
    lower_headers = {key.lower() for key in headers_dict}
    assert "connection" not in lower_headers
    assert "transfer-encoding" not in lower_headers
    assert headers_dict.get("x-request-id") == "up-123"


@pytest.mark.asyncio
async def test_streaming_prebody_error_response_closed_after_aread() -> None:
    """Direct test: a streaming 500 response can be read and closed cleanly."""

    class _TestTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self,
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                content=b'{"error": "internal"}',
            )

    client = httpx.AsyncClient(transport=_TestTransport())
    request = client.build_request(
        "POST",
        f"{UPSTREAM_BASE}/chat/completions",
        content=b'{"model": "gpt-4", "messages": []}',
    )
    response = await client.send(request, stream=True)

    assert response.status_code == 500
    await response.aread()
    assert response.content == b'{"error": "internal"}'

    await response.aclose()
    assert response.status_code == 500

    await client.aclose()
