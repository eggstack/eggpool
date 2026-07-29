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
    _MAX_PROBE_ERRORS,
    RuntimeMetricsService,
    _append_probe_error,
    _parse_proc_stat_ids,
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
    dispatch_span_recorder: Any | None = None,
    finalization_retry_queue: Any | None = None,
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
        dispatch_span_recorder=dispatch_span_recorder,
        finalization_retry_queue=finalization_retry_queue,
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


def test_append_probe_error_caps_count_and_message_length() -> None:
    errors: list[str] = []

    for index in range(_MAX_PROBE_ERRORS + 5):
        _append_probe_error(errors, f"{index}:" + ("x" * 300))

    assert len(errors) == _MAX_PROBE_ERRORS
    assert all(len(error) <= _MAX_PROBE_ERROR_LEN for error in errors)
    assert errors[-1].startswith(f"{_MAX_PROBE_ERRORS - 1}:")


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


def test_parse_proc_stat_ids_uses_linux_stat_field_numbers() -> None:
    """PPID and session must use fields 4 and 6 from /proc/<pid>/stat."""
    fields = [
        "S",  # 3 state
        "4321",  # 4 ppid
        "1111",  # 5 pgrp
        "2222",  # 6 session
        "0",  # 7 tty_nr
    ]
    stat = f"12345 (eggpool worker) {' '.join(fields)}"

    ppid, session_id = _parse_proc_stat_ids(stat)

    assert ppid == 4321
    assert session_id == 2222


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
    assert task["iteration_count"] == 0
    assert isinstance(task["restart_count"], int)
    assert task["last_started_at"] is None
    assert task["last_completed_at"] is None
    assert task["last_failure_at"] is None
    assert task["last_error_at"] is None
    assert task["last_error_class"] is None
    assert task["last_tick_duration_ms"] is None
    # interval_s is plumbed through to the runtime-metrics snapshot so
    # the dashboard can show "how often" each task runs. None means
    # the cadence is unknown.
    assert task["interval_s"] is None


@pytest.mark.asyncio
async def test_background_tasks_interval_s_plumbed(db: Database) -> None:
    from eggpool.background import TaskSupervisor

    supervisor = TaskSupervisor()

    async def dummy() -> None:
        await asyncio.sleep(3600)

    supervisor.register("interval-task", dummy, max_restarts=5, interval_s=42.0)
    service = _make_service(db, supervisor=supervisor)
    snapshot = await service.snapshot()
    task = snapshot["background_tasks"][0]
    assert task["interval_s"] == 42.0


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
    assert db_info["configured_worker_threads"] == 2
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


@pytest.mark.asyncio
async def test_routing_runtime_excludes_released_reservations(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (id, name, api_key_env) "
            "VALUES (1, 'test-acct', 'KEY')"
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES ('gpt-4', 'openai')"
        )
        await db.execute_write(
            "INSERT INTO requests "
            "(account_id, model_id, status, started_at) "
            "VALUES (1, 'gpt-4', 'completed', datetime('now'))"
        )
        await db.execute_write(
            "INSERT INTO reservations "
            "(request_id, account_id, model_id, reserved_microdollars, status) "
            "VALUES (1, 1, 'gpt-4', 100, 'released')"
        )

    routing = (await _make_service(db).snapshot())["routing_runtime"]

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


@pytest.mark.asyncio
async def test_background_task_summary_counts_overdue_and_errors(
    db: Database,
) -> None:
    """``background_task_summary`` aggregates registered / running /
    failed / overdue / last-error counts so the dashboard can render an
    at-a-glance overview without iterating every snapshot row."""
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor

    supervisor = TaskSupervisor()

    async def tick() -> None:
        return None

    supervisor.register("healthy_daemon", tick)
    supervisor.register_periodic("healthy_periodic", tick, interval_s=60.0)
    supervisor.register_periodic("overdue_periodic", tick, interval_s=60.0)
    # Force the overdue task's deadline into the past.
    overdue_task = supervisor.get_task("overdue_periodic")
    assert overdue_task is not None
    overdue_task._next_run_at = time.time() - 600  # pyright: ignore[reportPrivateUsage]

    monitor = BackgroundTaskMonitor(supervisor)
    service = _make_service(db, supervisor=supervisor, task_monitor=monitor)
    snapshot = await service.snapshot()

    summary = snapshot["background_task_summary"]
    assert summary["registered"] == 3
    assert summary["overdue"] == 1
    assert summary["failed"] == 0
    assert summary["running"] == 0  # none started yet
    assert summary["last_error_count"] == 0


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
        assert snap["p99_ms"] is None

    def test_rejects_non_positive_window_size(self) -> None:
        from eggpool.runtime_dispatch import DispatchOverheadRecorder

        with pytest.raises(ValueError, match="window_size must be at least 1"):
            DispatchOverheadRecorder(window_size=0)

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
        assert snap["p99_ms"] is not None
        assert snap["p99_ms"] >= snap["p95_ms"]
        assert snap["p99_ms"] <= snap["max_ms"]


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
    assert dispatch["p99_ms"] is None


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
    assert dispatch["p99_ms"] is not None


