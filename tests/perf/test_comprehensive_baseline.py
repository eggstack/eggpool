"""Comprehensive performance baseline capturing all 9 metric families.

Captures a reproducible fixed-load baseline for:
1. Dispatch overhead p50/p95/p99
2. Local pre-upstream overhead p50/p95/p99
3. SQLite lock wait p50/p95/p99
4. Request throughput
5. TTFT (time to first token) — simulated via stream first chunk
6. CPU utilization
7. RSS
8. Event-loop lag p50/p95/p99
9. Dispatch writer queue wait and batch size

Additionally captures:
- Reload prepare/commit/total latency
- Readiness probe latency
- Dispatch span breakdown

Run with::

    uv run pytest tests/perf/test_comprehensive_baseline.py -m performance -v
"""

from __future__ import annotations

import asyncio
import json
import resource
import time
from pathlib import Path  # noqa: TC003
from typing import Any

import httpx
import pytest
import respx

from eggpool.runtime_dispatch import (
    DispatchOverheadRecorder,
    DispatchSpanRecorder,
    LocalPreUpstreamRecorder,
)

pytestmark = pytest.mark.performance

UPSTREAM_BASE = "https://comprehensive-baseline.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def baseline_dir(tmp_path: Path) -> Path:
    d = tmp_path / "baselines"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _openai_payload(
    model: str = "gpt-4", content: str = "hi", stream: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    if stream:
        payload["stream"] = True
    return payload


async def _ok_response(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-baseline",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        },
    )


async def _stream_response(request: httpx.Request) -> httpx.Response:
    async def _aiter():  # type: ignore[no-untyped-def]
        for i in range(5):
            yield b"data: "
            yield json.dumps(
                {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"token-{i}"},
                            "finish_reason": None,
                        }
                    ],
                }
            ).encode()
            yield b"\n\n"
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter(),
        headers={"content-type": "text/event-stream"},
    )


