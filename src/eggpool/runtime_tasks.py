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
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from eggpool.background import TaskSupervisor
    from eggpool.models.config import AppConfig
    from eggpool.providers.outbound import OutboundClientManager
    from eggpool.runtime_manager import ProcessRuntime, RuntimeManager
    from eggpool.runtime_task_inventory import RuntimeTaskSpec


logger = logging.getLogger(__name__)


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
                await gen.catalog.refresh()

        supervisor.register_periodic(
            "catalog_refresh",
            _catalog_refresh_once,
            interval_s=float(config.models.refresh_interval_s),
        )

    # ----- model info refresh + backfill --------------------------------
    if config.model_info.enabled:
        if config.model_info.refresh_interval_s > 0:
            initial_model_info_refresh = True

            async def _model_info_refresh_once() -> None:
                nonlocal initial_model_info_refresh
                from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

                async with leased_runtime(runtime_manager) as gen:
                    mi = getattr(gen, "model_info", None)
                    if mi is None:
                        return
                    result = await mi.refresh_due_models(
                        force=initial_model_info_refresh
                    )
                    initial_model_info_refresh = False
                    mi.log_refresh_result(result)

            supervisor.register_periodic(
                "model_info_refresh",
                _model_info_refresh_once,
                interval_s=float(config.model_info.refresh_interval_s),
                run_immediately=True,
            )

        async def _model_info_backfill_once() -> None:
            from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

            async with leased_runtime(runtime_manager) as gen:
                mi = getattr(gen, "model_info", None)
                if mi is None:
                    return
                result = await mi.backfill_missing_canonical()
                if result.get("backfilled", 0) > 0:
                    logger.info("Model info canonical backfill: %s", result)

        supervisor.register_periodic(
            "model_info_canonical_backfill",
            _model_info_backfill_once,
            interval_s=60.0,
            initial_delay_s=10.0,
        )

    # ----- retention cleanup ---------------------------------------------
    # Reads retention values from the current generation's config on
    # each tick so config changes take effect without restart.
    async def _retention_cleanup_once() -> None:
        from eggpool.background.cleanup import (  # noqa: PLC0415
            cleanup_old_events,
            cleanup_old_requests,
            reconcile_expired_reservations,
        )
        from eggpool.db.repositories import PingRepository  # noqa: PLC0415
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            gen_config = gen.config
            ping_repo = PingRepository(db)
            await cleanup_old_requests(
                db, gen_config.dashboard.retain_request_stats_days
            )
            await cleanup_old_events(db, gen_config.dashboard.retain_event_days)
            await ping_repo.cleanup_old_pings(gen_config.models.ping_retain_days)
            await reconcile_expired_reservations(
                db,
                quota_estimator=gen.router.quota_estimator,
                router=gen.router,
            )

    supervisor.register_periodic(
        "retention_cleanup",
        _retention_cleanup_once,
        interval_s=3600.0,
    )

    # ----- checkpoint -----------------------------------------------------
    async def _checkpoint_once() -> None:
        from eggpool.background.cleanup import (  # noqa: PLC0415
            checkpoint_database,
        )

        await checkpoint_database(db)

    _process_target.register_periodic(
        "checkpoint",
        _checkpoint_once,
        interval_s=14400.0,
        run_immediately=True,
    )

    # ----- usage window refresh ------------------------------------------
    async def _refresh_usage_windows_once() -> None:
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            await gen.router.quota_estimator.load_persisted_windows()

    supervisor.register_periodic(
        "usage_window_refresh",
        _refresh_usage_windows_once,
        interval_s=60.0,
        initial_delay_s=15.0,
    )

    # ----- finalization retry drain --------------------------------------
    async def _finalization_retry_tick() -> None:
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            await gen.finalization_retry_queue.drain_once()

    supervisor.register_periodic(
        "finalization_retry_drain",
        _finalization_retry_tick,
        interval_s=15.0,
        initial_delay_s=5.0,
    )

    # ----- stale request finalizer ---------------------------------------
    # Reads upstream.read_timeout_s from the current generation's config
    # on each tick so the timeout threshold takes effect without restart.
    async def _stale_request_finalizer_once() -> None:
        from eggpool.app import finalize_stale_requests_once  # noqa: PLC0415
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            await finalize_stale_requests_once(
                db=db,
                router=gen.router,
                quota_estimator=gen.router.quota_estimator,
                max_pending_seconds=gen.config.upstream.read_timeout_s,
            )

    supervisor.register_periodic(
        "stale_request_finalizer",
        _stale_request_finalizer_once,
        interval_s=60.0,
        initial_delay_s=25.0,
    )

    # ----- health disabled-models prune ----------------------------------
    async def _health_disabled_models_prune_once() -> None:
        from eggpool.app import prune_health_disabled_models_once  # noqa: PLC0415
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            await prune_health_disabled_models_once(gen)

    supervisor.register_periodic(
        "health_disabled_models_prune",
        _health_disabled_models_prune_once,
        interval_s=60.0,
        initial_delay_s=40.0,
    )

    # ----- metrics flush (buffered modes only) ---------------------------
    if config.metrics.write_mode != "immediate":
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
    if context.update_checker_outbound is not None:
        _register_update_checker(
            supervisor=_process_target,
            outbound_manager=context.update_checker_outbound,
            app_state=getattr(context, "app_state", None),
        )

    # ----- automatic backup ----------------------------------------------
    if config.backup.enabled and config.backup.interval_s > 0:
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
        include_update_checker=context.update_checker_outbound is not None,
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
                    await gen.catalog.refresh()

            factories[name] = _catalog_refresh_factory

        elif name == "model_info_refresh":

            async def _model_info_refresh_factory() -> None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    mi = getattr(gen, "model_info", None)
                    if mi is None:
                        return
                    result = await mi.refresh_due_models(force=False)
                    mi.log_refresh_result(result)

            factories[name] = _model_info_refresh_factory

        elif name == "model_info_canonical_backfill":

            async def _model_info_backfill_factory() -> None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    mi = getattr(gen, "model_info", None)
                    if mi is None:
                        return
                    result = await mi.backfill_missing_canonical()
                    if result.get("backfilled", 0) > 0:
                        logger.info("Model info canonical backfill: %s", result)

            factories[name] = _model_info_backfill_factory

        elif name == "retention_cleanup":

            async def _retention_cleanup_factory() -> None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    gen_config = gen.config
                    ping_repo = PingRepository(db)
                    await cleanup_old_requests(
                        db, gen_config.dashboard.retain_request_stats_days
                    )
                    await cleanup_old_events(db, gen_config.dashboard.retain_event_days)
                    await ping_repo.cleanup_old_pings(
                        gen_config.models.ping_retain_days
                    )
                    await reconcile_expired_reservations(
                        db,
                        quota_estimator=gen.router.quota_estimator,
                        router=gen.router,
                    )

            factories[name] = _retention_cleanup_factory

        elif name == "checkpoint":

            async def _checkpoint_factory() -> None:
                await checkpoint_database(db)

            factories[name] = _checkpoint_factory

        elif name == "usage_window_refresh":

            async def _usage_window_refresh_factory() -> None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    await gen.router.quota_estimator.load_persisted_windows()

            factories[name] = _usage_window_refresh_factory

        elif name == "finalization_retry_drain":

            async def _finalization_retry_factory() -> None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    await gen.finalization_retry_queue.drain_once()

            factories[name] = _finalization_retry_factory

        elif name == "stale_request_finalizer":

            async def _stale_request_factory() -> None:
                from eggpool.app import (  # noqa: PLC0415
                    finalize_stale_requests_once,
                )
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    await finalize_stale_requests_once(
                        db=db,
                        router=gen.router,
                        quota_estimator=gen.router.quota_estimator,
                        max_pending_seconds=gen.config.upstream.read_timeout_s,
                    )

            factories[name] = _stale_request_factory

        elif name == "health_disabled_models_prune":

            async def _health_prune_factory() -> None:
                from eggpool.app import (  # noqa: PLC0415
                    prune_health_disabled_models_once,
                )
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    leased_runtime,
                )

                async with leased_runtime(runtime_manager) as gen:
                    await prune_health_disabled_models_once(gen)

            factories[name] = _health_prune_factory

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


__all__ = [
    "TaskRegistrationContext",
    "TaskSpecDiff",
    "TaskTransitionResult",
    "apply_spec_diff",
    "build_task_specs",
    "compute_spec_diff",
    "register_runtime_tasks",
]
