"""Shared runtime-generation factory (Phase 5).

Eliminates behavior drift between startup-created and reload-created
runtimes by extracting one authoritative production factory for the
complete generation-owned service graph.

Startup and reload both call :meth:`RuntimeGenerationFactory.prepare`
to construct the full generation-owned service graph.  The factory
accepts process-owned dependencies as inputs, constructs all
generation-owned services, and returns a publication-ready result.

Startup-only operations (database migrations, crash recovery, initial
catalog refresh, process-worker startup) remain outside the factory.

"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eggpool.constants import DEFAULT_PROVIDER_ID
from eggpool.errors import (
    ModelQuarantineHydrationError,
    ModelQuarantineRecoveryError,
)

if TYPE_CHECKING:
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.background import TaskSupervisor
    from eggpool.catalog.pricing import CostCalculator
    from eggpool.catalog.service import CatalogService
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig
    from eggpool.providers.client_pool import ProviderClientPool
    from eggpool.providers.outbound import OutboundClientManager
    from eggpool.request.coordinator import RequestCoordinator
    from eggpool.request.finalization_job import RequestFinalizationSupervisor
    from eggpool.request.routing_trace_guard import RoutingTraceGuard
    from eggpool.routing.router import Router
    from eggpool.runtime_dispatch import (
        DispatchOverheadRecorder,
        DispatchSpanRecorder,
    )
    from eggpool.runtime_manager import (
        ProcessRuntime,
        RuntimeGeneration,
        RuntimeGenerationCandidate,
        RuntimeManager,
    )
    from eggpool.stats import StatsService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factory result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedRuntimeGeneration:
    """Complete, publication-ready generation output from the factory.

    Contains all generation-owned services and the immutable
    :class:`RuntimeGeneration` snapshot.  The caller is responsible for
    installation/publishing via :class:`RuntimeManager`.
    """

    generation: RuntimeGeneration
    registry: AccountRegistry
    catalog: CatalogService
    router: Router
    coordinator: RequestCoordinator
    client_pool: ProviderClientPool
    outbound_manager: OutboundClientManager | None
    health_manager: HealthManager
    cost_calculator: CostCalculator
    transcoder_policy: Any
    dispatch_overhead_recorder: DispatchOverheadRecorder
    dispatch_span_recorder: DispatchSpanRecorder | None
    account_backoff_repo: Any
    stats_service: StatsService
    supervisor: TaskSupervisor
    routing_trace_guard: RoutingTraceGuard | None
    routing_trace_writer: Any
    local_pre_upstream_recorder: Any = None
    stream_diagnostics: Any = None
    finalization_supervisor: RequestFinalizationSupervisor | None = None
    model_info: Any = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class RuntimeGenerationFactory:
    """Single authoritative factory for the generation-owned service graph.

    Both startup and reload call :meth:`prepare` to construct
    generation-owned services.  The factory:

    - accepts process-owned dependencies as explicit inputs;
    - constructs all generation-owned services with identical wiring;
    - registers closeable resources on the candidate (reload case);
    - hydrates persisted health/backoff state before publication;
    - returns a complete, publication-ready result.

    The factory does **not** perform startup-only operations:
    database migrations, crash recovery, initial catalog refresh,
    process-worker startup, or control-socket setup.

    """

    async def prepare(
        self,
        *,
        config: AppConfig,
        config_digest: str,
        generation_id: int,
        process: ProcessRuntime,
        candidate: RuntimeGenerationCandidate | None = None,
        runtime_manager: RuntimeManager | None = None,
    ) -> PreparedRuntimeGeneration:
        """Construct the complete generation-owned service graph.

        Parameters
        ----------
        config:
            Validated configuration for this generation.
        config_digest:
            SHA-256 digest of the validated config bytes.
        generation_id:
            Monotonic generation identifier.
        process:
            Process-owned dependency container.
        candidate:
            Optional candidate owner for reload.  When provided,
            closeable resources are registered on the candidate
            for abort-on-failure cleanup.
        runtime_manager:
            Optional runtime manager for task registration.

        Returns
        -------
        PreparedRuntimeGeneration
            All generation-owned services ready for publication.
        """
        from eggpool.accounts.registry import AccountRegistry  # noqa: PLC0415
        from eggpool.background import TaskSupervisor  # noqa: PLC0415
        from eggpool.catalog.pricing import (  # noqa: PLC0415
            CostCalculator,
            PriceRepository,
        )
        from eggpool.catalog.service import CatalogService  # noqa: PLC0415
        from eggpool.db.repositories import (  # noqa: PLC0415
            AccountBackoffRepository,
            AttemptRepository,
            PingRepository,
            RequestRepository,
            ReservationRepository,
            UsageWindowRepository,
        )
        from eggpool.health.health_manager import HealthManager  # noqa: PLC0415
        from eggpool.providers.client_pool import ProviderClientPool  # noqa: PLC0415
        from eggpool.providers.outbound import OutboundClientManager  # noqa: PLC0415
        from eggpool.request.coordinator import RequestCoordinator  # noqa: PLC0415
        from eggpool.request.stream_diagnostics import (
            get_stream_diagnostics,  # noqa: PLC0415
        )
        from eggpool.runtime_dispatch import (  # noqa: PLC0415
            DispatchOverheadRecorder,
            DispatchSpanRecorder,
            LocalPreUpstreamRecorder,
        )
        from eggpool.runtime_manager import RuntimeGenerationBuilder  # noqa: PLC0415
        from eggpool.stats import StatsService  # noqa: PLC0415

        db = process.db
        config.validate_optional_dependencies()
        _register = candidate.register_resource if candidate is not None else None

        # -- Client pool (generation-owned) --------------------------------
        client_pool = ProviderClientPool.from_app_config(config)
        if _register is not None:
            _register("client_pool", client_pool.close)

        # -- Outbound client manager (generation-owned) --------------------
        pricing_catalogs = config.pricing.catalogs
        external_pricing_enabled = any(
            entry.enabled
            for entry in (
                pricing_catalogs.openrouter,
                pricing_catalogs.opencode_zen,
            )
        )
        needs_outbound_manager = (
            config.model_info.enabled
            or external_pricing_enabled
            or config.update_checker.enabled
        )
        outbound_manager = None
        outbound_client = None
        if needs_outbound_manager:
            outbound_manager = OutboundClientManager(
                config=config.network,
            )
            if _register is not None:
                _register("outbound_manager", outbound_manager.aclose)
            if config.model_info.enabled or external_pricing_enabled:
                outbound_client = await outbound_manager.get_client()

        # -- Account registry (generation-owned) ---------------------------
        registry = AccountRegistry(config)
        account_identities = await _load_account_identities(db, registry)

        # -- Transcoder policy snapshot -----------------------------------
        transcoder_policy = config.transcoder

        # -- Health manager (generation-owned) -----------------------------
        health_manager = HealthManager()

        # -- Persistent backoff repository ----------------------------------
        account_backoff_repo = AccountBackoffRepository(db)

        # -- Hydrate persisted backoffs into health manager -----------------
        await _hydrate_health_from_backoffs(account_backoff_repo, health_manager)

        # -- Plan 025: bounded model quarantine ------------------------------
        # The quarantine state machine is generation-owned and survives the
        # lifetime of the active generation.  Durable state is read from
        # ``model_quarantine`` table on hydration and written back when
        # effects are applied.
        from eggpool.db.repositories import ModelQuarantineRepository
        from eggpool.failure import EffectsApplier, ModelQuarantine

        model_quarantine_repo = ModelQuarantineRepository(db)
        quarantine = ModelQuarantine()
        await _hydrate_model_quarantine(model_quarantine_repo, quarantine)

        # -- Catalog service -----------------------------------------------
        ping_repo = PingRepository(db)
        catalog = CatalogService(
            config,
            registry,
            db,
            client_pool,
            ping_repo=ping_repo,
            outbound_client=outbound_client,
        )
        if external_pricing_enabled or pricing_catalogs.aliases:
            await catalog.attach_pricing_resolvers()
        await catalog._load_cached_models()  # pyright: ignore[reportPrivateUsage]

        # -- Optional model-info service (generation-owned) ---------------
        model_info = None
        if config.model_info.enabled:
            from eggpool.model_info.service import ModelInfoService  # noqa: PLC0415

            model_info = ModelInfoService(
                config=config.model_info,
                db=db,
                catalog=catalog.cache,
                outbound_client=outbound_client,
            )
            await model_info.load_cache()

        # -- Cost calculator ------------------------------------------------
        price_repo = PriceRepository(db)
        cost_calculator = CostCalculator(price_repo)
        catalog.set_price_change_callback(cost_calculator.invalidate_price)

        # -- Router ---------------------------------------------------------
        router = self._build_router(
            config,
            registry,
            catalog,
            health_manager,
            quarantine=quarantine,
        )

        # -- Load model price overrides into estimator ----------------------
        self._load_model_price_overrides(config, router)

        # -- Load persisted usage windows and account config ----------------
        usage_window_repo = UsageWindowRepository(db)
        await self._load_usage_windows(config, router, usage_window_repo)

        # -- Repositories (generation-owned) -------------------------------
        request_repo = RequestRepository(db)
        reservation_repo = ReservationRepository(db)
        attempt_repo = AttemptRepository(db)

        # -- Dispatch recorders ---------------------------------------------
        dispatch_overhead_recorder = DispatchOverheadRecorder(window_size=100)
        # The deprecated field is an override only when explicitly present.
        # Zero sampling avoids constructing the detailed recorder entirely.
        span_sample_rate = config.metrics.dispatch_spans.sample_rate
        if config.metrics.detailed_span_sample_rate is not None:
            span_sample_rate = config.metrics.detailed_span_sample_rate
        dispatch_span_recorder = (
            DispatchSpanRecorder(
                window_size=config.metrics.dispatch_spans.window_size,
                detailed_span_sample_rate=span_sample_rate,
            )
            if span_sample_rate > 0
            else None
        )

        # -- Local pre-upstream recorder ------------------------------------
        local_pre_upstream_recorder = (
            LocalPreUpstreamRecorder(window_size=100) if span_sample_rate > 0 else None
        )

        # -- Stream diagnostics (process-wide singleton) --------------------
        stream_diagnostics = get_stream_diagnostics()

        # -- Plan 025: typed failure effects applier ---------------------
        # Built here so the coordinator's :func:`_apply_health_transition`
        # can route through the typed classifier.  The applier captures
        # the same health_manager/quarantine/catalog cache the
        # coordinator uses for the legacy ``classify_failure_category``
        # path; it never falls back to silent category reclassification.
        effects_applier = EffectsApplier(
            health_manager=health_manager,
            quarantine=quarantine,
            catalog_cache=catalog.cache,
        )
        from eggpool.db.repositories import AccountRepository

        async def _clear_model_reappearance(
            account_name: str,
            provider_id: str,
            models: list[dict[str, Any]],
        ) -> None:
            """Clear exact model quarantine after authoritative reappearance."""
            await _clear_model_reappearance_durable_first(
                account_name=account_name,
                provider_id=provider_id,
                models=models,
                model_quarantine_repo=model_quarantine_repo,
                effects_applier=effects_applier,
                account_backoff_repo=account_backoff_repo,
                account_repo=AccountRepository(db),
            )

        catalog.set_model_reappearance_callback(_clear_model_reappearance)

        # -- Generation-owned finalization supervisor ----------------------
        # Retained jobs hold a retirement reference on this generation
        # until durable and runtime convergence completes.
        from eggpool.request.finalization_job import (  # noqa: PLC0415
            RequestFinalizationSupervisor,
        )

        finalization_supervisor = RequestFinalizationSupervisor(
            db=db,
            effects_applier=effects_applier,
        )
        if _register is not None:
            _register("finalization_supervisor", finalization_supervisor.shutdown)

        # -- Request coordinator --------------------------------------------
        coordinator = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=db,
            client_pool=client_pool,
            request_repo=request_repo,
            reservation_repo=reservation_repo,
            attempt_repo=attempt_repo,
            usage_window_repo=usage_window_repo,
            health_manager=health_manager,
            cost_calculator=cost_calculator,
            quota_estimator=router.quota_estimator,
            max_retry_attempts=1 + config.routing.max_retries_before_stream,
            quota_exhausted_cooldown_seconds=(
                config.routing.quota_exhausted_cooldown_seconds
            ),
            persist_error_detail=config.security.persist_redacted_error_detail,
            config=config,
            account_backoff_repo=account_backoff_repo,
            metrics_coalescer=process.metrics_coalescer,
            dispatch_overhead_recorder=dispatch_overhead_recorder,
            local_pre_upstream_recorder=local_pre_upstream_recorder,
            dispatch_span_recorder=dispatch_span_recorder,
            transcoder_policy=transcoder_policy,
            stream_diagnostics=stream_diagnostics,
            routing_trace_enabled=(
                config.routing.trace.mode != "off"
                and (
                    config.routing.trace.mode == "all"
                    or config.routing.trace.sample_rate > 0
                )
            ),
            effects_applier=effects_applier,
            quarantine=quarantine,
            account_identities=account_identities,
            finalization_supervisor=finalization_supervisor,
        )

        # -- Routing trace guard --------------------------------------------
        # The guard is generation-owned. Constructing and configuring a new
        # guard during candidate preparation cannot mutate the active
        # generation or leak state across a reload.
        from eggpool.request.routing_trace_guard import (  # noqa: PLC0415
            RoutingTraceGuard,
        )

        routing_trace_guard = (
            RoutingTraceGuard(
                threshold_ms=(config.routing.trace.skip_above_lock_wait_p95_ms),
                queue_occupancy_threshold=(
                    config.routing.trace.guard_queue_occupancy_threshold
                ),
                oldest_event_age_s=config.routing.trace.guard_oldest_event_age_s,
                cooldown_s=config.routing.trace.guard_cooldown_s,
            )
            if config.routing.trace.mode != "off"
            and (
                config.routing.trace.mode == "all"
                or config.routing.trace.sample_rate > 0
            )
            else None
        )
        coordinator._routing_trace_guard = (  # pyright: ignore[reportPrivateUsage]
            routing_trace_guard
        )

        # -- Routing trace writer (NOT configured during preparation) -------
        # Configuration is deferred to RoutingTraceWriterTransition at commit
        # time so candidate preparation has no process-owned side effects.
        routing_trace_writer = getattr(process, "routing_trace_writer", None)
        coordinator._routing_trace_writer = (  # pyright: ignore[reportPrivateUsage]
            routing_trace_writer
        )

        # -- Stats service (generation-owned) ------------------------------
        stats_db = process.stats_db
        stats_account_backoff_repo = (
            account_backoff_repo
            if stats_db is db
            else AccountBackoffRepository(stats_db)
        )
        from eggpool.db.rollup_repository import (  # noqa: PLC0415
            UsageRollupRepository,
        )

        stats_rollup_repo = UsageRollupRepository(stats_db)
        stats_service = StatsService(
            stats_db,
            health_manager=health_manager,
            ping_repo=PingRepository(stats_db),
            account_backoff_repo=stats_account_backoff_repo,
            rollup_repo=stats_rollup_repo,
        )

        # -- Task supervisor ------------------------------------------------
        supervisor = TaskSupervisor()
        if _register is not None:
            _register("supervisor", supervisor.stop_all)

        # Register background tasks if runtime_manager is provided
        if runtime_manager is not None:
            from eggpool.app import register_candidate_tasks  # noqa: PLC0415

            register_candidate_tasks(
                supervisor,
                config,
                process,
                runtime_manager,
            )

        # -- Assemble generation via builder --------------------------------
        builder = RuntimeGenerationBuilder()
        build_result = await builder.build_initial(
            config,
            process,
            generation_id=generation_id,
            config_digest=config_digest,
            registry=registry,
            catalog=catalog,
            router=router,
            coordinator=coordinator,
            client_pool=client_pool,
            outbound_manager=outbound_manager,
            health_manager=health_manager,
            cost_calculator=cost_calculator,
            transcoder_policy=transcoder_policy,
            dispatch_overhead_recorder=dispatch_overhead_recorder,
            dispatch_span_recorder=dispatch_span_recorder,
            account_backoff_repo=account_backoff_repo,
            stats_service=stats_service,
            supervisor=supervisor,
            routing_trace_guard=routing_trace_guard,
            routing_trace_writer=routing_trace_writer,
            effects_applier=effects_applier,
            model_quarantine=quarantine,
            finalization_supervisor=finalization_supervisor,
            model_info=model_info,
            local_pre_upstream_recorder=local_pre_upstream_recorder,
            stream_diagnostics=stream_diagnostics,
        )

        return PreparedRuntimeGeneration(
            generation=build_result.generation,
            registry=registry,
            catalog=catalog,
            router=router,
            coordinator=coordinator,
            client_pool=client_pool,
            outbound_manager=outbound_manager,
            health_manager=health_manager,
            cost_calculator=cost_calculator,
            transcoder_policy=transcoder_policy,
            dispatch_overhead_recorder=dispatch_overhead_recorder,
            dispatch_span_recorder=dispatch_span_recorder,
            account_backoff_repo=account_backoff_repo,
            stats_service=stats_service,
            supervisor=supervisor,
            routing_trace_guard=routing_trace_guard,
            routing_trace_writer=routing_trace_writer,
            local_pre_upstream_recorder=local_pre_upstream_recorder,
            stream_diagnostics=stream_diagnostics,
            finalization_supervisor=finalization_supervisor,
            model_info=model_info,
        )

    # -- Internal helpers ---------------------------------------------------

    def _build_router(
        self,
        config: AppConfig,
        registry: AccountRegistry,
        catalog: CatalogService,
        health_manager: HealthManager,
        quarantine: Any | None = None,
    ) -> Router:
        """Construct and configure a Router from config."""
        from eggpool.routing.config import routing_stale_after_s  # noqa: PLC0415
        from eggpool.routing.router import Router  # noqa: PLC0415

        def _schedule_missing_account_recovery(account_name: str) -> None:
            async def _run() -> None:
                try:
                    await catalog.refresh_one_account(account_name)
                except Exception:
                    logger.exception(
                        "One-shot catalog recovery failed for %r",
                        account_name,
                    )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(_run())

        router = Router(
            registry,
            catalog,
            health_manager=health_manager,
            stale_after_s=routing_stale_after_s(config),
            local_quota_mode=config.routing.local_quota_mode,
            fairness_mode=config.routing.fairness_mode,
            fairness_epsilon=config.routing.fairness_epsilon,
            fairness_scope=config.routing.fairness_scope,
            missing_account_recovery_callback=_schedule_missing_account_recovery,
            missing_account_recovery_min_interval_s=float(
                config.models.refresh_interval_s,
            ),
            quarantine=quarantine,
        )

        # Wire routing config into scorer and estimator
        five_hour_capacity = float(config.limits.five_hour_microdollars)
        router._scorer.tiebreaker_range = (  # pyright: ignore[reportPrivateUsage]
            config.routing.near_tie_epsilon
        )
        if not config.routing.randomize_near_ties:
            router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]
        if five_hour_capacity > 0:
            router._scorer.inflight_penalty_per_request = (  # pyright: ignore[reportPrivateUsage]
                config.routing.inflight_penalty / five_hour_capacity
            )
            router._scorer.health_penalty_value = (  # pyright: ignore[reportPrivateUsage]
                config.routing.health_penalty / five_hour_capacity
            )
        router.quota_estimator.default_unknown_reservation_microdollars = (
            config.routing.unknown_request_reservation_microdollars
        )
        router._scorer.prefer_native = (  # pyright: ignore[reportPrivateUsage]
            config.transcoder.prefer_native
        )

        return router

    def _load_model_price_overrides(
        self,
        config: AppConfig,
        router: Router,
    ) -> None:
        """Load configured model price overrides into the quota estimator."""
        for model_id, override in config.model_overrides.items():
            input_price = override.input_price_per_1k
            output_price = override.output_price_per_1k
            if input_price is not None and output_price is not None:
                router.quota_estimator.set_model_override(
                    model_id,
                    input_price * 1000,
                    output_price * 1000,
                )
        for provider in config.providers.values():
            for model_id, override in provider.model_overrides.items():
                global_override = config.model_overrides.get(model_id)
                input_price = (
                    override.input_price_per_1k
                    if override.input_price_per_1k is not None
                    else (
                        global_override.input_price_per_1k
                        if global_override is not None
                        else None
                    )
                )
                output_price = (
                    override.output_price_per_1k
                    if override.output_price_per_1k is not None
                    else (
                        global_override.output_price_per_1k
                        if global_override is not None
                        else None
                    )
                )
                if input_price is None or output_price is None:
                    continue
                for account in provider.accounts:
                    router.quota_estimator.set_account_model_override(
                        account.name,
                        model_id,
                        input_price * 1000,
                        output_price * 1000,
                    )

    async def _load_usage_windows(
        self,
        config: AppConfig,
        router: Router,
        usage_window_repo: Any,
    ) -> None:
        """Load persisted usage windows and configure account weights/offsets."""
        router.quota_estimator.set_usage_window_repo(usage_window_repo)
        config_offsets: dict[str, dict[str, int]] = {}
        for acct_cfg in config.all_accounts():
            config_offsets[acct_cfg.name] = {
                "five_hour": acct_cfg.five_hour_offset_microdollars,
                "weekly": acct_cfg.weekly_offset_microdollars,
                "monthly": acct_cfg.monthly_offset_microdollars,
            }
        await router.quota_estimator.load_persisted_windows(
            offsets=config_offsets,
        )
        for acct_cfg in config.all_accounts():
            router.set_account_weight(acct_cfg.name, acct_cfg.weight)
        for acct_cfg in config.all_accounts():
            router.configure_account_policy(
                account_name=acct_cfg.name,
                weight=acct_cfg.weight,
                capacity_5h_microdollars=int(
                    config.limits.five_hour_microdollars * acct_cfg.weight,
                ),
                capacity_7d_microdollars=int(
                    config.limits.weekly_microdollars * acct_cfg.weight,
                ),
                capacity_30d_microdollars=int(
                    config.limits.monthly_microdollars * acct_cfg.weight,
                ),
                offset_5h_microdollars=acct_cfg.five_hour_offset_microdollars,
                offset_7d_microdollars=acct_cfg.weekly_offset_microdollars,
                offset_30d_microdollars=acct_cfg.monthly_offset_microdollars,
            )


# ---------------------------------------------------------------------------
# Health/backoff hydration (shared between startup and reload)
# ---------------------------------------------------------------------------


async def _hydrate_health_from_backoffs(
    repo: Any,
    health_manager: HealthManager,
) -> None:
    """Reapply persisted upstream backoffs onto the in-memory health manager.

    Called once per generation construction (startup and reload) so
    upstream-derived suppression state survives restarts and rehashes.
    """
    from eggpool.db.repositories import AccountRepository  # noqa: PLC0415

    account_repo = AccountRepository(repo._db)  # type: ignore[arg-type]  # noqa: SLF001
    from eggpool.health.backoff import (
        MAX_NONTERMINAL_BACKOFF_SECONDS,
        get_backoff_policy,
    )

    list_all = getattr(repo, "list_all", None)
    rows = await (list_all() if list_all is not None else repo.list_active())
    if not rows:
        return
    logger.info(
        "Hydrating %d persisted upstream backoffs into HealthManager",
        len(rows),
    )
    now = time.time()
    for row in rows:
        try:
            account_id = int(row["account_id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring malformed persisted backoff row: invalid account")
            continue
        account_name = await account_repo.get_enabled_name_by_id(account_id)
        if account_name is None:
            logger.info(
                "Ignoring backoff for missing or disabled account_id=%s", account_id
            )
            continue
        reason = str(row.get("reason") or "")
        if get_backoff_policy(reason) is None:
            logger.warning("Ignoring persisted backoff with unknown reason=%r", reason)
            continue
        model_value = row.get("model_id")
        model_id = str(model_value) if model_value is not None else None
        if reason == "model_unavailable" and not model_id:
            logger.warning("Ignoring account-wide model_unavailable backoff")
            continue

        deadline = row.get("backoff_until_epoch")
        raw_valid = row.get("backoff_until_valid", True)
        try:
            deadline_value = None if deadline is None else float(deadline)
        except (TypeError, ValueError):
            deadline_value = None
            raw_valid = False
        if not raw_valid or (
            deadline_value is not None and not math.isfinite(deadline_value)
        ):
            logger.warning(
                "Ignoring malformed persisted backoff row id=%s", row.get("id")
            )
            continue

        if reason == "authentication_failed":
            # Authentication is terminal. A stale finite deadline from an
            # older release must never turn it into a timed suppression.
            health_manager.disable_account(
                account_name,
                reason="authentication_failed",
            )
            continue

        if deadline is None:
            assert model_id is not None
            health_manager.disable_model(
                account_name,
                model_id,
                terminal=True,
            )
            continue

        assert deadline_value is not None
        if deadline_value <= now:
            try:
                await repo.delete_row(row_id=int(row["id"]))
            except Exception:
                logger.warning(
                    "Could not delete expired backoff hint id=%s",
                    row.get("id"),
                    exc_info=True,
                )
            continue

        bounded_deadline = min(
            deadline_value,
            now + MAX_NONTERMINAL_BACKOFF_SECONDS,
        )
        if bounded_deadline != deadline_value:
            try:
                await repo.update_deadline(
                    row_id=int(row["id"]),
                    backoff_until=bounded_deadline,
                )
            except Exception:
                logger.warning(
                    "Could not clamp persisted backoff hint id=%s",
                    row.get("id"),
                    exc_info=True,
                )

        remaining = bounded_deadline - now
        if reason == "model_unavailable":
            assert model_id is not None
            health_manager.disable_model(
                account_name,
                model_id,
                duration_seconds=remaining,
            )
        elif model_id is not None:
            logger.warning(
                "Ignoring contradictory account-wide backoff with model_id=%r",
                model_id,
            )
        elif reason == "quota_exhausted":
            health_manager.record_quota_exhausted(account_name, remaining)
        elif reason == "rate_limited":
            health_manager.record_rate_limit(account_name, remaining)
        else:
            health = health_manager.get_account_health(account_name)
            health.cooldown_until = now + remaining
            health.health_state = "cooldown"
            health.is_healthy = False


async def _load_account_identities(
    db: Any,
    registry: AccountRegistry,
) -> dict[str, Any]:
    """Load the immutable, non-secret account identities for a generation."""
    from eggpool.accounts.registry import AccountRuntimeIdentity  # noqa: PLC0415
    from eggpool.db.repositories import AccountRepository  # noqa: PLC0415

    rows = await AccountRepository(db).list_enabled()
    identities: dict[str, AccountRuntimeIdentity] = {}
    for row in rows:
        name = str(row["name"])
        state = registry.get_state(name)
        if state is None:
            continue
        identities[name] = AccountRuntimeIdentity(
            account_id=int(row["id"]),
            account_name=name,
            provider_id=str(row.get("provider_id") or DEFAULT_PROVIDER_ID),
            has_usable_credentials=registry.has_usable_credentials(name),
            routing_priority=state.routing_priority,
            weight=float(row.get("weight", state.weight)),
        )
    return identities


def _quarantine_entry_from_row(row: dict[str, object]) -> Any:
    """Convert a quarantine repository row into a :class:`QuarantineEntry`."""
    from eggpool.failure import entry_from_row  # noqa: PLC0415

    return entry_from_row(row)


async def _hydrate_model_quarantine(repo: Any, quarantine: Any) -> None:
    """Hydrate quarantine state as a prerequisite for generation publication."""
    try:
        rows = await repo.list_all()
        entries = [_quarantine_entry_from_row(row) for row in rows]
        for entry in entries:
            quarantine.hydrate_entry(entry)
    except ModelQuarantineHydrationError:
        logger.error("model_quarantine: hydration failed; generation rejected")
        raise
    except Exception as exc:
        logger.error("model_quarantine: hydration failed; generation rejected")
        raise ModelQuarantineHydrationError(
            "Model quarantine hydration failed"
        ) from exc
    logger.info("model_quarantine: hydration succeeded rows=%d", len(entries))


async def _clear_model_reappearance_durable_first(
    *,
    account_name: str,
    provider_id: str,
    models: list[dict[str, Any]],
    model_quarantine_repo: Any,
    effects_applier: Any,
    account_backoff_repo: Any,
    account_repo: Any,
) -> None:
    """Publish authoritative reappearance only after durable clear success.

    Clear operations are intentionally per identity.  A failure leaves the
    current identity's in-memory suppression untouched; identities already
    converged earlier in this deterministic order remain converged.
    """
    identities: list[tuple[str, str, str]] = []
    for model in models:
        canonical_model_id = str(
            model.get("canonical_model_id") or model.get("model_id") or ""
        )
        protocol = str(model.get("protocol") or "")
        upstream_model_id = str(
            model.get("upstream_model_id")
            if model.get("upstream_model_id") is not None
            else canonical_model_id
        )
        if canonical_model_id and protocol:
            identities.append((canonical_model_id, upstream_model_id, protocol))

    for canonical_model_id, upstream_model_id, protocol in sorted(identities):
        try:
            rowcount = await model_quarantine_repo.mark_cleared(
                provider_id=provider_id,
                account_id=account_name,
                canonical_model_id=canonical_model_id,
                upstream_model_id=upstream_model_id,
                upstream_protocol=protocol,
                clear_reason="catalog_reappearance",
                cleared_epoch=time.time(),
            )
        except Exception as exc:
            logger.warning(
                "model_quarantine: durable reappearance clear failed; "
                "in-memory suppression preserved",
            )
            raise ModelQuarantineRecoveryError(
                "Model quarantine durable reappearance clear failed"
            ) from exc

        logger.info(
            "model_quarantine: durable reappearance clear converged rows=%d",
            rowcount,
        )
        effects_applier.clear_authoritative_reappearance(
            provider_id=provider_id,
            account_id=account_name,
            canonical_model_id=canonical_model_id,
            upstream_model_id=upstream_model_id,
            upstream_protocol=protocol,
        )

        try:
            account_id = await account_repo.get_id_by_name(account_name)
            if account_id is not None:
                await account_backoff_repo.clear_success(
                    account_id=account_id,
                    model_id=canonical_model_id,
                    reasons=["model_unavailable"],
                )
        except Exception:
            logger.warning(
                "model_quarantine: reappearance backoff clear failed after "
                "durable and in-memory convergence",
            )

        logger.info("model_quarantine: durable_and_memory_clear_converged")
