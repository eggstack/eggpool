"""Unified runtime task registration.

The closure-pass plan calls for one authoritative function used by both
initial-startup and candidate-generation construction.  This module
owns the registration table so the two paths cannot drift apart.

Usage
-----

Initial startup (after :class:`RuntimeManager` is installed)::

    from eggpool.runtime_tasks import TaskRegistrationContext, register_runtime_tasks

    runtime_manager = RuntimeManager()
    await runtime_manager.install_initial(initial_generation)
    register_runtime_tasks(
        supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=runtime_manager,
            config=config,
            update_checker_outbound=outbound_manager,
        ),
    )
    await supervisor.start_all()

Candidate generation (reload path)::

    from eggpool.runtime_tasks import TaskRegistrationContext, register_runtime_tasks

    register_runtime_tasks(
        supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=runtime_manager,
            config=candidate_config,
        ),
    )

The two paths now share one registration table.  Tests that compare the
two outputs can rely on a single function rather than inspecting two
duplicated copies.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable

    from eggpool.background import TaskSupervisor
    from eggpool.models.config import AppConfig
    from eggpool.providers.outbound import OutboundClientManager
    from eggpool.runtime_manager import ProcessRuntime, RuntimeManager
    from eggpool.runtime_task_inventory import RuntimeTaskSpec


logger = logging.getLogger(__name__)


async def run_catalog_refresh_once(gen: Any) -> None:  # noqa: ANN401
    """Run one catalog tick against the generation that owns the lease.

    Catalog discovery is the recurring event source for model-info
    enrichment. Keep the complete one-shot lifecycle here so initial task
    registration and reload-time callback factories cannot drift apart.
    External model-info work is advisory: each follow-up step is isolated so
    a source or sidecar failure never turns a successful catalog refresh into
    a failed task.
    """
    result = await gen.catalog.refresh()
    await _clear_quarantine_on_catalog_reappearance(gen, result)

    from eggpool.app import prune_health_disabled_models_once  # noqa: PLC0415

    await prune_health_disabled_models_once(gen)

    model_info = getattr(gen, "model_info", None)
    if model_info is None:
        return

    try:
        reconciliation = await model_info.reconcile_catalog_refresh(result)
        logger.debug("Model info catalog reconciliation: %s", reconciliation)
    except Exception:
        logger.exception("Model info catalog reconciliation failed")

    try:
        backfill = await model_info.backfill_missing_canonical()
        if isinstance(backfill, dict):
            backfill_summary = cast("dict[str, object]", backfill)
            backfilled = backfill_summary.get("backfilled")
            if isinstance(backfilled, (int, float)) and backfilled > 0:
                logger.info("Model info catalog backfill: %s", backfill_summary)
    except Exception:
        logger.exception("Model info catalog backfill failed")

    try:
        refresh = await model_info.refresh_due_models(force=False)
        log_refresh_result = getattr(model_info, "log_refresh_result", None)
        if callable(log_refresh_result):
            logged = log_refresh_result(refresh)
            if inspect.isawaitable(logged):
                await logged
        else:
            logger.debug("Model info due refresh: %s", refresh)
    except Exception:
        logger.exception("Model info due refresh failed")


@dataclass(frozen=True)
class TaskRegistrationContext:
    """Inputs for :func:`register_runtime_tasks`.

    Fields
    ------

    - ``process``: process-owned dependency container (database,
      metrics coalescer, config path).
    - ``runtime_manager``: the active ``RuntimeManager``.  Required;
      callers must publish the initial generation before invoking this
      function so every tick can lease the current generation.
    - ``config``: the configuration snapshot to schedule against.  For
      candidate generations this is the validated candidate config,
      not the active generation's config.
    - ``update_checker_outbound``: optional outbound manager used to
      prime the ``UpdateChecker`` HTTP client.  None disables the
      update-checker task.  Initial startup passes the outbound
      manager; candidate reloads do not (the checker is owned by the
      process, not by a generation).
    """

    process: ProcessRuntime
    runtime_manager: RuntimeManager
    config: AppConfig
    update_checker_outbound: OutboundClientManager | None = None
    app_state: Any | None = None
    process_supervisor: TaskSupervisor | None = None
    include_process_owned: bool = True


def register_runtime_tasks(
    supervisor: TaskSupervisor,
    context: TaskRegistrationContext,
) -> None:
    """Register the runtime background-task table on ``supervisor``.

    One authoritative function used by both initial-startup and
    candidate-generation construction.  Every tick acquires a
    generation lease through ``leased_runtime()`` so the body observes
    whichever generation is currently active when the tick fires.
    """
    config = context.config
    process = context.process
    runtime_manager = context.runtime_manager

    # Process-owned tasks go on the process supervisor when provided;
    # otherwise they fall back to the gen supervisor (backward compat).
    process_supervisor = context.process_supervisor
    _process_target = (
        process_supervisor if process_supervisor is not None else supervisor
    )

    db = process.db

    # ----- catalog refresh -----------------------------------------------
    if config.models.refresh_interval_s > 0:

        async def _catalog_refresh_once() -> None:
            from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

            async with leased_runtime(runtime_manager) as gen:
                await run_catalog_refresh_once(gen)

        supervisor.register_periodic(
            "catalog_refresh",
            _catalog_refresh_once,
            interval_s=float(config.models.refresh_interval_s),
        )

    # ----- retention cleanup ---------------------------------------------
    # Reads retention values from the current generation's config on
    # each tick so config changes take effect without restart.
    async def _retention_cleanup_once() -> None:
        from eggpool.background.cleanup import (  # noqa: PLC0415
            cleanup_old_events,
            cleanup_old_model_info_observations,
            cleanup_old_operational_events,
            cleanup_old_price_snapshots,
            cleanup_old_requests,
            cleanup_old_routing_decisions,
            cleanup_old_usage_rollups,
            reconcile_expired_reservations,
        )
        from eggpool.background.maintenance import (  # noqa: PLC0415
            ContentionGuard,
            MaintenanceBudget,
        )
        from eggpool.db.repositories import PingRepository  # noqa: PLC0415
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            gen_config = gen.config
            budget = MaintenanceBudget(
                max_rows_per_batch=gen_config.maintenance.max_rows_per_batch,
                max_batches_per_tick=gen_config.maintenance.max_batches_per_tick,
                max_tick_duration_ms=gen_config.maintenance.max_tick_duration_ms,
            )
            guard = ContentionGuard(
                db,
                threshold_ms=gen_config.maintenance.contention_defer_above_lock_wait_p95_ms,
                max_deferral_age_s=gen_config.maintenance.max_deferral_age_s,
            )
            should_defer = await guard.should_defer()

            results: dict[str, object] = {}

            if should_defer:
                logger.debug("Contention guard active, deferring P1/P2 retention tasks")
                results["expired_reservations"] = await reconcile_expired_reservations(
                    db,
                    quota_estimator=gen.router.quota_estimator,
                    router=gen.router,
                    budget=budget,
                )
                return

            guard.record_success()
            ping_repo = PingRepository(db)
            results["requests"] = await cleanup_old_requests(
                db, gen_config.dashboard.retain_request_stats_days, budget=budget
            )
            results["events"] = await cleanup_old_events(
                db, gen_config.dashboard.retain_event_days, budget=budget
            )
            results["pings"] = await ping_repo.cleanup_old_pings(
                gen_config.models.ping_retain_days, budget=budget
            )
            results["operational_events"] = await cleanup_old_operational_events(
                db,
                retain_days=gen_config.metrics.operational_event_retain_days,
                budget=budget,
            )
            results["routing_decisions"] = await cleanup_old_routing_decisions(
                db,
                retain_days=gen_config.metrics.routing_decision_retain_days,
                budget=budget,
            )
            results["rollups"] = await cleanup_old_usage_rollups(
                db,
                retain_days=gen_config.metrics.rollup_retain_days,
                budget=budget,
            )
            results["price_snapshots"] = await cleanup_old_price_snapshots(
                db, budget=budget
            )
            if gen_config.model_info.enabled:
                results["model_info_obs"] = await cleanup_old_model_info_observations(
                    db, budget=budget
                )
            results["expired_reservations"] = await reconcile_expired_reservations(
                db,
                quota_estimator=gen.router.quota_estimator,
                router=gen.router,
                budget=budget,
            )

            # Record results into MaintenanceState for runtime diagnostics.
            from eggpool.background.maintenance import (  # noqa: PLC0415
                MaintenancePassResult,
            )

            state = getattr(process, "maintenance_state", None)
            if state is not None:
                state.set_contention_guard(guard)
                for task_name, r in results.items():
                    if isinstance(r, MaintenancePassResult):
                        tagged = MaintenancePassResult(
                            task_name=f"retention_{task_name}",
                            rows_scanned=r.rows_scanned,
                            rows_changed=r.rows_changed,
                            batches_completed=r.batches_completed,
                            duration_ms=r.duration_ms,
                            remaining_estimate=r.remaining_estimate,
                            stopped_reason=r.stopped_reason,
                            last_cursor=r.last_cursor,
                            error_class=r.error_class,
                            contention_deferrals=r.contention_deferrals,
                            budget_exhausted=r.budget_exhausted,
                        )
                        state.record_result(tagged)

            total_rows = sum(
                getattr(r, "rows_changed", 0) for r in results.values() if r is not None
            )
            if total_rows > 0:
                logger.info(
                    "Retention cleanup: %d total rows changed across %d tasks",
                    total_rows,
                    len(results),
                )

    supervisor.register_periodic(
        "retention_cleanup",
        _retention_cleanup_once,
        interval_s=float(config.metrics.cleanup_interval_s),
    )

    # ----- checkpoint -----------------------------------------------------
    async def _checkpoint_once() -> None:
        from eggpool.background.cleanup import checkpoint_database  # noqa: PLC0415

        result = await checkpoint_database(db)
        if result.get("contention"):
            await asyncio.sleep(60.0)
            result = await checkpoint_database(db)
        if result.get("checkpointed"):
            logger.info("WAL checkpoint: %s", result)

    if context.include_process_owned:
        _process_target.register_periodic(
            "checkpoint",
            _checkpoint_once,
            interval_s=14400.0,
            run_immediately=True,
        )

    # ----- metrics flush (buffered modes only) ---------------------------
    if context.include_process_owned and config.metrics.write_mode != "immediate":
        metrics_coalescer = process.metrics_coalescer

        async def _metrics_flush_once() -> None:
            await metrics_coalescer.flush(reason="periodic")

        _process_target.register_periodic(
            "metrics_flush",
            _metrics_flush_once,
            interval_s=float(config.metrics.flush_interval_s),
            initial_delay_s=5.0,
        )

    # ----- update checker (process-owned; only registered at startup) ---
    if (
        context.include_process_owned
        and context.update_checker_outbound is not None
        and config.update_checker.enabled
    ):
        _register_update_checker(
            supervisor=_process_target,
            outbound_manager=context.update_checker_outbound,
            app_state=getattr(context, "app_state", None),
        )

    # ----- automatic backup ----------------------------------------------
    if (
        context.include_process_owned
        and config.backup.enabled
        and config.backup.interval_s > 0
    ):
        from eggpool.background.backup import run_backup_once  # noqa: PLC0415

        raw_config_path: str | None = getattr(process, "config_path", None)
        resolved_config_path = Path(raw_config_path) if raw_config_path else None
        resolved_env_path: Path | None = None
        if resolved_config_path is not None:
            candidate_env = resolved_config_path.parent / ".env"
            if candidate_env.exists():
                resolved_env_path = candidate_env

        async def _automatic_backup_once() -> None:
            try:
                await run_backup_once(
                    config=config,
                    db=db,
                    config_path=resolved_config_path,
                    env_path=resolved_env_path,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic backup tick failed")

        _process_target.register_periodic(
            "automatic_backup",
            _automatic_backup_once,
            interval_s=float(config.backup.interval_s),
            initial_delay_s=float(config.backup.startup_delay_s),
        )


# ---------------------------------------------------------------------------
# Phase 2: Unified reconfiguration mechanism
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpecDiff:
    """Result of comparing two task-spec tuples.

    ``added``: specs in ``candidate`` but not in ``active`` (by name).
    ``removed``: specs in ``active`` but not in ``candidate`` (by name).
    ``changed``: name matches but at least one scheduling parameter differs.
    ``unchanged``: identical specs.
    """

    added: tuple[RuntimeTaskSpec, ...]
    removed: tuple[RuntimeTaskSpec, ...]
    changed: tuple[tuple[RuntimeTaskSpec, RuntimeTaskSpec], ...]
    unchanged: tuple[RuntimeTaskSpec, ...]


@dataclass(frozen=True)
class TaskTransitionResult:
    """Outcome of applying a :class:`TaskSpecDiff` to a supervisor.

    Records which tasks were added, removed, or changed, and which
    were left untouched.  ``duplicates_rejected`` captures names that
    could not be added because a task with that name already existed.
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[tuple[str, tuple[float, float]], ...]
    unchanged: tuple[str, ...]
    duplicates_rejected: tuple[str, ...]
    error: str | None = None


