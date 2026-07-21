"""Transaction manager for live configuration rehash (Phases C + 6).

Orchestrates the complete reload flow as an application-level
transaction with prepared deltas, a narrow commit point, and defined
rollback or completion behavior.

Design principles
-----------------

- One lock serializes complete reload transactions.
- Concurrent commands are rejected with ``reload_in_progress``.
- No secrets in logs, events, or diagnostics.
- All failures before publication are rollback/fail-closed.
- Post-publication failures have tested completion or compensation
  paths (Phase 6).
- The ``_build_candidate_generation`` method mirrors the service
  construction from ``app._lifespan_runtime`` but uses the candidate
  config and shares process-owned resources.
- Process-supervisor task reconfiguration (``apply_spec_diff``) is
  deferred to the commit phase, after publication, to avoid leaving
  the process supervisor in a partially-reconfigured state on
  candidate build or persistence reconciliation failures.
- The :class:`ReloadTransaction` state machine tracks every state
  transition for observability and fault-injection testing.
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
from eggpool.reload_transaction import (
    PersistenceDelta,
    ProcessTransitionPlan,
    ReloadTransaction,
    TaskSpecTransition,
    TransactionState,
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
        #: Signaled when a reload transaction reaches a terminal state
        #: (completed, aborted, or compensation_failed).  Shutdown uses
        #: this to wait for an in-flight transaction before closing
        #: process-owned dependencies.
        self._transaction_complete_event: asyncio.Event = asyncio.Event()
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
        #: Current transaction (Phase 6) — ``None`` when idle.
        self._current_transaction: ReloadTransaction | None = None

    @property
    def operation_state(self) -> ReloadOperationState | None:
        """Return the current reload operation state for diagnostics."""
        return self._operation_state

    @property
    def active_transaction(self) -> ReloadTransaction | None:
        """Return the current reload transaction for diagnostics."""
        return self._current_transaction

    async def wait_for_transaction_completion(
        self,
        *,
        timeout_s: float = 30.0,
    ) -> bool:
        """Wait for an active reload transaction to reach a terminal state.

        Called by shutdown to ensure no in-flight commit is interrupted
        before closing process-owned dependencies.

        Returns ``True`` if a transaction completed within the timeout,
        ``False`` if no transaction was active or the timeout elapsed.
        """
        if self._current_transaction is None:
            return True
        try:
            await asyncio.wait_for(
                self._transaction_complete_event.wait(),
                timeout=timeout_s,
            )
            return True
        except TimeoutError:
            logger.warning(
                "Timed out waiting for reload transaction completion (timeout=%.1fs)",
                timeout_s,
            )
            return False

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
        # Phase 6: surface transaction state when active.
        if self._current_transaction is not None:
            result["active_transaction"] = self._current_transaction.snapshot()
        else:
            result["active_transaction"] = None
        return result

    # -- public entry point ------------------------------------------------

    async def reload(
        self,
        validation: ConfigValidationResult,
        *,
        expected_digest: str | None = None,
    ) -> ReloadResult:
        """Execute a complete reload transaction.

        Phase 6 transactional flow:

        1.  Acquire reload lock (reject if already in progress).
        2.  Validate digest matches.
        3.  Compute diff against active generation.
        4.  Check for restart-required changes (reject if any).
        5.  Handle semantic no-op (return success).
        6.  Build candidate generation (off to the side, no process
            supervisor mutation).
        7.  Prepare persistence delta (calculate, don't commit).
        8.  Prepare process transitions (calculate specs, don't apply).
        9.  Pre-commit verification (revalidate active generation).
        10. Commit: apply persistence delta → publish → apply process
            transitions → mark completed.
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
        process_transition_plan: ProcessTransitionPlan | None = None

        # Phase 6: create the transaction to track state.
        txn = ReloadTransaction(
            request_id=f"reload-{int(started_at * 1000)}",
            validation=validation,
            expected_digest=expected_digest,
        )
        self._current_transaction = txn

        # Atomic admission claim — no TOCTOU window.
        async with self._claim_mutex:
            if self._reload_claimed:
                self._current_transaction = None
                raise ReloadInProgressError(
                    "A reload transaction is already in progress"
                )
            self._reload_claimed = True
            self._admitted_at = time.monotonic()
            # Reset the completion event so shutdown waiters see a fresh
            # signal for this new transaction.
            self._transaction_complete_event.clear()

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
            txn.mark_validated()

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
                txn.mark_aborting(
                    RuntimeError(
                        f"{len(restart_required)} restart-required field(s) changed"
                    )
                )
                txn.mark_aborted()
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
                txn.mark_diffed(
                    diff,
                    changed_sections=(),
                    restart_required=(),
                )
                txn.mark_aborting(RuntimeError("No changes"))
                txn.mark_aborted()
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
                txn.mark_diffed(
                    diff,
                    changed_sections=ignored_sections,
                    restart_required=(),
                )
                txn.mark_aborting(RuntimeError("All changes ignored"))
                txn.mark_aborted()
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
            txn.mark_diffed(
                diff,
                changed_sections=changed_sections,
                restart_required=(),
            )

            # Stage 5: Build candidate generation (no process supervisor mutation)
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
            txn.mark_candidate_prepared(candidate, generation_id)

            # Observer: candidate complete
            await self._observer.on_candidate_complete(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 6: Prepare persistence delta (calculate, don't commit yet)
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
            persistence_delta = self._prepare_persistence_delta(validation.config)
            txn.mark_persistence_prepared(persistence_delta)
            # Observer: reconcile prepared
            await self._observer.on_reconcile_prepared(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # Stage 7: Prepare process transitions (calculate specs, don't apply)
            process_transition_plan = self._prepare_process_transitions(
                validation.config,
                runtime_manager=self._runtime_manager,
            )
            txn.mark_process_transitions_prepared(process_transition_plan)

            # Capture pre-commit snapshot for revalidation
            pre_commit_active = self._runtime_manager.active_snapshot()
            expected_gen_id = pre_commit_active.generation_id
            expected_digest = pre_commit_active.config_digest

            # Stage 8: Pre-commit verification
            await self._pre_commit_verification(
                txn,
                expected_generation_id=expected_gen_id,
                expected_digest=expected_digest,
            )

            # Stage 9: Commit (narrow commit guard)
            self._set_stage(
                ReloadOperationStage.COMMIT,
                started_at,
                generation_id,
                digest_prefix,
            )
            # Capture old generation ID before publication swaps it
            old_generation_id = expected_gen_id
            txn.mark_commit_started(old_generation_id)

            # Observer: publish started
            await self._observer.on_publish_started(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # 9a: Apply persistence delta in a SQLite transaction
            await self._apply_persistence_delta(persistence_delta)

            # 9b: Publish candidate generation atomically
            await self._publish_generation(candidate, diff)
            published_gen = candidate._built_generation  # pyright: ignore[reportPrivateUsage]
            assert published_gen is not None, "Generation must be built before publish"
            txn.mark_runtime_published(published_gen)

            # Observer: publish complete
            await self._observer.on_publish_complete(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # 9c: Apply process transitions (after publication)
            await self._apply_process_transitions(process_transition_plan)
            txn.mark_process_transitions_applied()

            # 9d: Mark persistence committed (SQLite already committed in 9a)
            txn.mark_persistence_committed()

            # 9e: Update observable state (no-op currently; placeholder
            #     for Phase 7 effective-config mechanism)
            txn.mark_observable_state_updated()

            # Stage 10: Begin retirement (non-blocking)
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
            txn.mark_retirement_scheduled()
            txn.mark_completed()

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
            # Phase 6: transition transaction to aborting/aborted.
            txn.mark_aborting(exc)
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
            txn.mark_aborted()
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
            # Phase 6: if we haven't published yet, cancellation is safe.
            # If we have published, shield the commit to completion.
            if txn.is_committing:
                # Publication already happened — shield remaining commit
                # work to avoid leaving mixed state.
                logger.warning(
                    "Reload cancelled during commit for generation %d; "
                    "shielding remaining commit work",
                    generation_id,
                )
                try:
                    # Best-effort: apply remaining process transitions
                    if txn.state == TransactionState.RUNTIME_PUBLISHED:
                        await self._apply_process_transitions(
                            process_transition_plan  # type: ignore[arg-type]
                        )
                        txn.mark_process_transitions_applied()
                    if txn.state == TransactionState.PROCESS_TRANSITIONS_APPLIED:
                        txn.mark_persistence_committed()
                    if txn.state == TransactionState.PERSISTENCE_COMMITTED:
                        txn.mark_observable_state_updated()
                    if txn.state == TransactionState.OBSERVABLE_STATE_UPDATED:
                        txn.mark_retirement_scheduled()
                    if txn.state == TransactionState.RETIREMENT_SCHEDULED:
                        txn.mark_completed()
                except Exception:
                    logger.exception("Failed to complete commit after cancellation")
                    txn.mark_aborting(
                        RuntimeError("Commit completion failed after cancellation")
                    )
            else:
                txn.mark_aborting(RuntimeError("Reload cancelled before commit point"))

            self._last_reload_result = ReloadOperationResult(
                ok=txn.state == TransactionState.COMPLETED,
                stage=error_stage,
                generation=generation_id,
                changed_sections=changed_sections,
                warnings=warnings,
                restart_required=restart_required,
                retirement_pending=txn.state == TransactionState.COMPLETED,
                message=(
                    "Reload completed despite cancellation"
                    if txn.state == TransactionState.COMPLETED
                    else f"Reload cancelled at stage {error_stage}"
                ),
                duration_s=duration,
            )
            self._last_reload_completed_at = time.time()
            # Shield candidate abort so bounded cleanup completes
            # before the cancellation propagates.
            if candidate is not None and not txn.is_committing:
                try:
                    diag = await asyncio.shield(
                        candidate.abort(
                            cause=asyncio.CancelledError(),
                            failure_stage=error_stage,
                        ),
                    )
                    self._last_cleanup_diagnostics = diag
                except asyncio.CancelledError:
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
            if txn.state != TransactionState.COMPLETED:
                txn.mark_aborted()
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
            # Phase 6: if we haven't published, abort cleanly.
            # If we have published, attempt compensation.
            if txn.state == TransactionState.RUNTIME_PUBLISHED:
                # Publication succeeded but a post-publication step failed.
                # Compensate by accepting the new generation and retrying
                # process transitions if needed.
                logger.warning(
                    "Post-publication failure for generation %d; "
                    "attempting compensation",
                    generation_id,
                )
                compensation_ok = await self._compensate_post_publication(
                    txn,
                    exc,
                    process_transition_plan=process_transition_plan,
                )
                if compensation_ok:
                    txn.mark_process_transitions_applied()
                    txn.mark_persistence_committed()
                    txn.mark_observable_state_updated()
                    txn.mark_retirement_scheduled()
                    txn.mark_completed()
                else:
                    txn.mark_aborting(exc)
                    txn.mark_compensation_failed()
            elif txn.state in (
                TransactionState.COMMIT_STARTED,
                TransactionState.PROCESS_TRANSITIONS_APPLIED,
                TransactionState.PERSISTENCE_COMMITTED,
                TransactionState.OBSERVABLE_STATE_UPDATED,
                TransactionState.RETIREMENT_SCHEDULED,
            ):
                # Publication or pre-publication failure — abort.
                txn.mark_aborting(exc)
            else:
                txn.mark_aborting(exc)
            # Abort the candidate if it exists and hasn't been
            # transferred to the runtime manager.  Shield the abort
            # so bounded cleanup completes even under cancellation.
            # Skip when compensation succeeded (candidate was transferred).
            compensation_succeeded = txn.state == TransactionState.COMPLETED
            if (
                candidate is not None
                and not txn.is_committing
                and not compensation_succeeded
            ):
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
                            candidate_diag = getattr(candidate, "diagnostics", None)
                            if candidate_diag is not None:
                                self._last_cleanup_diagnostics = candidate_diag
                    else:
                        candidate_diag = getattr(candidate, "diagnostics", None)
                        if candidate_diag is not None:
                            self._last_cleanup_diagnostics = candidate_diag
            ok = txn.state == TransactionState.COMPLETED
            self._last_reload_result = ReloadOperationResult(
                ok=ok,
                stage=error_stage,
                generation=generation_id,
                changed_sections=changed_sections,
                warnings=warnings,
                restart_required=restart_required,
                retirement_pending=ok,
                message=(
                    "Reload compensated after post-publication failure"
                    if ok
                    else f"Reload failed: {exc!r}"
                ),
                duration_s=duration,
            )
            self._last_reload_completed_at = time.time()
            event_type = "reload_preparation_failure"
            if error_stage == ReloadOperationStage.RECONCILIATION:
                event_type = "reload_reconciliation_failure"
            if txn.is_committing:
                event_type = "reload_post_publication_failure"
            await self._safe_record_event(
                event_type,
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                error=f"{exc!r}",
            )
            if not ok:
                return ReloadResult(
                    ok=False,
                    stage=ReloadStage.VALIDATION,
                    generation=None,
                    changed_sections=(),
                    warnings=warnings,
                    restart_required=(),
                    message=f"Reload failed: {exc!r}",
                )
            return ReloadResult(
                ok=True,
                stage=ReloadStage.RETIREMENT,
                generation=generation_id,
                changed_sections=changed_sections,
                warnings=warnings,
                restart_required=(),
                message="Reload compensated after post-publication failure",
            )
        finally:
            # Signal transaction completion before clearing the reference
            # so shutdown waiters are notified while the transaction is
            # still accessible for diagnostics.
            self._transaction_complete_event.set()
            # Release admission claim on every terminal path.
            self._current_transaction = None
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

        Does NOT reconfigure the process supervisor — task reconfiguration
        is deferred to the commit phase (Phase 6) to avoid leaving the
        process supervisor in a partially-reconfigured state on failure.

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

            # Phase 6: process_supervisor.apply_spec_diff is NOT called here.
            # Task reconfiguration is prepared during _prepare_process_transitions
            # and applied during the commit phase, after publication.

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

    # -- Phase 6: prepared deltas -------------------------------------------

    def _prepare_persistence_delta(
        self,
        candidate_config: AppConfig,
    ) -> PersistenceDelta:
        """Calculate persistence changes without applying them.

        Returns an immutable :class:`PersistenceDelta` that the commit
        step applies inside a SQLite transaction.
        """
        from eggpool.accounts.registry import (  # noqa: PLC0415
            account_config_rows,
        )

        configured_providers = {
            pid: {
                "base_url": pcfg.base_url,
                "protocols": pcfg.protocols,
            }
            for pid, pcfg in candidate_config.providers.items()
        }
        config_accounts = account_config_rows(candidate_config)
        return PersistenceDelta(
            configured_providers=configured_providers,
            config_accounts=tuple(config_accounts),
        )

    def _prepare_process_transitions(
        self,
        candidate_config: AppConfig,
        *,
        runtime_manager: RuntimeManager | None = None,
    ) -> ProcessTransitionPlan:
        """Calculate process-supervisor task specs without applying them.

        Returns a :class:`ProcessTransitionPlan` that the commit step
        applies after publication.
        """
        process = self._process
        process_supervisor = process.process_supervisor
        if process_supervisor is None:
            return ProcessTransitionPlan(
                task_specs=(),
                callback_factories={},
                transitions=(),
            )

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
        return ProcessTransitionPlan(
            task_specs=candidate_specs,
            callback_factories=callback_factories,
            transitions=(
                TaskSpecTransition(
                    process_supervisor=process_supervisor,
                    candidate_specs=candidate_specs,
                    callback_factories=callback_factories,
                    process=process,
                ),
            ),
        )

    async def _apply_persistence_delta(self, delta: PersistenceDelta) -> None:
        """Apply a prepared persistence delta inside a SQLite transaction."""
        from eggpool.db.repositories import (  # noqa: PLC0415
            AccountRepository,
            ProviderRepository,
        )

        db = self._process.db
        try:
            async with db.transaction():
                provider_repo = ProviderRepository(db)
                await provider_repo.sync_from_config(delta.configured_providers)

                account_repo = AccountRepository(db)
                await account_repo.sync_from_config(list(delta.config_accounts))
        except Exception as exc:
            logger.exception("Persistence delta application failed")
            raise ReloadReconciliationError(
                f"Failed to apply persistence delta: {exc!r}"
            ) from exc

    async def _apply_process_transitions(
        self,
        plan: ProcessTransitionPlan,
    ) -> None:
        """Apply prepared process transitions.

        Called after publication so the process supervisor is only
        reconfigured when the new generation is already live.
        Each transition's ``apply()`` method is called in order.
        """
        process_supervisor = self._process.process_supervisor
        if process_supervisor is None or not plan.task_specs:
            return

        for transition in plan.transitions:
            await transition.apply()

    async def _pre_commit_verification(
        self,
        txn: ReloadTransaction,
        *,
        expected_generation_id: int,
        expected_digest: str | None,
    ) -> None:
        """Verify preconditions immediately before commit.

        Checks that:
        - The active generation still matches the expected ID and digest.
        - The process is not shutting down.
        - The candidate remains prepared and open.
        - No restart-required change slipped through.

        This is the pre-commit gate described in the plan: if the active
        generation changed during candidate preparation (e.g. a
        concurrent reload completed), the commit must not proceed because
        our persistence delta and process-transition plan are based on
        stale state.
        """
        if self._runtime_manager._shutdown_in_progress:  # pyright: ignore[reportPrivateUsage]
            raise ReloadPreparationError(
                "Process is shutting down; reload cannot proceed"
            )

        # Revalidate the active generation ID — a concurrent reload
        # may have advanced it during candidate construction.
        try:
            active = self._runtime_manager.active_snapshot()
        except Exception:
            raise ReloadPreparationError(
                "No active runtime generation; cannot verify pre-commit state"
            ) from None

        if active.generation_id != expected_generation_id:
            raise ReloadPreparationError(
                "Active generation changed during candidate preparation; "
                f"expected {expected_generation_id}, "
                f"found {active.generation_id}"
            )

        # Digest verification (when caller supplies an expected digest).
        if expected_digest is not None and active.config_digest != expected_digest:
            raise ReloadPreparationError(
                "Active generation digest changed during candidate preparation; "
                f"expected {expected_digest[:12]}\u2026, "
                f"found {active.config_digest[:12]}\u2026"
            )

        candidate = txn.candidate
        if candidate is not None:
            from eggpool.runtime_manager import (  # noqa: PLC0415
                CandidateOwnershipState,
            )

            state = getattr(candidate, "ownership_state", None)
            if state in (
                CandidateOwnershipState.TRANSFERRED,
                CandidateOwnershipState.ABORTED,
            ):
                raise ReloadPreparationError(
                    f"Candidate is in unexpected state: {state.value}"
                )

        if txn.restart_required:
            raise ReloadPreparationError(
                "Restart-required changes detected during pre-commit verification"
            )

    async def _compensate_post_publication(
        self,
        txn: ReloadTransaction,
        exc: Exception,
        *,
        process_transition_plan: ProcessTransitionPlan | None = None,
    ) -> bool:
        """Attempt to compensate for a post-publication failure.

        After publication, the new generation is live and accepting
        leases.  We cannot roll it back.  The compensation strategy is:

        1. If the failure was in process transitions, retry applying
           them — the process supervisor can safely reconfigure after
           publication.
        2. Accept the new generation regardless — the persistence delta
           is idempotent and will be re-synced on the next reload.

        Returns ``True`` if compensation succeeded (new generation is
        accepted as the current state and process transitions were
        applied), ``False`` if compensation itself failed (operator
        intervention required but the new generation is still live).
        """
        logger.warning(
            "Post-publication compensation for generation %d: %s",
            txn.generation_id,
            exc,
        )

        compensation_ok = True

        # Retry process transitions if they haven't been applied yet.
        if (
            process_transition_plan is not None
            and txn.state == TransactionState.RUNTIME_PUBLISHED
        ):
            try:
                await self._apply_process_transitions(process_transition_plan)
                logger.info(
                    "Post-publication compensation: process transitions "
                    "applied successfully for generation %d",
                    txn.generation_id,
                )
            except Exception as retry_exc:
                logger.exception(
                    "Post-publication compensation: process transition "
                    "retry failed for generation %d",
                    txn.generation_id,
                )
                compensation_ok = False
                await self._safe_record_event(
                    "reload_post_publication_compensation_failure",
                    generation_id=txn.generation_id,
                    digest_prefix=txn.digest_prefix,
                    error=f"{retry_exc!r}",
                )

        await self._safe_record_event(
            "reload_post_publication_compensation",
            generation_id=txn.generation_id,
            digest_prefix=txn.digest_prefix,
            error=f"{exc!r}",
            compensation_ok=compensation_ok,
        )
        return compensation_ok

    # -- backward-compatible persistence reconciliation ---------------------

    async def _reconcile_persistence(
        self,
        candidate_config: AppConfig,
        active_config: AppConfig,  # noqa: ARG002 — kept for backward compat
    ) -> None:
        """Sync providers and accounts from candidate config to SQLite.

        Deprecated: prefer :meth:`_prepare_persistence_delta` +
        :meth:`_apply_persistence_delta` for the Phase 6 transactional
        flow.  This wrapper is retained for backward compatibility with
        tests that patch it directly.
        """
        if self.TEST_INJECT_RECONCILE_FAILURE is not None:
            raise self.TEST_INJECT_RECONCILE_FAILURE
        delta = self._prepare_persistence_delta(candidate_config)
        await self._apply_persistence_delta(delta)

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
    # Phase 6 transaction types (re-exported for convenience)
    "PersistenceDelta",
    "ProcessTransitionPlan",
    "ReloadTransaction",
    "TransactionState",
]
