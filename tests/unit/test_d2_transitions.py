"""Phase 5: Deterministic transition tests for Milestone D2.

Covers:
- Interval changes (single schedule, no overlapping ticks)
- Enable/disable transitions (model_info, backup)
- Retention policy live changes
- Metrics flush cadence reconfiguration
- Backup cadence changes
- Rapid reload convergence
"""

from __future__ import annotations

import asyncio
import time

import pytest

from eggpool.background import TaskSupervisor
from eggpool.runtime_manager import ProcessRuntime
from eggpool.runtime_task_inventory import (
    RuntimeTaskSpec,
    TaskOwnership,
    inventory_for_config,
)
from eggpool.runtime_tasks import (
    apply_spec_diff,
    compute_spec_diff,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"


def _make_config(**overrides: object) -> object:
    from eggpool.models.config import AppConfig

    base: dict[str, object] = {
        "server": {"api_key": SERVER_API_KEY},
        "providers": {
            "opencode-go": {
                "id": "opencode-go",
                "base_url": "https://opencode.ai/zen/go/v1",
                "protocols": ["openai"],
                "models_endpoint": {"method": "GET", "path": "/models"},
                "accounts": [
                    {
                        "name": "default",
                        "api_key": ACCOUNT_API_KEY,
                        "enabled": True,
                        "weight": 1.0,
                    }
                ],
            }
        },
    }
    if overrides:
        for k, v in overrides.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                base[k] = {**base[k], **v}  # type: ignore[index]
            else:
                base[k] = v
    return AppConfig.from_dict(base)


def _make_spec(
    name: str,
    interval: float = 60.0,
    *,
    enabled: bool = True,
    initial_delay_s: float | None = None,
    run_immediately: bool = False,
    ownership: TaskOwnership = TaskOwnership.PROCESS,
) -> RuntimeTaskSpec:
    return RuntimeTaskSpec(
        name=name,
        interval_s=interval,
        initial_delay_s=initial_delay_s,
        run_immediately=run_immediately,
        timeout_s=None,
        ownership=ownership,
        enabled=enabled,
        description=f"test {name}",
        reloadable_fields=(),
        generation_dependencies=(),
        process_dependencies=(),
        callback_kind=name,
    )


async def _noop_tick() -> None:
    return None


# ---------------------------------------------------------------------------
# 1. Interval changes: single schedule, new interval reflected
# ---------------------------------------------------------------------------


class TestIntervalChanges:
    @pytest.mark.asyncio
    async def test_start_interval_a_change_to_b_single_schedule(self) -> None:
        """Start with interval A; change to interval B; exactly one task exists;
        next_run reflects B."""
        supervisor = TaskSupervisor()
        task = supervisor.register_periodic(
            "interval_task", _noop_tick, interval_s=0.05
        )
        await task.start()

        # Verify initial interval
        assert task._interval_s == pytest.approx(0.05)

        # Update to interval B
        new_task = await supervisor.update_task_spec(
            "interval_task",
            tick_factory=_noop_tick,
            interval_s=0.1,
        )

        # Exactly one task with that name
        assert supervisor.get_task("interval_task") is new_task
        # New interval reflected
        assert new_task._interval_s == pytest.approx(0.1)
        # next_run_at reflects the new interval (monotonic deadline)
        assert new_task._next_run_at > time.monotonic()

        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_no_overlapping_ticks_during_update(self) -> None:
        """While a tick is in progress, update_task_spec runs cleanly.
        No two tasks with the same name exist at any moment."""
        supervisor = TaskSupervisor()
        barrier = asyncio.Event()
        tick_started = asyncio.Event()
        tick_count = 0

        async def blocking_tick() -> None:
            nonlocal tick_count
            tick_count += 1
            tick_started.set()
            await barrier.wait()

        task = supervisor.register_periodic(
            "blocking", blocking_tick, interval_s=0.05, run_immediately=True
        )
        await task.start()

        # Wait for tick to be in progress
        await asyncio.wait_for(tick_started.wait(), timeout=2.0)

        # While tick is running, update the task
        update_done = asyncio.Event()

        async def do_update() -> None:
            await supervisor.update_task_spec(
                "blocking",
                tick_factory=_noop_tick,
                interval_s=0.2,
            )
            update_done.set()

        update_task = asyncio.create_task(do_update())

        # Release the blocking tick
        barrier.set()
        await asyncio.wait_for(update_task, timeout=2.0)
        await asyncio.wait_for(update_done.wait(), timeout=2.0)

        # No duplicates: exactly one task
        current = supervisor.get_task("blocking")
        assert current is not None
        assert current._interval_s == pytest.approx(0.2)

        # No other task with the same name exists
        tasks_with_name = [
            t for t in supervisor._tasks.values() if t.name == "blocking"
        ]
        assert len(tasks_with_name) == 1

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 2. Enable/disable: model_info
# ---------------------------------------------------------------------------


class TestModelInfoEnableDisable:
    def test_inventory_omits_model_info_tasks(self) -> None:
        """Model-info work is not represented by the periodic task inventory."""
        config = _make_config()
        specs = inventory_for_config(
            config,
            include_update_checker=False,  # type: ignore[arg-type]
        )
        names = {spec.name for spec in specs}

        assert "model_info_refresh" not in names
        assert "model_info_canonical_backfill" not in names

    @pytest.mark.asyncio
    async def test_model_info_disable_enable_via_supervisor(self) -> None:
        """End-to-end: register, remove via spec diff, re-add via spec diff.
        Uses explicit active/candidate specs (not inventory) to simulate
        the process_supervisor filtering in reload_manager."""
        supervisor = TaskSupervisor()

        # Register and start model_info_refresh initially
        task = supervisor.register_periodic(
            "model_info_refresh", _noop_tick, interval_s=21600.0, run_immediately=True
        )
        await task.start()
        assert task.is_running

        # Build active specs from what's registered
        active_specs = (
            _make_spec("model_info_refresh", 21600.0, run_immediately=True),
        )

        # Candidate: task disabled (not in the enabled set, like reload_manager does)
        candidate_specs: tuple[RuntimeTaskSpec, ...] = ()

        result = await apply_spec_diff(
            supervisor,
            active_specs=active_specs,
            candidate_specs=candidate_specs,
            callback_factories={},
        )
        assert "model_info_refresh" in result.removed
        assert supervisor.get_task("model_info_refresh") is None

        # Re-enable: add back
        new_candidate = (
            _make_spec("model_info_refresh", 21600.0, run_immediately=True),
        )
        result2 = await apply_spec_diff(
            supervisor,
            active_specs=(),
            candidate_specs=new_candidate,
            callback_factories={"model_info_refresh": _noop_tick},
        )
        assert "model_info_refresh" in result2.added
        assert supervisor.get_task("model_info_refresh") is not None

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 3. Enable/disable: backup
# ---------------------------------------------------------------------------


class TestBackupEnableDisable:
    @pytest.mark.asyncio
    async def test_backup_disable_removes_task(self) -> None:
        """Start with backup enabled → register task. Disable → removed.
        Re-enable with different interval → registered with new interval."""
        supervisor = TaskSupervisor()

        task = supervisor.register_periodic(
            "automatic_backup", _noop_tick, interval_s=3600.0, initial_delay_s=60.0
        )
        await task.start()
        assert task.is_running
        assert task._interval_s == pytest.approx(3600.0)

        # Disable
        active_specs = (_make_spec("automatic_backup", 3600.0, initial_delay_s=60.0),)
        result = await apply_spec_diff(
            supervisor,
            active_specs=active_specs,
            candidate_specs=(),
            callback_factories={},
        )
        assert "automatic_backup" in result.removed
        assert supervisor.get_task("automatic_backup") is None

        # Re-enable with new interval
        new_candidate = (_make_spec("automatic_backup", 7200.0, initial_delay_s=120.0),)
        result2 = await apply_spec_diff(
            supervisor,
            active_specs=(),
            candidate_specs=new_candidate,
            callback_factories={"automatic_backup": _noop_tick},
        )
        assert "automatic_backup" in result2.added
        task2 = supervisor.get_task("automatic_backup")
        assert task2 is not None
        assert task2._interval_s == pytest.approx(7200.0)

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 4. Retention policy: change retention duration live
# ---------------------------------------------------------------------------


class TestRetentionPolicyLiveChange:
    def test_retention_spec_unchanged_when_only_days_differ(self) -> None:
        """Two configs differing only in dashboard.retain_request_stats_days
        produce a spec diff where retention_cleanup is unchanged (the
        retention days are read from gen.config at tick time, not from
        the task spec)."""
        config_a = _make_config()
        config_a.dashboard.retain_request_stats_days = 30  # type: ignore[union-attr]

        config_b = _make_config()
        config_b.dashboard.retain_request_stats_days = 60  # type: ignore[union-attr]

        specs_a = inventory_for_config(
            config_a,
            include_update_checker=False,  # type: ignore[arg-type]
        )
        specs_b = inventory_for_config(
            config_b,
            include_update_checker=False,  # type: ignore[arg-type]
        )

        diff = compute_spec_diff(specs_a, specs_b)
        # Retention task is unchanged because its interval (3600s) and
        # scheduling params are the same; the retention-days config is
        # read from gen.config at tick time.
        unchanged_names = [s.name for s in diff.unchanged]
        assert "retention_cleanup" in unchanged_names

    def test_retention_config_diff_has_live_disposition(self) -> None:
        """dashboard.retain_request_stats_days is classified LIVE in the
        config reload policy, confirming it can be changed without restart."""
        from eggpool.config_reload_policy import ReloadDisposition, _disposition_for

        assert (
            _disposition_for("dashboard.retain_request_stats_days")
            is ReloadDisposition.LIVE
        )
        assert _disposition_for("dashboard.retain_event_days") is ReloadDisposition.LIVE
        assert _disposition_for("models.ping_retain_days") is ReloadDisposition.LIVE

    @pytest.mark.asyncio
    async def test_retention_supervisor_unchanged_spec(self) -> None:
        """Verify retention_cleanup stays in unchanged when spec interval matches."""
        supervisor = TaskSupervisor()

        task = supervisor.register_periodic(
            "retention_cleanup", _noop_tick, interval_s=3600.0
        )
        await task.start()
        assert task.is_running

        active_specs = (_make_spec("retention_cleanup", 3600.0),)
        candidate_specs = (_make_spec("retention_cleanup", 3600.0),)

        result = await apply_spec_diff(
            supervisor,
            active_specs=active_specs,
            candidate_specs=candidate_specs,
            callback_factories={"retention_cleanup": _noop_tick},
        )

        # Unchanged since interval didn't change
        assert "retention_cleanup" in result.unchanged

        current = supervisor.get_task("retention_cleanup")
        assert current is not None
        assert current._interval_s == pytest.approx(3600.0)

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 5. Metrics flush cadence reconfigured on reload
# ---------------------------------------------------------------------------


class TestMetricsFlushCadence:
    @pytest.mark.asyncio
    async def test_metrics_flush_interval_change(self) -> None:
        """Build a supervisor with flush_interval_s=30, apply diff with 60.
        Verify the task's _interval_s is now 60."""
        supervisor = TaskSupervisor()

        supervisor.register_periodic(
            "metrics_flush", _noop_tick, interval_s=30.0, initial_delay_s=5.0
        )
        task = supervisor.get_task("metrics_flush")
        assert task is not None
        assert task._interval_s == pytest.approx(30.0)

        active_specs = (_make_spec("metrics_flush", 30.0, initial_delay_s=5.0),)
        candidate_specs = (_make_spec("metrics_flush", 60.0, initial_delay_s=5.0),)

        result = await apply_spec_diff(
            supervisor,
            active_specs=active_specs,
            candidate_specs=candidate_specs,
            callback_factories={"metrics_flush": _noop_tick},
        )

        assert len(result.changed) == 1
        assert result.changed[0][0] == "metrics_flush"
        assert result.changed[0][1] == (30.0, 60.0)

        new_task = supervisor.get_task("metrics_flush")
        assert new_task is not None
        assert new_task._interval_s == pytest.approx(60.0)

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 6. Backup cadence changed live
# ---------------------------------------------------------------------------


class TestBackupCadenceChange:
    @pytest.mark.asyncio
    async def test_backup_cadence_change_no_duplicates(self) -> None:
        """Start with backup.interval_s=3600, change to 7200.
        Verify new params and no duplicate execution."""
        supervisor = TaskSupervisor()
        tick_count = 0

        async def counting_tick() -> None:
            nonlocal tick_count
            tick_count += 1

        supervisor.register_periodic(
            "automatic_backup",
            counting_tick,
            interval_s=3600.0,
            initial_delay_s=60.0,
        )
        task = supervisor.get_task("automatic_backup")
        assert task is not None
        assert task._interval_s == pytest.approx(3600.0)

        active_specs = (_make_spec("automatic_backup", 3600.0, initial_delay_s=60.0),)
        candidate_specs = (
            _make_spec("automatic_backup", 7200.0, initial_delay_s=120.0),
        )

        result = await apply_spec_diff(
            supervisor,
            active_specs=active_specs,
            candidate_specs=candidate_specs,
            callback_factories={"automatic_backup": counting_tick},
        )

        assert len(result.changed) == 1
        assert result.changed[0][1] == (3600.0, 7200.0)

        new_task = supervisor.get_task("automatic_backup")
        assert new_task is not None
        assert new_task._interval_s == pytest.approx(7200.0)

        # No duplicates
        all_backup = [
            t for t in supervisor._tasks.values() if t.name == "automatic_backup"
        ]
        assert len(all_backup) == 1

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 7. Rapid reloads: multiple sequential diffs converge to newest state
# ---------------------------------------------------------------------------


class TestRapidReloads:
    @pytest.mark.asyncio
    async def test_rapid_reloads_converge_to_newest(self) -> None:
        """Apply 5 diffs in rapid succession. Final task has the
        last-applied interval. No orphan tasks."""
        supervisor = TaskSupervisor()

        intervals = [10.0, 20.0, 30.0, 40.0, 50.0]
        for i, interval in enumerate(intervals):
            active_specs = (
                _make_spec("rapid_task", intervals[i - 1] if i > 0 else 10.0),
            )
            candidate_specs = (_make_spec("rapid_task", interval),)
            await apply_spec_diff(
                supervisor,
                active_specs=active_specs if i > 0 else (),
                candidate_specs=candidate_specs,
                callback_factories={"rapid_task": _noop_tick},
            )

        # Final interval is the last one
        task = supervisor.get_task("rapid_task")
        assert task is not None
        assert task._interval_s == pytest.approx(50.0)

        # Exactly one task with that name (no orphans)
        all_rapid = [t for t in supervisor._tasks.values() if t.name == "rapid_task"]
        assert len(all_rapid) == 1

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 8. Observability: ProcessRuntime task_spec_version and last_task_transition
# ---------------------------------------------------------------------------


class TestProcessRuntimeObservability:
    @pytest.mark.asyncio
    async def test_task_spec_version_increments(self) -> None:
        """process.task_spec_version increments on each apply_spec_diff."""
        from unittest.mock import MagicMock

        process = MagicMock(spec=ProcessRuntime)
        process.task_spec_version = 0
        process.last_task_transition = None

        supervisor = TaskSupervisor()

        # First apply: add a task
        candidate1 = (_make_spec("obs_task", 60.0),)
        await apply_spec_diff(
            supervisor,
            active_specs=(),
            candidate_specs=candidate1,
            callback_factories={"obs_task": _noop_tick},
            process=process,
        )
        assert process.task_spec_version == 1
        assert process.last_task_transition is not None
        assert process.last_task_transition["added"] == ("obs_task",)

        # Second apply: change interval
        active = (_make_spec("obs_task", 60.0),)
        candidate2 = (_make_spec("obs_task", 120.0),)
        await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=candidate2,
            callback_factories={"obs_task": _noop_tick},
            process=process,
        )
        assert process.task_spec_version == 2
        assert process.last_task_transition["changed"] == (("obs_task", 60.0, 120.0),)

        # Third apply: remove task
        await apply_spec_diff(
            supervisor,
            active_specs=candidate2,
            candidate_specs=(),
            callback_factories={},
            process=process,
        )
        assert process.task_spec_version == 3
        assert process.last_task_transition["removed"] == ("obs_task",)

        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_last_task_transition_fields(self) -> None:
        """Verify all fields of last_task_transition are populated."""
        from unittest.mock import MagicMock

        process = MagicMock(spec=ProcessRuntime)
        process.task_spec_version = 0
        process.last_task_transition = None

        supervisor = TaskSupervisor()

        # First apply: add t1
        await apply_spec_diff(
            supervisor,
            active_specs=(),
            candidate_specs=(_make_spec("t1", 10.0),),
            callback_factories={"t1": _noop_tick},
            process=process,
        )
        assert process.task_spec_version == 1

        # Now add t2 as well so we can test removal
        await apply_spec_diff(
            supervisor,
            active_specs=(_make_spec("t1", 10.0),),
            candidate_specs=(
                _make_spec("t1", 10.0),
                _make_spec("t2", 20.0),
            ),
            callback_factories={"t1": _noop_tick, "t2": _noop_tick},
            process=process,
        )
        assert process.task_spec_version == 2

        # Third apply: change t1, remove t2, add t3
        active = (
            _make_spec("t1", 10.0),
            _make_spec("t2", 20.0),
        )
        candidate = (
            _make_spec("t1", 15.0),  # changed
            # t2 removed
            _make_spec("t3", 30.0),  # added
        )

        await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=candidate,
            callback_factories={"t1": _noop_tick, "t3": _noop_tick},
            process=process,
        )
        assert process.task_spec_version == 3

        transition = process.last_task_transition
        assert transition is not None
        assert isinstance(transition["last_reload_monotonic"], float)
        assert transition["added"] == ("t3",)
        assert transition["removed"] == ("t2",)
        assert transition["changed"] == (("t1", 10.0, 15.0),)

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 9. Runtime metrics: task_spec_version and task_reload_summary exposed
# ---------------------------------------------------------------------------


