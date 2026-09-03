"""Tests for the runtime task inventory (Phase 1) and reconfiguration (Phase 2-3).

Covers:
- Task inventory completeness and consistency
- inventory_for_config enable/disable rules
- Ownership classification
- compute_spec_diff correctness
- TaskSupervisor.update_task_spec / unregister / apply_spec_diff
- Interval changes take effect on next tick
"""

from __future__ import annotations

import asyncio

import pytest

from eggpool.background import TaskSupervisor
from eggpool.runtime_task_inventory import (
    RUNTIME_TASK_INVENTORY,
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
        # Shallow-merge top-level keys.
        for k, v in overrides.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                base[k] = {**base[k], **v}  # type: ignore[index]
            else:
                base[k] = v
    return AppConfig.from_dict(base)


# ---------------------------------------------------------------------------
# Phase 1: Inventory consistency
# ---------------------------------------------------------------------------


class TestInventoryConsistency:
    def test_inventory_has_no_duplicate_names(self) -> None:
        names = [s.name for s in RUNTIME_TASK_INVENTORY]
        assert len(names) == len(set(names)), (
            f"Duplicate task names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_inventory_names_match_expected_tasks(self) -> None:
        expected = {
            "catalog_refresh",
            "retention_cleanup",
            "checkpoint",
            "metrics_flush",
            "update_checker",
            "automatic_backup",
        }
        actual = {s.name for s in RUNTIME_TASK_INVENTORY}
        assert actual == expected

    def test_every_spec_has_non_empty_callback_kind(self) -> None:
        for spec in RUNTIME_TASK_INVENTORY:
            assert spec.callback_kind, f"{spec.name} has empty callback_kind"

    def test_every_spec_has_non_empty_description(self) -> None:
        for spec in RUNTIME_TASK_INVENTORY:
            assert spec.description, f"{spec.name} has empty description"

    def test_generation_leased_tasks_have_generation_deps(self) -> None:
        for spec in RUNTIME_TASK_INVENTORY:
            if spec.ownership == TaskOwnership.GENERATION_LEASED:
                assert spec.generation_dependencies, (
                    f"{spec.name} is GENERATION_LEASED "
                    "but has no generation_dependencies"
                )

    def test_process_owned_tasks_have_process_deps(self) -> None:
        for spec in RUNTIME_TASK_INVENTORY:
            if spec.ownership == TaskOwnership.PROCESS:
                assert spec.process_dependencies, (
                    f"{spec.name} is PROCESS but has no process_dependencies"
                )


class TestOwnershipClassification:
    def test_process_owned_tasks(self) -> None:
        process_tasks = {
            s.name
            for s in RUNTIME_TASK_INVENTORY
            if s.ownership == TaskOwnership.PROCESS
        }
        expected = {"checkpoint", "metrics_flush", "update_checker", "automatic_backup"}
        assert process_tasks == expected

    def test_generation_leased_tasks(self) -> None:
        gen_tasks = {
            s.name
            for s in RUNTIME_TASK_INVENTORY
            if s.ownership == TaskOwnership.GENERATION_LEASED
        }
        expected = {
            "catalog_refresh",
            "retention_cleanup",
        }
        assert gen_tasks == expected


class TestInventoryForConfig:
    def test_model_info_disabled_does_not_add_periodic_tasks(self) -> None:
        """Disabled model-info remains dormant and has no standalone task."""
        config = _make_config()
        config.model_info.enabled = False  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert "model_info_refresh" not in by_name
        assert "model_info_canonical_backfill" not in by_name

    def test_model_info_refresh_interval_does_not_add_periodic_task(self) -> None:
        """Compatibility interval cannot resurrect a model-info task."""
        config = _make_config()
        config.model_info.refresh_interval_s = 0  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert "model_info_refresh" not in by_name

    def test_metrics_immediate_disables_flush(self) -> None:
        config = _make_config()
        config.metrics.write_mode = "immediate"  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert by_name["metrics_flush"].enabled is False

    def test_backup_disabled_disables_automatic_backup(self) -> None:
        config = _make_config()
        config.backup.enabled = False  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert by_name["automatic_backup"].enabled is False

    def test_update_checker_excluded_when_flag_false(self) -> None:
        config = _make_config()
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert by_name["update_checker"].enabled is False

    def test_update_checker_included_when_flag_true(self) -> None:
        config = _make_config()
        config.update_checker.enabled = True  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=True)
        by_name = {s.name: s for s in specs}
        assert by_name["update_checker"].enabled is True

    def test_catalog_refresh_disabled_when_interval_zero(self) -> None:
        config = _make_config()
        config.models.refresh_interval_s = 0  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert by_name["catalog_refresh"].enabled is False

    def test_metrics_flush_interval_from_config(self) -> None:
        config = _make_config()
        config.metrics.flush_interval_s = 60  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert by_name["metrics_flush"].interval_s == 60.0

    def test_backup_interval_from_config(self) -> None:
        config = _make_config()
        config.backup.interval_s = 43200  # type: ignore[union-attr]
        config.backup.startup_delay_s = 60  # type: ignore[union-attr]
        specs = inventory_for_config(config, include_update_checker=False)
        by_name = {s.name: s for s in specs}
        assert by_name["automatic_backup"].interval_s == 43200.0
        assert by_name["automatic_backup"].initial_delay_s == 60.0

    def test_preserves_canonical_ordering(self) -> None:
        config = _make_config()
        specs = inventory_for_config(config, include_update_checker=False)
        names = [s.name for s in specs]
        assert names == [s.name for s in RUNTIME_TASK_INVENTORY]


# ---------------------------------------------------------------------------
# Phase 2: compute_spec_diff
# ---------------------------------------------------------------------------


class TestComputeSpecDiff:
    def _spec(
        self, name: str, interval: float = 10.0, **overrides: object
    ) -> RuntimeTaskSpec:
        return RuntimeTaskSpec(
            name=name,
            interval_s=interval,
            initial_delay_s=overrides.get("initial_delay_s"),
            run_immediately=overrides.get("run_immediately", False),
            timeout_s=overrides.get("timeout_s"),
            ownership=TaskOwnership.GENERATION_LEASED,
            enabled=overrides.get("enabled", True),
            description=f"test {name}",
            reloadable_fields=(),
            generation_dependencies=(),
            process_dependencies=(),
            callback_kind=name,
        )

    def test_identical_specs_produce_no_diff(self) -> None:
        specs = (self._spec("a"), self._spec("b"))
        diff = compute_spec_diff(specs, specs)
        assert diff.added == ()
        assert diff.removed == ()
        assert diff.changed == ()
        assert len(diff.unchanged) == 2

    def test_added_specs(self) -> None:
        active = (self._spec("a"),)
        candidate = (self._spec("a"), self._spec("b"))
        diff = compute_spec_diff(active, candidate)
        assert len(diff.added) == 1
        assert diff.added[0].name == "b"

    def test_removed_specs(self) -> None:
        active = (self._spec("a"), self._spec("b"))
        candidate = (self._spec("a"),)
        diff = compute_spec_diff(active, candidate)
        assert len(diff.removed) == 1
        assert diff.removed[0].name == "b"

    def test_changed_interval(self) -> None:
        active = (self._spec("a", interval=10.0),)
        candidate = (self._spec("a", interval=20.0),)
        diff = compute_spec_diff(active, candidate)
        assert len(diff.changed) == 1
        old, new = diff.changed[0]
        assert old.name == "a"
        assert old.interval_s == 10.0
        assert new.interval_s == 20.0

    def test_changed_enabled(self) -> None:
        active = (self._spec("a", enabled=True),)
        candidate = (self._spec("a", enabled=False),)
        diff = compute_spec_diff(active, candidate)
        assert len(diff.changed) == 1

    def test_changed_initial_delay(self) -> None:
        active = (self._spec("a", initial_delay_s=None),)
        candidate = (self._spec("a", initial_delay_s=5.0),)
        diff = compute_spec_diff(active, candidate)
        assert len(diff.changed) == 1

    def test_mixed_diff(self) -> None:
        active = (
            self._spec("a"),
            self._spec("b", interval=10.0),
            self._spec("c"),
        )
        candidate = (
            self._spec("a"),
            self._spec("b", interval=20.0),
            self._spec("d"),
        )
        diff = compute_spec_diff(active, candidate)
        assert len(diff.added) == 1 and diff.added[0].name == "d"
        assert len(diff.removed) == 1 and diff.removed[0].name == "c"
        assert len(diff.changed) == 1 and diff.changed[0][0].name == "b"
        assert len(diff.unchanged) == 1 and diff.unchanged[0].name == "a"


# ---------------------------------------------------------------------------
# Phase 2-3: apply_spec_diff integration
# ---------------------------------------------------------------------------


class TestApplySpecDiff:
    @pytest.mark.asyncio
    async def test_added_tasks_start(self) -> None:
        supervisor = TaskSupervisor()
        active: tuple[RuntimeTaskSpec, ...] = ()

        candidate = (
            RuntimeTaskSpec(
                name="new_task",
                interval_s=60.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="new",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="new_task",
            ),
        )

        async def new_tick() -> None:
            return None

        result = await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=candidate,
            callback_factories={"new_task": new_tick},
        )

        assert "new_task" in result.added
        new_task = supervisor.get_task("new_task")
        assert new_task is not None
        assert new_task.is_running

        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_removed_tasks_stop(self) -> None:
        supervisor = TaskSupervisor()

        async def tick() -> None:
            return None

        supervisor.register_periodic("old_task", tick, interval_s=60.0)
        task = supervisor.get_task("old_task")
        assert task is not None
        await task.start()
        assert task.is_running

        active: tuple[RuntimeTaskSpec, ...] = (
            RuntimeTaskSpec(
                name="old_task",
                interval_s=60.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="old",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="old_task",
            ),
        )

        result = await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=(),
            callback_factories={},
        )

        assert "old_task" in result.removed
        assert supervisor.get_task("old_task") is None

    @pytest.mark.asyncio
    async def test_changed_task_restarts_with_new_params(self) -> None:
        supervisor = TaskSupervisor()

        async def old_tick() -> None:
            return None

        supervisor.register_periodic("task_a", old_tick, interval_s=10.0)
        task = supervisor.get_task("task_a")
        assert task is not None
        await task.start()

        active: tuple[RuntimeTaskSpec, ...] = (
            RuntimeTaskSpec(
                name="task_a",
                interval_s=10.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="old",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="task_a",
            ),
        )
        candidate: tuple[RuntimeTaskSpec, ...] = (
            RuntimeTaskSpec(
                name="task_a",
                interval_s=30.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="new",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="task_a",
            ),
        )

        async def new_tick() -> None:
            return None

        result = await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=candidate,
            callback_factories={"task_a": new_tick},
        )

        assert len(result.changed) == 1
        assert result.changed[0][0] == "task_a"
        assert result.changed[0][1] == (10.0, 30.0)

        new_task = supervisor.get_task("task_a")
        assert new_task is not None
        assert new_task._interval_s == pytest.approx(30.0)
        assert new_task.is_running

        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_unchanged_tasks_not_touched(self) -> None:
        supervisor = TaskSupervisor()

        async def tick() -> None:
            return None

        supervisor.register_periodic("stable", tick, interval_s=60.0)
        task = supervisor.get_task("stable")
        assert task is not None
        await task.start()
        original_task = task

        active: tuple[RuntimeTaskSpec, ...] = (
            RuntimeTaskSpec(
                name="stable",
                interval_s=60.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="stable",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="stable",
            ),
        )

        result = await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=active,
            callback_factories={"stable": tick},
        )

        assert "stable" in result.unchanged
        # The task object should be the same (not re-created).
        assert supervisor.get_task("stable") is original_task

        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_duplicate_name_rejected(self) -> None:
        supervisor = TaskSupervisor()

        async def tick() -> None:
            return None

        supervisor.register_periodic("dup", tick, interval_s=60.0)

        active: tuple[RuntimeTaskSpec, ...] = ()
        candidate: tuple[RuntimeTaskSpec, ...] = (
            RuntimeTaskSpec(
                name="dup",
                interval_s=60.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="dup",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="dup",
            ),
        )

        result = await apply_spec_diff(
            supervisor,
            active_specs=active,
            candidate_specs=candidate,
            callback_factories={"dup": tick},
        )

        assert "dup" in result.duplicates_rejected

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# Phase 3: update_task_spec / unregister
# ---------------------------------------------------------------------------


