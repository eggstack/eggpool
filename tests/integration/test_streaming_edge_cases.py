"""Streaming edge cases: CRLF/LF handling, upstream errors, backpressure."""

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
from go_aggregator.proxy.sse_observer import IncrementalSSEObserver
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
        httpx_client=httpx_client,
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


def _sse_data_line(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}"


def _make_chunk_payload(index: int) -> dict[str, object]:
    return {
        "id": "cmpl-1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {"content": f"chunk-{index}"},
                "finish_reason": None,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Test 1: CRLF vs LF line endings in SSE observer
# ---------------------------------------------------------------------------


def test_crlf_and_lf_streams() -> None:
    """IncrementalSSEObserver handles both CRLF and LF line endings."""
    data_payload = _make_chunk_payload(0)

    # --- CRLF variant ---
    crlf_observer = IncrementalSSEObserver(protocol="openai")
    crlf_bytes = (
        _sse_data_line(data_payload) + "\r\n\r\n" + "data: [DONE]\r\n"
    ).encode()
    crlf_observer.observe(crlf_bytes)
    crlf_observer.flush()

    assert crlf_observer.frame_count == 2  # data line + [DONE] line
    assert crlf_observer.error_count == 0
    assert crlf_observer.bytes_emitted == len(crlf_bytes)

    # --- LF variant ---
    lf_observer = IncrementalSSEObserver(protocol="openai")
    lf_bytes = (_sse_data_line(data_payload) + "\n\n" + "data: [DONE]\n").encode()
    lf_observer.observe(lf_bytes)
    lf_observer.flush()

    assert lf_observer.frame_count == 2
    assert lf_observer.error_count == 0
    assert lf_observer.bytes_emitted == len(lf_bytes)


# ---------------------------------------------------------------------------
# Test 2b: Arbitrary byte splitting produces identical usage
# ---------------------------------------------------------------------------


def _build_usage_stream() -> bytes:
    """Build an SSE stream with a usage payload."""
    usage_payload = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
        }
    }
    return (_sse_data_line(usage_payload) + "\n\n" + "data: [DONE]\n").encode()


def test_arbitrary_splitting_identical_usage() -> None:
    """Splitting the stream at every byte boundary produces identical usage."""
    full_stream = _build_usage_stream()

    # Get expected usage from full stream
    expected = IncrementalSSEObserver(protocol="openai")
    expected.observe(full_stream)
    expected.flush()

    for split_point in range(1, len(full_stream)):
        obs = IncrementalSSEObserver(protocol="openai")
        obs.observe(full_stream[:split_point])
        obs.observe(full_stream[split_point:])
        obs.flush()

        assert obs.usage.input_tokens == expected.usage.input_tokens
        assert obs.usage.output_tokens == expected.usage.output_tokens
        assert obs.bytes_emitted == expected.bytes_emitted


# ---------------------------------------------------------------------------
# Test 2: Upstream non-200 before streaming body
# ---------------------------------------------------------------------------


async def _rate_limit_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        429,
        json={"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
        headers={"retry-after": "30"},
    )


@pytest.mark.asyncio
async def test_upstream_non_200_before_body(
    coordinator: RequestCoordinator,
) -> None:
    """When upstream returns 429 before any body, coordinator returns 429
    with no stream_iterator."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_rate_limit_handler
        )

        context = ProxyRequestContext(
            request_id="edge-429-001",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=_make_stream_body("429-test"),
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 429
    assert response.stream_iterator is None
    assert response.body is not None
    error_body = json.loads(response.body)
    assert "error" in error_body


# ---------------------------------------------------------------------------
# Test 3: Slow consumer backpressure
# ---------------------------------------------------------------------------


NUM_CHUNKS = 50


async def _multi_chunk_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes() -> AsyncGenerator[bytes, None]:  # type: ignore[type-arg]
        for i in range(NUM_CHUNKS):
            chunk_data = _make_chunk_payload(i)
            yield f"data: {json.dumps(chunk_data)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.asyncio
async def test_slow_consumer_backpressure(
    coordinator: RequestCoordinator,
) -> None:
    """Slow downstream consumer receives all chunks without buffer overflow."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_multi_chunk_handler
        )

        context = ProxyRequestContext(
            request_id="edge-backpressure-001",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=_make_stream_body("bp-test"),
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)
        assert response.status_code == 200
        assert response.stream_iterator is not None

        received_chunks: list[bytes] = []
        async for chunk in response.stream_iterator:
            received_chunks.append(chunk)
            # Simulate slow consumer
            await asyncio.sleep(0.001)

    # All data chunks plus the [DONE] chunk should arrive
    assert len(received_chunks) == NUM_CHUNKS + 1

    # Verify total bytes are non-zero and consistent
    total_bytes = sum(len(c) for c in received_chunks)
    assert total_bytes > 0

    # Verify the last meaningful chunk contains [DONE]
    assert b"[DONE]" in received_chunks[-1]
