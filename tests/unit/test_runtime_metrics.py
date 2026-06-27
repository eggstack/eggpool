"""Tests for RuntimeMetricsService.snapshot()."""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.runtime_metrics import (
    _MAX_PROBE_ERROR_LEN,
    RuntimeMetricsService,
    _parse_proc_stat_memory,
    _safe_int,
    _truncate_probe_error,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _build_config() -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
                "threads": 2,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


def _make_service(
    db: Database,
    *,
    config: AppConfig | None = None,
    stats_db: Database | None = None,
    supervisor: Any = None,
    task_monitor: Any = None,
    router: Any = None,
    health_manager: Any = None,
    started_monotonic: float | None = None,
    started_epoch: float | None = None,
    dispatch_overhead_recorder: Any | None = None,
) -> RuntimeMetricsService:
    if config is None:
        config = _build_config()
    if started_monotonic is None:
        started_monotonic = time.monotonic() - 100.0
    if started_epoch is None:
        started_epoch = time.time() - 100.0
    return RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=stats_db,
        supervisor=supervisor,
        task_monitor=task_monitor,
        router=router,
        health_manager=health_manager,
        started_monotonic=started_monotonic,
        started_epoch=started_epoch,
        dispatch_overhead_recorder=dispatch_overhead_recorder,
    )


# -- _safe_int / _truncate_probe_error -------------------------------------


def test_safe_int_valid() -> None:
    assert _safe_int("42") == 42
    assert _safe_int(3.7) == 3


def test_safe_int_invalid() -> None:
    assert _safe_int("not-a-number") is None
    assert _safe_int(None) is None  # type: ignore[arg-type]


def test_truncate_probe_error_short() -> None:
    msg = "short message"
    assert _truncate_probe_error(msg) == msg


def test_truncate_probe_error_long() -> None:
    msg = "x" * 300
    result = _truncate_probe_error(msg)
    assert len(result) == _MAX_PROBE_ERROR_LEN  # truncated to max + "..."
    assert result.endswith("...")


def test_parse_proc_stat_memory_uses_rss_pages_not_vsize() -> None:
    """RSS must come from stat field 24, not vsize field 23."""
    page_size = 4096
    vsize_bytes = 1_200_000_000
    rss_pages = 50_000
    fields = [
        "S",  # 3 state
        "1",  # 4 ppid
        "1",  # 5 pgrp
        "1",  # 6 session
        "0",  # 7 tty_nr
        "-1",  # 8 tpgid
        "4194560",  # 9 flags
        "100",  # 10 minflt
        "0",  # 11 cminflt
        "0",  # 12 majflt
        "0",  # 13 cmajflt
        "10",  # 14 utime
        "20",  # 15 stime
        "0",  # 16 cutime
        "0",  # 17 cstime
        "20",  # 18 priority
        "0",  # 19 nice
        "1",  # 20 num_threads
        "0",  # 21 itrealvalue
        "123456",  # 22 starttime
        str(vsize_bytes),  # 23 vsize
        str(rss_pages),  # 24 rss
    ]
    stat = f"12345 (eggpool worker) {' '.join(fields)}"

    vms_bytes, rss_bytes = _parse_proc_stat_memory(stat, page_size)

    assert vms_bytes == vsize_bytes
    assert rss_bytes == rss_pages * page_size
    assert rss_bytes != vsize_bytes * page_size


# -- snapshot() top-level structure -----------------------------------------


