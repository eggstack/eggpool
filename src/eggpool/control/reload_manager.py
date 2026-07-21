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
import os
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
    from eggpool.config_validation import (
        ConfigValidationResult,
        ConfigValidationWarning,
    )
    from eggpool.models.config import AppConfig
    from eggpool.runtime_manager import (
        CleanupDiagnostics,
        ProcessRuntime,
        RuntimeGeneration,
        RuntimeManager,
    )

from eggpool.runtime_manager import RuntimeGenerationCandidate

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
# Reload observer (test-stage-barrier hook)
# ---------------------------------------------------------------------------


class ReloadObserver:
    """No-op observer for reload pipeline stages.

    Subclass and override individual methods to intercept specific stages
    without modifying production code paths.  Every method is a no-op by
    default so attaching an observer has zero cost when no overrides are
    provided.

    Recommended stage order for barrier-based tests:

    1. ``on_admission_claimed``  — lock acquired
    2. ``on_validation_complete`` — digest validated
    3. ``on_diff_computed``      — config diff available
    4. ``on_candidate_started``  — before candidate construction
    5. ``on_candidate_complete`` — candidate ready
    6. ``on_reconcile_started``  — before DB reconciliation
    7. ``on_reconcile_prepared`` — DB transaction staged
    8. ``on_publish_started``    — before atomic publication
    9. ``on_publish_complete``   — generation published
    10. ``on_retirement_started`` — old generation draining
    11. ``on_retirement_complete`` — old generation closed
    """

    async def on_admission_claimed(
        self,
        *,
        generation_id: int | None,
        digest_prefix: str,
    ) -> None:
        """Called after the reload lock is acquired."""

    async def on_validation_complete(
        self,
        *,
        generation_id: int | None,
        digest_prefix: str,
    ) -> None:
        """Called after digest validation succeeds."""

    async def on_diff_computed(
        self,
        *,
        generation_id: int | None,
        digest_prefix: str,
        change_count: int,
        has_restart_required: bool,
    ) -> None:
        """Called after the config diff is computed."""

    async def on_candidate_started(
        self,
        *,
        generation_id: int | None,
        digest_prefix: str,
    ) -> None:
        """Called before candidate generation construction begins."""

    async def on_candidate_complete(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
    ) -> None:
        """Called after candidate generation construction succeeds."""

    async def on_reconcile_started(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
    ) -> None:
        """Called before persistence reconciliation."""

    async def on_reconcile_prepared(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
    ) -> None:
        """Called after the DB transaction is staged (before commit)."""

    async def on_publish_started(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
    ) -> None:
        """Called before atomic publication."""

    async def on_publish_complete(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
    ) -> None:
        """Called after generation publication succeeds."""

    async def on_retirement_started(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
        old_generation_id: int,
    ) -> None:
        """Called when old generation retirement begins."""

    async def on_retirement_complete(
        self,
        *,
        generation_id: int,
        old_generation_id: int,
    ) -> None:
        """Called when old generation retirement finishes."""


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
# Process-owned task callback factories for reload
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reload manager
# ---------------------------------------------------------------------------

DEFAULT_DRAIN_TIMEOUT_S: Final[float] = 300.0


def _resolve_drain_timeout_s() -> float:
    """Read ``$EGGPOOL_RELOAD_DRAIN_TIMEOUT_S`` if set, else default.

    Operators and tests can shorten the drain timeout (the time the
    runtime manager waits for in-flight leases to release before
    forcibly closing a retired generation) without touching config.
    """
    raw = os.environ.get("EGGPOOL_RELOAD_DRAIN_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_DRAIN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid EGGPOOL_RELOAD_DRAIN_TIMEOUT_S=%r; using default %.0fs",
            raw,
            DEFAULT_DRAIN_TIMEOUT_S,
        )
        return DEFAULT_DRAIN_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "Non-positive EGGPOOL_RELOAD_DRAIN_TIMEOUT_S=%r; using default %.0fs",
            raw,
            DEFAULT_DRAIN_TIMEOUT_S,
        )
        return DEFAULT_DRAIN_TIMEOUT_S
    return value


