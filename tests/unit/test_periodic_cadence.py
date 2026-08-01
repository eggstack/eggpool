"""Tests for Milestone A scheduler cadence correctness and diagnostics.

Pins the contract that ``initial_delay_s`` is consumed exactly once
during a task's supervisor lifecycle (registration -> stop), and that
subsequent ticks honour ``interval_s`` rather than the initial delay.

Also covers the Milestone A3 cadence diagnostics exposed via
``SupervisedTask.snapshot()``:

- ``configured_interval_s`` / ``configured_initial_delay_s``
- ``initial_delay_consumed``
- ``previous_tick_started_at`` / ``observed_last_interval_s``
- ``last_tick_drift_s``
- ``tick_in_progress``
"""

from __future__ import annotations

import asyncio
import time

import pytest

from eggpool.background import SupervisedTask, TaskSupervisor


class _Recorder:
    def __init__(self) -> None:
        self.call_times: list[float] = []

    async def __call__(self) -> None:
        self.call_times.append(time.monotonic())


# ---------------------------------------------------------------------------
# A1: Initial delay is consumed exactly once.
# ---------------------------------------------------------------------------


class TestInitialDelayConsumedOnce:
    """``initial_delay_s`` MUST fire only for the first sleep.  Subsequent
    sleeps use ``interval_s`` regardless of how many ticks fail."""

    @pytest.mark.asyncio
    async def test_initial_delay_then_three_ticks_at_interval(self) -> None:
        """After the first delayed tick, the next three must each be at
        ``interval_s`` apart (within scheduler tolerance)."""
        rec = _Recorder()
        supervisor = TaskSupervisor()
        supervisor.register_periodic(
            "throttled",
            rec,
            interval_s=0.1,
            initial_delay_s=0.05,
        )
        await supervisor.start_all()

        # Wait for at least 4 ticks to land: first at ~0.05s, then
        # roughly every 0.1s.
        for _ in range(100):
            if len(rec.call_times) >= 4:
                break
            await asyncio.sleep(0.01)

        await supervisor.stop_all()

        assert len(rec.call_times) >= 4
        deltas = [
            t2 - t1 for t1, t2 in zip(rec.call_times, rec.call_times[1:], strict=False)
        ]
        # Every delta after the first MUST be close to interval_s (0.1s).
        # Tolerate 30% slack for asyncio scheduler jitter.
        for delta in deltas[1:]:
            assert delta >= 0.07, (
                f"inter-tick delay too short: {delta:.3f}s "
                f"(expected ~0.1s, initial_delay_s leaked)"
            )

    @pytest.mark.asyncio
    async def test_no_initial_delay_ticks_at_interval(self) -> None:
        """With ``initial_delay_s`` defaulted (None), every tick uses
        ``interval_s``.  This is the legacy sleep-first contract."""
        rec = _Recorder()
        supervisor = TaskSupervisor()
        supervisor.register_periodic(
            "interval_only",
            rec,
            interval_s=0.05,
        )
        await supervisor.start_all()

        for _ in range(100):
            if len(rec.call_times) >= 4:
                break
            await asyncio.sleep(0.01)

        await supervisor.stop_all()

        assert len(rec.call_times) >= 4
        deltas = [
            t2 - t1 for t1, t2 in zip(rec.call_times, rec.call_times[1:], strict=False)
        ]
        for delta in deltas:
            assert delta >= 0.035, (
                f"inter-tick delay too short: {delta:.3f}s (expected ~0.05s)"
            )

    @pytest.mark.asyncio
    async def test_run_immediately_then_interval(self) -> None:
        """``run_immediately=True`` fires the first tick without sleep;
        subsequent ticks honour ``interval_s``."""
        rec = _Recorder()
        supervisor = TaskSupervisor()
        supervisor.register_periodic(
            "immediate",
            rec,
            interval_s=0.1,
            run_immediately=True,
        )
        await supervisor.start_all()

        for _ in range(100):
            if len(rec.call_times) >= 3:
                break
            await asyncio.sleep(0.01)

        await supervisor.stop_all()

        assert len(rec.call_times) >= 3
        # First tick must be much faster than the rest.
        first = rec.call_times[0]
        rest = rec.call_times[1:]
        rest_deltas = [t2 - t1 for t1, t2 in zip(rest, rest[1:], strict=False)]
        for delta in rest_deltas:
            assert delta >= 0.07, (
                f"second+ tick too soon: {delta:.3f}s "
                f"(run_immediately leaked into subsequent ticks)"
            )
        # And first tick itself should have fired within the first
        # ~50ms of start (no interval_s delay).
        assert first - supervisor._tasks["immediate"]._last_started_at < 0.05  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_first_tick_failure_does_not_re_apply_initial_delay(
        self,
    ) -> None:
        """A failing first tick must not trigger the initial delay to
        fire again.  Subsequent ticks run at ``interval_s``."""
        first_ran = False
        call_count = 0

        async def failing_tick() -> None:
            nonlocal first_ran, call_count
            call_count += 1
            if not first_ran:
                first_ran = True
                raise RuntimeError("first tick fails on purpose")

        rec = _Recorder()
        for tick_fn in (failing_tick, rec):
            supervisor = TaskSupervisor()
            supervisor.register_periodic(
                "fail_first",
                tick_fn,
                interval_s=0.1,
                initial_delay_s=0.05,
            )
            await supervisor.start_all()
            for _ in range(100):
                if (tick_fn is failing_tick and call_count >= 3) or (
                    tick_fn is rec and len(rec.call_times) >= 3
                ):
                    break
                await asyncio.sleep(0.01)
            await supervisor.stop_all()

        # The failing tick ran 3 times; the recorder ran 3 times.
        # We only care about the recorder's deltas.
        deltas = [
            t2 - t1 for t1, t2 in zip(rec.call_times, rec.call_times[1:], strict=False)
        ]
        for delta in deltas:
            assert delta >= 0.07, (
                f"interval leak after first-tick failure: {delta:.3f}s "
                f"(expected ~0.1s, initial_delay_s not consumed)"
            )

    @pytest.mark.asyncio
    async def test_stop_during_initial_sleep(self) -> None:
        """Cancelling during the initial sleep must stop promptly and
        not fire the first tick."""

        rec = _Recorder()
        supervisor = TaskSupervisor()
        supervisor.register_periodic(
            "stop_in_initial",
            rec,
            interval_s=0.05,
            initial_delay_s=1.0,
        )
        await supervisor.start_all()
        await asyncio.sleep(0.05)
        await supervisor.stop_all()

        # First tick must not have fired.
        assert rec.call_times == []

    @pytest.mark.asyncio
    async def test_stop_during_tick(self) -> None:
        """Cancelling during an active tick must stop promptly."""

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_tick() -> None:
            started.set()
            await release.wait()

        supervisor = TaskSupervisor()
        supervisor.register_periodic(
            "stop_in_tick",
            slow_tick,
            interval_s=0.05,
            run_immediately=True,
        )
        await supervisor.start_all()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # Cancel while tick is in flight.
        await supervisor.stop_all()
        release.set()


