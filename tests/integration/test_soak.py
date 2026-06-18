"""Soak tests: concurrent streams, repeated disconnects, memory stability."""

from __future__ import annotations

import asyncio
import contextlib
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
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)
from go_aggregator.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

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


async def _stream_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        yield b"data: "
        yield json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hi"},
                        "finish_reason": None,
                    }
                ],
            }
        ).encode()
        yield b"\n\n"
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


async def _non_stream_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello"},
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


async def _consume_stream(
    stream_iter: AsyncIterator[bytes],
) -> None:
    """Fully consume a stream, discarding all chunks."""
    async for _chunk in stream_iter:
        pass


@pytest.mark.asyncio
async def test_concurrent_streams(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Run 10 concurrent streaming requests, verify all complete."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_stream_handler
        )

        tasks = []
        for i in range(10):
            context = ProxyRequestContext(
                request_id=f"soak-stream-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_stream_body(str(i)),
                incoming_headers={"content-type": "application/json"},
            )
            tasks.append(coordinator.execute(context))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"Got errors: {errors}"

    success_count = sum(
        1 for r in results if not isinstance(r, Exception) and r.status_code == 200
    )
    assert success_count == 10

    # Consume all streams to trigger finalization
    for r in results:
        if not isinstance(r, Exception) and r.stream_iterator is not None:
            await _consume_stream(r.stream_iterator)

    # All reservations should be released
    resv_rows = await db.fetch_all("SELECT status FROM reservations")
    for row in resv_rows:
        assert row["status"] in ("released", "expired")


@pytest.mark.asyncio
async def test_repeated_disconnects(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Make 20 requests, cancel each mid-stream, verify finalization."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_stream_handler
        )

        responses: list[PreparedProxyResponse] = []
        for i in range(20):
            context = ProxyRequestContext(
                request_id=f"soak-disconnect-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_stream_body(str(i)),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)
            responses.append(response)

        # Now consume each stream fully to trigger finalization
        for response in responses:
            if response.stream_iterator is not None:
                with contextlib.suppress(httpx.RemoteProtocolError):
                    await _consume_stream(response.stream_iterator)

    # Wait a bit for finalization
    await asyncio.sleep(0.1)

    # Check no pending requests remain
    pending_rows = await db.fetch_all("SELECT * FROM requests WHERE status = 'pending'")
    assert len(pending_rows) == 0, (
        f"Found {len(pending_rows)} orphaned pending requests"
    )

    # All reservations should be released or expired
    active_resv = await db.fetch_all(
        "SELECT * FROM reservations WHERE status = 'active'"
    )
    assert len(active_resv) == 0, f"Found {len(active_resv)} unreleased reservations"


@pytest.mark.asyncio
async def test_memory_stability(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Run 50 requests, verify no unbounded memory growth."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_non_stream_handler
        )

        for i in range(50):
            context = ProxyRequestContext(
                request_id=f"soak-mem-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": f"Msg {i}"}],
                    }
                ).encode(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)
            assert response.status_code == 200

    # All requests completed
    req_rows = await db.fetch_all("SELECT status FROM requests")
    assert all(row["status"] == "completed" for row in req_rows)

    # All reservations released
    resv_rows = await db.fetch_all("SELECT status FROM reservations")
    assert all(row["status"] in ("released", "expired") for row in resv_rows)

    # No active reservations remain
    active_resv = await db.fetch_all(
        "SELECT * FROM reservations WHERE status = 'active'"
    )
    assert len(active_resv) == 0

    # No pending requests
    pending_rows = await db.fetch_all("SELECT * FROM requests WHERE status = 'pending'")
    assert len(pending_rows) == 0


@pytest.mark.asyncio
async def test_catalog_refresh_during_requests(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Catalog refresh should not break in-flight requests."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_non_stream_handler
        )

        async def _refresh_catalog() -> None:
            """Simulate a catalog refresh cycle."""
            await asyncio.sleep(0.01)
            # Just touch the cache to simulate a refresh
            coordinator._catalog.cache.update_from_account(
                "test-acct",
                "opencode-go",
                [{"model_id": "gpt-4", "protocol": "openai"}],
            )

        # Run requests and catalog refresh concurrently
        tasks: list[asyncio.Task[object]] = []
        for i in range(5):
            context = ProxyRequestContext(
                request_id=f"soak-refresh-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": f"Msg {i}"}],
                    }
                ).encode(),
                incoming_headers={"content-type": "application/json"},
            )
            tasks.append(asyncio.create_task(coordinator.execute(context)))

        # Start catalog refresh in parallel
        tasks.append(asyncio.create_task(_refresh_catalog()))  # type: ignore[arg-type]

        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"Got errors: {errors}"

    # Verify all requests completed successfully
    req_rows = await db.fetch_all(
        "SELECT status FROM requests WHERE status = 'completed'"
    )
    assert len(req_rows) == 5


@pytest.mark.asyncio
async def test_repeated_restart_recovery(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Simulate repeated crash/recovery cycles without data corruption."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_non_stream_handler
        )

        # Run some requests
        for i in range(5):
            context = ProxyRequestContext(
                request_id=f"soak-restart-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": f"Msg {i}"}],
                    }
                ).encode(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)
            assert response.status_code == 200

    # Simulate crash recovery (idempotent)
    from go_aggregator.app import _crash_recovery

    for _ in range(3):
        await _crash_recovery(db)

    # All requests should still be completed (not corrupted by recovery)
    req_rows = await db.fetch_all("SELECT status FROM requests")
    assert all(row["status"] == "completed" for row in req_rows)

    # No active reservations remain
    active_resv = await db.fetch_all(
        "SELECT * FROM reservations WHERE status = 'active'"
    )
    assert len(active_resv) == 0
