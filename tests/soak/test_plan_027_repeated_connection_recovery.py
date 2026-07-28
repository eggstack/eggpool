"""Plan 027 — Repeated connection recovery soak tests.

Verifies that repeated invalidation/recovery cycles do not leak
file descriptors, memory, or tasks.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tracemalloc
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from pathlib import Path

from eggpool.db.connection import Database, DatabaseLifecycleState
from eggpool.db.migrations import MigrationRunner
from eggpool.db.recovery import DatabaseRecoveryController
from eggpool.models.config import DatabaseRecoveryConfig

pytestmark = [pytest.mark.asyncio, pytest.mark.soak]

CYCLES = 20


def _get_fd_count() -> int:
    """Return the number of open file descriptors for this process."""
    proc_path = f"/proc/{os.getpid()}/fd"
    if os.path.exists(proc_path):
        return len(os.listdir(proc_path))
    try:
        result = subprocess.run(
            ["lsof", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        return max(0, len(lines) - 1)
    except Exception:
        return 0


@pytest_asyncio.fixture()
async def recovery_db(
    tmp_path: Path,
) -> tuple[Database, DatabaseRecoveryController]:
    """Create a file-backed DB with a recovery controller."""
    db_path = str(tmp_path / "soak.db")
    config = DatabaseRecoveryConfig(
        max_attempts=5,
        initial_backoff_ms=10,
        max_backoff_ms=200,
        reconciliation_timeout_s=5.0,
    )
    db = Database(path=db_path)
    await db.connect()
    await MigrationRunner(db).run()
    controller = DatabaseRecoveryController(db=db, config=config)
    try:
        yield db, controller
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)
        await db.disconnect()


_account_counter = 0


async def _insert_row(db: Database, value: str) -> None:
    """Insert a request row with required parent rows."""
    global _account_counter  # noqa: PLW0603
    _account_counter += 1
    async with db.transaction():
        account_rowid = await db.execute_insert(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            (f"soak-acct-{_account_counter}", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            (f"soak-model-{_account_counter}", "openai"),
        )
        await db.execute_write(
            "INSERT INTO requests (account_id, model_id, proxy_request_id, "
            "protocol, status) VALUES (?, ?, ?, ?, ?)",
            (
                account_rowid,
                f"soak-model-{_account_counter}",
                value,
                "openai",
                "pending",
            ),
        )


async def _count_rows(db: Database) -> int:
    rows = await db.fetch_all("SELECT COUNT(*) AS cnt FROM requests")
    return int(rows[0]["cnt"]) if rows else 0


async def _invalidate_and_recover(
    db: Database,
    controller: DatabaseRecoveryController,
) -> None:
    """Invalidate the connection and trigger recovery."""
    await db._invalidate_connection("soak invalidation")  # type: ignore[reportPrivateUsage]
    assert db.lifecycle_state is DatabaseLifecycleState.INVALIDATED
    await asyncio.wait_for(controller.wait_for_ready(timeout_s=10.0), timeout=10.0)


async def test_repeated_recovery_no_fd_growth(
    recovery_db: tuple[Database, DatabaseRecoveryController],
) -> None:
    """20 invalidation/recovery cycles do not leak file descriptors."""
    db, controller = recovery_db
    fd_before = _get_fd_count()

    for i in range(CYCLES):
        await _insert_row(db, f"row-{i}")
        await _invalidate_and_recover(db, controller)
        assert db.lifecycle_state is DatabaseLifecycleState.READY

    fd_after = _get_fd_count()
    growth = fd_after - fd_before
    assert growth <= 5, (
        f"FD count grew by {growth} (before={fd_before}, after={fd_after})"
    )

    count = await _count_rows(db)
    assert count == CYCLES


async def test_repeated_recovery_no_memory_growth(
    recovery_db: tuple[Database, DatabaseRecoveryController],
) -> None:
    """20 invalidation/recovery cycles do not leak memory."""
    db, controller = recovery_db
    tracemalloc.start()
    try:
        for i in range(CYCLES):
            await _insert_row(db, f"row-{i}")
            await _invalidate_and_recover(db, controller)

        current, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Allow 1MB tolerance for normal interpreter overhead.
    assert current < 1_000_000, f"Memory usage too high: {current} bytes"


async def test_repeated_recovery_data_integrity(
    recovery_db: tuple[Database, DatabaseRecoveryController],
) -> None:
    """100 pre-inserted rows survive 10 invalidation/recovery cycles."""
    db, controller = recovery_db

    for i in range(100):
        await _insert_row(db, f"pre-{i}")

    initial_count = await _count_rows(db)
    assert initial_count == 100

    for _ in range(10):
        await _invalidate_and_recover(db, controller)

    final_count = await _count_rows(db)
    assert final_count == 100, f"Expected 100 rows, got {final_count}"


async def test_recovery_under_concurrent_operations(
    recovery_db: tuple[Database, DatabaseRecoveryController],
) -> None:
    """Concurrent inserts survive interleaved recovery cycles."""
    db, controller = recovery_db

    insert_count = 0
    insert_lock = asyncio.Lock()

    async def _concurrent_inserter(task_id: int, count: int) -> None:
        nonlocal insert_count
        for i in range(count):
            try:
                await _insert_row(db, f"task-{task_id}-row-{i}")
                async with insert_lock:
                    insert_count += 1
            except Exception:
                pass  # Connection may be invalidated during insert

    async def _interleave_recoveries(cycles: int) -> None:
        for _ in range(cycles):
            await asyncio.sleep(0.05)
            await db._invalidate_connection("concurrent soak")  # type: ignore[reportPrivateUsage]
            await asyncio.wait_for(
                controller.wait_for_ready(timeout_s=10.0), timeout=10.0
            )

    await asyncio.gather(
        *[_concurrent_inserter(i, 20) for i in range(5)],
        _interleave_recoveries(3),
    )

    final_count = await _count_rows(db)
    assert final_count > 0, "No rows survived concurrent operations"
    assert db.lifecycle_state is DatabaseLifecycleState.READY
