r"""Reproduce the high-concurrency streaming harness from the CLI.

Drives a configurable burst of streaming requests through the
coordinator pipeline (mock upstream) and prints a structured summary of
runtime telemetry, DB state, and lock contention.  Useful for operators
who want to validate stream stability outside the pytest harness:

    python scripts/repro_high_concurrency_streams.py \\
        --concurrency 50 --cancel-rate 0.25 --scenario slow-stream

    python scripts/repro_high_concurrency_streams.py \\
        --concurrency 100 --cancel-rate 0.50 --scenario abrupt-upstream-close

The script is intentionally a thin shell over the same primitives the
integration test exercises so the harness and the script stay in sync.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx
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
    STREAM_OUTCOME_FINALIZER_TIMEOUT,
    STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
    get_stream_diagnostics,
)
from eggpool.routing.router import Router
from tests.helpers.stream_stability_harness import (
    ALL_CANCEL_OFFSETS,
    ALL_SCENARIOS,
    CANCEL_BEFORE_FIRST_BYTE,
    CANCEL_MIDSTREAM,
    SCENARIO_HAPPY_PATH,
    UPSTREAM_BASE,
    normalize_scenario,
    positive_delta,
    scenario_respx_response,
    should_cancel,
)


def _build_config() -> AppConfig:
    os.environ.setdefault("OPENCODE_TEST_KEY", "test-key-123")
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


async def _build_coordinator(
    config: AppConfig,
) -> tuple[RequestCoordinator, Database, httpx.AsyncClient]:
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
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, database, httpx_client)
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
    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=database,
        client_pool=httpx_client,
        request_repo=RequestRepository(database),
        reservation_repo=ReservationRepository(database),
        attempt_repo=AttemptRepository(database),
        usage_window_repo=UsageWindowRepository(database),
        health_manager=HealthManager(),
    )
    return coord, database, httpx_client


def _make_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
    ).encode()


async def _run_burst(
    coord: RequestCoordinator,
    db: Database,
    *,
    concurrency: int,
    cancel_rate: float,
    cancel_offset: str,
    scenario: str,
    chunks_per_stream: int,
    chunk_delay_s: float,
) -> dict[str, Any]:

    diagnostics = get_stream_diagnostics()
    initial_snap = diagnostics.snapshot()
    initial_outcomes = dict(initial_snap["outcomes"])
    initial_httpx = dict(initial_snap.get("httpx_exception_counts", {}))
    initial_upstream = dict(initial_snap.get("upstream_error_class_counts", {}))
    baseline_lock_wait = int(
        db.contention_snapshot().get("lock_wait_sample_count") or 0
    )
    baseline_quota = sum(
        coord._router.quota_estimator._account_reserved_cost.values()  # pyright: ignore[reportPrivateUsage]
    )
    baseline_active_requests = sum(
        s.active_request_count
        for s in coord._router._registry.get_all_states()  # pyright: ignore[reportPrivateUsage]
    )

    with respx.mock(assert_all_called=False) as mock:

        async def _side_effect(_request: httpx.Request) -> httpx.Response:
            return scenario_respx_response(
                scenario,
                chunks_per_stream=chunks_per_stream,
                chunk_delay_s=chunk_delay_s,
            )

        mock.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_side_effect)

        async def _drive_one(idx: int) -> dict[str, str]:
            req_id = f"repro-{idx:04d}"
            cancel = (idx / max(1, concurrency)) < cancel_rate
            ctx = ProxyRequestContext(
                request_id=req_id,
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_body(),
                incoming_headers={"content-type": "application/json"},
            )
            chunks_seen = 0
            started = asyncio.Event()
            at_target = asyncio.Event()

            async def _drive() -> None:
                response = await coord.execute(ctx)
                if response.stream_iterator is None:
                    return
                nonlocal chunks_seen
                async for _chunk in response.stream_iterator:
                    chunks_seen += 1
                    if not started.is_set():
                        started.set()
                    if cancel and should_cancel(
                        cancel_offset,
                        chunks_seen,
                        started.is_set(),
                    ):
                        at_target.set()
                    else:
                        await asyncio.sleep(0)

            task = asyncio.create_task(_drive())
            try:
                if cancel:
                    if cancel_offset == CANCEL_BEFORE_FIRST_BYTE:
                        await asyncio.wait_for(started.wait(), timeout=1.0)
                        await asyncio.sleep(0)
                    else:
                        await asyncio.wait_for(started.wait(), timeout=2.0)
                        await asyncio.wait_for(at_target.wait(), timeout=2.0)
                        await asyncio.sleep(0)
                    task.cancel()
                await task
                return {"outcome": "completed"}
            except asyncio.CancelledError:
                return {"outcome": "cancelled"}
            except Exception:
                return {"outcome": "failure"}
            finally:
                if cancel:
                    await asyncio.sleep(0.3)

        tasks: list[asyncio.Task[dict[str, str]]] = []
        for i in range(concurrency):
            tasks.append(asyncio.create_task(_drive_one(i)))

        try:
            results: list[dict[str, str] | BaseException] = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=30.0,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    completed = 0
    cancelled = 0
    failures = 0
    for r in results:
        if isinstance(r, BaseException):
            failures += 1
        elif r.get("outcome") == "completed":
            completed += 1
        elif r.get("outcome") == "cancelled":
            cancelled += 1
        else:
            failures += 1

    final_snap = diagnostics.snapshot()
    final_outcomes = dict(final_snap["outcomes"])
    final_httpx = dict(final_snap.get("httpx_exception_counts", {}))
    final_upstream = dict(final_snap.get("upstream_error_class_counts", {}))

    pending = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM requests WHERE status = 'pending'"
    )
    pending_count = int(pending["c"] if pending else 0)
    active_reservations = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM reservations WHERE status = 'active' "
        "AND expires_at > CURRENT_TIMESTAMP"
    )
    active_reservations_count = int(
        active_reservations["c"] if active_reservations else 0
    )
    final_active_requests = sum(
        s.active_request_count
        for s in coord._router._registry.get_all_states()  # pyright: ignore[reportPrivateUsage]
    )
    final_quota = sum(coord._router.quota_estimator._account_reserved_cost.values())  # pyright: ignore[reportPrivateUsage]
    contention = db.contention_snapshot()

    outcomes_delta = {
        key: final_outcomes.get(key, 0) - initial_outcomes.get(key, 0)
        for key in (
            STREAM_OUTCOME_COMPLETED,
            STREAM_OUTCOME_CLIENT_CANCELLED,
            "upstream_midstream_error",
            STREAM_OUTCOME_FINALIZER_TIMEOUT,
            "stream_finalizer_failed",
        )
    }
    upstream_failed = outcomes_delta.get(STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR, 0)
    finalizer_timed_out = outcomes_delta.get(STREAM_OUTCOME_FINALIZER_TIMEOUT, 0)
    total = completed + cancelled + upstream_failed + finalizer_timed_out + failures

    return {
        "concurrency": concurrency,
        "cancel_rate": cancel_rate,
        "scenario": scenario,
        "cancel_offset": cancel_offset,
        "total_requests": total,
        "completed": completed,
        "cancelled": cancelled,
        "upstream_failed": upstream_failed,
        "finalizer_timed_out": finalizer_timed_out,
        "failures": failures,
        "outcomes_delta": outcomes_delta,
        "httpx_exception_counts": positive_delta(initial_httpx, final_httpx),
        "upstream_error_class_counts": positive_delta(initial_upstream, final_upstream),
        "leaked_pending_rows": pending_count,
        "leaked_active_reservations": active_reservations_count,
        "router_active_requests_after": final_active_requests,
        "router_active_requests_delta": (
            final_active_requests - baseline_active_requests
        ),
        "quota_reserved_cost_after": final_quota,
        "quota_reserved_cost_delta": final_quota - baseline_quota,
        "finalization_retry_queue_size": 0,
        "db_lock_wait_p95_ms": contention.get("lock_wait_p95_ms"),
        "db_lock_wait_max_ms": contention.get("lock_wait_max_ms"),
        "db_lock_wait_sample_count_delta": (
            int(contention.get("lock_wait_sample_count") or 0) - baseline_lock_wait
        ),
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--cancel-rate", type=float, default=0.0)
    parser.add_argument(
        "--cancel-offset",
        type=str,
        default=CANCEL_MIDSTREAM,
        choices=ALL_CANCEL_OFFSETS,
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default=SCENARIO_HAPPY_PATH,
        choices=list(ALL_SCENARIOS) + ["slow-token-cadence"],
    )
    parser.add_argument("--chunks-per-stream", type=int, default=6)
    parser.add_argument("--chunk-delay-s", type=float, default=0.01)
    args = parser.parse_args()
    scenario = normalize_scenario(args.scenario)

    config = _build_config()
    coord, db, httpx_client = await _build_coordinator(config)
    try:
        summary = await _run_burst(
            coord,
            db,
            concurrency=args.concurrency,
            cancel_rate=args.cancel_rate,
            cancel_offset=args.cancel_offset,
            scenario=scenario,
            chunks_per_stream=args.chunks_per_stream,
            chunk_delay_s=args.chunk_delay_s,
        )
    finally:
        await httpx_client.aclose()
        await db.disconnect()

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
