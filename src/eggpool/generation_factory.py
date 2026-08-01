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

**Dispatch writer enablement (Phase 8)**: the factory derives the
coordinator's ``use_dispatch_writer`` selection from two conditions:
the process-owned writer object must be non-``None`` *and*
``config.dispatch_writer.enabled`` must be ``True``.  Both must hold
for the microbatch persistence path to be selected.  This prevents
the accidental default where a writer exists but is never used.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eggpool.constants import DEFAULT_PROVIDER_ID

if TYPE_CHECKING:
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.background import TaskSupervisor
    from eggpool.catalog.pricing import CostCalculator
    from eggpool.catalog.service import CatalogService
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig
    from eggpool.providers.client_pool import ProviderClientPool
    from eggpool.providers.dns_cache import DnsNetworkBackend
    from eggpool.providers.outbound import OutboundClientManager
    from eggpool.request.coordinator import RequestCoordinator
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
    outbound_manager: OutboundClientManager
    dns_backend: DnsNetworkBackend | None
    health_manager: HealthManager
    cost_calculator: CostCalculator
    transcoder_policy: Any
    compression_policy: Any
    cache_config: Any
    compression_tuning_registry: Any
    dispatch_overhead_recorder: DispatchOverheadRecorder
    dispatch_span_recorder: DispatchSpanRecorder
    account_backoff_repo: Any
    stats_service: StatsService
    supervisor: TaskSupervisor
    routing_trace_guard: RoutingTraceGuard | None
    routing_trace_writer: Any
    local_pre_upstream_recorder: Any = None
    stream_diagnostics: Any = None
    finalization_supervisor: Any = None


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

    **Dispatch writer enablement**: when the process-owned dispatch
    writer is non-``None`` *and* ``config.dispatch_writer.enabled`` is
    ``True``, the coordinator's ``use_dispatch_writer`` flag is set
    ``True`` so the microbatch persistence path is selected.  Both
    conditions must hold; a missing writer object or a disabled config
    results in direct persistence.  This is the single authoritative
    rule that prevents the writer from existing without being used.
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
        import httpcore  # noqa: PLC0415, TC002

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
        from eggpool.providers.dns_cache import DnsNetworkBackend  # noqa: PLC0415
        from eggpool.providers.outbound import (  # noqa: PLC0415
            OutboundClientManager,
            default_network_backend,
        )
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
        from eggpool.transcoder.compression.tuning import (  # noqa: PLC0415
            RuntimeCompressionPolicyOverrideRegistry,
        )

        db = process.db
        _register = candidate.register_resource if candidate is not None else None

        # -- DNS backend (generation-owned) --------------------------------
        dns_backend: httpcore.AsyncNetworkBackend | None = None
        if config.network.dns_cache.enabled:
            dns_backend = DnsNetworkBackend(
                config.network.dns_cache,
                default_network_backend(),
            )
            if _register is not None:
                _dns_close = getattr(dns_backend, "aclose", None)
                if _dns_close is not None:
                    _register("dns_backend", _dns_close)

        # -- Client pool (generation-owned) --------------------------------
        client_pool = ProviderClientPool.from_app_config(
            config,
            network_backend=dns_backend,
        )
        if _register is not None:
            _register("client_pool", client_pool.close)

        # -- Outbound client manager (generation-owned) --------------------
        outbound_manager = OutboundClientManager(
            config=config.network,
            network_backend=dns_backend,
        )
        if _register is not None:
            _register("outbound_manager", outbound_manager.aclose)
        outbound_client = await outbound_manager.get_client()

        # -- Account registry (generation-owned) ---------------------------
        registry = AccountRegistry(config)
        account_identities = await _load_account_identities(db, registry)

        # -- Transcoder / compression policy snapshots ---------------------
        transcoder_policy = config.transcoder
        compression_policy = config.compression
        cache_config = config.cache
        compression_tuning_registry = RuntimeCompressionPolicyOverrideRegistry()

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
        try:
            for row in await model_quarantine_repo.list_all():
                entry = _quarantine_entry_from_row(row)
                quarantine.hydrate_entry(entry)
        except Exception:
            logger.exception("model_quarantine: hydration failed; starting empty")

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
        await catalog.attach_pricing_resolvers()
        await catalog._load_cached_models()  # pyright: ignore[reportPrivateUsage]

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
        # Plan 029, Workstream H: request-coherent span sampling.
        # ``detailed_span_sample_rate`` is deprecated but overrides
        # ``dispatch_spans.sample_rate`` for backward compatibility.
        span_sample_rate = config.metrics.detailed_span_sample_rate
        if span_sample_rate == 1.0:
            span_sample_rate = config.metrics.dispatch_spans.sample_rate
        dispatch_span_recorder = DispatchSpanRecorder(
            window_size=config.metrics.dispatch_spans.window_size,
            detailed_span_sample_rate=span_sample_rate,
        )

        # -- Local pre-upstream recorder ------------------------------------
        local_pre_upstream_recorder = LocalPreUpstreamRecorder(window_size=100)

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
            cache_config=cache_config,
            compression_tuning_registry=compression_tuning_registry,
            compression_policy=compression_policy,
            stream_diagnostics=stream_diagnostics,
            dispatch_writer=process.dispatch_writer,
            use_dispatch_writer=(
                process.dispatch_writer is not None and config.dispatch_writer.enabled
            ),
            effects_applier=effects_applier,
            quarantine=quarantine,
            account_identities=account_identities,
        )

        # -- Finalization supervisor (Plan 026) ------------------------------
        # Process-owned supervisor for request finalization jobs.  Provides
        # retained-task finalization, bounded retry, and diagnostics.
        from eggpool.request.finalization_job import (  # noqa: PLC0415
            RequestFinalizationSupervisor,
        )

        finalization_supervisor = RequestFinalizationSupervisor(
            db=db,
            effects_applier=effects_applier,
        )
        coordinator._finalization_supervisor = (  # pyright: ignore[reportPrivateUsage]
            finalization_supervisor
        )

        # -- Routing trace guard (NOT configured during preparation) ---------
        # Configuration is deferred to RoutingTraceGuardTransition at commit
        # time so candidate preparation has no process-owned side effects.
        from eggpool.request.routing_trace_guard import (  # noqa: PLC0415
            get_routing_trace_guard,
        )

        routing_trace_guard = get_routing_trace_guard()
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
            dns_backend=dns_backend,
            health_manager=health_manager,
            cost_calculator=cost_calculator,
            transcoder_policy=transcoder_policy,
            compression_policy=compression_policy,
            cache_config=cache_config,
            compression_tuning_registry=compression_tuning_registry,
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
        )

        return PreparedRuntimeGeneration(
            generation=build_result.generation,
            registry=registry,
            catalog=catalog,
            router=router,
            coordinator=coordinator,
            client_pool=client_pool,
            outbound_manager=outbound_manager,
            dns_backend=dns_backend,
            health_manager=health_manager,
            cost_calculator=cost_calculator,
            transcoder_policy=transcoder_policy,
            compression_policy=compression_policy,
            cache_config=cache_config,
            compression_tuning_registry=compression_tuning_registry,
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
    active = await repo.list_active()
    if not active:
        return
    logger.info(
        "Hydrating %d persisted upstream backoffs into HealthManager",
        len(active),
    )
    for row in active:
        account_name = await account_repo.get_name_by_id(int(row["account_id"]))
        if account_name is None:
            continue
        reason = str(row.get("reason") or "")
        model_id = row.get("model_id")
        backoff_until_epoch = row.get("backoff_until_epoch")
        if reason == "model_unavailable" and backoff_until_epoch is None:
            if model_id:
                health_manager.disable_model(account_name, str(model_id))
            continue
        if backoff_until_epoch is None:
            continue
        remaining = max(0.0, float(backoff_until_epoch) - time.time())
        if remaining <= 0:
            continue
        if reason == "quota_exhausted":
            health_manager.record_quota_exhausted(account_name, remaining)
        elif reason == "rate_limited":
            health_manager.record_rate_limit(account_name, remaining)
        elif reason == "authentication_failed":
            health_manager.disable_account(
                account_name,
                reason="authentication_failed",
                duration_seconds=remaining,
            )
        else:
            health = health_manager.get_account_health(account_name)
            health.cooldown_until = time.time() + remaining
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