class ReloadManager:
    """Manages serialized live-reload transactions.

    One reload at a time.  Concurrent commands are rejected.
    Uses RuntimeManager for generation lifecycle.

    Test hook
    ---------
    ``preparation_event`` — when set to an :class:`asyncio.Event` instance,
    ``_build_candidate_generation`` awaits ``preparation_event.wait()``
    before constructing the candidate.  This lets tests deterministically
    hold a reload inside candidate preparation while a second concurrent
    reload is attempted.  Must be ``None`` in production (the default).
    """

    def __init__(
        self,
        runtime_manager: RuntimeManager,
        process: ProcessRuntime,
        *,
        drain_timeout_s: float | None = None,
        observer: ReloadObserver | None = None,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._process = process
        if drain_timeout_s is None:
            drain_timeout_s = _resolve_drain_timeout_s()
        self._drain_timeout_s = drain_timeout_s
        self._claim_mutex = asyncio.Lock()
        self._reload_claimed: bool = False
        self._admitted_request_id: str | None = None
        self._admitted_at: float | None = None
        self._operation_state: ReloadOperationState | None = None
        self._last_reload_result: ReloadOperationResult | None = None
        self._last_reload_completed_at: float | None = None
        self._reload_count: int = 0
        self._reload_error_count: int = 0
        #: Stage observer for test barriers — inert when ``None``.
        self._observer: ReloadObserver = observer or ReloadObserver()
        #: Test-only hook — see class docstring.
        self.preparation_event: asyncio.Event | None = None
        #: Test-only seam — when set to an exception instance,
        #: ``_build_candidate_generation`` raises it at entry.
        self.TEST_INJECT_BUILD_FAILURE: Exception | None = None
        #: Test-only seam — when set to an exception instance,
        #: ``_reconcile_persistence`` raises it at entry.
        self.TEST_INJECT_RECONCILE_FAILURE: Exception | None = None
        #: Test-only seam — when set to an exception instance,
        #: ``_publish_generation`` raises it at entry.
        self.TEST_INJECT_PUBLISH_FAILURE: Exception | None = None
        #: Last abort cleanup diagnostics from a failed reload.
        self._last_cleanup_diagnostics: CleanupDiagnostics | None = None

    @property
    def operation_state(self) -> ReloadOperationState | None:
        """Return the current reload operation state for diagnostics."""
        return self._operation_state

    def snapshot(self) -> dict[str, Any]:
        """Return reload state for diagnostics."""
        result: dict[str, Any] = {
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
            "admitted": self._reload_claimed,
            "admitted_at": self._admitted_at,
            "admitted_request_id": self._admitted_request_id,
        }
        # Surface last abort cleanup diagnostics when available.
        if self._last_cleanup_diagnostics is not None:
            d = self._last_cleanup_diagnostics
            result["last_cleanup_diagnostics"] = {
                "generation_id": d.generation_id,
                "ownership_state_at_failure": d.ownership_state_at_failure,
                "resource_types_registered": d.resource_types_registered,
                "resource_types_closed": d.resource_types_closed,
                "close_duration_s": d.close_duration_s,
                "close_errors": d.close_errors,
                "close_errors_by_type": d.close_errors_by_type,
                "timed_out": d.timed_out,
                "primary_failure": d.primary_failure,
                "primary_failure_stage": d.primary_failure_stage,
            }
        else:
            result["last_cleanup_diagnostics"] = None
        return result

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
        generation_id: int = 0
        changed_sections: tuple[str, ...] = ()
        warnings: tuple[ConfigValidationWarning, ...] = validation.warnings
        restart_required: tuple[Any, ...] = ()
        candidate: RuntimeGenerationCandidate | None = None

        # Atomic admission claim — no TOCTOU window.
        async with self._claim_mutex:
            if self._reload_claimed:
                raise ReloadInProgressError(
                    "A reload transaction is already in progress"
                )
            self._reload_claimed = True
            self._admitted_at = time.monotonic()

        try:
            # Record reload_requested event after claim succeeds.
            await self._safe_record_event(
                "reload_requested",
                digest_prefix=digest_prefix,
            )

            # Observer: admission claimed
            await self._observer.on_admission_claimed(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 1: Validate digest
            self._set_stage(
                ReloadOperationStage.VALIDATION,
                started_at,
                generation_id,
                digest_prefix,
            )
            await self._validate_digest(validation, expected_digest)

            # Observer: validation complete
            await self._observer.on_validation_complete(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 2: Compute diff
            self._set_stage(
                ReloadOperationStage.DIFF,
                started_at,
                generation_id,
                digest_prefix,
            )
            diff = await self._compute_reload_diff(validation.config)

            # Observer: diff computed
            await self._observer.on_diff_computed(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                change_count=len(diff.changes),
                has_restart_required=bool(diff.restart_required),
            )

            # Stage 3: Check restart-required changes
            restart_required = tuple(diff.restart_required)
            if restart_required:
                sections = tuple(sorted({c.section for c in restart_required}))
                await self._safe_record_event(
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
            # Observer: candidate started
            await self._observer.on_candidate_started(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )
            candidate = await self._build_candidate_generation(
                validation,
                diff,
                runtime_manager=self._runtime_manager,
            )
            # Extract generation metadata from the candidate.
            # RuntimeGenerationCandidate stores it on _built_generation;
            # CandidateGeneration (backward compat) stores it on .generation.
            _gen = getattr(candidate, "_built_generation", None) or getattr(  # pyright: ignore[reportPrivateUsage]
                candidate, "generation", None
            )
            generation_id = (
                _gen.generation_id if _gen is not None else candidate.generation_id
            )
            if _gen is not None:
                digest_prefix = (
                    _gen.config_digest[:12] if _gen.config_digest else "<empty>"
                )

            # Observer: candidate complete
            await self._observer.on_candidate_complete(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 6: Reconcile persistence
            self._set_stage(
                ReloadOperationStage.RECONCILIATION,
                started_at,
                generation_id,
                digest_prefix,
            )
            # Observer: reconcile started
            await self._observer.on_reconcile_started(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )
            await self._reconcile_persistence(
                validation.config,
                self._runtime_manager.active_snapshot().config,
            )
            # Observer: reconcile prepared
            await self._observer.on_reconcile_prepared(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 7: Atomic publication
            self._set_stage(
                ReloadOperationStage.COMMIT,
                started_at,
                generation_id,
                digest_prefix,
            )
            # Capture old generation ID before publication swaps it
            old_generation_id = self._runtime_manager.active_snapshot().generation_id
            # Observer: publish started
            await self._observer.on_publish_started(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )
            await self._publish_generation(candidate, diff)
            # Observer: publish complete
            await self._observer.on_publish_complete(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 8: Begin retirement (non-blocking)
            self._set_stage(
                ReloadOperationStage.RETIREMENT,
                started_at,
                generation_id,
                digest_prefix,
            )
            # Observer: retirement started
            await self._observer.on_retirement_started(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                old_generation_id=old_generation_id,
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
            await self._safe_record_event(
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
            # Capture abort diagnostics from the candidate if available.
            candidate_diag = getattr(candidate, "diagnostics", None)
            if candidate_diag is not None:
                self._last_cleanup_diagnostics = candidate_diag
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
            await self._safe_record_event(
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
        except asyncio.CancelledError:
            duration = time.monotonic() - started_at
            error_stage = (
                self._operation_state.stage
                if self._operation_state
                else ReloadOperationStage.IDLE
            )
            logger.warning("Reload cancelled at stage %s", error_stage)
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
                message=f"Reload cancelled at stage {error_stage}",
                duration_s=duration,
            )
            self._last_reload_completed_at = time.time()
            # Shield candidate abort so bounded cleanup completes
            # before the cancellation propagates.
            if candidate is not None:
                try:
                    diag = await asyncio.shield(
                        candidate.abort(
                            cause=asyncio.CancelledError(),
                            failure_stage=error_stage,
                        ),
                    )
                    self._last_cleanup_diagnostics = diag
                except asyncio.CancelledError:
                    # Shield itself was cancelled — abort is
                    # idempotent so a subsequent call will retry.
                    logger.warning(
                        "Candidate abort shield cancelled for generation %d",
                        generation_id,
                    )
            await self._safe_record_event(
                "reload_cancelled",
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                error=f"cancelled at {error_stage}",
            )
            raise
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
            # Abort the candidate if it exists and hasn't been
            # transferred to the runtime manager.  Shield the abort
            # so bounded cleanup completes even under cancellation.
            if candidate is not None:
                candidate_abort = getattr(candidate, "abort", None)
                if candidate_abort is not None:
                    candidate_state = getattr(candidate, "ownership_state", None)
                    from eggpool.runtime_manager import (  # noqa: PLC0415
                        CandidateOwnershipState,
                    )

                    if candidate_state not in (
                        CandidateOwnershipState.TRANSFERRED,
                        CandidateOwnershipState.ABORTED,
                    ):
                        try:
                            diag = await asyncio.shield(
                                candidate_abort(
                                    cause=exc,
                                    failure_stage=error_stage,
                                ),
                            )
                            self._last_cleanup_diagnostics = diag
                        except asyncio.CancelledError:
                            logger.warning(
                                "Candidate abort shield cancelled for generation %d",
                                generation_id,
                            )
                            # Abort is idempotent; diagnostics may be
                            # available from a concurrent call.
                            candidate_diag = getattr(candidate, "diagnostics", None)
                            if candidate_diag is not None:
                                self._last_cleanup_diagnostics = candidate_diag
                    else:
                        candidate_diag = getattr(candidate, "diagnostics", None)
                        if candidate_diag is not None:
                            self._last_cleanup_diagnostics = candidate_diag
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
            await self._safe_record_event(
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
        finally:
            # Release admission claim on every terminal path.
            async with self._claim_mutex:
                self._reload_claimed = False
                self._admitted_at = None
                self._admitted_request_id = None

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
        from eggpool.config_reload_policy import (
            sanitize_text_for_audit,  # noqa: PLC0415
        )
        from eggpool.db.repositories import (  # noqa: PLC0415
            OperationalEventRepository,
        )

        details: dict[str, Any] = {}
        if generation_id is not None:
            details["generation_id"] = generation_id
        if digest_prefix:
            details["digest_prefix"] = digest_prefix
        if changed_sections:
            details["changed_sections"] = list(changed_sections)
        if error:
            details["error"] = sanitize_text_for_audit(error)
        try:
            repo = OperationalEventRepository(self._process.db)
            await repo.record(event_type, details)
        except Exception:
            logger.debug(
                "Failed to record operational event %s", event_type, exc_info=True
            )

    async def _safe_record_event(
        self,
        event_type: str,
        **kwargs: Any,
    ) -> None:
        """Record an event, swallowing failures so they never break reload.

        Event-recording failures are logged at DEBUG level and are
        non-fatal — the reload transaction must proceed regardless.
        """
        try:
            await self._record_event(event_type, **kwargs)
        except Exception:
            logger.debug(
                "Event recording failed for %s (non-fatal)", event_type, exc_info=True
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
    ) -> RuntimeGenerationCandidate:
        """Construct all generation-owned services for the candidate config.

        Mirrors the service construction from ``app._lifespan_runtime``
        but uses the candidate config and shares process-owned resources
        (db, stats_db, config_path, metrics_coalescer).

        Does NOT perform startup-only operations: migrations, crash
        recovery, catalog staleness enforcement, or initial catalog
        refresh.  Those are startup concerns only.

        Each resource is registered on the candidate container
        immediately after construction.  Any failure aborts the
        candidate, closing all registered resources in reverse order.

        If ``preparation_event`` is set, awaits it before proceeding so
        tests can deterministically hold this method mid-flight.
        """

        candidate_config = validation.config
        process = self._process
        generation_id = self._runtime_manager.reserve_next_generation_id()

        candidate = RuntimeGenerationCandidate(generation_id=generation_id)

        if self.preparation_event is not None:
            await self.preparation_event.wait()

        try:
            if self.TEST_INJECT_BUILD_FAILURE is not None:
                raise self.TEST_INJECT_BUILD_FAILURE

            from eggpool.generation_factory import (
                RuntimeGenerationFactory,  # noqa: PLC0415
            )

            factory = RuntimeGenerationFactory()
            gen_result = await factory.prepare(
                config=candidate_config,
                config_digest=validation.content_digest,
                generation_id=generation_id,
                process=process,
                candidate=candidate,
                runtime_manager=runtime_manager,
            )

            # -- Reconfigure tasks on the process supervisor
            process_supervisor = process.process_supervisor
            if process_supervisor is not None:
                from eggpool.runtime_tasks import (  # noqa: PLC0415
                    TaskRegistrationContext,
                    build_callback_factories_for_specs,
                    build_task_specs,
                )

                candidate_specs = build_task_specs(
                    TaskRegistrationContext(
                        process=process,
                        runtime_manager=runtime_manager,  # type: ignore[arg-type]
                        config=candidate_config,
                        update_checker_outbound=None,
                        process_supervisor=process_supervisor,
                    )
                )
                callback_factories = build_callback_factories_for_specs(
                    candidate_specs,
                    process=process,
                    runtime_manager=runtime_manager,
                    config=candidate_config,
                )
                await process_supervisor.apply_spec_diff(
                    candidate_specs,
                    callback_factories=callback_factories,
                    process=process,
                )

            # Mark candidate prepared and store the generation + process
            candidate.mark_prepared()
            candidate._built_generation = gen_result.generation  # pyright: ignore[reportPrivateUsage]
            candidate._process_ref = process  # pyright: ignore[reportPrivateUsage]
            candidate._diff_ref = diff  # pyright: ignore[reportPrivateUsage]

            return candidate

        except Exception:
            logger.exception(
                "Candidate generation construction failed; aborting reload"
            )
            await candidate.abort(
                cause=RuntimeError("Candidate generation construction failed"),
                failure_stage="build",
            )
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
        if self.TEST_INJECT_RECONCILE_FAILURE is not None:
            raise self.TEST_INJECT_RECONCILE_FAILURE
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
        candidate: CandidateGeneration | RuntimeGenerationCandidate,
        diff: ConfigDiff,
    ) -> None:
        """Atomically publish the candidate generation.

        Delegates to ``RuntimeManager.install_candidate`` which swaps
        the active slot and begins retirement of the old generation.
        After successful publication, transfers ownership from the
        candidate to the runtime manager.
        """
        try:
            if self.TEST_INJECT_PUBLISH_FAILURE is not None:
                raise self.TEST_INJECT_PUBLISH_FAILURE
            # Support both old CandidateGeneration.generation and
            # new RuntimeGenerationCandidate._built_generation.
            generation = getattr(candidate, "generation", None) or getattr(
                candidate,
                "_built_generation",
                None,  # pyright: ignore[reportPrivateUsage]
            )
            if generation is None:
                raise ReloadCommitError(
                    "Candidate has no generation; was mark_prepared() called?"
                )
            active = self._runtime_manager.active_snapshot()
            await self._runtime_manager.install_candidate(
                generation,
                drain_timeout_s=self._drain_timeout_s,
                expected_active_generation_id=active.generation_id,
            )
            # Transfer ownership: candidate abort is now a no-op.
            transfer_fn = getattr(candidate, "transfer_to_runtime_manager", None)
            if transfer_fn is not None:
                transfer_fn()
        except Exception as exc:
            logger.exception("Generation publication failed")
            raise ReloadCommitError(f"Failed to publish generation: {exc!r}") from exc


__all__ = [
    "CandidateGeneration",
    "ReloadCommitError",
    "ReloadInProgressError",
    "ReloadManager",
    "ReloadObserver",
    "ReloadOperationResult",
    "ReloadOperationStage",
    "ReloadOperationState",
    "ReloadPreparationError",
    "ReloadReconciliationError",
]