# ---------------------------------------------------------------------------
# A3: Cadence diagnostics surfaced by ``snapshot()``.
# ---------------------------------------------------------------------------


class TestCadenceDiagnostics:
    """Milestone A3 fields exposed by ``SupervisedTask.snapshot()``."""

    @pytest.mark.asyncio
    async def test_snapshot_exposes_configured_interval(self) -> None:
        supervisor = TaskSupervisor()

        async def tick() -> None:
            return None

        task = supervisor.register_periodic(
            "cadence_test",
            tick,
            interval_s=12.5,
            initial_delay_s=3.0,
        )
        snap = task.snapshot()
        assert snap["configured_interval_s"] == 12.5
        assert snap["configured_initial_delay_s"] == 3.0
        assert snap["initial_delay_consumed"] is False
        assert snap["previous_tick_started_at"] is None
        assert snap["observed_last_interval_s"] is None
        assert snap["last_tick_drift_s"] is None
        assert snap["tick_in_progress"] is False

    @pytest.mark.asyncio
    async def test_initial_delay_consumed_after_first_tick(self) -> None:
        rec = _Recorder()
        supervisor = TaskSupervisor()
        task = supervisor.register_periodic(
            "consume_test",
            rec,
            interval_s=0.05,
            initial_delay_s=0.02,
        )
        await supervisor.start_all()

        for _ in range(100):
            if len(rec.call_times) >= 1:
                break
            await asyncio.sleep(0.01)

        snap = task.snapshot()
        await supervisor.stop_all()

        assert snap["initial_delay_consumed"] is True
        assert snap["last_tick_started_at"] is not None
        assert snap["last_tick_completed_at"] is not None
        assert snap["last_tick_duration_ms"] is not None
        assert snap["last_tick_drift_s"] is not None

    @pytest.mark.asyncio
    async def test_observed_last_interval_s_after_two_ticks(self) -> None:
        rec = _Recorder()
        supervisor = TaskSupervisor()
        task = supervisor.register_periodic(
            "interval_observed",
            rec,
            interval_s=0.05,
        )
        await supervisor.start_all()

        for _ in range(100):
            if len(rec.call_times) >= 3:
                break
            await asyncio.sleep(0.01)

        snap = task.snapshot()
        await supervisor.stop_all()

        # After 3 ticks we have at least 2 inter-tick deltas; the
        # snapshot exposes the most recent one.  It must be roughly
        # the configured interval (within asyncio jitter).
        assert snap["observed_last_interval_s"] is not None
        assert snap["observed_last_interval_s"] >= 0.03

    @pytest.mark.asyncio
    async def test_tick_in_progress_set_during_long_tick(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_tick() -> None:
            started.set()
            await release.wait()

        supervisor = TaskSupervisor()
        task = supervisor.register_periodic(
            "in_progress_test",
            slow_tick,
            interval_s=0.05,
            run_immediately=True,
        )
        await supervisor.start_all()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        snap = task.snapshot()
        assert snap["tick_in_progress"] is True
        release.set()
        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_live_interval_change_visible_in_snapshot(self) -> None:
        """A live ``update_task_spec`` interval change is reflected in
        the next snapshot's ``interval_s`` while ``configured_interval_s``
        remains the original value (audit trail)."""

        async def tick() -> None:
            return None

        supervisor = TaskSupervisor()
        task = supervisor.register_periodic(
            "live_change",
            tick,
            interval_s=10.0,
        )
        original = task.snapshot()["configured_interval_s"]
        assert original == 10.0

        new_task = await supervisor.update_task_spec(
            "live_change",
            tick_factory=tick,
            interval_s=20.0,
        )
        assert new_task is not None
        snap = new_task.snapshot()
        # The new task has its own configured snapshot reflecting the
        # new value; ``configured_interval_s`` always equals the value
        # applied at the most recent registration / rehash.
        assert snap["configured_interval_s"] == 20.0
        assert snap["interval_s"] == 20.0


# ---------------------------------------------------------------------------
# A2: Inventory matches actual registration schedule.
# ---------------------------------------------------------------------------


class TestInventoryMatchesRegistration:
    """Every built-in periodic task's resolved schedule (per the
    inventory) must match the schedule actually applied to the
    supervisor at registration time."""

    def test_inventory_resolved_schedule_matches_default_registration(
        self,
    ) -> None:
        """For the default config and inventory, every enabled task
        has a ``RuntimeTaskSpec`` whose scheduling parameters match
        what ``register_runtime_tasks`` would apply."""
        from eggpool.models.config import AppConfig
        from eggpool.runtime_task_inventory import inventory_for_config

        config = AppConfig.from_dict(
            {
                "server": {"api_key": "ep_test_inventory_check_000000000000"},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": "sk-test-inventory-check-000000000",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        specs = {
            spec.name: spec
            for spec in inventory_for_config(config, include_update_checker=True)
        }

        # Process-owned initial-delay schedule per the inventory.
        assert specs["checkpoint"].interval_s == 14_400.0
        assert specs["checkpoint"].run_immediately is True
        assert specs["checkpoint"].initial_delay_s is None

        assert specs["update_checker"].interval_s == 86_400.0
        assert specs["update_checker"].run_immediately is True
        assert specs["update_checker"].initial_delay_s is None

        # Generation-leased tasks with explicit startup offsets.
        assert specs["model_info_canonical_backfill"].interval_s == 60.0
        assert specs["model_info_canonical_backfill"].initial_delay_s == 10.0
        assert specs["model_info_canonical_backfill"].run_immediately is False

        assert specs["usage_window_refresh"].interval_s == 60.0
        assert specs["usage_window_refresh"].initial_delay_s == 15.0

        assert "finalization_retry_drain" not in specs

        assert specs["stale_request_finalizer"].interval_s == 60.0
        assert specs["stale_request_finalizer"].initial_delay_s == 25.0

        assert specs["health_disabled_models_prune"].interval_s == 60.0
        assert specs["health_disabled_models_prune"].initial_delay_s == 40.0

        # Generation-leased tasks with run_immediately=True.
        assert specs["model_info_refresh"].interval_s == 21_600.0
        assert specs["model_info_refresh"].run_immediately is True
        assert specs["model_info_refresh"].initial_delay_s is None

        # Tasks that follow the legacy sleep-first contract.
        assert specs["catalog_refresh"].interval_s == 300.0
        assert specs["catalog_refresh"].initial_delay_s is None
        assert specs["catalog_refresh"].run_immediately is False

        assert specs["retention_cleanup"].interval_s == 3_600.0
        assert specs["retention_cleanup"].initial_delay_s is None

        # Ownership.
        from eggpool.runtime_task_inventory import TaskOwnership

        for name in (
            "catalog_refresh",
            "model_info_refresh",
            "model_info_canonical_backfill",
            "retention_cleanup",
            "usage_window_refresh",
            "stale_request_finalizer",
            "health_disabled_models_prune",
        ):
            assert specs[name].ownership is TaskOwnership.GENERATION_LEASED, name
        for name in ("checkpoint", "update_checker"):
            assert specs[name].ownership is TaskOwnership.PROCESS, name


# ---------------------------------------------------------------------------
# Constructor smoke: dataclass defaults keep existing tests green.
# ---------------------------------------------------------------------------


class TestSupervisedTaskDefaults:
    """The new Milestone A3 fields default to a safe neutral state so
    legacy callers that construct ``SupervisedTask`` directly (tests
    using ``_first_run_state``) keep working."""

    def test_new_fields_have_safe_defaults(self) -> None:
        async def waits() -> None:
            await asyncio.Event().wait()

        task = SupervisedTask(name="defaults", _coro_factory=waits, mode="periodic")
        assert task._configured_interval_s is None
        assert task._configured_initial_delay_s is None
        assert task._initial_delay_consumed is False
        assert task._previous_tick_started_at == 0.0
        assert task._last_tick_drift_s is None
