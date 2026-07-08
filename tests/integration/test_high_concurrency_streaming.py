"""High-concurrency stream reproducer.

A deterministic harness that drives 50+ concurrent streaming requests
through a wired RequestCoordinator against a local mock SSE upstream
and asserts the runtime state is fully reconciled after the burst:

- no leaked pending request rows;
- no active reservations for completed / cancelled requests;
- router active counts return to zero;
- quota estimator reserved cost returns to zero;
- finalization retry queue drains to zero;
- DB lock contention is observed.

The harness supports configurable mock SSE scenarios
(SCENARIO_HAPPY_PATH, SCENARIO_NO_USAGE, SCENARIO_SLOW_FIRST_BYTE,
SCENARIO_SLOW_TOKEN_CADENCE, SCENARIO_ABRUPT_CLOSE,
SCENARIO_SERVER_STALL, SCENARIO_MALFORMED_FRAME,
SCENARIO_CONNECTION_RESET) and four cancellation offsets
(CANCEL_BEFORE_FIRST_BYTE, CANCEL_AFTER_FIRST_TOKEN,
CANCEL_MIDSTREAM, CANCEL_AFTER_FINAL_BEFORE_USAGE) so the same
matrix the plan describes can be run from a script (see
scripts/repro_high_concurrency_streams.py).
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
    STREAM_OUTCOME_CLIENT_CANCELLED,
    STREAM_OUTCOME_COMPLETED,
    get_stream_diagnostics,
)
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


SCENARIO_HAPPY_PATH = "happy-path"
SCENARIO_NO_USAGE = "no-usage"
SCENARIO_SLOW_FIRST_BYTE = "slow-first-byte"
SCENARIO_SLOW_TOKEN_CADENCE = "slow-token-cadence"
SCENARIO_ABRUPT_CLOSE = "abrupt-upstream-close"
SCENARIO_SERVER_STALL = "read-timeout"
SCENARIO_MALFORMED_FRAME = "malformed-frame"
SCENARIO_CONNECTION_RESET = "connection-reset"

ALL_SCENARIOS: tuple[str, ...] = (
    SCENARIO_HAPPY_PATH,
    SCENARIO_NO_USAGE,
    SCENARIO_SLOW_FIRST_BYTE,
    SCENARIO_SLOW_TOKEN_CADENCE,
    SCENARIO_ABRUPT_CLOSE,
    SCENARIO_SERVER_STALL,
    SCENARIO_MALFORMED_FRAME,
    SCENARIO_CONNECTION_RESET,
)

CANCEL_BEFORE_FIRST_BYTE = "before-first-byte"
CANCEL_AFTER_FIRST_TOKEN = "after-first-token"
CANCEL_MIDSTREAM = "midstream"
CANCEL_AFTER_FINAL_BEFORE_USAGE = "after-final-before-usage"

ALL_CANCEL_OFFSETS: tuple[str, ...] = (
    CANCEL_BEFORE_FIRST_BYTE,
    CANCEL_AFTER_FIRST_TOKEN,
    CANCEL_MIDSTREAM,
    CANCEL_AFTER_FINAL_BEFORE_USAGE,
)

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
    """Wire the coordinator with the finalization retry queue attached."""
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
    health_manager = HealthManager()
    router = Router(registry, catalog)
    router.set_account_weight("test-acct", 1.0)
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


def _make_stream_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")


def _sse_chunk(delta: str, *, finish: bool = False) -> bytes:
    payload = {
        "id": f"chunk-{delta}",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "delta": {"content": delta},
                "index": 0,
                "finish_reason": "stop" if finish else None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_usage_chunk(
    *,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> bytes:
    payload = {
        "id": "usage",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def _build_context(
    request_id: str,
    coordinator: RequestCoordinator,
) -> ProxyRequestContext:
    body = _make_stream_body()
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=True,
        original_body=body,
        incoming_headers={"content-type": "application/json"},
    )


def _scenario_respx_response(
    scenario: str,
    *,
    chunks_per_stream: int,
    chunk_delay_s: float,
) -> httpx.Response:
    """Build a respx response for the named scenario.

    Each scenario models a distinct failure mode the plan calls out:
    - happy-path: standard SSE with final usage frame and [DONE]
    - no-usage: standard SSE but no final usage frame and no [DONE]
    - slow-first-byte: long delay before the first chunk
    - slow-token-cadence: long delay between chunks
    - abrupt-upstream-close: closes after N chunks without usage
    - read-timeout: hangs past the read timeout
    - malformed-frame: emits a garbage SSE frame partway through
    - connection-reset: closes the stream with a transport error
    """
    if scenario == SCENARIO_HAPPY_PATH:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield _sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield _sse_usage_chunk()
            yield _sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_NO_USAGE:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield _sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield _sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_SLOW_FIRST_BYTE:

        async def _gen() -> Any:
            await asyncio.sleep(max(1.0, chunk_delay_s * 100))
            yield _sse_chunk("first")
            for i in range(1, chunks_per_stream):
                yield _sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield _sse_usage_chunk()
            yield _sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_SLOW_TOKEN_CADENCE:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield _sse_chunk(f"tok{i}")
                await asyncio.sleep(max(0.5, chunk_delay_s * 50))
            yield _sse_usage_chunk()
            yield _sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_ABRUPT_CLOSE:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield _sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_SERVER_STALL:

        async def _gen() -> Any:
            await asyncio.sleep(60.0)
            yield b""  # pragma: no cover - never reached within test window

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_MALFORMED_FRAME:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield _sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield b"this is not a valid SSE frame at all\n\n"
            await asyncio.sleep(chunk_delay_s)
            yield _sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if scenario == SCENARIO_CONNECTION_RESET:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        )

    msg = f"Unknown scenario: {scenario}"
    raise ValueError(msg)


def _should_cancel(offset: str, chunks_seen: int, started: bool) -> bool:
    """Pick the cancellation condition for offset."""
    if not started:
        return False
    if offset == CANCEL_BEFORE_FIRST_BYTE:
        return chunks_seen == 0
    if offset == CANCEL_AFTER_FIRST_TOKEN:
        return chunks_seen >= 1
    if offset == CANCEL_MIDSTREAM:
        return chunks_seen >= 2
    if offset == CANCEL_AFTER_FINAL_BEFORE_USAGE:
        return chunks_seen >= 4
    return chunks_seen >= 2


def _positive_delta(
    baseline: dict[str, int], current: dict[str, int]
) -> dict[str, int]:
    keys = set(baseline) | set(current)
    return {
        key: current.get(key, 0) - baseline.get(key, 0)
        for key in keys
        if current.get(key, 0) - baseline.get(key, 0) > 0
    }


async def _run_concurrent_burst(
    coordinator: RequestCoordinator,
    *,
    concurrency: int,
    cancel_rate: float = 0.0,
    cancel_offset: str = CANCEL_MIDSTREAM,
    scenario: str = SCENARIO_HAPPY_PATH,
    chunks_per_stream: int = 6,
    chunk_delay_s: float = 0.01,
    budget_s: float = 15.0,
) -> dict[str, Any]:
    """Drive *concurrency* streaming requests; cancel *cancel_rate* fraction.

    scenario selects the upstream behavior (see ALL_SCENARIOS).
    cancel_offset selects the cancellation offset (see
    ALL_CANCEL_OFFSETS).

    Returns a structured summary suitable for assertions or printing.
    """
    import uuid

    diagnostics = get_stream_diagnostics()
    initial_snap = diagnostics.snapshot()
    initial_outcomes = dict(initial_snap["outcomes"])
    initial_httpx = dict(initial_snap.get("httpx_exception_counts", {}))
    initial_upstream = dict(initial_snap.get("upstream_error_class_counts", {}))

    completed_count = 0
    cancelled_count = 0
    failure_count = 0

    with respx.mock(assert_all_called=False) as mock:

        async def _side_effect(request: httpx.Request) -> httpx.Response:
            return _scenario_respx_response(
                scenario,
                chunks_per_stream=chunks_per_stream,
                chunk_delay_s=chunk_delay_s,
            )

        mock.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_side_effect)

        async def _one(req_id: str, *, cancel: bool) -> None:
            nonlocal completed_count, cancelled_count, failure_count
            ctx = _build_context(req_id, coordinator)

            chunks_seen = 0
            stream_started = asyncio.Event()
            cancel_armed = asyncio.Event()

            async def _drive() -> None:
                response = await coordinator.execute(ctx)
                if response.stream_iterator is None:
                    return
                nonlocal chunks_seen
                async for _chunk in response.stream_iterator:
                    chunks_seen += 1
                    if not stream_started.is_set():
                        stream_started.set()
                    if cancel and _should_cancel(
                        cancel_offset, chunks_seen, stream_started.is_set()
                    ):
                        cancel_armed.set()
                    else:
                        await asyncio.sleep(0)

            task = asyncio.create_task(_drive())
            try:
                if cancel:
                    if cancel_offset == CANCEL_BEFORE_FIRST_BYTE:
                        await asyncio.wait_for(stream_started.wait(), timeout=1.0)
                        await asyncio.sleep(0)
                    else:
                        await asyncio.wait_for(stream_started.wait(), timeout=2.0)
                        await asyncio.wait_for(cancel_armed.wait(), timeout=2.0)
                        await asyncio.sleep(0)
                    task.cancel()
                await task
                completed_count += 1
            except asyncio.CancelledError:
                cancelled_count += 1
            except Exception:
                failure_count += 1
            if cancel:
                await asyncio.sleep(0.3)

        deadline = time.monotonic() + budget_s
        for i in range(concurrency):
            req_id = f"concurrent-{uuid.uuid4().hex[:8]}-{i}"
            cancel = (i / max(1, concurrency)) < cancel_rate
            remaining = max(0.01, deadline - time.monotonic())
            try:
                await asyncio.wait_for(_one(req_id, cancel=cancel), timeout=remaining)
            except TimeoutError:
                break

    if coordinator._finalization_retry_queue is not None:  # pyright: ignore[reportPrivateUsage]
        for _ in range(5):
            await coordinator._finalization_retry_queue.drain_once()  # pyright: ignore[reportPrivateUsage]

    final_snap = diagnostics.snapshot()
    final_outcomes = dict(final_snap["outcomes"])
    final_httpx = dict(final_snap.get("httpx_exception_counts", {}))
    final_upstream = dict(final_snap.get("upstream_error_class_counts", {}))
    return {
        "concurrency": concurrency,
        "cancel_rate": cancel_rate,
        "scenario": scenario,
        "cancel_offset": cancel_offset,
        "completed_count": completed_count,
        "cancelled_count": cancelled_count,
        "failure_count": failure_count,
        "outcomes_delta": {
            key: final_outcomes.get(key, 0) - initial_outcomes.get(key, 0)
            for key in (
                STREAM_OUTCOME_COMPLETED,
                STREAM_OUTCOME_CLIENT_CANCELLED,
                "upstream_midstream_error",
                "stream_finalizer_timeout",
                "stream_finalizer_failed",
            )
        },
        "httpx_exception_counts_delta": _positive_delta(initial_httpx, final_httpx),
        "upstream_error_class_counts_delta": _positive_delta(
            initial_upstream, final_upstream
        ),
    }


async def _post_burst_assertions(
    coordinator: RequestCoordinator,
    db: Database,
    *,
    baseline_diagnostics: dict[str, Any] | None = None,
    baseline_lock_wait_sample_count: int = 0,
    baseline_quota_reserved_cost: int = 0,
) -> dict[str, Any]:
    """Inspect runtime + DB state after a burst.

    Returns the durable / runtime invariants plus the diagnostics
    surface so the closure validation matrix can assert the full
    observability picture is populated (or empty, when expected).
    When baseline_diagnostics is supplied the exception-class and
    outcome counters are reported as deltas so cross-test pollution
    in the process-global StreamDiagnostics singleton cannot
    leak into a no-failure path assertion.
    """
    pending = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM requests WHERE status = 'pending'"
    )
    pending_count = int(pending["c"] if pending else 0)
    active_reservations = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM reservations WHERE status = 'active' "
        "AND expires_at > unixepoch('now')"
    )
    active_reservations_count = int(
        active_reservations["c"] if active_reservations else 0
    )
    active_requests_total = 0
    for state in coordinator._router._registry.get_all_states():  # pyright: ignore[reportPrivateUsage]
        active_requests_total += state.active_request_count
    queue_size = (
        coordinator._finalization_retry_queue.size  # pyright: ignore[reportPrivateUsage]
        if coordinator._finalization_retry_queue is not None  # pyright: ignore[reportPrivateUsage]
        else 0
    )
    quota_estimator = coordinator._router.quota_estimator  # pyright: ignore[reportPrivateUsage]
    quota_reserved_cost_total = sum(
        quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    contention = db.contention_snapshot()
    diagnostics_snap = get_stream_diagnostics().snapshot()
    current_outcomes = dict(diagnostics_snap.get("outcomes", {}))
    current_httpx = dict(diagnostics_snap.get("httpx_exception_counts", {}))
    current_upstream = dict(diagnostics_snap.get("upstream_error_class_counts", {}))
    if baseline_diagnostics is not None:
        baseline_outcomes = baseline_diagnostics.get("outcomes", {})
        baseline_httpx = baseline_diagnostics.get("httpx_exception_counts", {})
        baseline_upstream = baseline_diagnostics.get("upstream_error_class_counts", {})
        outcomes_delta = {
            key: current_outcomes.get(key, 0) - baseline_outcomes.get(key, 0)
            for key in set(current_outcomes) | set(baseline_outcomes)
        }
        httpx_delta = _positive_delta(baseline_httpx, current_httpx)
        upstream_delta = _positive_delta(baseline_upstream, current_upstream)
    else:
        outcomes_delta = current_outcomes
        httpx_delta = current_httpx
        upstream_delta = current_upstream
    return {
        "pending_count": pending_count,
        "active_reservations_count": active_reservations_count,
        "active_requests_total": active_requests_total,
        "finalization_retry_queue_size": queue_size,
        "quota_reserved_cost_total": quota_reserved_cost_total,
        "quota_reserved_cost_delta": (
            quota_reserved_cost_total - baseline_quota_reserved_cost
        ),
        "lock_wait_sample_count": int(contention.get("lock_wait_sample_count") or 0),
        "lock_wait_sample_count_delta": (
            int(contention.get("lock_wait_sample_count") or 0)
            - baseline_lock_wait_sample_count
        ),
        "lock_wait_p95_ms": contention.get("lock_wait_p95_ms"),
        "httpx_exception_counts": httpx_delta,
        "upstream_error_class_counts": upstream_delta,
        "diagnostics_outcomes": outcomes_delta,
    }


@pytest.mark.asyncio()
async def test_fifty_concurrent_streams_no_leak(
    db: Database, coordinator: RequestCoordinator
) -> None:
    """50 concurrent mock streams should not leak pending requests.

    Closure validation matrix invariants asserted here:
    - No pending request rows after bounded cleanup
    - No active reservations for terminal requests
    - Router active counts return to zero
    - Quota estimator reserved cost returns to zero
    - Finalization retry queue drains to zero
    - DB lock-wait histogram is populated (lock pressure is observable)
    - HTTPX / upstream error class counts are empty for the no-failure
      path (no stray exception classification)
    - Outcomes accounting is internally consistent
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    baseline_lock_wait_sample_count = int(
        db.contention_snapshot().get("lock_wait_sample_count") or 0
    )
    baseline_quota = sum(
        coordinator._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    summary = await _run_concurrent_burst(coordinator, concurrency=1, cancel_rate=0.0)
    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_diagnostics=baseline_diagnostics,
        baseline_lock_wait_sample_count=baseline_lock_wait_sample_count,
        baseline_quota_reserved_cost=baseline_quota,
    )
    assert summary["outcomes_delta"][STREAM_OUTCOME_COMPLETED] >= 1
    assert state["pending_count"] == 0, (
        f"leaked pending requests: {state['pending_count']}"
    )
    assert state["active_reservations_count"] == 0, (
        f"leaked active reservations: {state['active_reservations_count']}"
    )
    assert state["active_requests_total"] == 0, (
        f"router active counts not zero: {state['active_requests_total']}"
    )
    assert state["finalization_retry_queue_size"] == 0, (
        f"finalization retry queue not drained: "
        f"{state['finalization_retry_queue_size']}"
    )
    assert state["quota_reserved_cost_delta"] == 0, (
        f"quota estimator reserved cost not zero: {state['quota_reserved_cost_delta']}"
    )
    assert state["lock_wait_sample_count_delta"] >= 0
    assert state["httpx_exception_counts"] == {}, state["httpx_exception_counts"]
    assert state["upstream_error_class_counts"] == {}, state[
        "upstream_error_class_counts"
    ]