@pytest.mark.asyncio
async def test_snapshot_dispatch_spans_with_recorder(db: Database) -> None:
    from eggpool.runtime_dispatch import ALL_SPAN_KEYS, DispatchSpanRecorder

    recorder = DispatchSpanRecorder(window_size=10)
    recorder.record_ns("json_parse", 1_000_000)
    recorder.record_ns("compression_apply", 7_000_000)
    service = _make_service(db, dispatch_span_recorder=recorder)
    snapshot = await service.snapshot()
    assert "dispatch_spans" in snapshot
    dispatch_spans = snapshot["dispatch_spans"]
    assert dispatch_spans["window_size"] == 10
    spans = {row["span"]: row for row in dispatch_spans["spans"]}
    # Recorder keys must appear with their recorded values.
    assert spans["json_parse"]["sample_count"] == 1
    assert spans["json_parse"]["avg_ms"] == 1.0
    assert spans["compression_apply"]["sample_count"] == 1
    assert spans["compression_apply"]["avg_ms"] == 7.0
    # Every known span key must be present even with no samples.
    for key in ALL_SPAN_KEYS:
        assert key in spans
        if key not in {"json_parse", "compression_apply"}:
            assert spans[key]["sample_count"] == 0
            assert spans[key]["avg_ms"] is None


@pytest.mark.asyncio
async def test_snapshot_dispatch_spans_no_recorder(db: Database) -> None:
    from eggpool.runtime_dispatch import ALL_SPAN_KEYS

    service = _make_service(db)
    snapshot = await service.snapshot()
    dispatch_spans = snapshot["dispatch_spans"]
    keys = {row["span"] for row in dispatch_spans["spans"]}
    assert set(ALL_SPAN_KEYS) == keys


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


@pytest.mark.asyncio
async def test_rollup_freshness_disabled_without_coalescer(
    db: Database,
) -> None:
    service = _make_service(db)
    snapshot = await service.snapshot()
    freshness = snapshot["rollup_freshness"]
    assert freshness == {"enabled": False}


