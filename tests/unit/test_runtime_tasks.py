"""Parity tests between initial-startup and candidate task registration.

After the closure-pass refactor, both startup and reload paths use
:func:`eggpool.runtime_tasks.register_runtime_tasks`.  These tests
pin the equivalence of the two outputs and pin the tasks that must
always be present for a healthy runtime.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from eggpool.background import TaskSupervisor
from eggpool.config_reload_policy import ReloadDisposition
from eggpool.runtime_manager import ProcessRuntime, RuntimeManager
from eggpool.runtime_tasks import TaskRegistrationContext, register_runtime_tasks


def _make_process(db: Any) -> ProcessRuntime:
    return ProcessRuntime(
        db=db,
        stats_db=db,
        config_path=None,
        metrics_coalescer=None,
    )


def _make_config() -> Any:
    """Build a minimal valid AppConfig."""
    from eggpool.models.config import AppConfig

    return AppConfig.from_dict(
        {
            "server": {"api_key": "ep_test_server_key_1234567890"},
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "protocols": ["openai"],
                    "models_endpoint": {"method": "GET", "path": "/models"},
                    "accounts": [
                        {
                            "name": "default",
                            "api_key": "sk-test-account-key-1234567890",
                            "enabled": True,
                            "weight": 1.0,
                        }
                    ],
                }
            },
        }
    )


async def _register(
    *,
    include_update_checker: bool,
    metrics_coalescer: Any = None,
    override_config: Any = None,
    process_supervisor: bool = False,
) -> tuple[TaskSupervisor, set[str]]:
    """Build a supervisor, register the unified task table, return names."""
    from types import SimpleNamespace

    db = SimpleNamespace()
    db.execute = AsyncMockNoop
    db.fetch_all = AsyncMockNoop
    db.fetch_one = AsyncMockNoop
    process = _make_process(db)
    process.metrics_coalescer = metrics_coalescer

    manager = RuntimeManager()
    config = override_config if override_config is not None else _make_config()
    outbound = SimpleNamespace() if include_update_checker else None
    supervisor = TaskSupervisor()
    proc_super = TaskSupervisor() if process_supervisor else None
    if process_supervisor:
        process.process_supervisor = proc_super
    register_runtime_tasks(
        supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=manager,
            config=config,
            update_checker_outbound=outbound,
            app_state=SimpleNamespace(),
            process_supervisor=proc_super,
        ),
    )
    return supervisor, set(supervisor._tasks.keys())  # noqa: SLF001


async def _register_with_process_supervisor(
    *,
    include_update_checker: bool,
    metrics_coalescer: Any = None,
    override_config: Any = None,
) -> tuple[TaskSupervisor, set[str], TaskSupervisor, set[str]]:
    """Register tasks and return gen/proc supervisors with their task names."""
    from types import SimpleNamespace

    db = SimpleNamespace()
    db.execute = AsyncMockNoop
    db.fetch_all = AsyncMockNoop
    db.fetch_one = AsyncMockNoop
    process = _make_process(db)
    process.metrics_coalescer = metrics_coalescer

    manager = RuntimeManager()
    config = override_config if override_config is not None else _make_config()
    outbound = SimpleNamespace() if include_update_checker else None
    gen_supervisor = TaskSupervisor()
    proc_supervisor = TaskSupervisor()
    process.process_supervisor = proc_supervisor
    register_runtime_tasks(
        gen_supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=manager,
            config=config,
            update_checker_outbound=outbound,
            app_state=SimpleNamespace(),
            process_supervisor=proc_supervisor,
        ),
    )
    return (
        gen_supervisor,
        set(gen_supervisor._tasks.keys()),  # noqa: SLF001
        proc_supervisor,
        set(proc_supervisor._tasks.keys()),  # noqa: SLF001
    )


class AsyncMockNoop:
    async def __call__(self, *args: object, **kwargs: object) -> None:
        return None


class TestParity:
    """Both paths must produce equivalent task tables (modulo update_checker)."""

    def test_candidate_path_registers_expected_core_tasks(self) -> None:
        supervisor, names = asyncio.run(_register(include_update_checker=False))
        expected_core = {
            "catalog_refresh",
            "model_info_refresh",
            "model_info_canonical_backfill",
            "retention_cleanup",
            "checkpoint",
            "usage_window_refresh",
            "stale_request_finalizer",
            "health_disabled_models_prune",
        }
        assert expected_core.issubset(names)

    def test_startup_path_also_registers_update_checker(self) -> None:
        """Only the startup path registers the process-owned update_checker."""
        supervisor_startup, names_startup = asyncio.run(
            _register(include_update_checker=True)
        )
        supervisor_reload, names_reload = asyncio.run(
            _register(include_update_checker=False)
        )
        assert "update_checker" in names_startup
        assert "update_checker" not in names_reload

    def test_metrics_flush_omitted_when_immediate(self) -> None:
        """metrics_flush only registers when write_mode != immediate."""
        config_immediate = _make_config()
        config_immediate.metrics.write_mode = "immediate"
        supervisor, names = asyncio.run(
            _register(
                include_update_checker=False,
                metrics_coalescer=object(),
                override_config=config_immediate,
            )
        )
        assert "metrics_flush" not in names

    def test_automatic_backup_omitted_when_disabled(self) -> None:
        """automatic_backup only registers when backup.enabled and interval > 0."""
        config_disabled = _make_config()
        config_disabled.backup.enabled = False
        supervisor, names = asyncio.run(
            _register(
                include_update_checker=False,
                override_config=config_disabled,
            )
        )
        assert "automatic_backup" not in names


class TestPolicyInventoryMatchesClosurePass:
    """Sanity check: the closure-pass LIVE inventory is consistent."""

    def test_known_live_paths_are_subset_of_actual_live(self) -> None:
        from eggpool.config_reload_policy import _FIELD_DISPOSITION

        actual_live = {
            path
            for path, disp in _FIELD_DISPOSITION.items()
            if disp is ReloadDisposition.LIVE
        }
        # The closure-pass LIVE set must include the documented first
        # families (providers/accounts/routing).  If a future refactor
        # removes one of these, this guard will fail with a clear message.
        required = {
            "providers",
            "accounts",
            "model_overrides",
            "model_capabilities",
            "routing.strategy",
            "routing.fairness_mode",
        }
        assert required.issubset(actual_live), (
            f"Closure-pass LIVE inventory regressed: missing {required - actual_live}"
        )


# ---------------------------------------------------------------------------
# Phase 4: Process supervisor routing
# ---------------------------------------------------------------------------


class TestProcessSupervisorRouting:
    """When process_supervisor is provided, process-owned tasks land there."""

    def test_process_owned_tasks_register_on_process_supervisor(self) -> None:
        """Process-owned tasks (checkpoint, metrics_flush, automatic_backup,
        update_checker) register on the process supervisor, not the gen
        supervisor, when process_supervisor is provided."""
        supervisor, gen_names = asyncio.run(
            _register(
                include_update_checker=True,
                process_supervisor=True,
            )
        )
        # Process-owned tasks should NOT be on the gen supervisor.
        process_task_names = {
            "checkpoint",
            "metrics_flush",
            "update_checker",
            "automatic_backup",
        }
        # Some process tasks may be omitted (e.g. metrics_flush when
        # write_mode=immediate, automatic_backup when disabled), but
        # none should appear on the gen supervisor.
        assert not process_task_names.intersection(gen_names), (
            f"Process-owned tasks on gen supervisor: "
            f"{process_task_names.intersection(gen_names)}"
        )

    def test_gen_leased_tasks_still_register_on_gen_supervisor(self) -> None:
        """Generation-leased tasks still register on the gen supervisor."""
        supervisor, gen_names = asyncio.run(
            _register(
                include_update_checker=False,
                process_supervisor=True,
            )
        )
        expected_gen_leased = {
            "catalog_refresh",
            "model_info_refresh",
            "model_info_canonical_backfill",
            "retention_cleanup",
            "usage_window_refresh",
            "stale_request_finalizer",
            "health_disabled_models_prune",
        }
        assert expected_gen_leased.issubset(gen_names), (
            f"Missing gen-leased tasks on gen supervisor: "
            f"{expected_gen_leased - gen_names}"
        )

    def test_backward_compat_without_process_supervisor(self) -> None:
        """Without process_supervisor, all tasks register on gen supervisor."""
        supervisor, gen_names = asyncio.run(
            _register(
                include_update_checker=True,
                process_supervisor=False,
            )
        )
        # All tasks should be on the gen supervisor.
        assert "checkpoint" in gen_names
        assert "update_checker" in gen_names

    def test_process_supervisor_has_process_owned_tasks(self) -> None:
        """The process supervisor should contain process-owned tasks."""
        supervisor, _gen_names, process_supervisor, process_names = asyncio.run(
            _register_with_process_supervisor(
                include_update_checker=True,
            )
        )
        # Process-owned tasks should be on the process supervisor.
        expected_process = {"checkpoint", "update_checker"}
        # metrics_flush and automatic_backup may be omitted depending
        # on config, so only assert the ones that should always be there.
        assert expected_process.issubset(process_names), (
            f"Missing process-owned tasks on process supervisor: "
            f"{expected_process - process_names}"
        )
        # Gen supervisor should not have process-owned tasks.
        assert "checkpoint" not in set(supervisor._tasks.keys())
        assert "update_checker" not in set(supervisor._tasks.keys())


# ---------------------------------------------------------------------------
# Phase 4: Process supervisor survives reload
# ---------------------------------------------------------------------------


class TestProcessSupervisorSurvival:
    """Process-owned tasks survive generation swaps."""

    @pytest.mark.asyncio
    async def test_update_checker_survives_generation_swap(self) -> None:
        """update_checker continues running on process_supervisor after
        a candidate generation is built."""
        from types import SimpleNamespace

        from eggpool.runtime_tasks import (
            TaskRegistrationContext,
            register_runtime_tasks,
        )

        db = SimpleNamespace()
        db.execute = AsyncMockNoop
        db.fetch_all = AsyncMockNoop
        db.fetch_one = AsyncMockNoop
        process = _make_process(db)
        process.metrics_coalescer = None

        manager = RuntimeManager()
        config = _make_config()

        # Create both supervisors.
        gen_supervisor = TaskSupervisor()
        process_supervisor = TaskSupervisor()
        process.process_supervisor = process_supervisor

        # Register startup tasks.
        outbound = SimpleNamespace()
        register_runtime_tasks(
            gen_supervisor,
            TaskRegistrationContext(
                process=process,
                runtime_manager=manager,
                config=config,
                update_checker_outbound=outbound,
                app_state=SimpleNamespace(),
                process_supervisor=process_supervisor,
            ),
        )
        await process_supervisor.start_all()

        # update_checker should be on the process supervisor.
        assert process_supervisor.get_task("update_checker") is not None
        assert gen_supervisor.get_task("update_checker") is None

        # Simulate a reload: build a new gen supervisor with candidate tasks.
        # Process-owned tasks are NOT re-registered on the process supervisor
        # (they persist); only gen-leased tasks are registered on the new
        # gen supervisor.
        new_gen_supervisor = TaskSupervisor()
        new_config = _make_config()
        register_runtime_tasks(
            new_gen_supervisor,
            TaskRegistrationContext(
                process=process,
                runtime_manager=manager,
                config=new_config,
                update_checker_outbound=None,
                # process_supervisor=None means process-owned tasks register
                # on the gen supervisor (legacy path), but since we're only
                # testing survival, we skip re-registering process tasks.
            ),
        )

        # update_checker should STILL be on the original process supervisor.
        assert process_supervisor.get_task("update_checker") is not None
        # update_checker should NOT be on the new gen supervisor.
        assert new_gen_supervisor.get_task("update_checker") is None

        # Stop for cleanup.
        await process_supervisor.stop_all()
        await gen_supervisor.stop_all()
        await new_gen_supervisor.stop_all()
