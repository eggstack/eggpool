"""Task ownership inventory for runtime background tasks.

Phase 1 of Milestone D2: a reviewable, test-visible inventory that
classifies every registered background task by ownership model,
configurability, and dependency graph.  The inventory drives both
startup and candidate-generation task registration so the two paths
cannot drift apart.

Usage
-----

The inventory is consulted by :func:`eggpool.runtime_tasks.build_task_specs`
to resolve which tasks are enabled for a given configuration and to
compute the diff between active and candidate task sets during live
reload.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig


class TaskOwnership(enum.Enum):
    """Ownership model for a background task.

    ``PROCESS``: the task is bound to process-owned resources (database,
    metrics coalescer, outbound HTTP manager) and survives generation
    swaps.  Only one instance may exist at a time; reconfiguration
    mutates the existing schedule in place.

    ``GENERATION_LEASED``: the task acquires a generation lease on every
    tick and accesses generation-owned services (catalog, router, model
    info).  The task is retired when the generation it was registered
    under is retired; a new generation gets a fresh registration.
    """

    PROCESS = "process"
    GENERATION_LEASED = "generation_leased"


@dataclass(frozen=True)
class RuntimeTaskSpec:
    """Describes one registered background task.

    The spec is the single source of truth for a task's scheduling
    parameters, ownership model, and dependency graph.  Both startup
    and candidate-generation paths build a ``tuple[RuntimeTaskSpec, ...]``
    from the inventory; the diff between two spec tuples drives
    :func:`eggpool.background.TaskSupervisor.apply_spec_diff`.

    Fields
    ------

    - ``name``: unique task name (matches ``SupervisedTask.name``).
    - ``interval_s``: seconds between ticks.
    - ``initial_delay_s``: optional override for the first-tick delay.
    - ``run_immediately``: when ``True``, the first tick fires immediately.
    - ``timeout_s``: optional per-tick timeout.
    - ``ownership``: ``PROCESS`` or ``GENERATION_LEASED``.
    - ``enabled``: ``True`` when the task should be registered; ``False``
      when disabled by configuration (e.g. ``model_info.enabled = false``).
    - ``description``: human-readable summary for operator dashboards.
    - ``reloadable_fields``: config paths that affect this task's schedule
      or enabled state.
    - ``generation_dependencies``: generation-owned service names accessed
      via the lease (e.g. ``catalog``, ``router``).
    - ``process_dependencies``: process-owned resources accessed directly
      (e.g. ``db``, ``metrics_coalescer``).
    - ``callback_kind``: identifier for the callback function family.
    """

    name: str
    interval_s: float
    initial_delay_s: float | None
    run_immediately: bool
    timeout_s: float | None
    ownership: TaskOwnership
    enabled: bool
    description: str
    reloadable_fields: tuple[str, ...]
    generation_dependencies: tuple[str, ...]
    process_dependencies: tuple[str, ...]
    callback_kind: str


# ---------------------------------------------------------------------------
# Authoritative task inventory.
#
# Every task registered by :func:`eggpool.runtime_tasks.register_runtime_tasks`
# must appear exactly once in this tuple.  The ``enabled`` field reflects
# the *default* state; :func:`inventory_for_config` applies config-based
# overrides at runtime.
# ---------------------------------------------------------------------------

RUNTIME_TASK_INVENTORY: tuple[RuntimeTaskSpec, ...] = (
    RuntimeTaskSpec(
        name="catalog_refresh",
        interval_s=300.0,
        initial_delay_s=None,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.GENERATION_LEASED,
        enabled=True,
        description="Periodically refresh the model catalog from enabled accounts",
        reloadable_fields=("models.refresh_interval_s",),
        generation_dependencies=("catalog",),
        process_dependencies=("db",),
        callback_kind="catalog_refresh",
    ),
    RuntimeTaskSpec(
        name="model_info_refresh",
        interval_s=21_600.0,
        initial_delay_s=None,
        run_immediately=True,
        timeout_s=None,
        ownership=TaskOwnership.GENERATION_LEASED,
        enabled=True,
        description="Periodically refresh model-info from external sources",
        reloadable_fields=("model_info.enabled", "model_info.refresh_interval_s"),
        generation_dependencies=("model_info",),
        process_dependencies=("db",),
        callback_kind="model_info_refresh",
    ),
    RuntimeTaskSpec(
        name="model_info_canonical_backfill",
        interval_s=60.0,
        initial_delay_s=10.0,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.GENERATION_LEASED,
        enabled=True,
        description="Backfill missing canonical model-info rows",
        reloadable_fields=("model_info.enabled",),
        generation_dependencies=("model_info",),
        process_dependencies=("db",),
        callback_kind="model_info_canonical_backfill",
    ),
    RuntimeTaskSpec(
        name="retention_cleanup",
        interval_s=3600.0,
        initial_delay_s=None,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.GENERATION_LEASED,
        enabled=True,
        description="Clean up old requests, events, pings, and expired reservations",
        reloadable_fields=(
            "dashboard.retain_request_stats_days",
            "dashboard.retain_event_days",
            "models.ping_retain_days",
        ),
        generation_dependencies=("router",),
        process_dependencies=("db",),
        callback_kind="retention_cleanup",
    ),
    RuntimeTaskSpec(
        name="checkpoint",
        interval_s=14400.0,
        initial_delay_s=None,
        run_immediately=True,
        timeout_s=None,
        ownership=TaskOwnership.PROCESS,
        enabled=True,
        description="Periodic SQLite WAL checkpoint",
        reloadable_fields=(),
        generation_dependencies=(),
        process_dependencies=("db",),
        callback_kind="checkpoint",
    ),
    RuntimeTaskSpec(
        name="usage_window_refresh",
        interval_s=60.0,
        initial_delay_s=15.0,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.GENERATION_LEASED,
        enabled=True,
        description="Reload persisted usage windows into the quota estimator",
        reloadable_fields=(),
        generation_dependencies=("router",),
        process_dependencies=(),
        callback_kind="usage_window_refresh",
    ),
    RuntimeTaskSpec(
        name="health_disabled_models_prune",
        interval_s=60.0,
        initial_delay_s=40.0,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.GENERATION_LEASED,
        enabled=True,
        description="Prune stale disabled-model entries from the health manager",
        reloadable_fields=(),
        generation_dependencies=("health_manager",),
        process_dependencies=(),
        callback_kind="health_disabled_models_prune",
    ),
    RuntimeTaskSpec(
        name="metrics_flush",
        interval_s=30.0,
        initial_delay_s=5.0,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.PROCESS,
        enabled=True,
        description="Flush buffered metrics analytics to SQLite",
        reloadable_fields=("metrics.write_mode", "metrics.flush_interval_s"),
        generation_dependencies=(),
        process_dependencies=("metrics_coalescer",),
        callback_kind="metrics_flush",
    ),
    RuntimeTaskSpec(
        name="update_checker",
        interval_s=86_400.0,
        initial_delay_s=None,
        run_immediately=True,
        timeout_s=None,
        ownership=TaskOwnership.PROCESS,
        enabled=True,
        description="Periodically check PyPI for new EggPool releases",
        reloadable_fields=(),
        generation_dependencies=(),
        process_dependencies=("outbound_manager",),
        callback_kind="update_checker",
    ),
    RuntimeTaskSpec(
        name="automatic_backup",
        interval_s=86_400.0,
        initial_delay_s=300.0,
        run_immediately=False,
        timeout_s=None,
        ownership=TaskOwnership.PROCESS,
        enabled=True,
        description="Create periodic backup archives of config and database",
        reloadable_fields=(
            "backup.enabled",
            "backup.interval_s",
            "backup.startup_delay_s",
            "backup.retain_count",
        ),
        generation_dependencies=(),
        process_dependencies=("db",),
        callback_kind="automatic_backup",
    ),
)

# Quick lookup by name for tests and runtime.
_INVENTORY_BY_NAME: dict[str, RuntimeTaskSpec] = {
    spec.name: spec for spec in RUNTIME_TASK_INVENTORY
}


def inventory_for_config(
    config: AppConfig,
    *,
    include_update_checker: bool = True,
) -> tuple[RuntimeTaskSpec, ...]:
    """Return the task inventory with config-driven enabled/disabled flags.

    Applies enable/disable rules from the live config:
    - ``model_info.enabled`` gates ``model_info_refresh`` and
      ``model_info_canonical_backfill``.
    - ``backup.enabled`` gates ``automatic_backup``.
    - ``metrics.write_mode == "immediate"`` gates ``metrics_flush``.
    - ``include_update_checker`` gates ``update_checker`` (only the
      startup path passes ``True``).
    - ``models.refresh_interval_s == 0`` disables ``catalog_refresh``.

    The returned tuple preserves the canonical ordering of
    :data:`RUNTIME_TASK_INVENTORY`.
    """
    result: list[RuntimeTaskSpec] = []
    for spec in RUNTIME_TASK_INVENTORY:
        enabled = spec.enabled

        if spec.name == "catalog_refresh" and config.models.refresh_interval_s <= 0:
            enabled = False

        if (
            spec.name in ("model_info_refresh", "model_info_canonical_backfill")
            and not config.model_info.enabled
        ):
            enabled = False

        if (
            spec.name == "model_info_refresh"
            and config.model_info.refresh_interval_s <= 0
        ):
            enabled = False

        if spec.name == "metrics_flush" and config.metrics.write_mode == "immediate":
            enabled = False

        if spec.name == "update_checker" and not include_update_checker:
            enabled = False

        if spec.name == "automatic_backup" and (
            not config.backup.enabled or config.backup.interval_s <= 0
        ):
            enabled = False

        # Override interval from config when the field is reloadable.
        interval_s = spec.interval_s
        initial_delay_s = spec.initial_delay_s
        if spec.name == "catalog_refresh":
            interval_s = float(config.models.refresh_interval_s)
        elif spec.name == "model_info_refresh":
            interval_s = float(config.model_info.refresh_interval_s)
        elif spec.name == "metrics_flush":
            interval_s = float(config.metrics.flush_interval_s)
        elif spec.name == "automatic_backup":
            interval_s = float(config.backup.interval_s)
            initial_delay_s = float(config.backup.startup_delay_s)

        if (
            enabled != spec.enabled
            or interval_s != spec.interval_s
            or initial_delay_s != spec.initial_delay_s
        ):
            from dataclasses import replace as _replace

            spec = _replace(
                spec,
                enabled=enabled,
                interval_s=interval_s,
                initial_delay_s=initial_delay_s,
            )
        result.append(spec)

    return tuple(result)


__all__ = [
    "RUNTIME_TASK_INVENTORY",
    "RuntimeTaskSpec",
    "TaskOwnership",
    "inventory_for_config",
]