@pytest.mark.asyncio
async def test_rollup_freshness_reports_staleness(
    db: Database,
) -> None:
    """When the rollup table trails the live requests table, the
    snapshot must surface ``staleness_seconds`` so operators can spot
    a stalled coalescer."""
    from datetime import UTC, datetime, timedelta

    from eggpool.db.migrations import MigrationRunner
    from eggpool.db.rollup_repository import UsageRollupRepository
    from eggpool.metrics.buffer import MetricsWriteCoalescer
    from eggpool.models.config import MetricsConfig

    await MigrationRunner(db).run()  # idempotent

    # Anchor both timestamps inside the probe's 7-day look-back window.
    now = datetime.now(UTC)
    recent_dt = now - timedelta(minutes=5)
    older_dt = now - timedelta(hours=2)
    recent_str = recent_dt.strftime("%Y-%m-%d %H:%M:%S")
    older_str = older_dt.strftime("%Y-%m-%d %H:%M:%S")

    async with db.transaction():
        account_id_row = await db.fetch_one(
            "INSERT INTO accounts (name, api_key_env, enabled) "
            "VALUES ('rtm_acct', 'RTM_ENV', 1) RETURNING id"
        )
        account_id = int(account_id_row["id"])
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES ('rtm_model', 'openai')"
        )
        await db.execute_write(
            """
            INSERT INTO requests (
                account_id, model_id, provider_id, started_at,
                completed_at, status, input_tokens, output_tokens,
                cost_microdollars, upstream_latency_ms,
                cache_read_tokens, cache_write_tokens, reasoning_tokens
            ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                account_id,
                "rtm_model",
                "provider_a",
                recent_str,
                recent_str,
                1,
                1,
                0,
                10.0,
                0,
                0,
            ),
        )

    rollup_repo = UsageRollupRepository(db)
    coalescer = MetricsWriteCoalescer(
        config=MetricsConfig(write_mode="balanced"),
        db=db,
        rollup_repo=rollup_repo,
    )
    await coalescer.flush(reason="test_setup")
    async with db.transaction():
        await db.execute_write(
            """
            INSERT INTO usage_rollups (
                bucket_start, bucket_size_s, provider_id, model_id,
                account_id, protocol, streamed, status
            ) VALUES (?, 60, 'provider_a', 'rtm_model', ?, 'openai', 0, 'completed')
            """,
            (older_str, account_id),
        )

    service = _make_service(db)
    service._metrics_coalescer = coalescer  # noqa: SLF001
    snapshot = await service.snapshot()
    freshness = snapshot["rollup_freshness"]
    assert freshness["enabled"] is True
    assert freshness["rollup_latest_bucket_start"] == older_str
    assert freshness["requests_latest_started_at"] == recent_str
    expected_gap = (recent_dt - older_dt).total_seconds()
    assert freshness["staleness_seconds"] == pytest.approx(expected_gap)


# -- Finalization retry queue snapshot (async) -------------------------------


class _StubFinalizerForMetrics:
    """Minimal stub satisfying FinalizationRetryQueue's finalizer protocol."""

    async def finalize(self, selected: Any, data: Any) -> bool:
        return True


@pytest.mark.asyncio
async def test_finalization_retry_queue_snapshot_empty(db: Database) -> None:
    """snapshot() returns enabled=True and size=0 for a fresh queue."""
    from eggpool.request.finalization_queue import FinalizationRetryQueue

    queue = FinalizationRetryQueue(db=db, finalizer=_StubFinalizerForMetrics())
    service = _make_service(db, finalization_retry_queue=queue)
    snapshot = await service.snapshot()
    frq = snapshot["finalization_retry_queue"]
    assert frq["enabled"] is True
    assert frq["size"] == 0


@pytest.mark.asyncio
async def test_finalization_retry_queue_no_probe_error(db: Database) -> None:
    """No probe error mentioning finalization retry queue on success."""
    from eggpool.request.finalization_queue import FinalizationRetryQueue

    queue = FinalizationRetryQueue(db=db, finalizer=_StubFinalizerForMetrics())
    service = _make_service(db, finalization_retry_queue=queue)
    snapshot = await service.snapshot()
    for err in snapshot["probe_errors"]:
        assert "finalization retry queue" not in err.lower()


@pytest.mark.asyncio
async def test_finalization_retry_queue_no_coroutine_in_snapshot(
    db: Database,
) -> None:
    """The snapshot dict must contain plain values, not coroutine objects."""
    from eggpool.request.finalization_queue import FinalizationRetryQueue

    queue = FinalizationRetryQueue(db=db, finalizer=_StubFinalizerForMetrics())
    service = _make_service(db, finalization_retry_queue=queue)
    snapshot = await service.snapshot()
    frq = snapshot["finalization_retry_queue"]
    for key, value in frq.items():
        assert not callable(value), f"Field {key!r} is a callable: {value!r}"


@pytest.mark.asyncio
async def test_finalization_retry_queue_snapshot_json_serializable(
    db: Database,
) -> None:
    """json.dumps must succeed on the snapshot with the queue wired."""
    import json

    from eggpool.request.finalization_queue import FinalizationRetryQueue

    queue = FinalizationRetryQueue(db=db, finalizer=_StubFinalizerForMetrics())
    service = _make_service(db, finalization_retry_queue=queue)
    snapshot = await service.snapshot()
    result = json.dumps(snapshot, default=str)
    assert isinstance(result, str)
    assert "finalization_retry_queue" in result


@pytest.mark.asyncio
async def test_finalization_retry_queue_disabled_when_none(db: Database) -> None:
    """When no queue is wired, snapshot returns enabled=False."""
    service = _make_service(db, finalization_retry_queue=None)
    snapshot = await service.snapshot()
    frq = snapshot["finalization_retry_queue"]
    assert frq == {"enabled": False}


@pytest.mark.asyncio
async def test_snapshot_finalization_retry_queue_section_serializes(
    db: Database,
) -> None:
    """The finalization_retry_queue section must be JSON-serializable."""
    import json

    from eggpool.request.finalization_queue import FinalizationRetryQueue

    queue = FinalizationRetryQueue(db=db, finalizer=_StubFinalizerForMetrics())
    service = _make_service(db, finalization_retry_queue=queue)
    snapshot = await service.snapshot()
    # Must not raise
    serialized = json.dumps(snapshot, default=str)
    assert "finalization_retry_queue" in serialized
    frq = snapshot["finalization_retry_queue"]
    assert frq["enabled"] is True
    assert frq["size"] == 0
