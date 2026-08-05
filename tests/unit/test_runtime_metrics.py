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
                "threads": 1,
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
    finalization_supervisor: Any | None = None,
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
        finalization_supervisor=finalization_supervisor,
    )


# -- _safe_int / _truncate_probe_error -------------------------------------


def test_safe_int_and_truncate_probe_error_table() -> None:
    """``_safe_int`` accepts numeric strings/floats; ``_truncate_probe_error`` caps."""
    assert _safe_int("42") == 42
    assert _safe_int(3.7) == 3
    assert _safe_int("not-a-number") is None
    assert _safe_int(None) is None  # type: ignore[arg-type]

    short = _truncate_probe_error("short message")
    assert short == "short message"
    long = _truncate_probe_error("x" * 300)
    assert len(long) == _MAX_PROBE_ERROR_LEN
    assert long.endswith("...")

    errors: list[str] = []
    for index in range(_MAX_PROBE_ERRORS + 5):
        _append_probe_error(errors, f"{index}:" + ("x" * 300))
    assert len(errors) == _MAX_PROBE_ERRORS
    assert all(len(error) <= _MAX_PROBE_ERROR_LEN for error in errors)
    assert errors[-1].startswith(f"{_MAX_PROBE_ERRORS - 1}:")


def test_parse_proc_stat_uses_linux_field_numbers() -> None:
    """``/proc/<pid>/stat`` parsing: RSS from field 24, PPID/session from 4/6."""
    page_size = 4096
    vsize_bytes = 1_200_000_000
    rss_pages = 50_000
    mem_fields = [
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
    mem_stat = f"12345 (eggpool worker) {' '.join(mem_fields)}"
    vms_bytes, rss_bytes = _parse_proc_stat_memory(mem_stat, page_size)
    assert vms_bytes == vsize_bytes
    assert rss_bytes == rss_pages * page_size
    assert rss_bytes != vsize_bytes * page_size

    id_fields = [
        "S",  # 3 state
        "4321",  # 4 ppid
        "1111",  # 5 pgrp
        "2222",  # 6 session
        "0",  # 7 tty_nr
    ]
    id_stat = f"12345 (eggpool worker) {' '.join(id_fields)}"
    ppid, session_id = _parse_proc_stat_ids(id_stat)
    assert ppid == 4321
    assert session_id == 2222


# -- snapshot() top-level structure -----------------------------------------


@pytest.mark.asyncio
async def test_snapshot_returns_all_top_level_keys(db: Database) -> None:
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
    snap1 = await service.snapshot()
    snap2 = await service.snapshot()
    for key in (
        "server",
        "memory",
        "processes",
        "background_tasks",
        "db",
        "routing_runtime",
        "probe_errors",
    ):
        assert key in snap1
    assert isinstance(snap1["probe_errors"], list)
    assert len(snap1["probe_errors"]) <= 16  # _MAX_PROBE_ERRORS
    for err in snap1["probe_errors"]:
        assert "super-secret" not in err
        assert "OPENCODE_TEST_KEY" not in err
    assert set(snap1.keys()) == set(snap2.keys())
    assert set(snap1["server"].keys()) == set(snap2["server"].keys())
    assert set(snap1["memory"].keys()) == set(snap2["memory"].keys())
    assert set(snap1["db"].keys()) == set(snap2["db"].keys())
    assert set(snap1["routing_runtime"].keys()) == set(snap2["routing_runtime"].keys())


@pytest.mark.asyncio
async def test_snapshot_exposes_bounded_finalization_supervisor(
    db: Database,
) -> None:
    supervisor = type(
        "Supervisor",
        (),
        {"snapshot": lambda _self: {"active_count": 2, "retry_pending_count": 1}},
    )()
    snapshot = await _make_service(db, finalization_supervisor=supervisor).snapshot()
    assert snapshot["finalization_supervisor"] == {
        "active_count": 2,
        "retry_pending_count": 1,
    }


# -- Server fields ---------------------------------------------------------


@pytest.mark.asyncio
async def test_server_and_memory_fields_present(db: Database) -> None:
    """Server fields are typed correctly; memory keys exist; uptime increases."""
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
    assert server["configured_server_threads"] == 1

    memory = snapshot["memory"]
    for key in ("rss_bytes", "vms_bytes", "open_fd_count", "thread_count"):
        assert key in memory
    assert isinstance(memory["thread_count"], int)

    with patch("pathlib.Path.exists", side_effect=OSError("no /proc")):
        snapshot = await _make_service(db).snapshot()
    for key in ("rss_bytes", "vms_bytes"):
        assert key in snapshot["memory"]

    uptime_service = _make_service(db, started_monotonic=time.monotonic() - 1.0)
    snap1 = await uptime_service.snapshot()
    await asyncio.sleep(0.05)
    snap2 = await uptime_service.snapshot()
    assert snap2["server"]["uptime_seconds"] > snap1["server"]["uptime_seconds"]


# -- Process count warning -------------------------------------------------


@pytest.mark.asyncio
async def test_process_count_and_background_tasks_states(db: Database) -> None:
    """Process count fields exist; background tasks surface all states."""
    from eggpool.background import TaskSupervisor

    service = _make_service(db)
    processes = (await service.snapshot())["processes"]
    assert "eggpool_process_count" in processes
    assert "expected_worker_process_count" in processes
    assert "process_count_warning" in processes
    assert isinstance(processes["expected_worker_process_count"], int)

    empty = (await _make_service(db, supervisor=None).snapshot())["background_tasks"]
    assert empty == []

    supervisor = TaskSupervisor()

    async def dummy() -> None:
        await asyncio.sleep(3600)

    supervisor.register("test-task", dummy, max_restarts=5)
    snapshot = await _make_service(db, supervisor=supervisor).snapshot()
    task = snapshot["background_tasks"][0]
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
    assert task["interval_s"] is None

    interval_supervisor = TaskSupervisor()
    interval_supervisor.register(
        "interval-task", dummy, max_restarts=5, interval_s=42.0
    )
    interval_task = (
        await _make_service(db, supervisor=interval_supervisor).snapshot()
    )["background_tasks"][0]
    assert interval_task["interval_s"] == 42.0

    cancel_supervisor = TaskSupervisor()
    task_obj = cancel_supervisor.register("cancel-me", dummy)
    await task_obj.start()
    task_obj._task.cancel()  # type: ignore[union-attr]
    await asyncio.sleep(0.01)
    cancel_task = (await _make_service(db, supervisor=cancel_supervisor).snapshot())[
        "background_tasks"
    ][0]
    assert isinstance(cancel_task["cancelled"], bool)

    not_started_supervisor = TaskSupervisor()

    async def quick_finish() -> None:
        return

    not_started_supervisor.register("not-started", quick_finish)
    not_started_task = (
        await _make_service(db, supervisor=not_started_supervisor).snapshot()
    )["background_tasks"][0]
    assert not_started_task["running"] is False
    assert not_started_task["done"] is False


# -- DB snapshot fields ----------------------------------------------------


@pytest.mark.asyncio
async def test_db_snapshot_fields_and_modes(db: Database, tmp_path: Any) -> None:
    """db snapshot exposes keys; memory/file modes; stats connection separation."""
    service = _make_service(db)
    db_info = (await service.snapshot())["db"]
    for key in (
        "path",
        "is_memory_db",
        "wal_enabled",
        "wal_mode_live",
        "synchronous",
        "synchronous_live",
        "busy_timeout_ms",
        "primary_connected",
        "stats_connection_separate",
        "file_size_bytes",
        "wal_size_bytes",
        "shm_size_bytes",
    ):
        assert key in db_info
    assert db_info["configured_worker_threads"] == 2

    mem_config = AppConfig.from_dict(
        {
            "server": {"api_key_env": "OPENCODE_TEST_KEY"},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )
    mem_info = (await _make_service(db, config=mem_config).snapshot())["db"]
    assert mem_info["is_memory_db"] is True
    assert mem_info["path"] is None
    assert mem_info["file_size_bytes"] is None
    assert mem_info["wal_size_bytes"] is None
    assert mem_info["shm_size_bytes"] is None

    file_config = AppConfig.from_dict(
        {
            "server": {"api_key_env": "OPENCODE_TEST_KEY"},
            "database": {"path": str(tmp_path / "nonexistent.sqlite3")},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )
    db_info = (await _make_service(db, config=file_config).snapshot())["db"]
    assert db_info["is_memory_db"] is False
    assert db_info["path"] == str(tmp_path / "nonexistent.sqlite3")
    assert db_info["file_size_bytes"] is None

    same_info = (await _make_service(db, stats_db=db).snapshot())["db"]
    assert same_info["stats_connection_separate"] is False

    other_db = Database(path=str(tmp_path / "other.sqlite3"))
    await other_db.connect()
    try:
        separate_info = (await _make_service(db, stats_db=other_db).snapshot())["db"]
        assert separate_info["stats_connection_separate"] is True
    finally:
        await other_db.disconnect()


# -- Routing runtime fields ------------------------------------------------


@pytest.mark.asyncio
async def test_routing_runtime_fields_and_no_router(db: Database) -> None:
    """``routing_runtime`` exposes its keys; None branches leave them as ``None``;
    released reservations do not inflate active counts.
    """
    service = _make_service(db)
    snapshot = await service.snapshot()
    routing = snapshot["routing_runtime"]
    for key in (
        "active_requests_total",
        "active_requests_by_account",
        "pending_count",
        "oldest_pending_age_seconds",
        "active_reservations_count",
        "reserved_microdollars",
        "health_states_by_account",
        "active_backoff_count",
    ):
        assert key in routing
    assert routing["pending_count"] == 0
    assert routing["active_reservations_count"] == 0
    assert routing["reserved_microdollars"] == 0

    none_service = _make_service(db, router=None, health_manager=None)
    none_routing = (await none_service.snapshot())["routing_runtime"]
    assert none_routing["active_requests_total"] is None
    assert none_routing["active_requests_by_account"] is None
    assert none_routing["health_states_by_account"] is None

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

    released_routing = (await _make_service(db).snapshot())["routing_runtime"]
    assert released_routing["active_reservations_count"] == 0
    assert released_routing["reserved_microdollars"] == 0


# -- Probe errors covered by test_snapshot_returns_all_top_level_keys --


# -- BackgroundTaskMonitor --------------------------------------------------


@pytest.mark.asyncio
async def test_task_monitor_states_and_summary(db: Database) -> None:
    """Heartbeat fields / iteration tracking / exception class / summary counts."""
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor

    supervisor = TaskSupervisor()

    async def quick() -> None:
        return

    supervisor.register("heartbeat-test", quick)
    monitor = BackgroundTaskMonitor(supervisor)
    heartbeat_task = (
        await _make_service(db, supervisor=supervisor, task_monitor=monitor).snapshot()
    )["background_tasks"][0]
    assert heartbeat_task["last_started_at"] is None
    assert heartbeat_task["last_completed_at"] is None
    assert heartbeat_task["last_error_at"] is None
    assert heartbeat_task["last_error_class"] is None
    assert heartbeat_task["iteration_count"] == 0

    counter_supervisor = TaskSupervisor()
    iteration_count = 0

    async def counting_task() -> None:
        nonlocal iteration_count
        iteration_count += 1
        if iteration_count < 3:
            return
        await asyncio.sleep(3600)

    counter_supervisor.register("counter", counting_task)
    counter_monitor = BackgroundTaskMonitor(counter_supervisor)
    counter_service = _make_service(
        db, supervisor=counter_supervisor, task_monitor=counter_monitor
    )
    await counter_supervisor.start_all()
    await asyncio.sleep(0.1)
    counter_snapshot = await counter_service.snapshot()
    await counter_supervisor.stop_all()
    assert counter_snapshot["background_tasks"][0]["iteration_count"] >= 1

    fail_supervisor = TaskSupervisor()

    async def failing_task() -> None:
        raise ValueError("boom")

    fail_supervisor.register("failer", failing_task, max_restarts=1)
    fail_monitor = BackgroundTaskMonitor(fail_supervisor)
    fail_service = _make_service(
        db, supervisor=fail_supervisor, task_monitor=fail_monitor
    )
    await fail_supervisor.start_all()
    await asyncio.sleep(0.2)
    await fail_supervisor.stop_all()
    fail_snapshot = await fail_service.snapshot()
    assert fail_snapshot["background_tasks"][0]["last_error_class"] == "ValueError"
    assert fail_snapshot["background_tasks"][0]["last_error_at"] is not None

    summary_supervisor = TaskSupervisor()

    async def tick() -> None:
        return None

    summary_supervisor.register("healthy_daemon", tick)
    summary_supervisor.register_periodic("healthy_periodic", tick, interval_s=60.0)
    summary_supervisor.register_periodic("overdue_periodic", tick, interval_s=60.0)
    overdue_task = summary_supervisor.get_task("overdue_periodic")
    assert overdue_task is not None
    overdue_task._next_run_at = time.time() - 600  # pyright: ignore[reportPrivateUsage]
    summary_monitor = BackgroundTaskMonitor(summary_supervisor)
    summary = (
        await _make_service(
            db, supervisor=summary_supervisor, task_monitor=summary_monitor
        ).snapshot()
    )["background_task_summary"]
    assert summary["registered"] == 3
    assert summary["overdue"] == 1
    assert summary["failed"] == 0
    assert summary["running"] == 0
    assert summary["last_error_count"] == 0


# -- Database contention counters -------------------------------------------


@pytest.mark.asyncio
async def test_db_contention_snapshot_and_counters_increment(db: Database) -> None:
    """``db.contention`` is present and counters increment on read/write/transaction."""
    service = _make_service(db)
    snapshot = await service.snapshot()
    contention = snapshot["db"]["contention"]
    assert contention, "snapshot must expose db.contention"
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

    snap_before = await service.snapshot()
    write_ops_before = snap_before["db"]["contention"]["write_ops"]
    read_ops_before = snap_before["db"]["contention"]["read_ops"]
    txn_before = snap_before["db"]["contention"]["total_transactions"]

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO health_probe (probe_at) VALUES (CURRENT_TIMESTAMP)"
        )
    await db.fetch_one("SELECT 1")

    snap_after = await service.snapshot()
    assert snap_after["db"]["contention"]["write_ops"] > write_ops_before
    assert snap_after["db"]["contention"]["read_ops"] > read_ops_before
    assert snap_after["db"]["contention"]["total_transactions"] > txn_before


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
async def test_snapshot_dispatch_overhead_scenarios(db: Database) -> None:
    """``dispatch_overhead`` section is null-safe, present, and aggregates."""
    from eggpool.runtime_dispatch import DispatchOverheadRecorder

    service = _make_service(db)
    snapshot = await service.snapshot()
    dispatch = snapshot["dispatch_overhead"]
    assert dispatch["window_size"] == 100
    assert dispatch["sample_count"] == 0
    for key in ("avg_ms", "min_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"):
        assert dispatch[key] is None

    recorder = DispatchOverheadRecorder(window_size=100)
    recorder.record_ns(2_000_000)
    service = _make_service(db, dispatch_overhead_recorder=recorder)
    snapshot = await service.snapshot()
    dispatch = snapshot["dispatch_overhead"]
    assert dispatch["sample_count"] == 1
    assert dispatch["avg_ms"] == 2.0
    assert dispatch["min_ms"] == 2.0
    assert dispatch["max_ms"] == 2.0

    agg = DispatchOverheadRecorder(window_size=10)
    for ms in range(10, 110, 10):
        agg.record_ns(ms * 1_000_000)
    service = _make_service(db, dispatch_overhead_recorder=agg)
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
async def test_snapshot_dispatch_spans_recorded_and_default_keys(
    db: Database,
) -> None:
    """``dispatch_spans`` lists every known span key; recorded values surface."""
    from eggpool.runtime_dispatch import ALL_SPAN_KEYS, DispatchSpanRecorder

    service = _make_service(db)
    snapshot = await service.snapshot()
    keys = {row["span"] for row in snapshot["dispatch_spans"]["spans"]}
    assert set(ALL_SPAN_KEYS) == keys

    recorder = DispatchSpanRecorder(window_size=10)
    recorder.record_ns("json_parse", 1_000_000)
    recorder.record_ns("compression_apply", 7_000_000)
    service = _make_service(db, dispatch_span_recorder=recorder)
    snapshot = await service.snapshot()
    dispatch_spans = snapshot["dispatch_spans"]
    assert dispatch_spans["window_size"] == 10
    spans = {row["span"]: row for row in dispatch_spans["spans"]}
    assert spans["json_parse"]["sample_count"] == 1
    assert spans["json_parse"]["avg_ms"] == 1.0
    assert spans["compression_apply"]["sample_count"] == 1
    assert spans["compression_apply"]["avg_ms"] == 7.0
    for key in ALL_SPAN_KEYS:
        assert key in spans
        if key not in {"json_parse", "compression_apply"}:
            assert spans[key]["sample_count"] == 0
            assert spans[key]["avg_ms"] is None


@pytest.mark.asyncio
async def test_snapshot_load_section_scenarios(db: Database) -> None:
    """``load`` section handles present, unavailable, and zero-CPU scenarios."""
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
async def test_rollup_freshness_disabled_and_staleness(
    db: Database,
) -> None:
    """Without a coalescer freshness is disabled; with one it surfaces staleness."""
    from datetime import UTC, datetime, timedelta

    from eggpool.db.migrations import MigrationRunner
    from eggpool.db.rollup_repository import UsageRollupRepository
    from eggpool.metrics.buffer import MetricsWriteCoalescer
    from eggpool.models.config import MetricsConfig

    disabled = (await _make_service(db).snapshot())["rollup_freshness"]
    assert disabled == {"enabled": False}

    await MigrationRunner(db).run()  # idempotent

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


@pytest.mark.asyncio
async def test_finalization_retry_queue_snapshot_contract(db: Database) -> None:
    """Legacy retry queue diagnostics are no longer exposed."""
    service = _make_service(db)
    snapshot = await service.snapshot()
    assert "finalization_retry_queue" not in snapshot