@pytest.mark.asyncio
async def test_snapshot_returns_all_top_level_keys(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    assert "server" in snapshot
    assert "memory" in snapshot
    assert "processes" in snapshot
    assert "background_tasks" in snapshot
    assert "db" in snapshot
    assert "routing_runtime" in snapshot
    assert "probe_errors" in snapshot
    assert isinstance(snapshot["probe_errors"], list)


@pytest.mark.asyncio
async def test_snapshot_probe_errors_is_bounded(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    assert len(snapshot["probe_errors"]) <= 16  # _MAX_PROBE_ERRORS


# -- Server fields ---------------------------------------------------------


@pytest.mark.asyncio
async def test_server_fields_present(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    server = snapshot["server"]
    assert isinstance(server["pid"], int)
    assert server["pid"] == os.getpid()
    assert isinstance(server["ppid"], int)
    assert isinstance(server["process_group_id"], int)
    assert isinstance(server["session_id"], int)
    assert isinstance(server["uptime_seconds"], float)
    assert server["uptime_seconds"] >= 0
    assert isinstance(server["started_epoch"], float)
    assert isinstance(server["python_version"], str)
    assert isinstance(server["platform"], str)
    assert isinstance(server["is_daemon_hint"], bool)
    assert isinstance(server["configured_server_threads"], int)
    assert server["configured_server_threads"] == 2


@pytest.mark.asyncio
async def test_server_uptime_increases(db: Database) -> None:
    service = _make_service(db, started_monotonic=time.monotonic() - 1.0)
    snap1 = await service.snapshot()
    await asyncio.sleep(0.05)
    snap2 = await service.snapshot()
    assert snap2["server"]["uptime_seconds"] > snap1["server"]["uptime_seconds"]


# -- Memory fields (null-safe) ---------------------------------------------


@pytest.mark.asyncio
async def test_memory_fields_present(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    memory = snapshot["memory"]
    # rss_bytes may be populated or None depending on platform
    assert "rss_bytes" in memory
    assert "vms_bytes" in memory
    assert "open_fd_count" in memory
    assert "thread_count" in memory
    assert isinstance(memory["thread_count"], int)


@pytest.mark.asyncio
async def test_memory_null_safe_when_proc_unavailable(
    db: Database,
) -> None:
    """snapshot() must not raise when /proc is unavailable."""
    service = _make_service(db)
    # Patch Path to raise for /proc paths
    with patch("pathlib.Path.exists", side_effect=OSError("no /proc")):
        snapshot = await service.snapshot()
    # All memory fields should be set (possibly None)
    assert "rss_bytes" in snapshot["memory"]
    assert "vms_bytes" in snapshot["memory"]


# -- Process count warning -------------------------------------------------


@pytest.mark.asyncio
async def test_process_count_fields_present(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    processes = snapshot["processes"]
    assert "eggpool_process_count" in processes
    assert "expected_worker_process_count" in processes
    assert "process_count_warning" in processes
    assert isinstance(processes["expected_worker_process_count"], int)


# -- Background tasks snapshot ---------------------------------------------


@pytest.mark.asyncio
async def test_background_tasks_empty_when_no_supervisor(
    db: Database,
) -> None:
    service = _make_service(db, supervisor=None)
    snapshot = await service.snapshot()
    assert snapshot["background_tasks"] == []


@pytest.mark.asyncio
async def test_background_tasks_with_supervisor(db: Database) -> None:
    from eggpool.background import TaskSupervisor

    supervisor = TaskSupervisor()

    async def dummy() -> None:
        await asyncio.sleep(3600)

    supervisor.register("test-task", dummy, max_restarts=5)

    service = _make_service(db, supervisor=supervisor)
    snapshot = await service.snapshot()
    tasks = snapshot["background_tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["name"] == "test-task"
    assert task["registered"] is True
    assert task["max_restarts"] == 5
    assert isinstance(task["running"], bool)
    assert isinstance(task["done"], bool)
    assert isinstance(task["cancelled"], bool)
    assert isinstance(task["restart_count"], int)


@pytest.mark.asyncio
async def test_background_tasks_cancelled_state(db: Database) -> None:
    from eggpool.background import TaskSupervisor

    supervisor = TaskSupervisor()

    async def dummy() -> None:
        await asyncio.sleep(3600)

    task_obj = supervisor.register("cancel-me", dummy)
    await task_obj.start()
    # Cancel returns a bool, not a coroutine
    task_obj._task.cancel()  # type: ignore[union-attr]
    # Give the task a moment to process cancellation
    await asyncio.sleep(0.01)

    service = _make_service(db, supervisor=supervisor)
    snapshot = await service.snapshot()
    tasks = snapshot["background_tasks"]
    assert len(tasks) == 1
    # cancelled flag may be True if task was cancelled
    assert isinstance(tasks[0]["cancelled"], bool)


@pytest.mark.asyncio
async def test_background_tasks_not_started_state(db: Database) -> None:
    from eggpool.background import TaskSupervisor

    supervisor = TaskSupervisor()

    async def quick_finish() -> None:
        return

    supervisor.register("not-started", quick_finish)
    # Register but don't start — task is not running, _task is None
    service = _make_service(db, supervisor=supervisor)
    snapshot = await service.snapshot()
    tasks = snapshot["background_tasks"]
    assert len(tasks) == 1
    assert tasks[0]["running"] is False
    # _task is None so done() check yields False
    assert tasks[0]["done"] is False


# -- DB snapshot fields ----------------------------------------------------


@pytest.mark.asyncio
async def test_db_snapshot_fields_present(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    db_info = snapshot["db"]
    assert "path" in db_info
    assert "is_memory_db" in db_info
    assert "wal_enabled" in db_info
    assert "wal_mode_live" in db_info
    assert "synchronous" in db_info
    assert "synchronous_live" in db_info
    assert "busy_timeout_ms" in db_info
    assert db_info["configured_worker_threads"] == 1
    assert "primary_connected" in db_info
    assert "stats_connection_separate" in db_info
    assert "file_size_bytes" in db_info
    assert "wal_size_bytes" in db_info
    assert "shm_size_bytes" in db_info


@pytest.mark.asyncio
async def test_db_memory_db_detected(db: Database) -> None:
    config = AppConfig.from_dict(
        {
            "server": {"api_key_env": "OPENCODE_TEST_KEY"},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )
    service = _make_service(db, config=config)
    snapshot = await service.snapshot()
    assert snapshot["db"]["is_memory_db"] is True
    assert snapshot["db"]["path"] is None
    assert snapshot["db"]["file_size_bytes"] is None
    assert snapshot["db"]["wal_size_bytes"] is None
    assert snapshot["db"]["shm_size_bytes"] is None


@pytest.mark.asyncio
async def test_db_file_based_handles_missing_file(
    tmp_path: Any,
) -> None:
    config = AppConfig.from_dict(
        {
            "server": {"api_key_env": "OPENCODE_TEST_KEY"},
            "database": {"path": str(tmp_path / "nonexistent.sqlite3")},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )
    database = Database(path=str(tmp_path / "test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    try:
        service = _make_service(database, config=config)
        snapshot = await service.snapshot()
        db_info = snapshot["db"]
        assert db_info["is_memory_db"] is False
        assert db_info["path"] == str(tmp_path / "nonexistent.sqlite3")
        assert db_info["file_size_bytes"] is None
    finally:
        await database.disconnect()


@pytest.mark.asyncio
async def test_db_stats_connection_separate(db: Database) -> None:
    service = _make_service(db, stats_db=db)
    snapshot = await service.snapshot()
    assert snapshot["db"]["stats_connection_separate"] is False


@pytest.mark.asyncio
async def test_db_stats_connection_separate_true(
    db: Database,
    tmp_path: Any,
) -> None:
    other_db = Database(path=str(tmp_path / "other.sqlite3"))
    await other_db.connect()
    try:
        service = _make_service(db, stats_db=other_db)
        snapshot = await service.snapshot()
        assert snapshot["db"]["stats_connection_separate"] is True
    finally:
        await other_db.disconnect()


# -- Routing runtime fields ------------------------------------------------


@pytest.mark.asyncio
async def test_routing_runtime_fields_present(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    routing = snapshot["routing_runtime"]
    assert "active_requests_total" in routing
    assert "active_requests_by_account" in routing
    assert "pending_count" in routing
    assert "oldest_pending_age_seconds" in routing
    assert "active_reservations_count" in routing
    assert "reserved_microdollars" in routing
    assert "health_states_by_account" in routing
    assert "active_backoff_count" in routing


@pytest.mark.asyncio
async def test_routing_runtime_no_router(db: Database) -> None:
    service = _make_service(db, router=None, health_manager=None)
    snapshot = await service.snapshot()
    routing = snapshot["routing_runtime"]
    assert routing["active_requests_total"] is None
    assert routing["active_requests_by_account"] is None
    assert routing["health_states_by_account"] is None


@pytest.mark.asyncio
async def test_routing_runtime_pending_health(db: Database) -> None:
    """Pending count should be 0 when there are no pending requests."""
    service = _make_service(db)
    snapshot = await service.snapshot()
    routing = snapshot["routing_runtime"]
    assert routing["pending_count"] == 0
    assert routing["active_reservations_count"] == 0
    assert routing["reserved_microdollars"] == 0


# -- Probe errors do not leak secrets --------------------------------------


@pytest.mark.asyncio
async def test_probe_errors_do_not_include_api_keys(
    db: Database,
) -> None:
    config = AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "api_key": "super-secret-key-12345678",
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )
    service = _make_service(db, config=config)
    snapshot = await service.snapshot()
    for err in snapshot["probe_errors"]:
        assert "super-secret" not in err
        assert "OPENCODE_TEST_KEY" not in err


# -- Snapshot is deterministic for same inputs -----------------------------


@pytest.mark.asyncio
async def test_snapshot_returns_stable_keys(db: Database) -> None:
    service = _make_service(db)
    snap1 = await service.snapshot()
    snap2 = await service.snapshot()
    assert set(snap1.keys()) == set(snap2.keys())
    assert set(snap1["server"].keys()) == set(snap2["server"].keys())
    assert set(snap1["memory"].keys()) == set(snap2["memory"].keys())
    assert set(snap1["db"].keys()) == set(snap2["db"].keys())
    assert set(snap1["routing_runtime"].keys()) == set(snap2["routing_runtime"].keys())


# -- BackgroundTaskMonitor --------------------------------------------------


@pytest.mark.asyncio
async def test_background_tasks_with_task_monitor(db: Database) -> None:
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor

    supervisor = TaskSupervisor()

    async def dummy() -> None:
        await asyncio.sleep(3600)

    supervisor.register("monitored-task", dummy, max_restarts=5)
    monitor = BackgroundTaskMonitor(supervisor)

    service = _make_service(db, supervisor=supervisor, task_monitor=monitor)
    snapshot = await service.snapshot()
    tasks = snapshot["background_tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["name"] == "monitored-task"
    assert task["registered"] is True
    assert task["max_restarts"] == 5
    assert "iteration_count" in task
    assert "last_started_at" in task
    assert "last_completed_at" in task
    assert "last_error_at" in task
    assert "last_error_class" in task


@pytest.mark.asyncio
async def test_task_monitor_heartbeat_fields(db: Database) -> None:
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor

    supervisor = TaskSupervisor()

    async def quick() -> None:
        return

    supervisor.register("heartbeat-test", quick)
    monitor = BackgroundTaskMonitor(supervisor)

    service = _make_service(db, supervisor=supervisor, task_monitor=monitor)
    snapshot = await service.snapshot()
    task = snapshot["background_tasks"][0]
    # Not started yet — all heartbeat timestamps should be None
    assert task["last_started_at"] is None
    assert task["last_completed_at"] is None
    assert task["last_error_at"] is None
    assert task["last_error_class"] is None
    assert task["iteration_count"] == 0


@pytest.mark.asyncio
async def test_task_monitor_tracks_iteration(db: Database) -> None:
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor

    supervisor = TaskSupervisor()
    iteration_count = 0

    async def counting_task() -> None:
        nonlocal iteration_count
        iteration_count += 1
        if iteration_count < 3:
            return  # completes "unexpectedly", supervisor restarts
        await asyncio.sleep(3600)  # stay running after 3 iterations

    supervisor.register("counter", counting_task)
    monitor = BackgroundTaskMonitor(supervisor)

    service = _make_service(db, supervisor=supervisor, task_monitor=monitor)
    # Give the task loop time to run a few iterations
    await supervisor.start_all()
    await asyncio.sleep(0.1)
    snapshot = await service.snapshot()
    await supervisor.stop_all()

    task = snapshot["background_tasks"][0]
    assert task["iteration_count"] >= 1


@pytest.mark.asyncio
async def test_task_monitor_handles_exception_class(db: Database) -> None:
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor

    supervisor = TaskSupervisor()

    async def failing_task() -> None:
        raise ValueError("boom")

    supervisor.register("failer", failing_task, max_restarts=1)
    monitor = BackgroundTaskMonitor(supervisor)

    service = _make_service(db, supervisor=supervisor, task_monitor=monitor)
    await supervisor.start_all()
    # Give the task loop time to fail
    await asyncio.sleep(0.2)
    await supervisor.stop_all()

    snapshot = await service.snapshot()
    task = snapshot["background_tasks"][0]
    assert task["last_error_class"] == "ValueError"
    assert task["last_error_at"] is not None


# -- Database contention counters -------------------------------------------


@pytest.mark.asyncio
async def test_db_contention_snapshot_fields(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    contention = snapshot["db"]["contention"]
    assert "write_ops" in contention
    assert "read_ops" in contention
    assert "total_transactions" in contention
    assert "last_operation_error_class" in contention
    assert "cumulative_lock_wait_s" in contention
    assert "max_lock_wait_s" in contention
    assert isinstance(contention["write_ops"], int)
    assert isinstance(contention["read_ops"], int)
    assert isinstance(contention["total_transactions"], int)
    assert contention["last_operation_error_class"] is None


@pytest.mark.asyncio
async def test_db_contention_increments_on_write(db: Database) -> None:
    """Write ops counter should increment after a write operation."""
    service = _make_service(db)
    snap_before = await service.snapshot()
    write_ops_before = snap_before["db"]["contention"]["write_ops"]

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO health_probe (probe_at) VALUES (CURRENT_TIMESTAMP)"
        )

    snap_after = await service.snapshot()
    write_ops_after = snap_after["db"]["contention"]["write_ops"]
    assert write_ops_after > write_ops_before


@pytest.mark.asyncio
async def test_db_contention_increments_on_read(db: Database) -> None:
    """Read ops counter should increment after a read operation."""
    service = _make_service(db)
    snap_before = await service.snapshot()
    read_ops_before = snap_before["db"]["contention"]["read_ops"]

    await db.fetch_one("SELECT 1")

    snap_after = await service.snapshot()
    read_ops_after = snap_after["db"]["contention"]["read_ops"]
    assert read_ops_after > read_ops_before


@pytest.mark.asyncio
async def test_db_contention_transactions_increment(db: Database) -> None:
    """Total transactions counter should increment."""
    service = _make_service(db)
    snap_before = await service.snapshot()
    txn_before = snap_before["db"]["contention"]["total_transactions"]

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO health_probe (probe_at) VALUES (CURRENT_TIMESTAMP)"
        )

    snap_after = await service.snapshot()
    txn_after = snap_after["db"]["contention"]["total_transactions"]
    assert txn_after > txn_before


# -- DispatchOverheadRecorder ------------------------------------------------


class TestDispatchOverheadRecorder:
    """Tests for the in-memory dispatch-overhead recorder."""

    def test_empty_snapshot(self) -> None:
        from eggpool.runtime_dispatch import DispatchOverheadRecorder

        recorder = DispatchOverheadRecorder(window_size=100)
        snap = recorder.snapshot()
        assert snap["window_size"] == 100
        assert snap["sample_count"] == 0
        assert snap["avg_ms"] is None
        assert snap["min_ms"] is None
        assert snap["max_ms"] is None
        assert snap["p50_ms"] is None
        assert snap["p95_ms"] is None

    def test_bounded_window_drops_oldest(self) -> None:
        from eggpool.runtime_dispatch import DispatchOverheadRecorder

        recorder = DispatchOverheadRecorder(window_size=3)
        recorder.record_ns(1_000_000)
        recorder.record_ns(2_000_000)
        recorder.record_ns(3_000_000)
        recorder.record_ns(4_000_000)
        snap = recorder.snapshot()
        assert snap["sample_count"] == 3
        assert snap["min_ms"] == 2.0
        assert snap["max_ms"] == 4.0
        assert snap["avg_ms"] == 3.0

    def test_ignores_negative_samples(self) -> None:
        from eggpool.runtime_dispatch import DispatchOverheadRecorder

        recorder = DispatchOverheadRecorder()
        recorder.record_ns(-1)
        recorder.record_ns(-100_000)
        snap = recorder.snapshot()
        assert snap["sample_count"] == 0
        assert snap["avg_ms"] is None

    def test_aggregates_percentiles(self) -> None:
        from eggpool.runtime_dispatch import DispatchOverheadRecorder

        recorder = DispatchOverheadRecorder(window_size=10)
        for ms in range(10, 110, 10):
            recorder.record_ns(ms * 1_000_000)
        snap = recorder.snapshot()
        assert snap["sample_count"] == 10
        assert snap["min_ms"] == 10.0
        assert snap["max_ms"] == 100.0
        assert snap["avg_ms"] == 55.0
        assert snap["p50_ms"] is not None
        assert snap["p95_ms"] is not None
        assert snap["p50_ms"] >= snap["min_ms"]
        assert snap["p50_ms"] <= snap["max_ms"]
        assert snap["p95_ms"] >= snap["p50_ms"]
        assert snap["p95_ms"] <= snap["max_ms"]


# -- RuntimeMetricsService dispatch overhead / load sections ----------------


@pytest.mark.asyncio
async def test_snapshot_dispatch_overhead_section_present(db: Database) -> None:
    from eggpool.runtime_dispatch import DispatchOverheadRecorder

    recorder = DispatchOverheadRecorder(window_size=100)
    recorder.record_ns(2_000_000)
    service = _make_service(db, dispatch_overhead_recorder=recorder)
    snapshot = await service.snapshot()
    assert "dispatch_overhead" in snapshot
    dispatch = snapshot["dispatch_overhead"]
    assert dispatch["window_size"] == 100
    assert dispatch["sample_count"] == 1
    assert dispatch["avg_ms"] == 2.0
    assert dispatch["min_ms"] == 2.0
    assert dispatch["max_ms"] == 2.0


@pytest.mark.asyncio
async def test_snapshot_dispatch_overhead_no_recorder(db: Database) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    dispatch = snapshot["dispatch_overhead"]
    assert dispatch["window_size"] == 100
    assert dispatch["sample_count"] == 0
    assert dispatch["avg_ms"] is None
    assert dispatch["min_ms"] is None
    assert dispatch["max_ms"] is None
    assert dispatch["p50_ms"] is None
    assert dispatch["p95_ms"] is None


@pytest.mark.asyncio
async def test_snapshot_dispatch_overhead_aggregates(db: Database) -> None:
    from eggpool.runtime_dispatch import DispatchOverheadRecorder

    recorder = DispatchOverheadRecorder(window_size=10)
    for ms in range(10, 110, 10):
        recorder.record_ns(ms * 1_000_000)
    service = _make_service(db, dispatch_overhead_recorder=recorder)
    snapshot = await service.snapshot()
    dispatch = snapshot["dispatch_overhead"]
    assert dispatch["sample_count"] == 10
    assert dispatch["avg_ms"] == 55.0
    assert dispatch["max_ms"] == 100.0
    assert dispatch["min_ms"] == 10.0
    assert dispatch["p50_ms"] is not None
    assert dispatch["p95_ms"] is not None


@pytest.mark.asyncio
async def test_snapshot_load_section_present(db: Database) -> None:
    with (
        patch("eggpool.runtime_metrics.os.getloadavg", return_value=(0.5, 0.3, 0.2)),
        patch("eggpool.runtime_metrics.os.cpu_count", return_value=4),
    ):
        service = _make_service(db)
        snapshot = await service.snapshot()
    load = snapshot["load"]
    assert load["available"] is True
    assert load["cpu_count"] == 4
    assert load["load_1m"] == 0.5
    assert load["load_5m"] == 0.3
    assert load["load_15m"] == 0.2
    assert load["normalized_1m"] == 0.125
    assert load["normalized_5m"] == 0.075
    assert load["normalized_15m"] == 0.05


@pytest.mark.asyncio
async def test_snapshot_load_unavailable(db: Database) -> None:
    with (
        patch(
            "eggpool.runtime_metrics.os.getloadavg",
            side_effect=OSError("not available"),
        ),
        patch("eggpool.runtime_metrics.os.cpu_count", return_value=4),
    ):
        service = _make_service(db)
        snapshot = await service.snapshot()
    load = snapshot["load"]
    assert load["available"] is False
    assert load["cpu_count"] == 4
    assert load["load_1m"] is None
    assert load["load_5m"] is None
    assert load["load_15m"] is None
    assert load["normalized_1m"] is None


@pytest.mark.asyncio
async def test_snapshot_load_zero_cpu_count(db: Database) -> None:
    with (
        patch("eggpool.runtime_metrics.os.getloadavg", return_value=(1.0, 1.0, 1.0)),
        patch("eggpool.runtime_metrics.os.cpu_count", return_value=0),
    ):
        service = _make_service(db)
        snapshot = await service.snapshot()
    load = snapshot["load"]
    assert load["available"] is True
    assert load["cpu_count"] == 0
    assert load["normalized_1m"] is None