class TestSupervisorUpdateTaskSpec:
    @pytest.mark.asyncio
    async def test_update_task_spec_swaps_atomically(self) -> None:
        supervisor = TaskSupervisor()
        tick_count = 0

        async def old_tick() -> None:
            nonlocal tick_count
            tick_count += 1

        task = supervisor.register_periodic("swap_test", old_tick, interval_s=60.0)
        await task.start()
        assert task.is_running

        async def new_tick() -> None:
            nonlocal tick_count
            tick_count += 10

        new_task = await supervisor.update_task_spec(
            "swap_test",
            tick_factory=new_tick,
            interval_s=30.0,
        )

        assert new_task is not supervisor
        assert new_task._interval_s == pytest.approx(30.0)
        assert new_task.is_running
        # Old task should be stopped.
        assert not task.is_running

        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_unregister_removes_task(self) -> None:
        supervisor = TaskSupervisor()

        async def tick() -> None:
            return None

        supervisor.register_periodic("remove_me", tick, interval_s=60.0)
        removed = supervisor.unregister("remove_me")
        assert removed is not None
        assert supervisor.get_task("remove_me") is None

    @pytest.mark.asyncio
    async def test_unregister_returns_none_for_unknown(self) -> None:
        supervisor = TaskSupervisor()
        assert supervisor.unregister("nonexistent") is None

    @pytest.mark.asyncio
    async def test_apply_spec_diff_on_supervisor(self) -> None:
        supervisor = TaskSupervisor()

        async def tick_a() -> None:
            return None

        async def tick_b() -> None:
            return None

        supervisor.register_periodic("task_a", tick_a, interval_s=10.0)
        task_a = supervisor.get_task("task_a")
        assert task_a is not None
        await task_a.start()

        candidate = (
            RuntimeTaskSpec(
                name="task_a",
                interval_s=20.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="a",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="task_a",
            ),
            RuntimeTaskSpec(
                name="task_b",
                interval_s=30.0,
                initial_delay_s=None,
                run_immediately=False,
                timeout_s=None,
                ownership=TaskOwnership.PROCESS,
                enabled=True,
                description="b",
                reloadable_fields=(),
                generation_dependencies=(),
                process_dependencies=(),
                callback_kind="task_b",
            ),
        )

        result = await supervisor.apply_spec_diff(
            candidate,
            callback_factories={"task_a": tick_a, "task_b": tick_b},
        )

        assert "task_a" in [c[0] for c in result.changed]
        assert "task_b" in result.added
        assert supervisor.get_task("task_a") is not None
        assert supervisor.get_task("task_b") is not None

        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# Phase 3: interval changes take effect on next tick
# ---------------------------------------------------------------------------


class TestIntervalLiveUpdate:
    @pytest.mark.asyncio
    async def test_interval_change_takes_effect_on_next_tick(self) -> None:
        """After update_task_spec with a shorter interval, the next tick
        fires after the new interval, not the old one."""
        tick_count = 0
        tick_times: list[float] = []

        async def fast_tick() -> None:
            nonlocal tick_count
            tick_count += 1
            tick_times.append(asyncio.get_event_loop().time())

        supervisor = TaskSupervisor()
        supervisor.register_periodic(
            "interval_test",
            fast_tick,
            interval_s=10.0,  # very long
        )

        await supervisor.start_all()
        # Wait briefly — no tick should fire yet (interval is 10s).
        await asyncio.sleep(0.1)
        assert tick_count == 0

        # Update to a very short interval.
        await supervisor.update_task_spec(
            "interval_test",
            tick_factory=fast_tick,
            interval_s=0.05,
        )

        # Wait for at least one tick.
        for _ in range(40):
            if tick_count >= 1:
                break
            await asyncio.sleep(0.02)

        assert tick_count >= 1

        await supervisor.stop_all()