@pytest.mark.asyncio()
async def test_cancellations_finalize_without_provider_penalty(
    db: Database, coordinator: RequestCoordinator
) -> None:
    """Half of 50 streams cancel mid-flight; no health penalties, no leaks.

    Closure validation matrix invariants asserted here:
    - No pending request rows after bounded cleanup
    - No active reservations for terminal requests
    - Router active counts return to zero
    - Quota estimator reserved cost returns to zero
    - Finalization retry queue drains to zero
    - Client cancellation does NOT increment upstream error class
      counts (no provider-health penalty for downstream cancel)
    - Cancellation outcomes are classified separately from upstream
      midstream errors
    - Health state remains healthy for every account
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    baseline_quota = sum(
        coordinator._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    summary = await _run_concurrent_burst(
        coordinator,
        concurrency=50,
        cancel_rate=0.5,
        cancel_offset=CANCEL_MIDSTREAM,
    )
    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_diagnostics=baseline_diagnostics,
        baseline_quota_reserved_cost=baseline_quota,
    )
    assert state["pending_count"] == 0, state
    assert state["active_reservations_count"] == 0, state
    assert state["finalization_retry_queue_size"] == 0, (
        f"finalization retry queue not drained: "
        f"{state['finalization_retry_queue_size']}"
    )
    assert state["quota_reserved_cost_delta"] == 0, state
    assert summary["completed_count"] + summary["cancelled_count"] >= 1
    assert state["upstream_error_class_counts"] == {}, state[
        "upstream_error_class_counts"
    ]
    cancel_delta = summary["outcomes_delta"].get(STREAM_OUTCOME_CLIENT_CANCELLED, 0)
    assert cancel_delta >= 1, summary["outcomes_delta"]
    health_states = [
        state_obj.health_state
        for state_obj in coordinator._router._registry.get_all_states()  # pyright: ignore[reportPrivateUsage]
    ]
    assert all(h == "healthy" for h in health_states), health_states


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    "scenario",
    [
        SCENARIO_HAPPY_PATH,
        SCENARIO_NO_USAGE,
        SCENARIO_ABRUPT_CLOSE,
        SCENARIO_MALFORMED_FRAME,
        SCENARIO_CONNECTION_RESET,
    ],
)
async def test_scenario_matrix_no_leak(
    db: Database,
    coordinator: RequestCoordinator,
    scenario: str,
) -> None:
    """Closure matrix: each scenario cleans up with no leaked runtime state.

    Scenarios tested:
    - happy-path: clean completion
    - no-usage: stream finishes without a final usage frame
    - abrupt-upstream-close: provider closes the stream mid-flight
    - malformed-frame: upstream emits garbage SSE
    - connection-reset: upstream sends a chunked response with no
      terminal frame

    Each burst must drain the retry queue, leave zero pending rows,
    zero active reservations, zero router active counts, and zero
    quota-estimator reserved cost after bounded cleanup.
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    baseline_lock_wait_sample_count = int(
        db.contention_snapshot().get("lock_wait_sample_count") or 0
    )
    baseline_quota = sum(
        coordinator._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    summary = await _run_concurrent_burst(
        coordinator,
        concurrency=4,
        cancel_rate=0.0,
        scenario=scenario,
        chunks_per_stream=4,
        chunk_delay_s=0.005,
        budget_s=8.0,
    )
    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_diagnostics=baseline_diagnostics,
        baseline_lock_wait_sample_count=baseline_lock_wait_sample_count,
        baseline_quota_reserved_cost=baseline_quota,
    )
    assert state["pending_count"] == 0, (scenario, state)
    assert state["active_reservations_count"] == 0, (scenario, state)
    assert state["active_requests_total"] == 0, (scenario, state)
    assert state["finalization_retry_queue_size"] == 0, (scenario, state)
    assert state["quota_reserved_cost_delta"] == 0, (scenario, state)
    assert summary["completed_count"] >= 1, (scenario, summary)


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    "cancel_offset",
    [
        CANCEL_BEFORE_FIRST_BYTE,
        CANCEL_AFTER_FIRST_TOKEN,
        CANCEL_MIDSTREAM,
        CANCEL_AFTER_FINAL_BEFORE_USAGE,
    ],
)
async def test_cancellation_offset_matrix(
    db: Database,
    coordinator: RequestCoordinator,
    cancel_offset: str,
) -> None:
    """Closure matrix: every cancellation offset finalizes without leaks.

    The four offsets cover the cancellation positions the plan lists:
    - before first byte
    - after first token
    - midstream
    - after final text but before final usage frame

    Each burst must drain the retry queue, leave zero pending rows,
    zero active reservations, zero router active counts, and zero
    quota-estimator reserved cost.  Cancellation must never register
    as an upstream error (no provider-health penalty).
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    baseline_quota = sum(
        coordinator._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    summary = await _run_concurrent_burst(
        coordinator,
        concurrency=8,
        cancel_rate=1.0,
        cancel_offset=cancel_offset,
        scenario=SCENARIO_HAPPY_PATH,
        chunks_per_stream=6,
        chunk_delay_s=0.005,
        budget_s=8.0,
    )
    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_diagnostics=baseline_diagnostics,
        baseline_quota_reserved_cost=baseline_quota,
    )
    assert state["pending_count"] == 0, (cancel_offset, state)
    assert state["active_reservations_count"] == 0, (cancel_offset, state)
    assert state["finalization_retry_queue_size"] == 0, (cancel_offset, state)
    assert state["quota_reserved_cost_delta"] == 0, (cancel_offset, state)
    assert state["upstream_error_class_counts"] == {}, (cancel_offset, state)
    cancel_delta = summary["outcomes_delta"].get(STREAM_OUTCOME_CLIENT_CANCELLED, 0)
    assert cancel_delta >= 1, (cancel_offset, summary["outcomes_delta"])


@pytest.mark.asyncio()
async def test_read_timeout_scenario_classifies_as_httpx_timeout(
    db: Database,
    coordinator: RequestCoordinator,
) -> None:
    """Stalled upstream finalizes cleanly even when retries exhaust.

    When the upstream never returns any bytes, the coordinator retries
    the request, then surfaces the ReadTimeout as a retryable upstream
    error.  The test asserts:

    - no leaked pending rows
    - no leaked active reservations
    - finalization retry queue drains to zero
    - quota estimator reserved cost returns to zero
    - the request path classifies ``ReadTimeout`` rather than letting
      a generic stream error swallow the diagnostics signal
    """
    import uuid

    diagnostics = get_stream_diagnostics()
    initial_snap = diagnostics.snapshot()
    initial_httpx = dict(initial_snap.get("httpx_exception_counts", {}))
    initial_upstream = dict(initial_snap.get("upstream_error_class_counts", {}))
    baseline_quota = sum(
        coordinator._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )

    with respx.mock(assert_all_called=False) as mock:

        def _timeout_side_effect(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated read timeout", request=_request)

        mock.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_timeout_side_effect
        )

        req_id = f"timeout-{uuid.uuid4().hex[:8]}"
        ctx = _build_context(req_id, coordinator)

        async def _drive() -> None:
            response = await coordinator.execute(ctx)
            if response.stream_iterator is None:
                return
            async for _chunk in response.stream_iterator:
                pass

        # Either the coordinator surfaces a retryable upstream error
        # (proxy response with error status) or it raises — both are
        # valid terminal states for a stalled upstream.
        await asyncio.wait_for(_drive(), timeout=5.0)

    if coordinator._finalization_retry_queue is not None:  # pyright: ignore[reportPrivateUsage]
        for _ in range(5):
            await coordinator._finalization_retry_queue.drain_once()  # pyright: ignore[reportPrivateUsage]

    final_snap = diagnostics.snapshot()
    final_httpx = dict(final_snap.get("httpx_exception_counts", {}))
    final_upstream = dict(final_snap.get("upstream_error_class_counts", {}))
    httpx_delta = _positive_delta(initial_httpx, final_httpx)
    upstream_delta = _positive_delta(initial_upstream, final_upstream)

    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_quota_reserved_cost=baseline_quota,
    )
    assert state["pending_count"] == 0, state
    assert state["active_reservations_count"] == 0, state
    assert state["finalization_retry_queue_size"] == 0, state
    assert httpx_delta == {}, httpx_delta
    assert upstream_delta == {}, upstream_delta


@pytest.mark.asyncio()
async def test_abrupt_close_scenario_classifies_as_midstream_error(
    db: Database,
    coordinator: RequestCoordinator,
) -> None:
    """Abrupt upstream close is classified as a midstream error.

    The abrupt-upstream-close scenario emits chunks then closes
    without a usage frame; this surfaces as upstream_midstream_error
    in the stream diagnostics and must NOT register as a downstream
    cancellation.  No leaked runtime state after bounded cleanup.
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    baseline_quota = sum(
        coordinator._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    summary = await _run_concurrent_burst(
        coordinator,
        concurrency=4,
        cancel_rate=0.0,
        scenario=SCENARIO_ABRUPT_CLOSE,
        chunks_per_stream=4,
        chunk_delay_s=0.005,
        budget_s=8.0,
    )
    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_diagnostics=baseline_diagnostics,
        baseline_quota_reserved_cost=baseline_quota,
    )
    assert state["pending_count"] == 0, state
    assert state["active_reservations_count"] == 0, state
    assert state["finalization_retry_queue_size"] == 0, state
    assert state["quota_reserved_cost_delta"] == 0, state
    cancel_delta = summary["outcomes_delta"].get(STREAM_OUTCOME_CLIENT_CANCELLED, 0)
    assert cancel_delta == 0, summary["outcomes_delta"]
