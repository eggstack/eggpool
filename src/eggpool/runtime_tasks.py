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
    from eggpool.background import TaskSupervisor
    from eggpool.models.config import AppConfig
    from eggpool.providers.outbound import OutboundClientManager
    from eggpool.runtime_manager import ProcessRuntime, RuntimeManager


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
    async def _retention_cleanup_once() -> None:
        from eggpool.background.cleanup import (  # noqa: PLC0415
            cleanup_old_events,
            cleanup_old_requests,
            reconcile_expired_reservations,
        )
        from eggpool.db.repositories import PingRepository  # noqa: PLC0415
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        ping_repo = PingRepository(db)
        await cleanup_old_requests(db, config.dashboard.retain_request_stats_days)
        await cleanup_old_events(db, config.dashboard.retain_event_days)
        await ping_repo.cleanup_old_pings(config.models.ping_retain_days)
        async with leased_runtime(runtime_manager) as gen:
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

    supervisor.register_periodic(
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
    async def _stale_request_finalizer_once() -> None:
        from eggpool.app import finalize_stale_requests_once  # noqa: PLC0415
        from eggpool.runtime_manager import leased_runtime  # noqa: PLC0415

        async with leased_runtime(runtime_manager) as gen:
            await finalize_stale_requests_once(
                db=db,
                router=gen.router,
                quota_estimator=gen.router.quota_estimator,
                max_pending_seconds=config.upstream.read_timeout_s,
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

        supervisor.register_periodic(
            "metrics_flush",
            _metrics_flush_once,
            interval_s=float(config.metrics.flush_interval_s),
            initial_delay_s=5.0,
        )

    # ----- update checker (process-owned; only registered at startup) ---
    if context.update_checker_outbound is not None:
        _register_update_checker(
            supervisor=supervisor,
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

        supervisor.register_periodic(
            "automatic_backup",
            _automatic_backup_once,
            interval_s=float(config.backup.interval_s),
            initial_delay_s=float(config.backup.startup_delay_s),
        )


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
    "register_runtime_tasks",
]