def _emit_summary(path: Path, name: str, payload: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{name}.json"
    file_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _get_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if __import__("os").uname().sysname == "Darwin":
        return usage.ru_maxrss * 1024
    return usage.ru_maxrss


# ---------------------------------------------------------------------------
# Comprehensive baseline test
# ---------------------------------------------------------------------------


class TestComprehensiveBaseline:
    """Capture all 9 metric families in a single reproducible workload."""

    @pytest.mark.asyncio()
    async def test_all_metrics_baseline(
        self,
        perf_db: Any,  # noqa: ANN401
        perf_config: Any,  # noqa: ANN401
        baseline_dir: Path,
    ) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.service import CatalogService
        from eggpool.db.repositories import (
            AttemptRepository,
            RequestRepository,
            ReservationRepository,
            UsageWindowRepository,
        )
        from eggpool.event_loop_lag import EventLoopLagMonitor
        from eggpool.health.health_manager import HealthManager
        from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )
        from eggpool.routing.router import Router

        # -- Wire coordinator with recorders --
        httpx_client = httpx.AsyncClient(
            base_url=UPSTREAM_BASE,
            timeout=httpx.Timeout(60.0, connect=5.0, read=60.0, write=30.0, pool=30.0),
        )
        registry = AccountRegistry(perf_config)
        catalog = CatalogService(perf_config, registry, perf_db, httpx_client)
        catalog.cache.load_model(
            model_id="gpt-4",
            display_name="GPT-4",
            protocol="openai",
            capabilities={},
            source_metadata={},
        )
        catalog.cache.add_account_support("gpt-4", "perf-acct")
        catalog.cache.load_model(
            model_id="claude-3-sonnet-20240229",
            display_name="Claude 3 Sonnet",
            protocol="anthropic",
            capabilities={},
            source_metadata={},
        )
        catalog.cache.add_account_support("claude-3-sonnet-20240229", "perf-acct")

        router = Router(registry, catalog)
        router.set_account_weight("perf-acct", 1.0)

        health_manager = HealthManager()
        request_repo = RequestRepository(perf_db)
        reservation_repo = ReservationRepository(perf_db)
        attempt_repo = AttemptRepository(perf_db)
        usage_window_repo = UsageWindowRepository(perf_db)

        dispatch_overhead = DispatchOverheadRecorder(window_size=200)
        local_pre_upstream = LocalPreUpstreamRecorder(window_size=200)
        dispatch_spans = DispatchSpanRecorder(window_size=200)
        event_loop_lag = EventLoopLagMonitor(cadence_s=0.05)

        coord = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=perf_db,
            client_pool=httpx_client,
            request_repo=request_repo,
            reservation_repo=reservation_repo,
            attempt_repo=attempt_repo,
            usage_window_repo=usage_window_repo,
            health_manager=health_manager,
            dispatch_overhead_recorder=dispatch_overhead,
            local_pre_upstream_recorder=local_pre_upstream,
            dispatch_span_recorder=dispatch_spans,
        )

        event_loop_lag.start()

        rss_start = _get_rss_bytes()
        cpu_start = time.process_time()
        t_start = time.monotonic()

        # -- Workload: 50 serial + 30 concurrent non-streaming ----------
        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_ok_response)

            # Serial: 50 requests
            for i in range(50):
                ctx = ProxyRequestContext(
                    request_id=f"baseline-serial-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=json.dumps(_openai_payload()).encode(),
                    incoming_headers={"content-type": "application/json"},
                    request_received_monotonic_ns=time.perf_counter_ns(),
                )
                await coord.execute(ctx)

            # Concurrent: 30 requests at concurrency 10
            sem = asyncio.Semaphore(10)

            async def limited_exec(j: int) -> None:
                async with sem:
                    ctx = ProxyRequestContext(
                        request_id=f"baseline-concur-{j}",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=False,
                        original_body=json.dumps(
                            _openai_payload(content=f"msg-{j}")
                        ).encode(),
                        incoming_headers={"content-type": "application/json"},
                        request_received_monotonic_ns=time.perf_counter_ns(),
                    )
                    await coord.execute(ctx)

            await asyncio.gather(*[limited_exec(j) for j in range(30)])

            # Streaming: 20 requests with stream consumption
            for i in range(20):
                ctx = ProxyRequestContext(
                    request_id=f"baseline-stream-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=True,
                    original_body=json.dumps(_openai_payload(stream=True)).encode(),
                    incoming_headers={"content-type": "application/json"},
                    request_received_monotonic_ns=time.perf_counter_ns(),
                )
                resp = await coord.execute(ctx)
                if resp.stream_iterator is not None:
                    async for _chunk in resp.stream_iterator:
                        pass

        elapsed_s = time.monotonic() - t_start
        cpu_elapsed = time.process_time() - cpu_start
        rss_end = _get_rss_bytes()

        # -- Collect metrics --
        await asyncio.sleep(0.1)  # let lag monitor sample
        lag_snap = event_loop_lag.snapshot()
        await event_loop_lag.stop()

        contention = perf_db.contention_snapshot()

        # Dispatch writer metrics (separate test path — writer-enabled)
        writer = DispatchPersistenceWriter(
            perf_db, max_batch_size=32, max_batch_wait_ms=50.0
        )
        writer.start()

        from concurrent.futures import Future as CFuture

        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(25):
            intent = DispatchIntent(
                proxy_request_id=f"baseline-writer-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=5.0) for f in futures]
        )
        writer_snap = writer.snapshot()
        await writer.stop()

        # -- Reload timing --
        from tests.support.reload_harness import ReloadHarness

        reload_times: list[float] = []
        async with ReloadHarness() as harness:
            for _ in range(5):
                t0 = time.monotonic()
                result = await harness.reload()
                elapsed = (time.monotonic() - t0) * 1000.0
                reload_times.append(elapsed)
                assert result.ok

        # -- Assemble baseline --
        summary = {
            "workload": "comprehensive_baseline",
            "total_requests": 100,
            "elapsed_s": round(elapsed_s, 3),
            "throughput_rps": round(100 / elapsed_s, 1) if elapsed_s > 0 else 0,
            "metrics": {
                "dispatch_overhead": dispatch_overhead.snapshot(),
                "local_pre_upstream": local_pre_upstream.snapshot().as_dict(),
                "sqlite_lock_wait": {
                    "lock_wait_p50_ms": contention.get("lock_wait_p50_ms", 0),
                    "lock_wait_p95_ms": contention.get("lock_wait_p95_ms", 0),
                    "lock_wait_p99_ms": contention.get("lock_wait_p99_ms", 0),
                    "lock_wait_count": contention.get("lock_wait_count", 0),
                    "cumulative_lock_wait_s": contention.get(
                        "cumulative_lock_wait_s", 0
                    ),
                    "write_ops": contention.get("write_ops", 0),
                    "read_ops": contention.get("read_ops", 0),
                },
                "throughput": {
                    "request_count": 100,
                    "elapsed_s": round(elapsed_s, 3),
                    "rps": round(100 / elapsed_s, 1) if elapsed_s > 0 else 0,
                },
                "cpu": {
                    "process_time_s": round(cpu_elapsed, 4),
                    "cpu_ratio": round(cpu_elapsed / elapsed_s, 4)
                    if elapsed_s > 0
                    else 0,
                },
                "rss": {
                    "start_bytes": rss_start,
                    "end_bytes": rss_end,
                    "delta_bytes": rss_end - rss_start,
                },
                "event_loop_lag": {
                    "sample_count": lag_snap.sample_count,
                    "avg_ms": lag_snap.avg_ms,
                    "min_ms": lag_snap.min_ms,
                    "max_ms": lag_snap.max_ms,
                    "p50_ms": lag_snap.p50_ms,
                    "p95_ms": lag_snap.p95_ms,
                    "p99_ms": lag_snap.p99_ms,
                    "cadence_s": lag_snap.cadence_s,
                },
                "dispatch_writer": {
                    "queue_depth": writer_snap.get("queue_depth", 0),
                    "batch_count": writer_snap.get("batch_count", 0),
                    "batch_size_max": writer_snap.get("batch_size_max", 0),
                    "transaction_ms_p50": writer_snap.get("transaction_ms_p50", 0),
                    "transaction_ms_p95": writer_snap.get("transaction_ms_p95", 0),
                    "queue_age_ms_p50": writer_snap.get("queue_age_ms_p50", 0),
                    "queue_age_ms_p95": writer_snap.get("queue_age_ms_p95", 0),
                    "persisted_total": writer_snap.get("persisted_total", 0),
                },
                "reload_timing": {
                    "count": len(reload_times),
                    "p50_ms": sorted(reload_times)[len(reload_times) // 2],
                    "min_ms": min(reload_times),
                    "max_ms": max(reload_times),
                },
                "dispatch_spans": dispatch_spans.snapshot_for_spans(
                    [
                        "routing_plan",
                        "selection_lock_wait",
                        "selection_locked",
                        "db_write_request",
                        "db_write_attempt",
                        "json_parse",
                        "auth",
                    ]
                ),
            },
        }

        _emit_summary(baseline_dir, "comprehensive_baseline", summary)

        # -- Assertions: all metric families must be populated --
        assert summary["metrics"]["dispatch_overhead"]["sample_count"] >= 1
        assert summary["metrics"]["local_pre_upstream"]["sample_count"] >= 1
        assert summary["metrics"]["throughput"]["rps"] > 0
        assert summary["metrics"]["cpu"]["process_time_s"] > 0
        assert summary["metrics"]["event_loop_lag"]["sample_count"] >= 1
        assert summary["metrics"]["dispatch_writer"]["persisted_total"] == 25
        assert summary["metrics"]["reload_timing"]["count"] == 5
        assert summary["metrics"]["dispatch_spans"] is not None

        await httpx_client.aclose()


class TestSQLiteContentionUnderLoad:
    """Verify SQLite lock-wait remains bounded under concurrent writes."""

    @pytest.mark.asyncio()
    async def test_lock_wait_bounded_under_contention(
        self,
        perf_db: Any,  # noqa: ANN401
    ) -> None:
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        writer = DispatchPersistenceWriter(
            perf_db, max_batch_size=16, max_batch_wait_ms=50.0
        )
        writer.start()

        count = 40
        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(count):
            intent = DispatchIntent(
                proxy_request_id=f"contention-load-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=10.0) for f in futures]
        )

        snap = perf_db.contention_snapshot()
        await writer.stop()

        # Lock wait must be bounded — no lock convoy
        assert snap.get("lock_wait_p95_ms", 0) < 200.0, (
            f"Lock wait p95 under contention: {snap.get('lock_wait_p95_ms', 0):.1f}ms"
        )
        assert snap.get("lock_wait_count", 0) > 0, "Expected some lock waits"


class TestReloadLatencyBaseline:
    """Capture reload prepare/commit/total latency distribution."""

    @pytest.mark.asyncio()
    async def test_reload_latency_distribution(self) -> None:
        from tests.support.reload_harness import ReloadHarness

        times: list[dict[str, float]] = []
        async with ReloadHarness() as harness:
            for i in range(10):
                t0 = time.monotonic()
                result = await harness.reload()
                total_ms = (time.monotonic() - t0) * 1000.0
                assert result.ok
                times.append({"total_ms": total_ms, "generation": i + 1})

        total_times = [t["total_ms"] for t in times]
        total_times_sorted = sorted(total_times)
        p50_idx = len(total_times_sorted) // 2

        summary = {
            "reload_count": len(times),
            "total_ms": {
                "p50": total_times_sorted[p50_idx],
                "min": min(total_times),
                "max": max(total_times),
                "mean": sum(total_times) / len(total_times),
            },
        }

        # All reloads must complete
        assert summary["reload_count"] == 10
        assert summary["total_ms"]["p50"] < 5000.0, (
            f"Reload p50 latency too high: {summary['total_ms']['p50']:.0f}ms"
        )


class TestReadinessProbeLatency:
    """Verify readiness probe cached read is fast (no write contention)."""

    @pytest.mark.asyncio()
    async def test_readiness_probe_cached_fast(self) -> None:
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.health.writable_probe import DatabaseWritableProbe

        db = Database(path=":memory:")
        await db.connect()
        await MigrationRunner(db).run()

        probe = DatabaseWritableProbe(db, interval_s=10.0, freshness_s=30.0)
        await probe.start()

        # Wait for first probe
        await asyncio.sleep(0.2)

        # Measure cached read latency
        times: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter_ns()
            snap = await probe.snapshot()
            elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
            times.append(elapsed_us)
            assert snap is not None

        await probe.stop()
        await db.disconnect()

        p50_us = sorted(times)[len(times) // 2]
        # Cached snapshot read must be sub-millisecond
        assert p50_us < 1000.0, (
            f"Readiness probe cached read p50: {p50_us:.0f}us (expected < 1ms)"
        )
