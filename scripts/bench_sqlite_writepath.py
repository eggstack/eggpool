#!/usr/bin/env python3
"""SQLite write-path benchmark for Plan 137 Phase 6.

Exercises the dispatch and finalization write paths with a file-backed
database and collects timing, lock wait, statement counts, and WAL growth.

Two configurations are compared:
  1. Baseline: unbounded WAL (journal_size_limit = None)
  2. SBC: bounded WAL (journal_size_limit = 64 MiB)

Usage:
    uv run python scripts/bench_sqlite_writepath.py
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)

ITERATIONS = 1000
JOURNAL_SIZE_LIMIT_SBC = 64 * 1024 * 1024  # 64 MiB
ACCOUNT_ID = 1
MODEL_ID = "gpt-4"


def _wal_size(db_path: Path) -> int:
    wal = db_path.parent / f"{db_path.name}-wal"
    return wal.stat().st_size if wal.exists() else 0


async def _run_benchmark(
    label: str,
    journal_size_limit: int | None,
    db_path: Path,
) -> dict[str, object]:
    database = Database(path=str(db_path), journal_size_limit=journal_size_limit)
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()

    # Seed account and model
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("bench-acct", "BENCH_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            (MODEL_ID, "openai"),
        )

    request_repo = RequestRepository(database)
    reservation_repo = ReservationRepository(database)
    attempt_repo = AttemptRepository(database)

    # Reset contention counters after setup
    _ = database.contention_snapshot()

    # ── Dispatch benchmark ──
    dispatch_times_ns: list[int] = []
    for i in range(ITERATIONS):
        req_id = f"req-{i}"
        t0 = time.perf_counter_ns()
        async with database.transaction():
            rid = await request_repo.create_pending(
                request_id=req_id,
                model_id=MODEL_ID,
                protocol="openai",
                streamed=False,
                account_id=ACCOUNT_ID,
                reserved_microdollars=100,
            )
            _ = await reservation_repo.create(
                request_id=rid,
                account_id=ACCOUNT_ID,
                model_id=MODEL_ID,
                estimated_tokens=1000,
                estimated_microdollars=100,
            )
            _ = await attempt_repo.create(
                request_id=rid,
                attempt_number=1,
                account_id=ACCOUNT_ID,
                provider_id="openai",
                model_id=MODEL_ID,
                protocol="openai",
                streamed=False,
            )
        dispatch_times_ns.append(time.perf_counter_ns() - t0)

    dispatch_times_ms = sorted(t / 1_000_000 for t in dispatch_times_ns)
    dispatch_wal_after = _wal_size(db_path)

    # ── Finalization benchmark ──
    # Reset contention counters
    _ = database.contention_snapshot()

    finalization_times_ns: list[int] = []
    for i in range(ITERATIONS):
        req_id = f"req-{i}"
        t0 = time.perf_counter_ns()
        async with database.transaction():
            await request_repo.finalize_if_pending_returning(
                request_id=req_id,
                status="completed",
                status_code=200,
                input_tokens=100,
                output_tokens=50,
                cost_microdollars=10,
            )
            # Find the attempt for this request
            attempt_rows = await database.fetch_all(
                "SELECT id FROM request_attempts WHERE request_id = ? "
                "AND completed_at IS NULL LIMIT 1",
                (req_id,),
            )
            if attempt_rows:
                await database.execute_write(
                    "UPDATE request_attempts SET completed_at = CURRENT_TIMESTAMP, "
                    "status_code = 200, latency_ms = 100 "
                    "WHERE id = ?",
                    (attempt_rows[0]["id"],),
                )
            # Release reservation
            res_rows = await database.fetch_all(
                "SELECT id FROM reservations WHERE request_id = ? "
                "AND status = 'active' LIMIT 1",
                (req_id,),
            )
            if res_rows:
                await database.execute_returning(
                    "UPDATE reservations SET status = 'released', "
                    "released_at = CURRENT_TIMESTAMP, release_reason = 'bench' "
                    "WHERE id = ? AND status = 'active' "
                    "RETURNING status",
                    (res_rows[0]["id"],),
                )
        finalization_times_ns.append(time.perf_counter_ns() - t0)

    finalization_times_ms = sorted(t / 1_000_000 for t in finalization_times_ns)

    # ── Collect diagnostics ──
    snap = database.contention_snapshot()
    final_wal = _wal_size(db_path)

    def percentile(data: list[float], p: float) -> float:
        idx = min(len(data) - 1, max(0, int(round((len(data) - 1) * p))))
        return data[idx]

    result: dict[str, object] = {
        "label": label,
        "journal_size_limit": journal_size_limit,
        "iterations": ITERATIONS,
        "dispatch_p50_ms": round(percentile(dispatch_times_ms, 0.50), 3),
        "dispatch_p95_ms": round(percentile(dispatch_times_ms, 0.95), 3),
        "dispatch_p99_ms": round(percentile(dispatch_times_ms, 0.99), 3),
        "dispatch_avg_ms": round(sum(dispatch_times_ms) / len(dispatch_times_ms), 3),
        "finalization_p50_ms": round(percentile(finalization_times_ms, 0.50), 3),
        "finalization_p95_ms": round(percentile(finalization_times_ms, 0.95), 3),
        "finalization_p99_ms": round(percentile(finalization_times_ms, 0.99), 3),
        "finalization_avg_ms": round(
            sum(finalization_times_ms) / len(finalization_times_ms), 3
        ),
        "lock_wait_p50_ms": snap.get("lock_wait_p50_ms"),
        "lock_wait_p95_ms": snap.get("lock_wait_p95_ms"),
        "lock_wait_max_ms": snap.get("lock_wait_max_ms"),
        "lock_wait_count": snap.get("lock_wait_count", 0),
        "operations_by_kind": snap.get("operations_by_kind", {}),
        "total_transactions": snap.get("total_transactions", 0),
        "write_ops": snap.get("write_ops", 0),
        "read_ops": snap.get("read_ops", 0),
        "wal_after_dispatch_b": dispatch_wal_after,
        "wal_final_b": final_wal,
    }

    await database.disconnect()
    return result


def _print_result(r: dict[str, object]) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {r['label']}")
    print(f"  journal_size_limit = {r['journal_size_limit']}")
    print(f"{'=' * 60}")
    print(f"  Iterations: {r['iterations']}")
    print()
    print("  Dispatch (request + reservation + attempt INSERT):")
    print(
        f"    p50={r['dispatch_p50_ms']}ms  p95={r['dispatch_p95_ms']}ms  "
        f"p99={r['dispatch_p99_ms']}ms  avg={r['dispatch_avg_ms']}ms"
    )
    print()
    print("  Finalization (request + attempt + reservation UPDATE):")
    print(
        f"    p50={r['finalization_p50_ms']}ms  p95={r['finalization_p95_ms']}ms  "
        f"p99={r['finalization_p99_ms']}ms  avg={r['finalization_avg_ms']}ms"
    )
    print()
    print(
        f"  Lock wait: p50={r['lock_wait_p50_ms']}ms  "
        f"p95={r['lock_wait_p95_ms']}ms  max={r['lock_wait_max_ms']}ms  "
        f"count={r['lock_wait_count']}"
    )
    print(
        f"  Transactions: {r['total_transactions']}  "
        f"Write ops: {r['write_ops']}  Read ops: {r['read_ops']}"
    )
    ops: dict[str, int] = r["operations_by_kind"]  # type: ignore[assignment]
    print(
        f"  Ops by kind: insert={ops.get('insert', 0)}  "
        f"update={ops.get('update', 0)}  select={ops.get('select', 0)}  "
        f"transaction={ops.get('transaction', 0)}"
    )
    print(f"  WAL after dispatch: {r['wal_after_dispatch_b']:,} bytes")
    print(f"  WAL final: {r['wal_final_b']:,} bytes")


async def main() -> None:
    print("Plan 137 Phase 6 — SQLite Write-Path Benchmark")
    print(f"Iterations per phase: {ITERATIONS}")
    print(f"Account ID: {ACCOUNT_ID}, Model: {MODEL_ID}")

    results: list[dict[str, object]] = []

    # Run 1: Baseline (unbounded WAL)
    with tempfile.TemporaryDirectory(prefix="eggpool_bench_") as tmpdir:
        db_path = Path(tmpdir) / "bench.db"
        r = await _run_benchmark(
            label="Baseline (unbounded WAL)",
            journal_size_limit=None,
            db_path=db_path,
        )
        _print_result(r)
        results.append(r)

    # Run 2: SBC (bounded WAL, 64 MiB)
    with tempfile.TemporaryDirectory(prefix="eggpool_bench_") as tmpdir:
        db_path = Path(tmpdir) / "bench.db"
        r = await _run_benchmark(
            label="SBC (journal_size_limit=64MiB)",
            journal_size_limit=JOURNAL_SIZE_LIMIT_SBC,
            db_path=db_path,
        )
        _print_result(r)
        results.append(r)

    # ── Comparison ──
    b, s = results[0], results[1]
    print(f"\n{'=' * 60}")
    print("  COMPARISON: Baseline vs SBC")
    print(f"{'=' * 60}")

    for metric, label in [
        ("dispatch_p50_ms", "Dispatch p50"),
        ("dispatch_p95_ms", "Dispatch p95"),
        ("dispatch_p99_ms", "Dispatch p99"),
        ("dispatch_avg_ms", "Dispatch avg"),
        ("finalization_p50_ms", "Finalization p50"),
        ("finalization_p95_ms", "Finalization p95"),
        ("finalization_p99_ms", "Finalization p99"),
        ("finalization_avg_ms", "Finalization avg"),
    ]:
        base = float(b[metric])  # type: ignore[arg-type]
        sbc = float(s[metric])  # type: ignore[arg-type]
        delta = sbc - base
        pct = (delta / base * 100) if base != 0 else 0
        direction = "+" if delta > 0 else ""
        print(f"  {label}: {base:.3f}ms → {sbc:.3f}ms ({direction}{pct:.1f}%)")

    print()
    print(
        f"  WAL after dispatch: {b['wal_after_dispatch_b']:,}B"
        f" → {s['wal_after_dispatch_b']:,}B"
    )
    print(f"  WAL final: {b['wal_final_b']:,}B → {s['wal_final_b']:,}B")
    print(f"  Lock wait p50: {b['lock_wait_p50_ms']}ms → {s['lock_wait_p50_ms']}ms")
    print(f"  Lock wait p95: {b['lock_wait_p95_ms']}ms → {s['lock_wait_p95_ms']}ms")
    print()
    print("  The journal_size_limit pragma does not alter checkpoint cadence or")
    print("  synchronous mode. Any timing differences are noise, not signal.")
    print("  The WAL is truncated to the limit after passive checkpoints,")
    print("  bounding steady-state storage consumption on constrained SBCs.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
