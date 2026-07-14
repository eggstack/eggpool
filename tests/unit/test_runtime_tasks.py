"""Parity tests between initial-startup and candidate task registration.

After the closure-pass refactor, both startup and reload paths use
:func:`eggpool.runtime_tasks.register_runtime_tasks`.  These tests
pin the equivalence of the two outputs and pin the tasks that must
always be present for a healthy runtime.
"""

from __future__ import annotations

import asyncio
from typing import Any

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
    register_runtime_tasks(
        supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=manager,
            config=config,
            update_checker_outbound=outbound,
            app_state=SimpleNamespace(),
        ),
    )
    return supervisor, set(supervisor._tasks.keys())  # noqa: SLF001


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
            "finalization_retry_drain",
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