class TestRuntimeMetricsTaskReload:
    @pytest.mark.asyncio
    async def test_snapshot_exposes_task_reload_diagnostics(self) -> None:
        """RuntimeMetricsService.snapshot() includes task_spec_version
        and task_reload_summary in the runtime_manager section."""
        from unittest.mock import MagicMock

        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.runtime_manager import RuntimeManager
        from eggpool.runtime_metrics import RuntimeMetricsService

        # Set up a minimal in-memory database for the snapshot
        db = Database(path=":memory:")
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()

            config = _make_config()

            # Create a mock process with task observability fields
            process = MagicMock(spec=ProcessRuntime)
            process.task_spec_version = 3
            process.last_task_transition = {
                "last_reload_monotonic": 12345.0,
                "added": ("new_task",),
                "removed": ("old_task",),
                "changed": (("existing", 30.0, 60.0),),
                "unchanged": ("stable_task",),
            }

            runtime_manager = RuntimeManager()

            service = RuntimeMetricsService(
                config=config,
                db=db,
                stats_db=db,
                supervisor=None,
                task_monitor=None,
                router=None,
                health_manager=None,
                started_monotonic=time.monotonic() - 100,
                started_epoch=time.time() - 100,
                runtime_manager=runtime_manager,
                process=process,
            )

            snapshot = await service.snapshot()
            rm = snapshot.get("runtime_manager")
            assert rm is not None
            assert rm["task_spec_version"] == 3
            trs = rm["task_reload_summary"]
            assert trs is not None
            assert trs["added"] == ("new_task",)
            assert trs["removed"] == ("old_task",)
            assert trs["changed"] == (("existing", 30.0, 60.0),)
            assert trs["unchanged"] == ("stable_task",)
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_snapshot_defaults_when_no_process(self) -> None:
        """When process is None, task_spec_version=0 and task_reload_summary=None."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.runtime_manager import RuntimeManager
        from eggpool.runtime_metrics import RuntimeMetricsService

        db = Database(path=":memory:")
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()

            config = _make_config()
            runtime_manager = RuntimeManager()

            service = RuntimeMetricsService(
                config=config,
                db=db,
                stats_db=db,
                supervisor=None,
                task_monitor=None,
                router=None,
                health_manager=None,
                started_monotonic=time.monotonic() - 100,
                started_epoch=time.time() - 100,
                runtime_manager=runtime_manager,
                process=None,
            )

            snapshot = await service.snapshot()
            rm = snapshot.get("runtime_manager")
            assert rm is not None
            assert rm["task_spec_version"] == 0
            assert rm["task_reload_summary"] is None
        finally:
            await db.disconnect()