_SCHEDULING_KEYS = (
    "interval_s",
    "initial_delay_s",
    "run_immediately",
    "timeout_s",
    "enabled",
)


def build_task_specs(context: TaskRegistrationContext) -> tuple[RuntimeTaskSpec, ...]:
    """Build resolved task specs from the inventory for a given context.

    Reads the authoritative inventory from
    :mod:`eggpool.runtime_task_inventory`, applies config-driven
    enable/disable rules, and overlays config values onto reloadable
    scheduling parameters.  Returns the resolved spec tuple.
    """
    from eggpool.runtime_task_inventory import inventory_for_config  # noqa: PLC0415

    return inventory_for_config(
        context.config,
        include_update_checker=(
            context.include_process_owned
            and context.update_checker_outbound is not None
            and context.config.update_checker.enabled
        ),
    )


def build_callback_factories_for_specs(
    specs: tuple[RuntimeTaskSpec, ...],
    *,
    process: Any,
    runtime_manager: Any,
    config: Any,
) -> dict[str, Callable[[], Coroutine[Any, Any, None]]]:
    """Build callback factories for the given task specs.

    Returns a dict mapping task name → coroutine factory suitable for
    :meth:`~eggpool.background.TaskSupervisor.apply_spec_diff`.  Handles
    both process-owned and generation-leased task types.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    from eggpool.background.backup import run_backup_once  # noqa: PLC0415
    from eggpool.background.cleanup import (  # noqa: PLC0415
        checkpoint_database,
        cleanup_old_events,
        cleanup_old_requests,
        reconcile_expired_reservations,
    )
    from eggpool.db.repositories import PingRepository  # noqa: PLC0415

    factories: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {}
    db = process.db

    raw_config_path: str | None = getattr(process, "config_path", None)
    resolved_config_path = _Path(raw_config_path) if raw_config_path else None
    resolved_env_path: _Path | None = None
    if resolved_config_path is not None:
        candidate_env = resolved_config_path.parent / ".env"
        if candidate_env.exists():
            resolved_env_path = candidate_env

    for spec in specs:
        name = spec.name
        if name == "catalog_refresh":

            async def _catalog_refresh_factory() -> None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    await run_catalog_refresh_once(gen)

            factories[name] = _catalog_refresh_factory

        elif name == "retention_cleanup":

            async def _retention_cleanup_factory() -> None:
                from eggpool.background.cleanup import (  # noqa: PLC0415
                    cleanup_old_model_info_observations,
                    cleanup_old_operational_events,
                    cleanup_old_price_snapshots,
                    cleanup_old_routing_decisions,
                    cleanup_old_usage_rollups,
                )
                from eggpool.background.maintenance import (  # noqa: PLC0415
                    ContentionGuard,
                    MaintenanceBudget,
                    MaintenancePassResult,
                )
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    gen_config = gen.config
                    budget = MaintenanceBudget(
                        max_rows_per_batch=gen_config.maintenance.max_rows_per_batch,
                        max_batches_per_tick=gen_config.maintenance.max_batches_per_tick,
                        max_tick_duration_ms=gen_config.maintenance.max_tick_duration_ms,
                    )
                    guard = ContentionGuard(
                        db,
                        threshold_ms=gen_config.maintenance.contention_defer_above_lock_wait_p95_ms,
                        max_deferral_age_s=gen_config.maintenance.max_deferral_age_s,
                    )
                    should_defer = await guard.should_defer()

                    results: dict[str, object] = {}

                    if should_defer:
                        logger.debug(
                            "Contention guard active, deferring P1/P2 retention tasks"
                        )
                        results[
                            "expired_reservations"
                        ] = await reconcile_expired_reservations(
                            db,
                            quota_estimator=gen.router.quota_estimator,
                            router=gen.router,
                            budget=budget,
                        )
                        return

                    guard.record_success()
                    ping_repo = PingRepository(db)
                    results["requests"] = await cleanup_old_requests(
                        db,
                        gen_config.dashboard.retain_request_stats_days,
                        budget=budget,
                    )
                    results["events"] = await cleanup_old_events(
                        db, gen_config.dashboard.retain_event_days, budget=budget
                    )
                    results["pings"] = await ping_repo.cleanup_old_pings(
                        gen_config.models.ping_retain_days, budget=budget
                    )
                    results[
                        "operational_events"
                    ] = await cleanup_old_operational_events(
                        db,
                        retain_days=gen_config.metrics.operational_event_retain_days,
                        budget=budget,
                    )
                    results["routing_decisions"] = await cleanup_old_routing_decisions(
                        db,
                        retain_days=gen_config.metrics.routing_decision_retain_days,
                        budget=budget,
                    )
                    results["rollups"] = await cleanup_old_usage_rollups(
                        db,
                        retain_days=gen_config.metrics.rollup_retain_days,
                        budget=budget,
                    )
                    results["price_snapshots"] = await cleanup_old_price_snapshots(
                        db, budget=budget
                    )
                    if gen_config.model_info.enabled:
                        results[
                            "model_info_obs"
                        ] = await cleanup_old_model_info_observations(db, budget=budget)
                    results[
                        "expired_reservations"
                    ] = await reconcile_expired_reservations(
                        db,
                        quota_estimator=gen.router.quota_estimator,
                        router=gen.router,
                        budget=budget,
                    )

                    # Record results into MaintenanceState for runtime diagnostics.
                    maintenance_state = getattr(process, "maintenance_state", None)
                    if maintenance_state is not None:
                        maintenance_state.set_contention_guard(guard)
                        for task_name, r in results.items():
                            if isinstance(r, MaintenancePassResult):
                                tagged = MaintenancePassResult(
                                    task_name=f"retention_{task_name}",
                                    rows_scanned=r.rows_scanned,
                                    rows_changed=r.rows_changed,
                                    batches_completed=r.batches_completed,
                                    duration_ms=r.duration_ms,
                                    remaining_estimate=r.remaining_estimate,
                                    stopped_reason=r.stopped_reason,
                                    last_cursor=r.last_cursor,
                                    error_class=r.error_class,
                                    contention_deferrals=r.contention_deferrals,
                                    budget_exhausted=r.budget_exhausted,
                                )
                                maintenance_state.record_result(tagged)

                    total_rows = sum(
                        getattr(r, "rows_changed", 0)
                        for r in results.values()
                        if r is not None
                    )
                    if total_rows > 0:
                        logger.info(
                            "Retention cleanup: %d total rows changed across %d tasks",
                            total_rows,
                            len(results),
                        )

            factories[name] = _retention_cleanup_factory

        elif name == "checkpoint":

            async def _checkpoint_factory() -> None:
                result = await checkpoint_database(db)
                if result.get("contention"):
                    await asyncio.sleep(60.0)
                    result = await checkpoint_database(db)
                if result.get("checkpointed"):
                    logger.info("WAL checkpoint: %s", result)

            factories[name] = _checkpoint_factory

        elif name == "metrics_flush":
            coalescer = process.metrics_coalescer

            async def _metrics_flush_factory(
                _coalescer: Any = coalescer,
            ) -> None:
                await _coalescer.flush(reason="periodic")

            factories[name] = _metrics_flush_factory

        elif name == "update_checker":
            # update_checker is only reconfigured at startup, not on
            # reload.  Provide a no-op factory so the spec diff can
            # proceed without error.
            async def _update_checker_factory() -> None:
                pass

            factories[name] = _update_checker_factory

        elif name == "automatic_backup":

            async def _automatic_backup_factory() -> None:
                try:
                    await run_backup_once(
                        config=config,
                        db=db,
                        config_path=resolved_config_path,
                        env_path=resolved_env_path,
                    )
                except _asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Automatic backup tick failed")

            factories[name] = _automatic_backup_factory

    return factories


def compute_spec_diff(
    active: tuple[RuntimeTaskSpec, ...],
    candidate: tuple[RuntimeTaskSpec, ...],
) -> TaskSpecDiff:
    """Compute the diff between two task-spec tuples.

    Compares by ``name``.  A spec is ``changed`` when the name matches
    but any scheduling parameter (``interval_s``, ``initial_delay_s``,
    ``run_immediately``, ``timeout_s``, ``enabled``) differs.  Ownership
    changes are included in ``changed`` but not tracked separately in
    this phase.
    """
    active_by_name = {s.name: s for s in active}
    candidate_by_name = {s.name: s for s in candidate}

    active_names = set(active_by_name)
    candidate_names = set(candidate_by_name)

    added = tuple(candidate_by_name[n] for n in sorted(candidate_names - active_names))
    removed = tuple(active_by_name[n] for n in sorted(active_names - candidate_names))

    changed_pairs: list[tuple[RuntimeTaskSpec, RuntimeTaskSpec]] = []
    unchanged: list[RuntimeTaskSpec] = []
    for name in sorted(active_names & candidate_names):
        a = active_by_name[name]
        c = candidate_by_name[name]
        is_different = any(getattr(a, k) != getattr(c, k) for k in _SCHEDULING_KEYS)
        if is_different:
            changed_pairs.append((a, c))
        else:
            unchanged.append(a)

    return TaskSpecDiff(
        added=added,
        removed=removed,
        changed=tuple(changed_pairs),
        unchanged=tuple(unchanged),
    )


async def apply_spec_diff(
    supervisor: TaskSupervisor,
    *,
    active_specs: tuple[RuntimeTaskSpec, ...],
    candidate_specs: tuple[RuntimeTaskSpec, ...],
    callback_factories: dict[str, Callable[[], Coroutine[Any, Any, None]]],
    process: Any | None = None,  # noqa: ANN401 — ProcessRuntime, avoids circular import
) -> TaskTransitionResult:
    """Apply a spec diff to the supervisor atomically.

    For ``unchanged`` tasks: no-op.
    For ``added`` tasks: register and start.
    For ``removed`` tasks: stop and unregister.
    For ``changed`` tasks: stop the old, register with new params, start.

    ``callback_factories`` maps task name → coroutine factory for the
    tick callback.  Every added or changed task must have a factory.

    When ``process`` is a :class:`~eggpool.runtime_manager.ProcessRuntime`,
    the method increments ``process.task_spec_version`` and records
    the transition summary in ``process.last_task_transition``.

    Returns a :class:`TaskTransitionResult` with counters and outcomes.
    """
    diff = compute_spec_diff(active_specs, candidate_specs)

    added_names: list[str] = []
    removed_names: list[str] = []
    changed_details: list[tuple[str, tuple[float, float]]] = []
    unchanged_names: list[str] = [s.name for s in diff.unchanged]
    duplicates_rejected: list[str] = []

    for spec in diff.removed:
        existing = supervisor.get_task(spec.name)
        if existing is not None:
            await existing.stop()
            supervisor._tasks.pop(spec.name, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            removed_names.append(spec.name)

    for spec in diff.added:
        factory = callback_factories.get(spec.name)
        if factory is None:
            logger.warning("No callback factory for added task %r", spec.name)
            continue
        if spec.name in supervisor._tasks:  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            duplicates_rejected.append(spec.name)
            continue
        supervisor.register_periodic(
            spec.name,
            factory,
            interval_s=spec.interval_s,
            run_immediately=spec.run_immediately,
            initial_delay_s=spec.initial_delay_s,
            timeout_s=spec.timeout_s,
        )
        task = supervisor.get_task(spec.name)
        if task is not None:
            await task.start()
        added_names.append(spec.name)

    for active_spec, candidate_spec in diff.changed:
        factory = callback_factories.get(candidate_spec.name)
        if factory is None:
            logger.warning(
                "No callback factory for changed task %r", candidate_spec.name
            )
            unchanged_names.append(candidate_spec.name)
            continue
        existing = supervisor.get_task(candidate_spec.name)
        if existing is not None:
            old_interval = active_spec.interval_s
            await existing.stop()
            supervisor._tasks.pop(candidate_spec.name, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            old_interval = active_spec.interval_s
        supervisor.register_periodic(
            candidate_spec.name,
            factory,
            interval_s=candidate_spec.interval_s,
            run_immediately=candidate_spec.run_immediately,
            initial_delay_s=candidate_spec.initial_delay_s,
            timeout_s=candidate_spec.timeout_s,
        )
        task = supervisor.get_task(candidate_spec.name)
        if task is not None:
            await task.start()
        changed_details.append(
            (candidate_spec.name, (old_interval, candidate_spec.interval_s))
        )

    result = TaskTransitionResult(
        added=tuple(added_names),
        removed=tuple(removed_names),
        changed=tuple(changed_details),
        unchanged=tuple(unchanged_names),
        duplicates_rejected=tuple(duplicates_rejected),
    )

    if process is not None:
        import time as _time  # noqa: PLC0415

        process.task_spec_version += 1  # pyright: ignore[reportOptionalMemberAccess]
        process.last_task_transition = {  # pyright: ignore[reportOptionalMemberAccess]
            "last_reload_monotonic": _time.monotonic(),
            "added": tuple(added_names),
            "removed": tuple(removed_names),
            "changed": tuple(
                (name, old_int, new_int) for name, (old_int, new_int) in changed_details
            ),
            "unchanged": tuple(unchanged_names),
        }

    return result


def _register_update_checker(
    *,
    supervisor: TaskSupervisor,
    outbound_manager: OutboundClientManager,
    app_state: Any | None = None,
) -> None:
    """Register the periodic PyPI update checker as a supervised task.

    Process-owned: only registered by the initial startup path through
    :attr:`TaskRegistrationContext.update_checker_outbound`.  Candidate
    generations leave ``update_checker_outbound=None`` so the task
    table for reloads does not duplicate the checker.

    The checker instance is attached to ``app_state.update_checker``
    when provided so the dashboard can read ``checker.snapshot()``.
    """
    from eggpool.update_checker import UpdateChecker  # noqa: PLC0415

    update_checker = UpdateChecker()
    if app_state is not None:
        app_state.update_checker = update_checker

    async def _update_check_once() -> None:
        update_checker._client = await outbound_manager.get_client()  # pyright: ignore[reportPrivateUsage]
        try:
            await update_checker.check_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Update check failed: %s", exc)

    supervisor.register_periodic(
        "update_checker",
        _update_check_once,
        interval_s=float(update_checker.check_interval_s),
        run_immediately=True,
    )


async def _clear_quarantine_on_catalog_reappearance(
    gen: Any,  # noqa: ANN401
    result: Any,  # noqa: ANN401
) -> None:
    """Clear quarantine entries when a model reappears in the catalog.

    Plan 025 (Workstream D) requires that authoritative catalog
    reappearance clears bounded quarantine entries.  The catalog
    refresh returns ``new_model_ids`` (model-level) and
    ``changed_provider_keys`` (per-provider).  For each
    reappearing ``(model_id, provider_id)`` pair we resolve the
    accounts that serve that provider and call
    :meth:`EffectsApplier.clear_authoritative_reappearance` for
    each scope key.

    Best-effort: never raises.  Quarantine clearing is an
    observability/recovery concern; if it fails the catalog
    refresh still completed successfully.
    """
    try:
        from eggpool.failure import (
            EffectsApplier,
            ModelQuarantine,
        )
    except ImportError:
        return

    quarantine = getattr(gen, "model_quarantine", None)
    if not isinstance(quarantine, ModelQuarantine):
        return
    applier = getattr(gen, "effects_applier", None)
    if not isinstance(applier, EffectsApplier):
        return

    new_model_ids: frozenset[str] = getattr(result, "new_model_ids", frozenset())
    changed_provider_keys: frozenset[tuple[str, str]] = getattr(
        result, "changed_provider_keys", frozenset()
    )

    # Models that newly appear (or whose provider key changed)
    reapparition_pairs: set[tuple[str, str]] = set()
    for model_id in new_model_ids:
        for provider_id, entry_model_id in changed_provider_keys:
            if entry_model_id == model_id:
                reapparition_pairs.add((model_id, provider_id))
    for key_tuple in changed_provider_keys:
        reapparition_pairs.add(key_tuple)

    registry = getattr(gen, "registry", None)
    if registry is None:
        return

    for model_id, provider_id in reapparition_pairs:
        # AccountRegistry exposes the reverse lookup from
        # provider → enabled accounts.  Iterate and clear each
        # scope key so per-account quarantine is fully cleared.
        for account_name in _accounts_for_provider(registry, provider_id):
            protocols_fn = getattr(registry, "get_provider_protocols", None)
            try:
                protocols_value: object = (
                    cast("object", protocols_fn(provider_id)) if protocols_fn else ()
                )
            except Exception:
                protocols_value = ()
            protocols: list[str] = []
            if isinstance(protocols_value, (set, frozenset, list, tuple)):
                protocols = [
                    protocol
                    for protocol in cast("Iterable[object]", protocols_value)
                    if isinstance(protocol, str)
                ]
            for upstream_protocol in protocols or ["openai"]:
                applier.clear_authoritative_reappearance(
                    provider_id=provider_id,
                    account_id=account_name,
                    canonical_model_id=model_id,
                    upstream_model_id=model_id,
                    upstream_protocol=upstream_protocol,
                )


def _accounts_for_provider(registry: Any, provider_id: str) -> list[str]:  # noqa: ANN401
    """Return account names served by *provider_id*.

    Wraps the registry lookup so the periodic task factory remains
    usable against test doubles that do not implement
    ``get_accounts_for_provider``.
    """
    fn = getattr(registry, "get_accounts_for_provider", None)
    if fn is None:
        return []
    try:
        states = fn(provider_id)
    except Exception:
        return []
    names: list[str] = []
    for state in states or ():
        name = getattr(state, "name", None)
        if isinstance(name, str):
            names.append(name)
    return names


__all__ = [
    "TaskRegistrationContext",
    "TaskSpecDiff",
    "TaskTransitionResult",
    "apply_spec_diff",
    "build_task_specs",
    "compute_spec_diff",
    "register_runtime_tasks",
]
