"""E2E concurrency regression test for transcoded streaming.

Spawns concurrent transcoded streams alongside non-streaming probes
through a wired ``RequestCoordinator`` against mocked upstreams and
asserts the runtime state is fully reconciled after the burst.

Run with::

    pytest tests/integration/test_streaming_transcode_concurrency.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any

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
from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_COMPLETED,
    get_stream_diagnostics,
)
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.request_path

UPSTREAM_BASE = "https://transcode-concurrency-test.example.com"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_config() -> AppConfig:
    os.environ["TRANSCODE_CONCURRENCY_KEY"] = "tc-key-000"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "TRANSCODE_CONCURRENCY_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "tc-acct", "api_key_env": "TRANSCODE_CONCURRENCY_KEY"},
            ],
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
            ("tc-acct", "TRANSCODE_CONCURRENCY_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3-haiku-20240307", "anthropic"),
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
    for model_id, proto in [
        ("gpt-4", "openai"),
        ("claude-3-haiku-20240307", "anthropic"),
        # Register with both protocols so transcodable routes exist
        ("claude-3-haiku-20240307", "openai"),
    ]:
        catalog.cache.load_model(
            model_id=model_id,
            display_name=model_id,
            protocol=proto,
            capabilities={},
            source_metadata={},
        )
        catalog.cache.add_account_support(model_id, "tc-acct")
    health_manager = HealthManager()
    router = Router(registry, catalog)
    router.set_account_weight("tc-acct", 1.0)
    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=RequestRepository(db),
        reservation_repo=ReservationRepository(db),
        attempt_repo=AttemptRepository(db),
        usage_window_repo=UsageWindowRepository(db),
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


# ---------------------------------------------------------------------------
# Upstream SSE builders
# ---------------------------------------------------------------------------


def _anthropic_sse_upstream_response(
    *,
    chunks_per_stream: int = 6,
    chunk_delay_s: float = 0.05,
) -> httpx.Response:
    """Build a slow-streaming Anthropic SSE response for mocked upstream."""
    events: list[str] = []
    # message_start
    ms = json.dumps(
        {
            "type": "message_start",
            "message": {
                "id": "msg-upstream-001",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-3-haiku-20240307",
                "stop_reason": None,
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
        separators=(",", ":"),
    )
    events.append(f"event: message_start\ndata: {ms}\n\n")
    # content_block_start
    cbs = json.dumps(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        separators=(",", ":"),
    )
    events.append(f"event: content_block_start\ndata: {cbs}\n\n")
    # content_block_delta × chunks
    for i in range(chunks_per_stream):
        delta = json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f"word-{i} "},
            },
            separators=(",", ":"),
        )
        events.append(f"event: content_block_delta\ndata: {delta}\n\n")
    # message_delta
    md = json.dumps(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": chunks_per_stream},
        },
        separators=(",", ":"),
    )
    events.append(f"event: message_delta\ndata: {md}\n\n")
    # message_stop
    events.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')

    body = "".join(events)
    content = body.encode()
    return httpx.Response(
        200,
        headers=[
            ("content-type", "text/event-stream"),
            ("x-request-id", "req-upstream-001"),
        ],
        content=content,
    )


def _openai_sse_upstream_response(
    *,
    chunks_per_stream: int = 6,
    chunk_delay_s: float = 0.05,
) -> httpx.Response:
    """Build a slow-streaming OpenAI SSE response for mocked upstream."""
    events: list[str] = []
    # initial role chunk
    role_chunk = json.dumps(
        {
            "id": "chatcmpl-upstream-001",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        },
        separators=(",", ":"),
    )
    events.append(f"data: {role_chunk}\n\n")
    # content chunks
    for i in range(chunks_per_stream):
        chunk = json.dumps(
            {
                "id": "chatcmpl-upstream-001",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": f"word-{i} "},
                        "finish_reason": None,
                    }
                ],
            },
            separators=(",", ":"),
        )
        events.append(f"data: {chunk}\n\n")
    # finish
    finish = json.dumps(
        {
            "id": "chatcmpl-upstream-001",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": chunks_per_stream,
                "total_tokens": 5 + chunks_per_stream,
            },
        },
        separators=(",", ":"),
    )
    events.append(f"data: {finish}\n\n")
    events.append("data: [DONE]\n\n")

    body = "".join(events)
    content = body.encode()
    return httpx.Response(
        200,
        headers=[
            ("content-type", "text/event-stream"),
            ("x-request-id", "req-upstream-002"),
        ],
        content=content,
    )


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _make_stream_body(model: str = "gpt-4") -> bytes:
    return json.dumps(
        {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()


def _make_non_stream_body(model: str = "gpt-4") -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode()


def _build_context(
    request_id: str,
    *,
    model: str = "gpt-4",
    streaming: bool = True,
    protocol: str = "openai",
) -> ProxyRequestContext:
    body = _make_stream_body(model) if streaming else _make_non_stream_body(model)
    return ProxyRequestContext(
        request_id=request_id,
        protocol=protocol,
        model_id=model,
        streaming=streaming,
        original_body=body,
        incoming_headers={"content-type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestStreamingTranscodeConcurrency:
    """E2E regression: concurrent transcoded streams + non-stream probes."""

    @pytest.mark.asyncio()
    async def test_concurrent_transcoded_streams(
        self, coordinator: RequestCoordinator
    ) -> None:
        import uuid

        diagnostics = get_stream_diagnostics()
        initial_snap = diagnostics.snapshot()
        initial_outcomes = dict(initial_snap["outcomes"])

        n_streams = 5
        n_probes = 5
        chunk_delay_s = 0.05
        budget_s = 30.0

        with respx.mock(assert_all_called=False) as mock:
            # Mock Anthropic upstream (OpenAI client → Anthropic upstream)
            async def _anthropic_side_effect(request: httpx.Request) -> httpx.Response:
                return _anthropic_sse_upstream_response(
                    chunks_per_stream=6,
                    chunk_delay_s=chunk_delay_s,
                )

            mock.post(f"{UPSTREAM_BASE}/v1/messages").mock(
                side_effect=_anthropic_side_effect
            )

            # Mock OpenAI upstream (native OpenAI → OpenAI upstream)
            async def _openai_side_effect(request: httpx.Request) -> httpx.Response:
                return _openai_sse_upstream_response(
                    chunks_per_stream=6,
                    chunk_delay_s=chunk_delay_s,
                )

            mock.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_openai_side_effect
            )

            t_start = time.perf_counter()

            # Spawn concurrent transcoded streams (OpenAI client → Anthropic upstream)
            async def _drive_stream(idx: int) -> dict[str, Any]:
                req_id = f"stream-{uuid.uuid4().hex[:8]}-{idx}"
                ctx = _build_context(
                    req_id,
                    model="claude-3-haiku-20240307",
                    streaming=True,
                    protocol="openai",
                )
                response = await coordinator.execute(ctx)
                if response.stream_iterator is not None:
                    chunk_count = 0
                    async for _chunk in response.stream_iterator:
                        chunk_count += 1
                    return {
                        "outcome": "completed",
                        "chunks": chunk_count,
                        "status": response.status_code,
                    }
                return {
                    "outcome": "no_stream",
                    "status": response.status_code,
                }

            # Spawn concurrent non-stream probes (native OpenAI)
            async def _drive_probe(idx: int) -> dict[str, Any]:
                req_id = f"probe-{uuid.uuid4().hex[:8]}-{idx}"
                ctx = _build_context(
                    req_id,
                    model="gpt-4",
                    streaming=False,
                    protocol="openai",
                )
                response = await coordinator.execute(ctx)
                return {
                    "outcome": "completed",
                    "status": response.status_code,
                }

            tasks: list[asyncio.Task[dict[str, Any]]] = []
            for i in range(n_streams):
                tasks.append(asyncio.create_task(_drive_stream(i)))
            for i in range(n_probes):
                tasks.append(asyncio.create_task(_drive_probe(i)))

            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=budget_s,
                )
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            wall_ms = (time.perf_counter() - t_start) * 1000

        # Verify all tasks completed successfully
        stream_results = []
        probe_results = []
        exceptions = []
        for r in results:
            if isinstance(r, BaseException):
                exceptions.append(r)
            elif isinstance(r, dict) and r.get("outcome") == "completed":
                # Determine if it was a stream or probe by checking chunks
                if "chunks" in r:
                    stream_results.append(r)
                else:
                    probe_results.append(r)
            else:
                exceptions.append(r)

        print(f"\n  Wall time: {wall_ms:.0f}ms")
        print(f"  Streams completed: {len(stream_results)}/{n_streams}")
        print(f"  Probes completed: {len(probe_results)}/{n_probes}")
        print(f"  Exceptions: {len(exceptions)}")
        for exc in exceptions:
            print(f"    {exc}")

        # All streams and probes must complete
        assert len(exceptions) == 0, f"Got exceptions: {exceptions}"
        assert len(stream_results) == n_streams
        assert len(probe_results) == n_probes

        # All streams must have status 200
        for sr in stream_results:
            assert sr["status"] == 200, f"Stream got status {sr['status']}"
            assert sr["chunks"] > 0, "Stream produced no chunks"

        # Verify no leaked state via DB query
        pending = await coordinator._db.fetch_one(
            "SELECT COUNT(*) AS c FROM requests WHERE status = 'pending'"
        )
        pending_count = int(pending["c"] if pending else 0)
        assert pending_count == 0, f"Leaked pending requests: {pending_count}"

        active_res = await coordinator._db.fetch_one(
            "SELECT COUNT(*) AS c FROM reservations WHERE released_at IS NULL"
        )
        active_reservations = int(active_res["c"] if active_res else 0)
        assert active_reservations == 0, (
            f"Leaked active reservations: {active_reservations}"
        )

        # Stream diagnostics should show completed streams
        final_snap = diagnostics.snapshot()
        final_outcomes = dict(final_snap["outcomes"])
        completed_delta = final_outcomes.get(
            STREAM_OUTCOME_COMPLETED, 0
        ) - initial_outcomes.get(STREAM_OUTCOME_COMPLETED, 0)
        print(f"  Stream diagnostics completed delta: {completed_delta}")
        assert completed_delta >= n_streams, (
            f"Expected at least {n_streams} completed diagnostics, "
            f"got {completed_delta}"
        )
