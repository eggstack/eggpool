"""Transaction manager for live configuration rehash (Milestone C).

Orchestrates the complete reload flow: validation → diff → candidate
preparation → persistence reconciliation → atomic publication →
retirement.

Design principles
-----------------

- One lock serializes complete reload transactions.
- Concurrent commands are rejected with ``reload_in_progress``.
- Cancellation after candidate preparation does NOT abort the reload.
- No secrets in logs, events, or diagnostics.
- All failures are rollback/fail-closed before publication.
- The ``_build_candidate_generation`` method mirrors the service
  construction from ``app._lifespan_runtime`` but uses the candidate
  config and shares process-owned resources.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from eggpool.config_reload_policy import (
    ConfigDiff,
    ReloadResult,
    ReloadStage,
    compute_diff,
)

if TYPE_CHECKING:
    import httpcore

    from eggpool.config_validation import (
        ConfigValidationResult,
        ConfigValidationWarning,
    )
    from eggpool.models.config import AppConfig
    from eggpool.runtime_manager import (
        ProcessRuntime,
        RuntimeGeneration,
        RuntimeManager,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReloadInProgressError(Exception):
    """Raised when a reload is attempted while another is in progress."""


class ReloadPreparationError(Exception):
    """Raised when candidate generation construction fails."""


class ReloadReconciliationError(Exception):
    """Raised when persistence reconciliation fails."""


class ReloadCommitError(Exception):
    """Raised when atomic publication fails."""


# ---------------------------------------------------------------------------
# Operation tracking
# ---------------------------------------------------------------------------


class ReloadOperationStage:
    """Stages of a reload operation for diagnostics."""

    IDLE: Final = "idle"
    VALIDATION: Final = "validation"
    DIFF: Final = "diff"
    PREPARATION: Final = "preparation"
    RECONCILIATION: Final = "reconciliation"
    COMMIT: Final = "commit"
    ACTIVATION: Final = "activation"
    RETIREMENT: Final = "retirement"


@dataclass(frozen=True)
class ReloadOperationState:
    """Current state of a reload operation for diagnostics."""

    stage: str
    started_at: float
    generation_id: int | None
    digest_prefix: str
    error: str | None = None


@dataclass(frozen=True)
class ReloadOperationResult:
    """Structured outcome of a complete reload transaction."""

    ok: bool
    stage: str
    generation: int | None
    changed_sections: tuple[str, ...]
    warnings: tuple[ConfigValidationWarning, ...]
    restart_required: tuple[Any, ...]
    retirement_pending: bool
    message: str
    duration_s: float


# ---------------------------------------------------------------------------
# Candidate generation container
# ---------------------------------------------------------------------------


@dataclass
class CandidateGeneration:
    """A prepared but not-yet-published generation."""

    generation: RuntimeGeneration
    process: ProcessRuntime
    diff: ConfigDiff


# ---------------------------------------------------------------------------
# Reload manager
# ---------------------------------------------------------------------------

DEFAULT_DRAIN_TIMEOUT_S: Final[float] = 300.0


class ReloadManager:
    """Manages serialized live-reload transactions.

    One reload at a time.  Concurrent commands are rejected.
    Uses RuntimeManager for generation lifecycle.
    """

    def __init__(
        self,
        runtime_manager: RuntimeManager,
        process: ProcessRuntime,
        *,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._process = process
        self._drain_timeout_s = drain_timeout_s
        self._reload_lock = asyncio.Lock()
        self._operation_state: ReloadOperationState | None = None
        self._last_reload_result: ReloadOperationResult | None = None
        self._last_reload_completed_at: float | None = None
        self._reload_count: int = 0
        self._reload_error_count: int = 0

    @property
    def operation_state(self) -> ReloadOperationState | None:
        """Return the current reload operation state for diagnostics."""
        return self._operation_state

    def snapshot(self) -> dict[str, Any]:
        """Return reload state for diagnostics."""
        return {
            "operation_state": {
                "stage": self._operation_state.stage,
                "started_at": self._operation_state.started_at,
                "generation_id": self._operation_state.generation_id,
                "digest_prefix": self._operation_state.digest_prefix,
                "error": self._operation_state.error,
            }
            if self._operation_state
            else None,
            "last_reload_result": {
                "ok": self._last_reload_result.ok,
                "stage": self._last_reload_result.stage,
                "generation": self._last_reload_result.generation,
                "changed_sections": self._last_reload_result.changed_sections,
                "restart_required": self._last_reload_result.restart_required,
                "warnings_count": len(self._last_reload_result.warnings),
                "retirement_pending": self._last_reload_result.retirement_pending,
                "message": self._last_reload_result.message,
                "duration_s": self._last_reload_result.duration_s,
            }
            if self._last_reload_result
            else None,
            "last_reload_completed_at": self._last_reload_completed_at,
            "reload_count": self._reload_count,
            "reload_error_count": self._reload_error_count,
        }

    # -- public entry point ------------------------------------------------

    async def reload(
        self,
        validation: ConfigValidationResult,
        *,
        expected_digest: str | None = None,
    ) -> ReloadResult:
        """Execute a complete reload transaction.

        Steps:
        1.  Acquire reload lock (reject if already in progress).
        2.  Validate digest matches.
        3.  Compute diff against active generation.
        4.  Check for restart-required changes (reject if any).
        5.  Handle semantic no-op (return success).
        6.  Build candidate generation (off to the side).
        7.  Reconcile persistence (DB transaction).
        8.  Atomic publication (swap generations).
        9.  Begin old generation retirement (non-blocking).
        """
        started_at = time.monotonic()
        digest_prefix = (
            validation.content_digest[:12] if validation.content_digest else "<empty>"
        )
        generation_id: int | None = None
        changed_sections: tuple[str, ...] = ()
        warnings: tuple[ConfigValidationWarning, ...] = validation.warnings
        restart_required: tuple[Any, ...] = ()

        if self._reload_lock.locked():
            await self._record_event(
                "reload_publication_conflict",
                digest_prefix=digest_prefix,
                error="A reload transaction is already in progress",
            )
            raise ReloadInProgressError("A reload transaction is already in progress")

        await self._record_event(
            "reload_requested",
            digest_prefix=digest_prefix,
        )

        async with self._reload_lock:
            try:
                # Stage 1: Validate digest
                self._set_stage(
                    ReloadOperationStage.VALIDATION,
                    started_at,
                    generation_id,
                    digest_prefix,
                )
                await self._validate_digest(validation, expected_digest)

                # Stage 2: Compute diff
                self._set_stage(
                    ReloadOperationStage.DIFF,
                    started_at,
                    generation_id,
                    digest_prefix,
                )
                diff = await self._compute_reload_diff(validation.config)

                # Stage 3: Check restart-required changes
                restart_required = tuple(diff.restart_required)
                if restart_required:
                    sections = tuple(sorted({c.section for c in restart_required}))
                    await self._record_event(
                        "reload_restart_required_rejected",
                        digest_prefix=digest_prefix,
                        changed_sections=sections,
                    )
                    duration = time.monotonic() - started_at
                    self._reload_count += 1
                    self._reload_error_count += 1
                    self._last_reload_result = ReloadOperationResult(
                        ok=False,
                        stage=ReloadStage.DIFF.value,
                        generation=None,
                        changed_sections=sections,
                        warnings=warnings,
                        restart_required=tuple(restart_required),
                        retirement_pending=False,
                        message=(
                            f"Reload rejected: {len(restart_required)} "
                            "restart-required field(s) changed"
                        ),
                        duration_s=duration,
                    )
                    self._last_reload_completed_at = time.time()
                    return ReloadResult(
                        ok=False,
                        stage=ReloadStage.DIFF,
                        generation=None,
                        changed_sections=sections,
                        warnings=warnings,
                        restart_required=tuple(restart_required),
                        message=(
                            f"Reload rejected: {len(restart_required)} "
                            "restart-required field(s) changed"
                        ),
                    )

                # Stage 4: Semantic no-op
                if not diff.changes:
                    active = self._runtime_manager.active_snapshot()
                    return ReloadResult(
                        ok=True,
                        stage=ReloadStage.COMMIT,
                        generation=active.generation_id,
                        changed_sections=(),
                        warnings=warnings,
                        restart_required=(),
                        message="No configuration changes detected",
                    )

                # All changes are IGNORED (no LIVE changes) — success with explanation
                if not diff.live:
                    active = self._runtime_manager.active_snapshot()
                    ignored_sections = tuple(sorted({c.section for c in diff.changes}))
                    return ReloadResult(
                        ok=True,
                        stage=ReloadStage.DIFF,
                        generation=active.generation_id,
                        changed_sections=ignored_sections,
                        warnings=warnings,
                        restart_required=(),
                        message="Configuration changes detected but all are ignored",
                    )

                changed_sections = tuple(sorted({c.section for c in diff.changes}))

                # Stage 5: Build candidate generation
                self._set_stage(
                    ReloadOperationStage.PREPARATION,
                    started_at,
                    generation_id,
                    digest_prefix,
                )
                candidate = await self._build_candidate_generation(
                    validation,
                    diff,
                    runtime_manager=self._runtime_manager,
                )
                generation_id = candidate.generation.generation_id
                digest_prefix = candidate.generation.config_digest[:12]

                # Stage 6: Reconcile persistence
                self._set_stage(
                    ReloadOperationStage.RECONCILIATION,
                    started_at,
                    generation_id,
                    digest_prefix,
                )
                await self._reconcile_persistence(
                    validation.config,
                    self._runtime_manager.active_snapshot().config,
                )

                # Stage 7: Atomic publication
                self._set_stage(
                    ReloadOperationStage.COMMIT,
                    started_at,
                    generation_id,
                    digest_prefix,
                )
                await self._publish_generation(candidate, diff)

                # Stage 8: Begin retirement (non-blocking)
                self._set_stage(
                    ReloadOperationStage.RETIREMENT,
                    started_at,
                    generation_id,
                    digest_prefix,
                )
                self._set_stage(
                    ReloadOperationStage.IDLE,
                    started_at,
                    generation_id,
                    digest_prefix,
                )

                duration = time.monotonic() - started_at
                logger.info(
                    "Reload committed: generation=%d duration=%.3fs sections=%s",
                    generation_id,
                    duration,
                    ",".join(changed_sections) or "(none)",
                )
                result = ReloadResult(
                    ok=True,
                    stage=ReloadStage.RETIREMENT,
                    generation=generation_id,
                    changed_sections=changed_sections,
                    warnings=warnings,
                    restart_required=(),
                    message=(
                        f"Reload applied: generation {generation_id}, "
                        f"{len(changed_sections)} section(s) changed"
                    ),
                )
                self._reload_count += 1
                self._last_reload_result = ReloadOperationResult(
                    ok=True,
                    stage=ReloadStage.RETIREMENT.value,
                    generation=generation_id,
                    changed_sections=changed_sections,
                    warnings=warnings,
                    restart_required=(),
                    retirement_pending=True,
                    message=result.message,
                    duration_s=duration,
                )
                self._last_reload_completed_at = time.time()
                await self._record_event(
                    "reload_activated",
                    generation_id=generation_id,
                    digest_prefix=digest_prefix,
                    changed_sections=changed_sections,
                )
                return result

            except ReloadInProgressError:
                raise
            except ReloadPreparationError as exc:
                duration = time.monotonic() - started_at
                error_stage = (
                    self._operation_state.stage
                    if self._operation_state
                    else ReloadOperationStage.IDLE
                )
                logger.exception("Reload failed at stage %s", error_stage)
                self._reload_count += 1
                self._reload_error_count += 1
                event_type = "reload_preparation_failure"
                if "digest mismatch" in str(exc).lower():
                    event_type = "reload_digest_mismatch"
                self._last_reload_result = ReloadOperationResult(
                    ok=False,
                    stage=error_stage,
                    generation=generation_id,
                    changed_sections=changed_sections,
                    warnings=warnings,
                    restart_required=restart_required,
                    retirement_pending=False,
                    message=f"Reload failed: {exc!r}",
                    duration_s=duration,
                )
                self._last_reload_completed_at = time.time()
                await self._record_event(
                    event_type,
                    generation_id=generation_id,
                    digest_prefix=digest_prefix,
                    changed_sections=changed_sections,
                    error=f"{exc!r}",
                )
                return ReloadResult(
                    ok=False,
                    stage=ReloadStage.VALIDATION,
                    generation=None,
                    changed_sections=(),
                    warnings=warnings,
                    restart_required=(),
                    message=f"Reload failed: {exc!r}",
                )
            except Exception as exc:
                duration = time.monotonic() - started_at
                error_stage = (
                    self._operation_state.stage
                    if self._operation_state
                    else ReloadOperationStage.IDLE
                )
                logger.exception("Reload failed at stage %s", error_stage)
                self._reload_count += 1
                self._reload_error_count += 1
                self._last_reload_result = ReloadOperationResult(
                    ok=False,
                    stage=error_stage,
                    generation=generation_id,
                    changed_sections=changed_sections,
                    warnings=warnings,
                    restart_required=restart_required,
                    retirement_pending=False,
                    message=f"Reload failed: {exc!r}",
                    duration_s=duration,
                )
                self._last_reload_completed_at = time.time()
                event_type = "reload_preparation_failure"
                if error_stage == ReloadOperationStage.RECONCILIATION:
                    event_type = "reload_reconciliation_failure"
                await self._record_event(
                    event_type,
                    generation_id=generation_id,
                    digest_prefix=digest_prefix,
                    changed_sections=changed_sections,
                    error=f"{exc!r}",
                )
                return ReloadResult(
                    ok=False,
                    stage=ReloadStage.VALIDATION,
                    generation=None,
                    changed_sections=(),
                    warnings=warnings,
                    restart_required=(),
                    message=f"Reload failed: {exc!r}",
                )

    # -- stage helpers -----------------------------------------------------

    def _set_stage(
        self,
        stage: str,
        started_at: float,
        generation_id: int | None,
        digest_prefix: str,
        *,
        error: str | None = None,
    ) -> None:
        self._operation_state = ReloadOperationState(
            stage=stage,
            started_at=started_at,
            generation_id=generation_id,
            digest_prefix=digest_prefix,
            error=error,
        )

    async def _record_event(
        self,
        event_type: str,
        *,
        generation_id: int | None = None,
        digest_prefix: str = "",
        changed_sections: tuple[str, ...] = (),
        error: str | None = None,
    ) -> None:
        """Record an operational event for reload lifecycle tracking."""
        from eggpool.db.repositories import OperationalEventRepository  # noqa: PLC0415

        details: dict[str, Any] = {}
        if generation_id is not None:
            details["generation_id"] = generation_id
        if digest_prefix:
            details["digest_prefix"] = digest_prefix
        if changed_sections:
            details["changed_sections"] = list(changed_sections)
        if error:
            details["error"] = error
        try:
            repo = OperationalEventRepository(self._process.db)
            await repo.record(event_type, details)
        except Exception:
            logger.debug(
                "Failed to record operational event %s", event_type, exc_info=True
            )

    # -- step implementations ----------------------------------------------

    async def _validate_digest(
        self,
        validation: ConfigValidationResult,
        expected: str | None,
    ) -> None:
        """Verify the content digest matches the caller's expectation."""
        if expected is not None and expected != validation.content_digest:
            raise ReloadPreparationError(
                "Content digest mismatch: expected "
                f"{expected[:12]}… got {validation.content_digest[:12]}…"
            )

    async def _compute_reload_diff(self, candidate_config: AppConfig) -> ConfigDiff:
        """Compute the structured diff against the active generation."""
        active = self._runtime_manager.active_snapshot()
        return compute_diff(active.config, candidate_config)

    async def _build_candidate_generation(
        self,
        validation: ConfigValidationResult,
        diff: ConfigDiff,
        *,
        runtime_manager: RuntimeManager | None = None,
    ) -> CandidateGeneration:
        """Construct all generation-owned services for the candidate config.

        Mirrors the service construction from ``app._lifespan_runtime``
        but uses the candidate config and shares process-owned resources
        (db, stats_db, config_path, metrics_coalescer).

        Does NOT perform startup-only operations: migrations, crash
        recovery, catalog staleness enforcement, or initial catalog
        refresh.  Those are startup concerns only.
        """
        from eggpool.accounts.registry import (  # noqa: PLC0415
            AccountRegistry,
        )
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
        from eggpool.routing.config import routing_stale_after_s  # noqa: PLC0415
        from eggpool.routing.router import Router  # noqa: PLC0415
        from eggpool.runtime_dispatch import (  # noqa: PLC0415
            DispatchOverheadRecorder,
            DispatchSpanRecorder,
        )
        from eggpool.runtime_manager import (  # noqa: PLC0415
            RuntimeGenerationBuilder,
        )
        from eggpool.stats import StatsService  # noqa: PLC0415
        from eggpool.transcoder.compression.tuning import (  # noqa: PLC0415
            RuntimeCompressionPolicyOverrideRegistry,
        )

        candidate_config = validation.config
        db = self._process.db
        process = self._process
        generation_id = self._runtime_manager.reserve_next_generation_id()

        try:
            # -- Network (generation-owned) --------------------------------
            dns_backend: httpcore.AsyncNetworkBackend | None = None
            if candidate_config.network.dns_cache.enabled:
                dns_backend = DnsNetworkBackend(
                    candidate_config.network.dns_cache,
                    default_network_backend(),
                )

            client_pool = ProviderClientPool.from_app_config(
                candidate_config,
                network_backend=dns_backend,
            )

            outbound_manager = OutboundClientManager(
                config=candidate_config.network,
                network_backend=dns_backend,
            )
            outbound_client = await outbound_manager.get_client()

            # -- Account registry ------------------------------------------
            registry = AccountRegistry(candidate_config)

            # -- Transcoder / compression policy snapshots -----------------
            transcoder_policy = candidate_config.transcoder
            compression_policy = candidate_config.compression
            cache_config = candidate_config.cache
            compression_tuning_registry = RuntimeCompressionPolicyOverrideRegistry()

            # -- Health manager --------------------------------------------
            health_manager = HealthManager()

            # -- Persistent backoff repository -----------------------------
            account_backoff_repo = AccountBackoffRepository(db)

            # -- Catalog service -------------------------------------------
            ping_repo = PingRepository(db)
            catalog = CatalogService(
                candidate_config,
                registry,
                db,
                client_pool,
                ping_repo=ping_repo,
                outbound_client=outbound_client,
            )
            await catalog.attach_pricing_resolvers()
            await catalog._load_cached_models()  # pyright: ignore[reportPrivateUsage]

            # -- Cost calculator -------------------------------------------
            price_repo = PriceRepository(db)
            cost_calculator = CostCalculator(price_repo)
            catalog.set_price_change_callback(cost_calculator.invalidate_price)

            # -- Router ----------------------------------------------------
            def _schedule_missing_account_recovery(
                account_name: str,
            ) -> None:
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
                stale_after_s=routing_stale_after_s(candidate_config),
                local_quota_mode=candidate_config.routing.local_quota_mode,
                fairness_mode=candidate_config.routing.fairness_mode,
                fairness_epsilon=candidate_config.routing.fairness_epsilon,
                fairness_scope=candidate_config.routing.fairness_scope,
                missing_account_recovery_callback=(_schedule_missing_account_recovery),
                missing_account_recovery_min_interval_s=float(
                    candidate_config.models.refresh_interval_s,
                )
                / 2.0,
            )

            # Wire routing config into scorer and estimator
            five_hour_capacity = float(
                candidate_config.limits.five_hour_microdollars,
            )
            router._scorer.tiebreaker_range = (  # pyright: ignore[reportPrivateUsage]
                candidate_config.routing.near_tie_epsilon
            )
            if not candidate_config.routing.randomize_near_ties:
                router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]
            if five_hour_capacity > 0:
                router._scorer.inflight_penalty_per_request = (  # pyright: ignore[reportPrivateUsage]
                    candidate_config.routing.inflight_penalty / five_hour_capacity
                )
                router._scorer.health_penalty_value = (  # pyright: ignore[reportPrivateUsage]
                    candidate_config.routing.health_penalty / five_hour_capacity
                )
            router.quota_estimator.default_unknown_reservation_microdollars = (
                candidate_config.routing.unknown_request_reservation_microdollars
            )
            router._scorer.prefer_native = (  # pyright: ignore[reportPrivateUsage]
                candidate_config.transcoder.prefer_native
            )

            # Load configured model price overrides into estimator
            for model_id, override in candidate_config.model_overrides.items():
                input_price = override.input_price_per_1k
                output_price = override.output_price_per_1k
                if input_price is not None and output_price is not None:
                    router.quota_estimator.set_model_override(
                        model_id,
                        input_price * 1000,
                        output_price * 1000,
                    )
            for provider in candidate_config.providers.values():
                for model_id, override in provider.model_overrides.items():
                    global_override = candidate_config.model_overrides.get(
                        model_id,
                    )
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

            # Load persisted usage windows and set account weights/offsets
            usage_window_repo = UsageWindowRepository(db)
            router.quota_estimator.set_usage_window_repo(usage_window_repo)
            config_offsets: dict[str, dict[str, int]] = {}
            for acct_cfg in candidate_config.all_accounts():
                config_offsets[acct_cfg.name] = {
                    "five_hour": acct_cfg.five_hour_offset_microdollars,
                    "weekly": acct_cfg.weekly_offset_microdollars,
                    "monthly": acct_cfg.monthly_offset_microdollars,
                }
            await router.quota_estimator.load_persisted_windows(
                offsets=config_offsets,
            )
            for acct_cfg in candidate_config.all_accounts():
                router.set_account_weight(acct_cfg.name, acct_cfg.weight)
            for acct_cfg in candidate_config.all_accounts():
                router.configure_account_policy(
                    account_name=acct_cfg.name,
                    weight=acct_cfg.weight,
                    capacity_5h_microdollars=int(
                        candidate_config.limits.five_hour_microdollars
                        * acct_cfg.weight,
                    ),
                    capacity_7d_microdollars=int(
                        candidate_config.limits.weekly_microdollars * acct_cfg.weight,
                    ),
                    capacity_30d_microdollars=int(
                        candidate_config.limits.monthly_microdollars * acct_cfg.weight,
                    ),
                    offset_5h_microdollars=(acct_cfg.five_hour_offset_microdollars),
                    offset_7d_microdollars=(acct_cfg.weekly_offset_microdollars),
                    offset_30d_microdollars=(acct_cfg.monthly_offset_microdollars),
                )

            # -- Repositories (generation-owned) ---------------------------
            request_repo = RequestRepository(db)
            reservation_repo = ReservationRepository(db)
            attempt_repo = AttemptRepository(db)

            # -- Dispatch recorders ----------------------------------------
            dispatch_overhead_recorder = DispatchOverheadRecorder(
                window_size=100,
            )
            dispatch_span_recorder = DispatchSpanRecorder(window_size=200)

            # -- Request coordinator ----------------------------------------
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
                max_retry_attempts=(
                    1 + candidate_config.routing.max_retries_before_stream
                ),
                quota_exhausted_cooldown_seconds=(
                    candidate_config.routing.quota_exhausted_cooldown_seconds
                ),
                persist_error_detail=(
                    candidate_config.security.persist_redacted_error_detail
                ),
                config=candidate_config,
                account_backoff_repo=account_backoff_repo,
                metrics_coalescer=process.metrics_coalescer,
                dispatch_overhead_recorder=dispatch_overhead_recorder,
                dispatch_span_recorder=dispatch_span_recorder,
                transcoder_policy=transcoder_policy,
                cache_config=cache_config,
                compression_tuning_registry=compression_tuning_registry,
                compression_policy=compression_policy,
            )

            # -- Finalization retry queue ----------------------------------
            from eggpool.request.finalization_queue import (  # noqa: PLC0415
                FinalizationRetryQueue,
            )

            finalization_retry_queue = FinalizationRetryQueue(
                db=db,
                finalizer=coordinator._finalizer,  # pyright: ignore[reportPrivateUsage]
                router=router,
                quota_estimator=router.quota_estimator,
            )
            coordinator._finalization_retry_queue = (  # pyright: ignore[reportPrivateUsage]
                finalization_retry_queue
            )

            # -- Routing trace guard ---------------------------------------
            from eggpool.request.routing_trace_guard import (  # noqa: PLC0415
                get_routing_trace_guard,
            )

            routing_trace_guard = get_routing_trace_guard()
            routing_trace_guard.configure(
                threshold_ms=(
                    candidate_config.routing.trace.skip_above_lock_wait_p95_ms
                ),
            )
            coordinator._routing_trace_guard = (  # pyright: ignore[reportPrivateUsage]
                routing_trace_guard
            )

            # -- Stats service (generation-owned) --------------------------
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

            # -- Task supervisor (tasks registered for candidate generation)
            supervisor = TaskSupervisor()

            # Register background tasks on the candidate supervisor so
            # periodic work (catalog refresh, metrics flush, etc.)
            # continues after the old generation is retired.
            if runtime_manager is not None:
                from eggpool.app import register_candidate_tasks  # noqa: PLC0415

                register_candidate_tasks(
                    supervisor,
                    candidate_config,
                    process,
                    runtime_manager,
                )

            # -- Assemble generation via builder ---------------------------
            builder = RuntimeGenerationBuilder()
            build_result = await builder.build_initial(
                candidate_config,
                process,
                generation_id=generation_id,
                config_digest=validation.content_digest,
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
                finalization_retry_queue=finalization_retry_queue,
                routing_trace_guard=routing_trace_guard,
            )

            return CandidateGeneration(
                generation=build_result.generation,
                process=process,
                diff=diff,
            )

        except Exception:
            logger.exception(
                "Candidate generation construction failed; aborting reload"
            )
            # Clean up any partially constructed network resources.
            # The builder's cleanup_partial delegates to app.cleanup_partial_generation.
            try:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    RuntimeGenerationBuilder,
                )

                builder_cleanup = RuntimeGenerationBuilder()
                await builder_cleanup.cleanup_partial(process)
            except Exception:
                logger.debug("Partial generation cleanup failed", exc_info=True)
            raise ReloadPreparationError(
                "Failed to construct candidate generation"
            ) from None

    async def _reconcile_persistence(
        self,
        candidate_config: AppConfig,
        active_config: AppConfig,
    ) -> None:
        """Sync providers and accounts from candidate config to SQLite.

        Runs inside a single database transaction so the persistence
        layer is atomically consistent with the candidate config after
        this returns.
        """
        from eggpool.accounts.registry import (  # noqa: PLC0415
            account_config_rows,
        )
        from eggpool.db.repositories import (  # noqa: PLC0415
            AccountRepository,
            ProviderRepository,
        )

        db = self._process.db
        try:
            async with db.transaction():
                provider_repo = ProviderRepository(db)
                configured_providers = {
                    pid: {
                        "base_url": pcfg.base_url,
                        "protocols": pcfg.protocols,
                    }
                    for pid, pcfg in candidate_config.providers.items()
                }
                await provider_repo.sync_from_config(configured_providers)

                account_repo = AccountRepository(db)
                config_accounts = account_config_rows(candidate_config)
                await account_repo.sync_from_config(config_accounts)

        except Exception as exc:
            logger.exception("Persistence reconciliation failed")
            raise ReloadReconciliationError(
                f"Failed to reconcile persistence: {exc!r}"
            ) from exc

    async def _publish_generation(
        self,
        candidate: CandidateGeneration,
        diff: ConfigDiff,
    ) -> None:
        """Atomically publish the candidate generation.

        Delegates to ``RuntimeManager.install_candidate`` which swaps
        the active slot and begins retirement of the old generation.
        """
        try:
            active = self._runtime_manager.active_snapshot()
            await self._runtime_manager.install_candidate(
                candidate.generation,
                drain_timeout_s=self._drain_timeout_s,
                expected_active_generation_id=active.generation_id,
            )
        except Exception as exc:
            logger.exception("Generation publication failed")
            raise ReloadCommitError(f"Failed to publish generation: {exc!r}") from exc


__all__ = [
    "CandidateGeneration",
    "ReloadCommitError",
    "ReloadInProgressError",
    "ReloadManager",
    "ReloadOperationResult",
    "ReloadOperationStage",
    "ReloadOperationState",
    "ReloadPreparationError",
    "ReloadReconciliationError",
]
