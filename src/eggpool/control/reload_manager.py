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
import collections
import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from eggpool.config_reload_policy import (
    ConfigDiff,
    ReloadResult,
    ReloadStage,
    compute_diff,
)
from eggpool.config_validation import ConfigValidationWarning
from eggpool.reload_diagnostics import (
    ReloadCounters,
    ReloadDiagnosticResult,
    ReloadResultCategory,
    ReloadRetirementStatus,
    ReloadTerminalStage,
    classify_result_category,
    stage_from_error_class,
)
from eggpool.reload_transaction import (
    AcceptedReloadFinalization,
    EffectiveStateTransition,
    PersistenceDelta,
    ProcessTransition,
    ProcessTransitionApplyError,
    ProcessTransitionPlan,
    ReloadAcceptanceState,
    ReloadTransaction,
    RoutingTraceGuardTransition,
    RoutingTraceWriterTransition,
    TaskSpecTransition,
    TransactionState,
    TransactionStateError,
    TransitionApplyResult,
    TransitionRollbackOutcome,
)

if TYPE_CHECKING:
    from eggpool.config_validation import (
        ConfigValidationResult,
    )
    from eggpool.models.config import AppConfig
    from eggpool.runtime_manager import (
        CleanupDiagnostics,
        ProcessRuntime,
        RuntimeGeneration,
        RuntimeManager,
    )

from eggpool.control.accepted_finalization import (
    FINALIZATION_HISTORY_MAX,
    AcceptedFinalizationOutcome,
    AcceptedFinalizationRecord,
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
    FinalizationStatus,
)
from eggpool.errors import AcceptedFinalizationInvariantError
from eggpool.reload_transaction import preflight_all_transitions
from eggpool.runtime_manager import (
    RuntimeGenerationCandidate,
    RuntimeManagerSwapStateError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReloadInProgressError(Exception):
    """Raised when a reload is attempted while another is in progress."""


@dataclass(frozen=True)
class _PreparedSwap:
    """State captured between prepare / commit / finalize phases.

    The publication pipeline is split into three phases to keep the
    ``prepare_swap`` / ``commit_publication`` / ``finalize_retirement``
    transitions auditable individually:

    - :meth:`ReloadManager._prepare_swap` populates this record.
    - :meth:`ReloadManager._commit_publication` consumes it to swap
      the active slot.
    - :meth:`ReloadManager._finalize_retirement_handling` consumes it
      to transfer ownership and mirror onto ``app.state``.

    Splitting the phases lets the transaction state machine record
    publication facts as soon as each step succeeds, even if a later
    step fails.  See the "prepared-swap protocol" section of
    :mod:`architecture.reload`.
    """

    candidate: object
    generation: RuntimeGeneration
    active_generation_id: int
    drain_timeout_s: float


@dataclass(frozen=True)
class AcceptedCommitContext:
    """Immutable state handed from the commit boundary to finalization."""

    transaction: ReloadTransaction
    candidate: RuntimeGenerationCandidate
    pending_swap: Any
    transition_result: TransitionApplyResult | None
    published_generation: RuntimeGeneration
    old_generation_id: int | None
    generation_id: int
    changed_sections: tuple[str, ...]
    started_at: float
    digest_prefix: str


@dataclass(frozen=True)
class ReloadShutdownPreparation:
    """Explicit ownership decision made before runtime shutdown."""

    transaction_wait_completed: bool
    unresolved_jobs: int
    adopted_jobs: int
    active_transaction_state: str | None
    ownership_safe_for_runtime_shutdown: bool


@dataclass(frozen=True)
class PrecommitAbortOutcome:
    """Structured result of a precommit cleanup operation.

    Plan 017 Workstream C: every precommit failure path funnels
    through :meth:`ReloadManager._abort_precommit_reload` which
    returns this typed outcome so callers and diagnostics can
    inspect exactly what cleanup was attempted and succeeded.
    """

    swap_rollback_attempted: bool = False
    swap_rollback_succeeded: bool = False
    transition_rollback_outcome: TransitionRollbackOutcome | None = None
    candidate_abort_attempted: bool = False
    candidate_abort_succeeded: bool = False
    candidate_cleanup_diagnostics: CleanupDiagnostics | None = None
    admission_reopened: bool = False
    degraded: bool = False
    primary_error: str = ""


class ReloadPreparationError(Exception):
    """Raised when candidate generation construction fails."""

    error_kind: str = "preparation"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class ReloadDigestMismatchError(ReloadPreparationError):
    """Raised when the caller's expected content digest does not match.

    Carries ``expected`` and ``actual`` hex strings (both truncated for
    diagnostic brevity) so typed callers can route this rejection to
    the validation counter without parsing the message.
    """

    error_kind: str = "digest_mismatch"

    def __init__(
        self,
        message: str,
        *,
        expected: str,
        actual: str,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


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
        app: Any = None,  # noqa: ANN401 — FastAPI app for mirror updates
    ) -> None:
        self._runtime_manager = runtime_manager
        self._process = process
        self._app = app
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
        #: The control-request task currently executing ``reload()``.
        #: Shutdown uses this handle to request bounded cancellation when
        #: transaction waiting times out before acceptance.
        self._active_reload_task: asyncio.Task[Any] | None = None
        #: Test-only seam — when set to an exception instance,
        #: ``_build_candidate_generation`` raises it at entry.
        self.TEST_INJECT_BUILD_FAILURE: Exception | None = None
        #: Test-only seam — when set to an exception instance,
        #: ``_reconcile_persistence`` raises it at entry.
        self.TEST_INJECT_RECONCILE_FAILURE: Exception | None = None
        #: Test-only seam — when set to an exception instance,
        #: ``_publish_generation`` raises it at entry.
        self.TEST_INJECT_PUBLISH_FAILURE: Exception | None = None
        #: Test-only seam — when set to an exception instance,
        #: the commit flow's ``TransitionApplyResult.apply_all()`` raises
        #: it (for testing process-transition apply failures inside the
        #: SQLite transaction).
        self.TEST_INJECT_TRANSITION_APPLY_FAILURE: Exception | None = None
        #: Test-only seam — when set to an exception instance,
        #: the post-acceptance finalization block raises it after
        #: ownership transfer (for testing post-acceptance cancellation).
        self.TEST_INJECT_FINALIZATION_CANCEL: BaseException | None = None
        #: Test-only seam — when set to an exception instance,
        #: the retirement scheduling section raises it (for testing
        #: retirement failure after acceptance).
        self.TEST_INJECT_RETIREMENT_FAILURE: Exception | None = None
        #: Test-only persistent retirement failure used through drain/adoption.
        self.TEST_PERSISTENT_RETIREMENT_FAILURE: Exception | None = None
        #: Test-only seam for production-boundary transition-prefix tests.
        self.TEST_INJECT_PROCESS_TRANSITION_PLAN: ProcessTransitionPlan | None = None
        #: Last abort cleanup diagnostics from a failed reload.
        self._last_cleanup_diagnostics: CleanupDiagnostics | None = None
        #: Current transaction (Phase 6) — ``None`` when idle.
        self._current_transaction: ReloadTransaction | None = None
        #: Phase 11: precise counters for reload operations.
        self._counters = ReloadCounters()
        #: Phase 11: canonical diagnostic result for the most recent reload.
        self._last_diagnostic_result: ReloadDiagnosticResult | None = None
        #: Phase 11: bounded history of recent reload diagnostic results.
        self._reload_history: list[ReloadDiagnosticResult] = []
        self._reload_history_max: int = 50
        #: Plan 018/019: process-owned registry of accepted finalization
        #: jobs.  Keyed by request_id for O(1) lookup.  Only one reload
        #: is admitted at a time so the normal bound is one active job.
        self._accepted_finalization_jobs: dict[str, AcceptedReloadFinalizationJob] = {}
        #: Plan 019 Workstream C1: bounded diagnostic history of
        #: completed finalization jobs.  Contains no live runtime
        #: references -- only scalar/diagnostic data.
        self._finalization_history: collections.deque[AcceptedFinalizationRecord] = (
            collections.deque(maxlen=FINALIZATION_HISTORY_MAX)
        )
        #: Shutdown-adopted jobs leave normal retry admission but remain
        #: here until runtime shutdown confirms ownership transfer.
        self._shutdown_adopted_finalization_jobs: dict[
            str, AcceptedReloadFinalizationJob
        ] = {}
        #: Strong references for fire-and-forget observation/event tasks.
        #: asyncio keeps only weak references to tasks; without this set
        #: a pending task can be garbage-collected before it runs.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        """Release and observe a tracked fire-and-forget task."""
        self._background_tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

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

    async def drain_finalization_jobs(
        self,
        *,
        timeout_s: float = 10.0,
    ) -> int:
        """Attempt bounded completion of all pending finalization jobs.

        Plan 018/019/020: called during shutdown to drain
        accepted-finalization jobs before retiring the active
        generation.  Returns the number of jobs that remain incomplete
        after the bounded retry.
        """
        self._reconcile_completed_registered_jobs()
        pending = [
            j for j in self._accepted_finalization_jobs.values() if j.is_unresolved
        ]
        if not pending:
            return 0
        logger.info(
            "Draining %d accepted finalization job(s) during shutdown",
            len(pending),
        )
        per_job_timeout = max(timeout_s / len(pending), 1.0)
        for job in pending:
            # Plan 020 Workstream E3: shield the retained task so
            # cancelling the timeout does not cancel the attempt.
            drain_task: asyncio.Task[AcceptedFinalizationOutcome] | None = None
            try:
                drain_task = asyncio.create_task(job.run())
                self._background_tasks.add(drain_task)
                drain_task.add_done_callback(self._background_task_done)
                outcome = await asyncio.wait_for(
                    asyncio.shield(drain_task),
                    timeout=per_job_timeout,
                )
                self._reconcile_finalization_job(job, outcome)
            except TimeoutError:
                logger.warning(
                    "Finalization job drain timed out for generation %d "
                    "(step=%s, attempts=%d)",
                    job.generation_id,
                    job.step.value,
                    job.attempts,
                )
            except asyncio.CancelledError:
                # Shield broken — leave the task running, do NOT cancel it.
                logger.debug(
                    "Finalization job drain cancelled for generation %d",
                    job.generation_id,
                )
            except Exception:
                logger.debug(
                    "Finalization job drain raised for generation %d",
                    job.generation_id,
                    exc_info=True,
                )
                # Reconcile if the job completed despite the exception.
                if job.is_complete:
                    self._reconcile_finalization_job(
                        job,
                        AcceptedFinalizationOutcome(
                            completed=True,
                            next_step=None,
                            attempt_count=job.attempts,
                            failure_count=job.failure_count,
                            retry_attempt_count=job.retry_attempt_count,
                            retirement_retry_attempt_count=job.retirement_retry_attempt_count,
                            failed_step=job.last_error_step,
                            error_class=job.last_error_class,
                            error_message=job.last_error_message,
                            retry_permitted=False,
                            status=job.status,
                        ),
                    )
        remaining = [
            j for j in self._accepted_finalization_jobs.values() if j.is_unresolved
        ]
        if remaining:
            for job in remaining:
                logger.warning(
                    "Unresolved finalization job: generation=%d step=%s "
                    "attempts=%d last_error=%s",
                    job.generation_id,
                    job.step.value,
                    job.attempts,
                    job.last_error_step,
                )
        return len(remaining)

    def _shutdown_adoption_outcome(
        self,
        job: AcceptedReloadFinalizationJob,
    ) -> AcceptedFinalizationOutcome:
        """Build the scalar outcome recorded when shutdown takes ownership."""
        return AcceptedFinalizationOutcome(
            completed=False,
            next_step=job.step.value,
            attempt_count=job.attempts,
            failure_count=job.failure_count,
            retry_attempt_count=job.retry_attempt_count,
            retirement_retry_attempt_count=job.retirement_retry_attempt_count,
            failed_step=job.last_error_step,
            error_class=job.last_error_class,
            error_message=job.last_error_message,
            retry_permitted=False,
            status=FinalizationStatus.SHUTDOWN_ADOPTED,
        )

    async def _adopt_unresolved_finalization_jobs(self) -> int:
        """Move unresolved jobs out of normal admission into shutdown ownership."""
        adopted = 0
        for request_id, job in tuple(self._accepted_finalization_jobs.items()):
            if not job.is_unresolved:
                continue
            await job.adopt_for_shutdown()
            self._accepted_finalization_jobs.pop(request_id, None)
            self._shutdown_adopted_finalization_jobs[request_id] = job
            outcome = self._shutdown_adoption_outcome(job)
            self._update_finalization_diagnostic(job, outcome)
            self._schedule_finalization_event(
                "reload_finalization_shutdown_adopted",
                job,
                outcome,
            )
            adopted += 1
        return adopted

    async def release_shutdown_adopted_references(self) -> int:
        """Release adopted job references after runtime shutdown completes."""
        released = 0
        for request_id, job in tuple(self._shutdown_adopted_finalization_jobs.items()):
            task = job.retained_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            job.release_references()
            self._finalization_history.append(job.to_record())
            self._shutdown_adopted_finalization_jobs.pop(request_id, None)
            released += 1
        return released

    async def prepare_for_shutdown(
        self,
        *,
        transaction_timeout_s: float = 5.0,
        finalization_timeout_s: float = 10.0,
    ) -> ReloadShutdownPreparation:
        """Resolve reload ownership before the runtime manager is closed.

        A transaction wait timeout is handled explicitly: the active reload
        task receives one bounded cancellation request, and shutdown is
        considered safe only after the transaction reference clears.  Any
        accepted work that cannot finish is adopted into a separate registry
        before runtime shutdown.
        """
        transaction_wait_completed = await self.wait_for_transaction_completion(
            timeout_s=transaction_timeout_s,
        )
        if not transaction_wait_completed:
            task = self._active_reload_task
            current_task = asyncio.current_task()
            if task is not None and task is not current_task and not task.done():
                logger.warning(
                    "Requesting bounded cancellation of reload during shutdown"
                )
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=max(transaction_timeout_s, 0.1),
                    )
            transaction_wait_completed = await self.wait_for_transaction_completion(
                timeout_s=max(transaction_timeout_s, 0.1),
            )

        active_transaction = self._current_transaction
        if active_transaction is not None and not transaction_wait_completed:
            return ReloadShutdownPreparation(
                transaction_wait_completed=False,
                unresolved_jobs=len(self._accepted_finalization_jobs),
                adopted_jobs=0,
                active_transaction_state=active_transaction.state.value,
                ownership_safe_for_runtime_shutdown=False,
            )

        unresolved = await self.drain_finalization_jobs(
            timeout_s=finalization_timeout_s,
        )
        adopted = 0
        if unresolved:
            adopted = await self._adopt_unresolved_finalization_jobs()
        remaining_active = sum(
            1 for job in self._accepted_finalization_jobs.values() if job.is_unresolved
        )
        return ReloadShutdownPreparation(
            transaction_wait_completed=transaction_wait_completed,
            unresolved_jobs=remaining_active,
            adopted_jobs=adopted,
            active_transaction_state=(
                self._current_transaction.state.value
                if self._current_transaction is not None
                else None
            ),
            ownership_safe_for_runtime_shutdown=(
                self._current_transaction is None and remaining_active == 0
            ),
        )

    def _ensure_accepted_owner_registered(
        self,
        *,
        txn: ReloadTransaction,
        candidate: RuntimeGenerationCandidate,
        pending_swap: Any,
        transition_result: TransitionApplyResult | None,
        published_gen: RuntimeGeneration,
        generation_id: int,
        old_generation_id: int | None,
    ) -> AcceptedReloadFinalizationJob:
        """Create and register the finalization job after acceptance.

        Plan 020 Workstream A2: called after ``txn.mark_accepted()``
        but before any post-acceptance await.  Dictionary insertion is
        not mixed with observers, event writes, logging formatters, or
        arbitrary user callbacks.

        Returns the registered job for use by the caller.
        """
        pending_count = sum(
            1 for j in self._accepted_finalization_jobs.values() if not j.is_complete
        )
        if pending_count > 0:
            logger.error(
                "Invariant violation: %d pending finalization "
                "job(s) when registering new job for generation %d",
                pending_count,
                generation_id,
            )
        finalization_job = AcceptedReloadFinalizationJob(
            request_id=txn.request_id,
            generation_id=generation_id,
            old_generation_id=old_generation_id,
            transaction=txn,
            candidate=candidate,
            pending_swap=pending_swap,
            transition_result=transition_result,
            published_generation=published_gen,
            app=self._app,
            observer=self._observer,
            _reload_manager=self,
            _on_attempt_done=self._schedule_finalization_reconciliation,
        )
        self._accepted_finalization_jobs[finalization_job.request_id] = finalization_job
        return finalization_job

    def _schedule_finalization_reconciliation(
        self,
        job: AcceptedReloadFinalizationJob,
        task: asyncio.Task[AcceptedFinalizationOutcome],
    ) -> None:
        """Schedule process-owned observation of a retained attempt.

        The callback is intentionally non-blocking.  It remains attached
        to the retained task even when every request waiter is cancelled,
        so completion cannot depend on a particular control request.
        """
        try:
            loop = task.get_loop()
            if loop.is_closed():
                return
            observe_task = loop.create_task(
                self._observe_finalization_attempt(job, task),
                name=f"observe-finalization-{job.request_id}",
            )
            self._background_tasks.add(observe_task)
            observe_task.add_done_callback(self._background_tasks.discard)
        except (RuntimeError, TypeError):
            # The event loop may be closing during process teardown.  The
            # shutdown preparation path remains the synchronous backstop.
            logger.debug(
                "Could not schedule finalization reconciliation for %s",
                job.request_id,
                exc_info=True,
            )

    @staticmethod
    def _outcome_from_completed_task(
        job: AcceptedReloadFinalizationJob,
        task: asyncio.Task[AcceptedFinalizationOutcome],
    ) -> AcceptedFinalizationOutcome:
        """Extract a scalar outcome without allowing callback exceptions."""
        try:
            return task.result()
        except BaseException as exc:
            return AcceptedFinalizationOutcome(
                completed=job.is_complete,
                next_step=None if job.is_complete else job.step.value,
                attempt_count=job.attempts,
                failure_count=job.failure_count,
                retry_attempt_count=job.retry_attempt_count,
                retirement_retry_attempt_count=job.retirement_retry_attempt_count,
                failed_step=job.last_error_step,
                error_class=type(exc).__name__,
                error_message=str(exc),
                retry_permitted=False,
                status=job.status,
            )

    async def _observe_finalization_attempt(
        self,
        job: AcceptedReloadFinalizationJob,
        task: asyncio.Task[AcceptedFinalizationOutcome],
    ) -> None:
        """Reconcile one retained task from the process-owned callback."""
        try:
            outcome = self._outcome_from_completed_task(job, task)
            self._reconcile_finalization_job(
                job,
                outcome,
                observation_path="callback",
            )
        except Exception:
            # A callback must never become an unhandled event-loop task.
            logger.error(
                "Finalization reconciliation failed for %s",
                job.request_id,
                exc_info=True,
            )

    def _reconcile_completed_registered_jobs(self) -> None:
        """Synchronously sweep completed retained tasks before admission."""
        for job in tuple(self._accepted_finalization_jobs.values()):
            task = job.retained_task
            if task is None or not task.done():
                continue
            outcome = self._outcome_from_completed_task(job, task)
            try:
                self._reconcile_finalization_job(
                    job,
                    outcome,
                    observation_path="admission_sweep",
                )
            except Exception:
                logger.error(
                    "Completed finalization sweep failed for %s",
                    job.request_id,
                    exc_info=True,
                )

    def _record_reload_accepted_once(self, txn: ReloadTransaction) -> None:
        """Increment accepted/committed counters at the acceptance boundary."""
        if not txn.mark_acceptance_accounted():
            return
        self._counters = replace(
            self._counters,
            committed_reloads=self._counters.committed_reloads + 1,
            accepted_reloads=self._counters.accepted_reloads + 1,
        )

    def _update_finalization_diagnostic(
        self,
        job: AcceptedReloadFinalizationJob,
        outcome: AcceptedFinalizationOutcome,
    ) -> None:
        """Replace manager-visible immutable diagnostics after reconciliation."""
        current = self._last_diagnostic_result
        if current is None or current.request_id != job.request_id:
            return
        status = outcome.status.value
        category = classify_result_category(
            ok=True,
            stage=current.terminal_stage,
            finalization_status=status,
        )
        updated = replace(
            current,
            category=category,
            counters=self._counters,
            post_commit_finalization_pending=not outcome.completed,
            ownership_transfer_pending=(
                job.step is AcceptedFinalizationStep.REGISTERED
            ),
            mirror_update_pending=(
                job.step is AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED
            ),
            retirement_scheduling_pending=(
                job.step is AcceptedFinalizationStep.RETIREMENT_SCHEDULING
            ),
            finalization_status=status,
            finalization_next_step=outcome.next_step,
            finalization_attempt_count=outcome.attempt_count,
            finalization_failure_count=outcome.failure_count,
            finalization_retry_attempt_count=outcome.retry_attempt_count,
            finalization_last_error_step=outcome.failed_step,
            finalization_last_error_class=outcome.error_class,
            finalization_last_error_message=outcome.error_message,
        )
        self._last_diagnostic_result = updated
        for index, record in enumerate(self._reload_history):
            if record.request_id == job.request_id:
                self._reload_history[index] = updated

    def _schedule_finalization_event(
        self,
        event_type: str,
        job: AcceptedReloadFinalizationJob,
        outcome: AcceptedFinalizationOutcome,
    ) -> None:
        """Emit bounded lifecycle evidence without blocking reconciliation."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        transaction = getattr(job, "transaction", None)
        event_task = loop.create_task(
            self._safe_record_event(
                event_type,
                generation_id=job.generation_id,
                digest_prefix=(transaction.digest_prefix if transaction else ""),
                error=outcome.error_message,
                finalization_status=outcome.status.value,
                finalization_next_step=outcome.next_step,
                finalization_attempt_count=outcome.attempt_count,
                finalization_failure_count=outcome.failure_count,
                finalization_retry_attempt_count=outcome.retry_attempt_count,
                finalization_retirement_retry_attempt_count=(
                    outcome.retirement_retry_attempt_count
                ),
            )
        )
        self._background_tasks.add(event_task)
        event_task.add_done_callback(self._background_tasks.discard)

    def _reconcile_finalization_job(
        self,
        job: AcceptedReloadFinalizationJob,
        outcome: AcceptedFinalizationOutcome,
        *,
        observation_path: str = "inline",
    ) -> None:
        """Idempotently update counters and history for a finalization outcome.

        Plan 020 Workstream C2: every observation path (inline run,
        admission retry, drain, shutdown drain) calls this.  Use
        ``mark_reconciled()`` to guarantee double-counting cannot occur.

        Per-job counters are tracked by delta against the previously
        observed attempt count.  The first observation captures the
        full ``retry_attempt_count`` and ``retirement_retry_attempt_count``;
        subsequent observations only add the delta (new attempts
        minus previously accounted attempts).  This is what makes
        the inline-path, admission-retry-path, and drain-path
        observations idempotent.
        """
        cursor = job.accounting
        deltas = {
            "attempts": outcome.attempt_count - cursor.attempts,
            "failures": outcome.failure_count - cursor.failures,
            "retries": outcome.retry_attempt_count - cursor.retries,
            "retirement_retries": (
                outcome.retirement_retry_attempt_count - cursor.retirement_retries
            ),
        }
        if any(value < 0 for value in deltas.values()):
            raise AcceptedFinalizationInvariantError(
                f"Finalization counters regressed for {job.request_id}: {deltas}",
                request_id=job.request_id,
                generation_id=job.generation_id,
            )
        cursor.attempts = outcome.attempt_count
        cursor.failures = outcome.failure_count
        cursor.retries = outcome.retry_attempt_count
        cursor.retirement_retries = outcome.retirement_retry_attempt_count
        self._counters = replace(
            self._counters,
            accepted_finalization_failures=(
                self._counters.accepted_finalization_failures + deltas["failures"]
            ),
            accepted_finalization_retries=(
                self._counters.accepted_finalization_retries + deltas["retries"]
            ),
            retirement_retry_count=(
                self._counters.retirement_retry_count + deltas["retirement_retries"]
            ),
        )

        if not outcome.completed:
            job.mark_response_returned(completed=False)
            self._update_finalization_diagnostic(job, outcome)
            if deltas["failures"] > 0:
                if outcome.status is FinalizationStatus.RETIREMENT_SCHEDULE_FAILED:
                    event_type = "reload_retirement_schedule_failed"
                elif outcome.status is FinalizationStatus.INVARIANT_FAILED:
                    event_type = "reload_finalization_invariant_failed"
                else:
                    event_type = "reload_finalization_retry_failed"
                self._schedule_finalization_event(event_type, job, outcome)
            return

        if cursor.completion_accounted:
            return
        cursor.completion_accounted = True
        job.mark_completion_observed(observation_path)
        self._counters = replace(
            self._counters,
            fully_finalized_reloads=self._counters.fully_finalized_reloads + 1,
            accepted_finalization_failures_recovered=(
                self._counters.accepted_finalization_failures_recovered + 1
                if outcome.failure_count > 0 and not cursor.recovery_accounted
                else self._counters.accepted_finalization_failures_recovered
            ),
            delayed_completion_count=(
                self._counters.delayed_completion_count + 1
                if (
                    not cursor.delayed_completion_accounted
                    and (
                        job.response_returned_before_completion
                        or observation_path != "inline"
                        or outcome.attempt_count > 1
                    )
                )
                else self._counters.delayed_completion_count
            ),
        )
        cursor.recovery_accounted = cursor.recovery_accounted or (
            outcome.failure_count > 0
        )
        if (
            job.response_returned_before_completion
            or observation_path != "inline"
            or outcome.attempt_count > 1
        ):
            cursor.delayed_completion_accounted = True
        self._update_finalization_diagnostic(job, outcome)
        job.mark_reconciled()
        self._accepted_finalization_jobs.pop(job.request_id, None)
        job.release_references()
        self._finalization_history.append(job.to_record())
        if observation_path != "inline" or outcome.attempt_count > 1:
            self._schedule_finalization_event(
                "reload_finalization_completed_delayed",
                job,
                outcome,
            )

    def snapshot(self) -> dict[str, Any]:
        """Return reload state for diagnostics."""
        # A done callback normally performs this work on the next loop
        # turn.  The synchronous sweep is the defensive backstop for
        # callers that inspect diagnostics or admission immediately after
        # a retained task finishes.
        self._reconcile_completed_registered_jobs()
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
        # Phase 11: precise counters.
        result["counters"] = {
            "total_requests": self._counters.total_requests,
            "admitted_operations": self._counters.admitted_operations,
            "busy_rejections": self._counters.busy_rejections,
            "committed_reloads": self._counters.committed_reloads,
            "noop_outcomes": self._counters.noop_outcomes,
            "ignored_only_outcomes": self._counters.ignored_only_outcomes,
            "validation_rejections": self._counters.validation_rejections,
            "restart_required_rejections": self._counters.restart_required_rejections,
            "prepare_failures": self._counters.prepare_failures,
            "commit_failures": self._counters.commit_failures,
            "cancellations": self._counters.cancellations,
            "compensation_failures": self._counters.compensation_failures,
            "retirement_failures": self._counters.retirement_failures,
            # Plan 019 Workstream G3: finalization counters.
            "accepted_reloads": self._counters.accepted_reloads,
            "fully_finalized_reloads": self._counters.fully_finalized_reloads,
            "accepted_finalization_failures": (
                self._counters.accepted_finalization_failures
            ),
            "accepted_finalization_retries": (
                self._counters.accepted_finalization_retries
            ),
            "retirement_retry_count": self._counters.retirement_retry_count,
            # Plan 020 Workstream C2: finalization reconciliation counters.
            "accepted_finalization_failures_recovered": (
                self._counters.accepted_finalization_failures_recovered
            ),
            "delayed_completion_count": self._counters.delayed_completion_count,
        }
        # Phase 11: canonical diagnostic result.
        if self._last_diagnostic_result is not None:
            d = self._last_diagnostic_result
            result["last_diagnostic_result"] = {
                "request_id": d.request_id,
                "category": d.category.value,
                "terminal_stage": d.terminal_stage.value,
                "started_at": d.started_at,
                "completed_at": d.completed_at,
                "duration_s": d.duration_s,
                "old_generation_id": d.old_generation_id,
                "candidate_generation_id": d.candidate_generation_id,
                "active_generation_id": d.active_generation_id,
                "changed_sections": d.changed_sections,
                "ignored_sections": d.ignored_sections,
                "restart_required_sections": d.restart_required_sections,
                "semantic_noop": d.semantic_noop,
                "publication_occurred": d.publication_occurred,
                "persistence_committed": d.persistence_committed,
                "process_transitions_applied": d.process_transitions_applied,
                "compensation_attempted": d.compensation_attempted,
                "compensation_succeeded": d.compensation_succeeded,
                "retirement_pending": d.retirement.retirement_pending,
                "retiring_generation_id": d.retirement.retiring_generation_id,
                "error_code": d.error_code,
                "error_class": d.error_class,
                "message": d.message,
                "warning_count": len(d.warning_messages),
                # Plan 016 Workstream H2/H3: per-stage progress flags.
                "pending_swap_state": d.pending_swap_state,
                "lease_admission_gated": d.lease_admission_gated,
                "post_commit_finalization_pending": (
                    d.post_commit_finalization_pending
                ),
                "ownership_transfer_pending": d.ownership_transfer_pending,
                "mirror_update_pending": d.mirror_update_pending,
                "retirement_scheduling_pending": (d.retirement_scheduling_pending),
                "publication_epoch": d.publication_epoch,
                # Plan 020 Workstream D3: canonical finalization fields.
                "finalization_status": d.finalization_status,
                "finalization_next_step": d.finalization_next_step,
                "finalization_attempt_count": d.finalization_attempt_count,
                "finalization_failure_count": d.finalization_failure_count,
                "finalization_retry_attempt_count": d.finalization_retry_attempt_count,
                "finalization_last_error_step": d.finalization_last_error_step,
                "finalization_last_error_class": d.finalization_last_error_class,
                "finalization_last_error_message": d.finalization_last_error_message,
                "pending_swap_committed": d.pending_swap_committed,
                "accepted_generation_authoritative": (
                    d.accepted_generation_authoritative
                ),
            }
        else:
            result["last_diagnostic_result"] = None
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
        # Plan 018/019: surface accepted finalization jobs and history.
        result["accepted_finalization_jobs"] = [
            job.snapshot()
            for job in self._accepted_finalization_jobs.values()
            if job.is_unresolved
        ]
        result["finalization_history"] = [
            {
                "request_id": r.request_id,
                "generation_id": r.generation_id,
                "old_generation_id": r.old_generation_id,
                "completion_status": r.completion_status,
                "attempts": r.attempts,
                "failure_count": r.failure_count,
                "retry_attempt_count": r.retry_attempt_count,
                "retirement_retry_attempt_count": r.retirement_retry_attempt_count,
                "last_failed_step": r.last_failed_step,
                "last_error_class": r.last_error_class,
                "last_error_message": r.last_error_message,
                "completed_at": r.completed_at,
                "duration_s": r.duration_s,
                "adopted_for_shutdown": r.adopted_for_shutdown,
                "references_released": r.references_released,
                "completion_observation_path": r.completion_observation_path,
            }
            for r in self._finalization_history
        ]
        result["shutdown_adopted_finalization_jobs"] = [
            job.snapshot() for job in self._shutdown_adopted_finalization_jobs.values()
        ]
        result["unresolved_finalization_count"] = sum(
            1 for j in self._accepted_finalization_jobs.values() if j.is_unresolved
        )
        # Phase 11: bounded reload history (most recent first).
        result["reload_history"] = [
            {
                "request_id": d.request_id,
                "category": d.category.value,
                "terminal_stage": d.terminal_stage.value,
                "started_at": d.started_at,
                "completed_at": d.completed_at,
                "duration_s": d.duration_s,
                "old_generation_id": d.old_generation_id,
                "candidate_generation_id": d.candidate_generation_id,
                "active_generation_id": d.active_generation_id,
                "changed_sections": d.changed_sections,
                "ignored_sections": d.ignored_sections,
                "restart_required_sections": d.restart_required_sections,
                "semantic_noop": d.semantic_noop,
                "publication_occurred": d.publication_occurred,
                "retirement_pending": d.retirement.retirement_pending,
                "error_code": d.error_code,
                "error_class": d.error_class,
                "message": d.message,
                "warning_count": len(d.warning_messages),
                # Plan 016 Workstream H2/H3: per-stage progress flags.
                "pending_swap_state": d.pending_swap_state,
                "lease_admission_gated": d.lease_admission_gated,
                "post_commit_finalization_pending": (
                    d.post_commit_finalization_pending
                ),
                "ownership_transfer_pending": d.ownership_transfer_pending,
                "mirror_update_pending": d.mirror_update_pending,
                "retirement_scheduling_pending": (d.retirement_scheduling_pending),
                "publication_epoch": d.publication_epoch,
                # Plan 020 Workstream D3: canonical finalization fields.
                "finalization_status": d.finalization_status,
                "finalization_next_step": d.finalization_next_step,
                "finalization_attempt_count": d.finalization_attempt_count,
                "finalization_failure_count": d.finalization_failure_count,
                "finalization_retry_attempt_count": d.finalization_retry_attempt_count,
                "finalization_last_error_step": d.finalization_last_error_step,
                "finalization_last_error_class": d.finalization_last_error_class,
                "finalization_last_error_message": d.finalization_last_error_message,
                "pending_swap_committed": d.pending_swap_committed,
                "accepted_generation_authoritative": (
                    d.accepted_generation_authoritative
                ),
            }
            for d in reversed(self._reload_history)
        ]
        return result

    async def _execute_accepted_phase(
        self,
        context: AcceptedCommitContext,
        *,
        warnings: tuple[ConfigValidationWarning, ...],
    ) -> ReloadResult:
        """Own the post-commit lifecycle after the rollback boundary.

        This method is intentionally separate from the pre-acceptance
        ``try``/``except`` region in :meth:`reload`.  Its acceptance
        marker and accounting cannot be lexically governed by a handler
        that performs precommit rollback or candidate cleanup.
        """
        txn = context.transaction
        txn.mark_accepted()
        self._record_reload_accepted_once(txn)

        # Synchronous owner registration is the first operation after the
        # acceptance fact and occurs before the first post-acceptance await.
        finalization_job = self._ensure_accepted_owner_registered(
            txn=txn,
            candidate=context.candidate,
            pending_swap=context.pending_swap,
            transition_result=context.transition_result,
            published_gen=context.published_generation,
            generation_id=context.generation_id,
            old_generation_id=context.old_generation_id,
        )

        self._set_stage(
            ReloadOperationStage.RETIREMENT,
            context.started_at,
            context.generation_id,
            context.digest_prefix,
        )
        try:
            finalization_outcome = await finalization_job.run()
        except AcceptedFinalizationInvariantError:
            # The accepted generation remains authoritative, but an
            # invariant failure must remain visible as unresolved
            # finalization rather than entering precommit classification.
            task = finalization_job.retained_task
            if task is None or not task.done():
                raise
            finalization_outcome = self._outcome_from_completed_task(
                finalization_job,
                task,
            )
        if finalization_outcome.status is not FinalizationStatus.COMPLETED:
            logger.warning(
                "Accepted finalization pending for generation %d (status=%s step=%s)",
                context.generation_id,
                finalization_outcome.status.value,
                finalization_outcome.next_step,
            )
        self._reconcile_finalization_job(
            finalization_job,
            finalization_outcome,
            observation_path="inline",
        )

        self._set_stage(
            ReloadOperationStage.IDLE,
            context.started_at,
            context.generation_id,
            context.digest_prefix,
        )
        duration = time.monotonic() - context.started_at
        logger.info(
            "Reload committed: generation=%d duration=%.3fs sections=%s",
            context.generation_id,
            duration,
            ",".join(context.changed_sections) or "(none)",
        )

        finalization_status = finalization_outcome.status.value
        finalization_next_step = finalization_outcome.next_step
        enriched_warnings = warnings
        if finalization_status in ("retry_pending", "retirement_schedule_failed"):
            enriched_warnings = warnings + (
                ConfigValidationWarning(
                    code="finalization_retry_pending",
                    section="finalization",
                    message=(
                        f"Finalization retry pending at step {finalization_next_step}"
                    ),
                ),
            )
        diagnostic, wire_result = self._finalize_reload(
            request_id=txn.request_id,
            started_at=context.started_at,
            txn=txn,
            txn_state=txn.state,
            ok=True,
            stage=ReloadTerminalStage.RETIREMENT,
            generation_id=context.generation_id,
            digest_prefix=context.digest_prefix,
            changed_sections=context.changed_sections,
            ignored_sections=(),
            restart_required_sections=(),
            warnings=enriched_warnings,
            publication_occurred=True,
            persistence_committed=True,
            process_transitions_applied=True,
            finalization_status=finalization_status,
            finalization_next_step=finalization_next_step,
            finalization_attempt_count=finalization_outcome.attempt_count,
            finalization_failure_count=finalization_outcome.failure_count,
            finalization_retry_attempt_count=(finalization_outcome.retry_attempt_count),
            finalization_last_error_step=finalization_outcome.failed_step,
            finalization_last_error_class=finalization_outcome.error_class,
            finalization_last_error_message=finalization_outcome.error_message,
            old_generation_id=context.old_generation_id,
            pending_swap_committed=True,
            accepted_generation_authoritative=True,
        )
        self._last_diagnostic_result = diagnostic
        await self._record_terminal_event(diagnostic)
        await self._safe_record_event(
            "reload_activated",
            generation_id=context.generation_id,
            digest_prefix=context.digest_prefix,
            changed_sections=context.changed_sections,
        )
        return wire_result

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
        # Plan 016 Workstream C2: pre-initialize cleanup-owner state
        # so the cancellation and exception handlers see consistent
        # identifiers even when construction failed before reaching
        # the assignment sites.
        pending_swap: Any | None = None
        transition_result: TransitionApplyResult | None = None
        old_generation_id: int | None = None
        published_gen: RuntimeGeneration | None = None

        # Phase 11: increment total requests.
        self._counters = replace(
            self._counters,
            total_requests=self._counters.total_requests + 1,
        )

        # Phase 6: create the transaction to track state.
        txn = ReloadTransaction(
            request_id=f"reload-{int(started_at * 1000)}",
            validation=validation,
            expected_digest=expected_digest,
        )
        # Atomic admission claim — no TOCTOU window.  The claim helper
        # deliberately contains no awaitable work beyond acquiring the
        # short claim mutex; validation, finalization retries, candidate
        # construction, and publication all happen after it returns.
        await self._claim_reload(txn.request_id)

        try:
            self._current_transaction = txn

            # Plan 018 Workstream C5: before admitting a new reload,
            # check if there are pending finalization jobs from a
            # previous (possibly cancelled) reload and attempt bounded
            # completion.  A committed swap cannot be force-cleared, so
            # the new reload must wait for finalization to resolve.  This
            # work intentionally runs outside ``_claim_mutex`` so a
            # competing caller receives the busy result immediately.
            self._reconcile_completed_registered_jobs()
            pending_jobs = [
                j for j in self._accepted_finalization_jobs.values() if j.is_unresolved
            ]
            if pending_jobs:
                for job in pending_jobs:
                    try:
                        outcome = await asyncio.wait_for(job.run(), timeout=10.0)
                        self._reconcile_finalization_job(job, outcome)
                    except TimeoutError:
                        logger.warning(
                            "Bounded finalization retry timed out for "
                            "generation %d (step=%s)",
                            job.generation_id,
                            job.step.value,
                        )
                    except Exception:
                        logger.debug(
                            "Finalization retry raised for generation %d",
                            job.generation_id,
                            exc_info=True,
                        )
                        # Reconcile if the job completed despite the exception.
                        if job.is_complete:
                            self._reconcile_finalization_job(
                                job,
                                AcceptedFinalizationOutcome(
                                    completed=True,
                                    next_step=None,
                                    attempt_count=job.attempts,
                                    failure_count=job.failure_count,
                                    retry_attempt_count=job.retry_attempt_count,
                                    retirement_retry_attempt_count=job.retirement_retry_attempt_count,
                                    failed_step=job.last_error_step,
                                    error_class=job.last_error_class,
                                    error_message=job.last_error_message,
                                    retry_permitted=False,
                                    status=job.status,
                                ),
                            )
                # Check again after retry -- still pending means
                # we cannot admit a new reload.
                still_pending = [
                    j
                    for j in self._accepted_finalization_jobs.values()
                    if j.is_unresolved
                ]
                if still_pending:
                    self._counters = replace(
                        self._counters,
                        busy_rejections=self._counters.busy_rejections + 1,
                    )
                    raise ReloadInProgressError(
                        "Accepted finalization still pending for "
                        "generation(s): "
                        + ", ".join(str(j.generation_id) for j in still_pending)
                    )

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
                # Phase 11: update counters and finalize.
                self._counters = replace(
                    self._counters,
                    restart_required_rejections=(
                        self._counters.restart_required_rejections + 1
                    ),
                )
                diag, wire_result = self._finalize_reload(
                    request_id=txn.request_id,
                    started_at=started_at,
                    txn=txn,
                    txn_state=txn.state,
                    ok=False,
                    stage=ReloadTerminalStage.DIFF,
                    generation_id=None,
                    digest_prefix=digest_prefix,
                    changed_sections=(),
                    ignored_sections=(),
                    restart_required_sections=sections,
                    restart_required_changes=tuple(restart_required),
                    warnings=warnings,
                    is_restart_required=True,
                )
                self._last_diagnostic_result = diag
                await self._record_terminal_event(diag)
                return wire_result

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
                # Phase 11: update counters and finalize.
                self._counters = replace(
                    self._counters,
                    noop_outcomes=self._counters.noop_outcomes + 1,
                )
                diag, wire_result = self._finalize_reload(
                    request_id=txn.request_id,
                    started_at=started_at,
                    txn=txn,
                    txn_state=txn.state,
                    ok=True,
                    stage=ReloadTerminalStage.COMMIT,
                    generation_id=active.generation_id,
                    digest_prefix=digest_prefix,
                    changed_sections=(),
                    ignored_sections=(),
                    restart_required_sections=(),
                    warnings=warnings,
                    is_noop=True,
                )
                self._last_diagnostic_result = diag
                await self._record_terminal_event(diag)
                return wire_result

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
                # Phase 11: update counters and finalize.
                self._counters = replace(
                    self._counters,
                    ignored_only_outcomes=self._counters.ignored_only_outcomes + 1,
                )
                diag, wire_result = self._finalize_reload(
                    request_id=txn.request_id,
                    started_at=started_at,
                    txn=txn,
                    txn_state=txn.state,
                    ok=True,
                    stage=ReloadTerminalStage.DIFF,
                    generation_id=active.generation_id,
                    digest_prefix=digest_prefix,
                    changed_sections=ignored_sections,
                    ignored_sections=ignored_sections,
                    restart_required_sections=(),
                    warnings=warnings,
                    is_ignored_only=True,
                )
                self._last_diagnostic_result = diag
                await self._record_terminal_event(diag)
                return wire_result

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
            persistence_delta = self._prepare_persistence_delta(
                validation.config,
                candidate_registry=(_gen.registry if _gen is not None else None),
            )
            if _gen is not None:
                for account_name in persistence_delta.authentication_reset_names:
                    _gen.health_manager.enable_account(account_name)
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
                generation_id=generation_id,
                config_digest=validation.content_digest or "",
                routing_trace_guard=(
                    getattr(_gen, "routing_trace_guard", None)
                    if _gen is not None
                    else None
                ),
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
            # The lease-gated staged-swap protocol ensures no request can
            # acquire the candidate generation until the SQLite commit
            # and process transitions succeed.  If the transaction rolls
            # back, PendingGenerationSwap.rollback() restores the old
            # slot and reopens admission.
            self._set_stage(
                ReloadOperationStage.COMMIT,
                started_at,
                generation_id,
                digest_prefix,
            )
            # Capture old generation ID before publication swaps it
            old_generation_id = expected_gen_id
            # Plan 017 Workstream C: capture the current operation stage
            # so the inner exception handler can pass it to the shared
            # precommit abort helper.
            error_stage = (
                self._operation_state.stage
                if self._operation_state
                else ReloadOperationStage.IDLE
            )

            # Observer: publish started
            await self._observer.on_publish_started(
                generation_id=generation_id,
                digest_prefix=digest_prefix,
            )

            # 9a: Run transition preflights before the commit gate.
            # Plan 016 Workstream D5: preflight must complete before
            # ``mark_commit_started`` so the state machine records the
            # documented ordering PROCESS_TRANSITIONS_PREPARED →
            # PROCESS_TRANSITIONS_PREFLIGHTED → COMMIT_STARTED.
            preflighted = await preflight_all_transitions(process_transition_plan)
            txn.mark_process_transitions_preflighted(preflighted)
            txn.mark_commit_started(old_generation_id)

            # 9b: Create the pending swap — does not mutate any state yet.
            published_gen = candidate._built_generation  # pyright: ignore[reportPrivateUsage]
            if published_gen is None:
                raise RuntimeError("Generation must be built before publish")
            pending_swap = await self._runtime_manager.prepare_candidate_swap(
                published_gen,
                drain_timeout_s=self._drain_timeout_s,
                expected_active_generation_id=expected_gen_id,
            )
            transition_result: TransitionApplyResult | None = None
            # Pre-acceptance commit region.  Acceptance is marked only
            # after this rollback-capable try/except has exited.
            try:
                # 9c: Enter SQLite transaction, apply persistence delta,
                # stage the runtime swap (installs lease gate), then apply
                # process transitions — all inside the same transaction so
                # any failure rolls back everything atomically.
                db = self._process.db
                async with db.transaction():
                    await self._apply_persistence_delta(persistence_delta, nested=True)

                    # Stage the swap — gates lease admission and creates
                    # a non-accepting candidate slot.  The old slot remains
                    # active but new acquire() calls block on the gate.
                    await pending_swap.stage()
                    txn.mark_runtime_staged(pending_swap)

                    # 9d: Apply process transitions inside the transaction
                    # so they roll back atomically on failure.
                    # Plan 018 Workstream A1: caller creates the result
                    # so ownership is explicit before apply_all().
                    transition_result = TransitionApplyResult(process_transition_plan)
                    await self._publish_generation(
                        candidate,
                        diff,
                        pending_swap=pending_swap,
                        transition_result=transition_result,
                    )
                # SQLite committed successfully — mark the narrow boundary
                # between persistence commit and runtime swap commit.
                txn.mark_persistence_committed_runtime_pending()
                # Commit the swap.  This makes the candidate active and
                # reopens admission.
                old_gen_id = await pending_swap.commit()
                # Plan 016 Workstream H1: pass the published generation
                # ID so the transaction records the new active
                # generation identity on commit.
                txn.mark_runtime_swap_committed(
                    old_gen_id,
                    new_generation_id=published_gen.generation_id,
                )
            except asyncio.CancelledError as exc:
                # Plan 017 Workstream D: pre-acceptance cancellation.
                # Route through the shared precommit abort helper.
                await self._abort_precommit_reload(
                    txn=txn,
                    pending_swap=pending_swap,
                    transition_result=transition_result,
                    candidate=candidate,
                    cause=exc,
                    error_stage=error_stage,
                )
                raise
            except Exception:
                # Plan 016 Workstream C1/C2: route precommit cleanup
                # through the shared helper so cancellation and
                # ordinary exceptions use the same path.
                # Plan 017 Workstream C: pass txn and candidate so
                # the helper owns all cleanup deterministically.
                await self._abort_precommit_reload(
                    txn=txn,
                    pending_swap=pending_swap,
                    transition_result=transition_result,
                    candidate=candidate,
                    cause=sys.exc_info()[1] or RuntimeError("precommit failure"),
                    error_stage=error_stage,
                )
                raise

            accepted_context = AcceptedCommitContext(
                transaction=txn,
                candidate=candidate,
                pending_swap=pending_swap,
                transition_result=transition_result,
                published_generation=published_gen,
                old_generation_id=old_generation_id,
                generation_id=generation_id,
                changed_sections=changed_sections,
                started_at=started_at,
                digest_prefix=digest_prefix,
            )
            return await self._execute_accepted_phase(
                accepted_context,
                warnings=warnings,
            )

        except ReloadInProgressError:
            raise
        except ReloadPreparationError as exc:
            error_stage = (
                self._operation_state.stage
                if self._operation_state
                else ReloadOperationStage.IDLE
            )
            logger.exception("Reload failed at stage %s", error_stage)
            # Phase 11: update counters.
            # Distinguish digest mismatch (validation) from build failure (prep)
            # using the typed ``error_kind`` discriminator — string matching
            # is fragile and bypassed by translation/localization changes.
            is_digest_mismatch = (
                isinstance(exc, ReloadDigestMismatchError)
                or getattr(exc, "error_kind", "") == "digest_mismatch"
            )
            self._counters = replace(
                self._counters,
                validation_rejections=(
                    self._counters.validation_rejections + 1
                    if is_digest_mismatch
                    else self._counters.validation_rejections
                ),
                prepare_failures=(
                    self._counters.prepare_failures
                    if is_digest_mismatch
                    else self._counters.prepare_failures + 1
                ),
            )
            # Phase 6: transition transaction to aborting/aborted.
            txn.mark_aborting(exc)
            # Capture abort diagnostics from the candidate if available.
            candidate_diag = getattr(candidate, "diagnostics", None)
            if candidate_diag is not None:
                self._last_cleanup_diagnostics = candidate_diag
            event_type = "reload_preparation_failure"
            if (
                isinstance(exc, ReloadDigestMismatchError)
                or getattr(exc, "error_kind", "") == "digest_mismatch"
            ):
                event_type = "reload_digest_mismatch"
            # Phase 11: derive correct stage from the operation state.
            # The _set_stage() call before the failed step already set
            # the correct stage (e.g., VALIDATION for digest mismatch,
            # PREPARATION for build failure).
            try:
                terminal_stage = ReloadTerminalStage(
                    self._operation_state.stage
                    if self._operation_state
                    else ReloadOperationStage.VALIDATION
                )
            except ValueError:
                terminal_stage = ReloadTerminalStage.VALIDATION
            diagnostic, wire_result = self._finalize_reload(
                request_id=txn.request_id,
                started_at=started_at,
                txn=txn,
                txn_state=txn.state,
                ok=False,
                stage=terminal_stage,
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                ignored_sections=(),
                restart_required_sections=(),
                warnings=warnings,
                error=exc,
                error_class="ReloadPreparationError",
                candidate_cleanup_attempted=candidate_diag is not None,
                candidate_cleanup_succeeded=candidate_diag is not None,
            )
            self._last_diagnostic_result = diagnostic
            await self._record_terminal_event(diagnostic)
            await self._safe_record_event(
                event_type,
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                error=f"{exc!r}",
            )
            txn.mark_aborted()
            return wire_result
        except asyncio.CancelledError:
            error_stage = (
                self._operation_state.stage
                if self._operation_state
                else ReloadOperationStage.IDLE
            )
            logger.warning("Reload cancelled at stage %s", error_stage)
            # Phase 11: update counters.
            self._counters = replace(
                self._counters,
                cancellations=self._counters.cancellations + 1,
            )
            # Plan 017 Workstream C: pre-init cleanup outcome so the
            # finalize_reload call sees consistent state regardless of
            # which branch executes.
            _cleanup_outcome: PrecommitAbortOutcome | None = None
            # Plan 018 Workstream B2/C4: branch exclusively on
            # txn.reload_accepted to distinguish pre-acceptance and
            # post-acceptance cancellation.
            if txn.reload_accepted:
                # Plan 018/020 Workstream C4: post-acceptance cancellation:
                # do NOT call mark_aborting/mark_aborted.  Ensure the
                # finalization job is registered and run a bounded
                # shielded critical prefix to preserve ownership.
                finalization_job: AcceptedReloadFinalizationJob | None = None
                for job in self._accepted_finalization_jobs.values():
                    if job.generation_id == generation_id:
                        finalization_job = job
                        break
                if finalization_job is None:
                    # Job not yet registered (cancelled between
                    # mark_accepted and job creation).  Create it now
                    # synchronously so ownership is retained.
                    if candidate is None:
                        raise RuntimeError(
                            "candidate must be set when reload is accepted"
                        ) from None
                    if published_gen is None:
                        raise RuntimeError(
                            "published_gen must be set when reload is accepted"
                        ) from None
                    self._ensure_accepted_owner_registered(
                        txn=txn,
                        candidate=candidate,
                        pending_swap=pending_swap,
                        transition_result=transition_result,
                        published_gen=published_gen,
                        generation_id=generation_id,
                        old_generation_id=old_generation_id,
                    )
                    # Re-find the job after registration.
                    for job in self._accepted_finalization_jobs.values():
                        if job.generation_id == generation_id:
                            finalization_job = job
                            break
                if finalization_job is None:
                    raise RuntimeError(
                        "finalization job must be registered after acceptance"
                    ) from None
                # The request waiter is returning by cancellation before
                # the retained attempt is known to be complete.  Record
                # that fact for delayed-completion accounting; the
                # process-owned callback remains authoritative afterward.
                finalization_job.mark_response_returned(completed=False)
                # Run a bounded shielded critical prefix to preserve
                # ownership (transfer candidate if not yet transferred).
                # Plan 020 Workstream B4: do NOT cancel the retained task
                # when the shield or bound expires — leave it running.
                critical_task: asyncio.Task[AcceptedFinalizationOutcome] | None = None
                try:
                    critical_task = asyncio.create_task(finalization_job.run())
                    await asyncio.wait_for(
                        asyncio.shield(critical_task),
                        timeout=5.0,
                    )
                except (asyncio.CancelledError, TimeoutError):
                    # Shield broken or bound expired — RETAIN the job
                    # as pending/degraded.  Do NOT cancel the retained task.
                    logger.warning(
                        "Post-acceptance cancellation shield broken "
                        "for generation %d; finalization job retained",
                        generation_id,
                    )
            elif txn.publication_occurred:
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
                # Plan 017 Workstream C: cancellation before commit
                # routes through the shared precommit abort helper
                # which owns swap rollback, transition rollback,
                # candidate abort, and admission verification.
                # Shield the cleanup so bounded work completes before
                # cancellation propagates.
                txn.mark_aborting(RuntimeError("Reload cancelled before commit point"))
                _cleanup_task: asyncio.Task[PrecommitAbortOutcome] | None = None
                try:
                    _cleanup_task = asyncio.create_task(
                        self._abort_precommit_reload(
                            txn=txn,
                            pending_swap=pending_swap,
                            transition_result=transition_result,
                            candidate=candidate,
                            cause=asyncio.CancelledError(),
                            error_stage=error_stage,
                        )
                    )
                    _cleanup_outcome = await asyncio.shield(_cleanup_task)
                except asyncio.CancelledError:
                    # Shield broken — wait for cleanup task to finish
                    # then propagate the cancellation.
                    if _cleanup_task is not None and not _cleanup_task.done():
                        try:
                            _cleanup_outcome = await _cleanup_task
                        except Exception:
                            logger.debug(
                                "Cleanup task raised after shield break",
                                exc_info=True,
                            )
                    logger.warning(
                        "Precommit abort shield cancelled for generation %d",
                        generation_id,
                    )
                except Exception:
                    logger.exception(
                        "Precommit abort raised during cancellation for generation %d",
                        generation_id,
                    )

            # Phase 11: finalize.
            terminal_stage = ReloadTerminalStage(error_stage)
            diagnostic, wire_result = self._finalize_reload(
                request_id=txn.request_id,
                started_at=started_at,
                txn=txn,
                txn_state=txn.state,
                ok=txn.state == TransactionState.COMPLETED,
                stage=terminal_stage,
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                ignored_sections=(),
                restart_required_sections=(),
                warnings=warnings,
                is_cancelled=True,
                publication_occurred=txn.publication_occurred,
                candidate_cleanup_attempted=(
                    _cleanup_outcome.candidate_abort_attempted
                    if _cleanup_outcome is not None
                    else False
                ),
                candidate_cleanup_succeeded=(
                    _cleanup_outcome.candidate_abort_succeeded
                    if _cleanup_outcome is not None
                    else False
                ),
            )
            self._last_diagnostic_result = diagnostic
            await self._record_terminal_event(diagnostic)
            await self._safe_record_event(
                "reload_cancelled",
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                error=f"cancelled at {error_stage}",
            )
            # Plan 018 Workstream B2: accepted reloads cannot be
            # aborted.  Skip mark_aborting/mark_aborted when the
            # reload was accepted.
            if txn.state != TransactionState.COMPLETED and not txn.reload_accepted:
                if txn.state != TransactionState.ABORTING:
                    txn.mark_aborting(RuntimeError("Reload cancelled"))
                txn.mark_aborted()
            raise
        except Exception as exc:
            error_stage = (
                self._operation_state.stage
                if self._operation_state
                else ReloadOperationStage.IDLE
            )
            logger.exception("Reload failed at stage %s", error_stage)
            # Phase 11: capture pre-abort transaction state for failure
            # classification before mark_aborting() transitions to ABORTING.
            pre_abort_txn_state = txn.state
            # Phase 11: update counters.
            self._counters = replace(
                self._counters,
                commit_failures=self._counters.commit_failures + 1,
            )
            # Phase 6: if we haven't published, abort cleanly.
            # If we have published, attempt compensation.
            compensation_ok = False
            compensation_attempted = False
            if txn.reload_accepted:
                # Plan 018 Workstream B5: post-acceptance — do NOT
                # enter compensation.  The accepted-finalization job
                # handles remaining housekeeping.
                pass
            elif txn.state == TransactionState.RUNTIME_PUBLISHED:
                # Publication succeeded but a post-publication step failed.
                # Compensate by accepting the new generation and retrying
                # process transitions if needed.
                logger.warning(
                    "Post-publication failure for generation %d; "
                    "attempting compensation",
                    generation_id,
                )
                compensation_attempted = True
                compensation_ok = await self._compensate_post_publication(
                    txn,
                    exc,
                    process_transition_plan=process_transition_plan,
                    pending_swap=pending_swap,
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
                    # Phase 11: update compensation failure counter.
                    self._counters = replace(
                        self._counters,
                        compensation_failures=self._counters.compensation_failures + 1,
                    )
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
            # Plan 017 Workstream C: route precommit cleanup through
            # the shared helper.  Skip when compensation succeeded
            # (candidate was already transferred to the runtime manager)
            # or when publication already occurred (candidate is
            # transferred or will be cleaned up by retirement).
            compensation_succeeded = txn.state == TransactionState.COMPLETED
            _cleanup_outcome: PrecommitAbortOutcome | None = None
            if (
                candidate is not None
                and not compensation_succeeded
                and not txn.publication_occurred
            ):
                _cleanup_outcome = await self._abort_precommit_reload(
                    txn=txn,
                    pending_swap=pending_swap,
                    transition_result=transition_result,
                    candidate=candidate,
                    cause=exc,
                    error_stage=error_stage,
                )
            ok = txn.state == TransactionState.COMPLETED
            # Phase 11: derive correct stage from the operation state.
            # The _set_stage() call before the failed step already set
            # the correct stage.  Fall back to error class mapping
            # only if the operation stage is not a valid terminal stage.
            error_class_name = type(exc).__name__
            try:
                terminal_stage = ReloadTerminalStage(error_stage)
            except ValueError:
                terminal_stage = stage_from_error_class(error_class_name)

            # Phase 11: detect granular failure type for precise category.
            is_pub_failed = False
            is_pt_prep_failed = False
            is_pt_apply_failed = False
            is_persist_commit_failed = False
            if pre_abort_txn_state in (
                TransactionState.RUNTIME_PUBLISHED,
                TransactionState.RUNTIME_SWAP_COMMITTED,
            ):
                # Publication succeeded; failure is in a post-publication step.
                if error_class_name == "ReloadCommitError":
                    is_pub_failed = True
                else:
                    is_pt_apply_failed = True
            elif pre_abort_txn_state == TransactionState.RUNTIME_STAGED:
                # Runtime staged but swap not committed; failure is in
                # process transitions or the swap commit path.
                if error_class_name == "ReloadCommitError":
                    is_pub_failed = True
                else:
                    is_pt_apply_failed = True
            elif (
                pre_abort_txn_state == TransactionState.COMMIT_STARTED
                and error_stage == ReloadOperationStage.COMMIT
            ):
                # Failure at commit stage during the transaction.
                if error_class_name == "ReloadCommitError":
                    is_pub_failed = True
                elif error_class_name == "ReloadReconciliationError":
                    is_persist_commit_failed = True
                else:
                    # Process transitions now run inside the transaction;
                    # a RuntimeError here is a transition apply failure,
                    # not a publication failure.
                    is_pt_apply_failed = True
            elif (
                pre_abort_txn_state in (TransactionState.PERSISTENCE_COMMITTED,)
                and error_stage == ReloadOperationStage.COMMIT
            ):
                # Failure at commit stage after persistence.
                if error_class_name == "ReloadReconciliationError":
                    is_persist_commit_failed = True
                else:
                    is_persist_commit_failed = True

            diagnostic, wire_result = self._finalize_reload(
                request_id=txn.request_id,
                started_at=started_at,
                txn=txn,
                txn_state=txn.state,
                ok=ok,
                stage=terminal_stage,
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                ignored_sections=(),
                restart_required_sections=(),
                warnings=warnings,
                error=exc,
                error_class=error_class_name,
                is_compensation_failed=not compensation_ok and compensation_attempted,
                is_publication_failed=is_pub_failed,
                is_process_transition_prepare_failed=is_pt_prep_failed,
                is_process_transition_apply_failed=is_pt_apply_failed,
                is_persistence_commit_failed=is_persist_commit_failed,
                # Plan 016 Workstream E6: wire fields derive from
                # explicit transaction facts (``publication_occurred``)
                # rather than the broad ``is_committing`` state category.
                publication_occurred=txn.publication_occurred,
                persistence_committed=txn.persistence_committed,
                process_transitions_applied=txn.process_transitions_applied,
                compensation_attempted=compensation_attempted,
                compensation_succeeded=compensation_ok,
                candidate_cleanup_attempted=(
                    _cleanup_outcome.candidate_abort_attempted
                    if _cleanup_outcome is not None
                    else False
                ),
                candidate_cleanup_succeeded=(
                    _cleanup_outcome.candidate_abort_succeeded
                    if _cleanup_outcome is not None
                    else False
                ),
            )
            self._last_diagnostic_result = diagnostic
            await self._record_terminal_event(diagnostic)

            event_type = "reload_preparation_failure"
            if error_stage == ReloadOperationStage.RECONCILIATION:
                event_type = "reload_reconciliation_failure"
            if txn.publication_occurred:
                event_type = "reload_post_publication_failure"
            await self._safe_record_event(
                event_type,
                generation_id=generation_id,
                digest_prefix=digest_prefix,
                changed_sections=changed_sections,
                error=f"{exc!r}",
            )
            return wire_result
        finally:
            # Signal transaction completion before clearing the reference
            # so shutdown waiters are notified while the transaction is
            # still accessible for diagnostics.
            self._transaction_complete_event.set()
            # Always release the lease gate on every terminal path —
            # ensures requests resume after cancellation or failure.
            gate_release_task = asyncio.create_task(
                self._runtime_manager.ensure_reload_gate_released()
            )
            try:
                await asyncio.shield(gate_release_task)
            except asyncio.CancelledError:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(gate_release_task)
            finally:
                # Release admission claim on every terminal path, including
                # cancellation while the gate release is in progress.
                self._current_transaction = None
                await self._release_reload_claim()

    async def _claim_reload(self, request_id: str) -> None:
        """Atomically claim the single reload slot.

        The mutex protects only the claim metadata.  In particular, no
        event persistence, finalization retry, validation, database work, or
        generation lifecycle operation may run while it is held.
        """
        async with self._claim_mutex:
            if self._reload_claimed:
                self._counters = replace(
                    self._counters,
                    busy_rejections=self._counters.busy_rejections + 1,
                )
                raise ReloadInProgressError(
                    "A reload transaction is already in progress"
                )
            self._reload_claimed = True
            self._active_reload_task = asyncio.current_task()
            self._admitted_at = time.monotonic()
            self._admitted_request_id = request_id
            self._counters = replace(
                self._counters,
                admitted_operations=self._counters.admitted_operations + 1,
            )
            # Reset the completion event so shutdown waiters see a fresh
            # signal for this new transaction.
            self._transaction_complete_event.clear()

    async def _release_reload_claim(self) -> None:
        """Release claim state under the same short mutex used to acquire it."""
        async with self._claim_mutex:
            self._reload_claimed = False
            self._active_reload_task = None
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

    # -- Phase 11: retirement derivation -----------------------------------

    def _derive_retirement_status(
        self,
        *,
        txn_state: TransactionState,
        old_generation_id: int | None,
    ) -> ReloadRetirementStatus:
        """Derive retirement fields from the runtime manager state.

        Reflects actual tracked retirement tasks rather than inferring
        pending status from result success.
        """
        if old_generation_id is None:
            return ReloadRetirementStatus(retirement_pending=False)

        if txn_state == TransactionState.COMPLETED:
            # Check the retirement task registry which is populated
            # synchronously at create_task time, avoiding a race with
            # the transient diagnostics.retiring list.
            if old_generation_id in self._runtime_manager._retirement_tasks:  # pyright: ignore[reportPrivateUsage]
                return ReloadRetirementStatus(
                    retirement_pending=True,
                    retiring_generation_id=old_generation_id,
                )
            return ReloadRetirementStatus(retirement_pending=False)

        if txn_state in (
            TransactionState.ABORTED,
            TransactionState.COMPENSATION_FAILED,
        ):
            return ReloadRetirementStatus(retirement_pending=False)

        return ReloadRetirementStatus(retirement_pending=False)

    # -- Phase 11: single finalization path --------------------------------

    def _finalize_reload(
        self,
        *,
        request_id: str,
        started_at: float,
        txn: ReloadTransaction,
        txn_state: TransactionState,
        ok: bool,
        stage: ReloadTerminalStage,
        generation_id: int | None,
        digest_prefix: str,
        changed_sections: tuple[str, ...],
        ignored_sections: tuple[str, ...],
        restart_required_sections: tuple[str, ...],
        restart_required_changes: tuple[Any, ...] = (),
        warnings: tuple[ConfigValidationWarning, ...],
        error: Exception | None = None,
        error_class: str | None = None,
        is_noop: bool = False,
        is_ignored_only: bool = False,
        is_restart_required: bool = False,
        is_cancelled: bool = False,
        is_shutdown: bool = False,
        is_compensation_failed: bool = False,
        is_publication_failed: bool = False,
        is_process_transition_prepare_failed: bool = False,
        is_process_transition_apply_failed: bool = False,
        is_persistence_commit_failed: bool = False,
        publication_occurred: bool = False,
        persistence_committed: bool = False,
        process_transitions_applied: bool = False,
        compensation_attempted: bool = False,
        compensation_succeeded: bool = False,
        candidate_cleanup_attempted: bool = False,
        candidate_cleanup_succeeded: bool = False,
        # Plan 019 Workstream G1: finalization status fields.
        finalization_status: str = "completed",
        finalization_next_step: str | None = None,
        finalization_attempt_count: int = 0,
        finalization_failure_count: int = 0,
        finalization_retry_attempt_count: int = 0,
        finalization_last_error_step: str | None = None,
        finalization_last_error_class: str | None = None,
        finalization_last_error_message: str | None = None,
        old_generation_id: int | None = None,
        pending_swap_committed: bool = False,
        accepted_generation_authoritative: bool = True,
    ) -> tuple[ReloadDiagnosticResult, ReloadResult]:
        """Single terminal finalizer for every admitted reload.

        Derives terminal outcome from transaction state and primary error,
        captures active-generation and retirement snapshots, updates
        counters, sets completion time and duration, returns operation
        state to idle, and produces the canonical result.
        """
        import time as _time

        completed_at = _time.time()
        duration_s = _time.monotonic() - started_at

        # Derive category from flags.
        category = classify_result_category(
            ok=ok,
            stage=stage,
            is_noop=is_noop,
            is_ignored_only=is_ignored_only,
            is_restart_required=is_restart_required,
            is_cancelled=is_cancelled,
            is_shutdown=is_shutdown,
            is_compensation_failed=is_compensation_failed,
            is_publication_failed=is_publication_failed,
            is_process_transition_prepare_failed=is_process_transition_prepare_failed,
            is_process_transition_apply_failed=is_process_transition_apply_failed,
            is_persistence_commit_failed=is_persistence_commit_failed,
            error_class=error_class,
            finalization_status=finalization_status,
        )

        # Derive retirement status from runtime manager.
        retirement = self._derive_retirement_status(
            txn_state=txn_state,
            old_generation_id=txn.old_generation_id,
        )

        # Plan 016 Workstream H2: capture the runtime manager's
        # publication_epoch and pending_swap_state at finalization
        # so the diagnostic reflects the manager's view, not just
        # the transaction's local copy.
        try:
            rm_diag = self._runtime_manager.diagnostics()
            txn._publication_epoch = rm_diag.publication_epoch  # pyright: ignore[reportPrivateUsage]
            if (
                txn._pending_swap_state_at_terminal is None  # pyright: ignore[reportPrivateUsage]
                and rm_diag.pending_swap_state is not None
            ):
                txn._pending_swap_state_at_terminal = (  # pyright: ignore[reportPrivateUsage]
                    rm_diag.pending_swap_state
                )
            if not txn._lease_admission_gated_at_terminal:  # pyright: ignore[reportPrivateUsage]
                txn._lease_admission_gated_at_terminal = (  # pyright: ignore[reportPrivateUsage]
                    rm_diag.lease_admission_gated
                )
        except Exception:
            logger.debug(
                "Runtime manager diagnostics unavailable at finalization",
                exc_info=True,
            )

        # Active generation snapshot and old generation digest.
        old_generation_digest: str | None = None
        active_snap = None
        try:
            active_snap = self._runtime_manager.active_snapshot()
            active_generation_id = active_snap.generation_id
            active_generation_digest = active_snap.config_digest
        except Exception:
            active_generation_id = None
            active_generation_digest = None

        # Look up old generation digest from the active snapshot
        # or retiring generations when we have an old generation ID.
        if txn.old_generation_id is not None:
            try:
                diagnostics = self._runtime_manager.diagnostics()
                if (
                    active_snap is not None
                    and active_snap.generation_id == txn.old_generation_id
                ):
                    old_generation_digest = active_snap.config_digest
                else:
                    for retiring_diag in diagnostics.retiring:
                        if retiring_diag.generation_id == txn.old_generation_id:
                            # Only prefix available from diagnostics; use
                            # None to avoid misleading partial digests.
                            old_generation_digest = None
                            break
            except Exception:
                old_generation_digest = None

        # Build warning messages (bounded).
        warning_messages = tuple(
            w if isinstance(w, str) else str(w) for w in warnings[:10]
        )

        # Error classification.
        err_code: str | None = None
        err_class: str | None = error_class
        if error is not None:
            err_code = type(error).__name__
            if err_class is None:
                err_class = err_code

        # Message.
        if ok and is_noop:
            message = "No configuration changes detected"
        elif ok and is_ignored_only:
            message = "Configuration changes detected but all are ignored"
        elif ok:
            message = (
                f"Reload applied: generation {generation_id}, "
                f"{len(changed_sections)} section(s) changed"
            )
        elif is_restart_required:
            n = len(restart_required_sections)
            message = f"Reload rejected: {n} restart-required field(s) changed"
        elif is_cancelled and txn_state == TransactionState.COMPLETED:
            message = "Reload completed despite cancellation"
        elif is_cancelled:
            message = f"Reload cancelled at stage {stage.value}"
        elif is_compensation_failed:
            message = (
                "Reload compensated after post-publication failure"
                if compensation_succeeded
                else f"Reload failed: {error!r}"
            )
        elif error is not None:
            message = f"Reload failed: {error!r}"
        else:
            message = f"Reload failed at stage {stage.value}"

        # Build diagnostic result.
        diagnostic = ReloadDiagnosticResult(
            request_id=request_id,
            category=category,
            terminal_stage=stage,
            admitted_at=self._admitted_at,
            started_at=started_at,
            completed_at=completed_at,
            duration_s=duration_s,
            old_generation_id=txn.old_generation_id,
            old_generation_digest=old_generation_digest,
            candidate_generation_id=generation_id,
            candidate_generation_digest=digest_prefix,
            active_generation_id=active_generation_id,
            active_generation_digest=active_generation_digest,
            changed_sections=changed_sections,
            ignored_sections=ignored_sections,
            restart_required_sections=restart_required_sections,
            semantic_noop=is_noop,
            publication_occurred=publication_occurred,
            persistence_committed=persistence_committed,
            process_transitions_applied=process_transitions_applied,
            compensation_attempted=compensation_attempted,
            compensation_succeeded=compensation_succeeded,
            candidate_cleanup_attempted=candidate_cleanup_attempted,
            candidate_cleanup_succeeded=candidate_cleanup_succeeded,
            retirement=retirement,
            error_code=err_code,
            error_class=err_class,
            message=message,
            warnings=warnings,
            warning_messages=warning_messages,
            counters=self._counters,
            # Plan 016 Workstream H2/H3: surface explicit per-stage
            # progress flags so operators can see *which* post-commit
            # step is still pending.  Each flag flips to ``True`` only
            # when the matching ``ReloadTransaction`` marker has
            # been called; the values reflect the state at
            # finalization time.
            pending_swap_state=txn.pending_swap_state_at_terminal,
            lease_admission_gated=txn.lease_admission_gated_at_terminal,
            post_commit_finalization_pending=txn.post_commit_finalization_pending,
            ownership_transfer_pending=txn.ownership_transfer_pending,
            mirror_update_pending=txn.mirror_update_pending,
            retirement_scheduling_pending=txn.retirement_scheduling_pending,
            publication_epoch=txn.publication_epoch,
            # Plan 020 Workstream D1: canonical finalization fields.
            finalization_status=finalization_status,
            finalization_next_step=finalization_next_step,
            finalization_attempt_count=finalization_attempt_count,
            finalization_failure_count=finalization_failure_count,
            finalization_retry_attempt_count=finalization_retry_attempt_count,
            finalization_last_error_step=finalization_last_error_step,
            finalization_last_error_class=finalization_last_error_class,
            finalization_last_error_message=finalization_last_error_message,
            pending_swap_committed=pending_swap_committed,
            accepted_generation_authoritative=accepted_generation_authoritative,
        )

        # Build the wire-format ReloadResult.
        wire_stage: ReloadStage
        if stage == ReloadTerminalStage.VALIDATION:
            wire_stage = ReloadStage.VALIDATION
        elif stage == ReloadTerminalStage.DIFF:
            wire_stage = ReloadStage.DIFF
        elif stage == ReloadTerminalStage.PREPARATION:
            wire_stage = ReloadStage.PREPARATION
        elif stage == ReloadTerminalStage.RECONCILIATION:
            wire_stage = ReloadStage.RECONCILIATION
        elif stage == ReloadTerminalStage.COMMIT:
            wire_stage = ReloadStage.COMMIT
        elif stage == ReloadTerminalStage.RETIREMENT:
            wire_stage = ReloadStage.RETIREMENT
        else:
            wire_stage = ReloadStage.COMMIT

        wire_result = ReloadResult(
            ok=ok,
            stage=wire_stage,
            generation=generation_id if ok else None,
            changed_sections=changed_sections,
            warnings=warnings,
            restart_required=(
                restart_required_changes
                if restart_required_changes
                else tuple(
                    c
                    for c in txn.restart_required
                    if c.section in restart_required_sections
                )
            ),
            message=message,
            retirement_pending=retirement.retirement_pending,
            retiring_generation_id=retirement.retiring_generation_id,
            # Plan 019 Workstream G1: finalization status.
            finalization_status=finalization_status,
            finalization_next_step=finalization_next_step,
            finalization_attempt_count=finalization_attempt_count,
            finalization_failure_count=finalization_failure_count,
            finalization_retry_attempt_count=finalization_retry_attempt_count,
            finalization_last_error_step=finalization_last_error_step,
            finalization_last_error_class=finalization_last_error_class,
            finalization_last_error_message=finalization_last_error_message,
            old_generation_id=old_generation_id,
            pending_swap_committed=pending_swap_committed,
        )

        # Store internal result (backward compat).
        self._last_reload_result = ReloadOperationResult(
            ok=ok,
            stage=stage.value,
            generation=generation_id,
            changed_sections=changed_sections,
            warnings=warnings,
            restart_required=(
                restart_required_changes
                if restart_required_changes
                else tuple(txn.restart_required)
            ),
            retirement_pending=retirement.retirement_pending,
            message=message,
            duration_s=duration_s,
        )
        self._last_reload_completed_at = completed_at

        # Phase 11: append to bounded reload history.
        self._reload_history.append(diagnostic)
        if len(self._reload_history) > self._reload_history_max:
            self._reload_history = self._reload_history[-self._reload_history_max :]

        return diagnostic, wire_result

    async def _record_event(
        self,
        event_type: str,
        *,
        generation_id: int | None = None,
        digest_prefix: str = "",
        changed_sections: tuple[str, ...] = (),
        error: str | None = None,
        finalization_status: str | None = None,
        finalization_next_step: str | None = None,
        finalization_attempt_count: int | None = None,
        finalization_failure_count: int | None = None,
        finalization_retry_attempt_count: int | None = None,
        finalization_retirement_retry_attempt_count: int | None = None,
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
        if finalization_status is not None:
            details["finalization_status"] = finalization_status
        if finalization_next_step is not None:
            details["finalization_next_step"] = finalization_next_step
        if finalization_attempt_count is not None:
            details["finalization_attempt_count"] = finalization_attempt_count
        if finalization_failure_count is not None:
            details["finalization_failure_count"] = finalization_failure_count
        if finalization_retry_attempt_count is not None:
            details["finalization_retry_attempt_count"] = (
                finalization_retry_attempt_count
            )
        if finalization_retirement_retry_attempt_count is not None:
            details["finalization_retirement_retry_attempt_count"] = (
                finalization_retirement_retry_attempt_count
            )
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

    async def _record_terminal_event(
        self,
        diagnostic: ReloadDiagnosticResult,
    ) -> None:
        """Record a terminal operational event for the given diagnostic.

        Called after every admitted reload reaches its finalizer.
        Best-effort: failures are swallowed so they never break reload.
        """
        event_type = f"reload_terminal_{diagnostic.category.value}"
        await self._safe_record_event(
            event_type,
            generation_id=diagnostic.candidate_generation_id,
            digest_prefix=diagnostic.candidate_generation_digest or "",
            changed_sections=diagnostic.changed_sections,
        )
        # Update the diagnostic result to reflect event was recorded.
        object.__setattr__(diagnostic, "operational_event_recorded", True)

    # -- step implementations ----------------------------------------------

    async def _validate_digest(
        self,
        validation: ConfigValidationResult,
        expected: str | None,
    ) -> None:
        """Verify the content digest matches the caller's expectation."""
        if expected is not None and expected != validation.content_digest:
            raise ReloadDigestMismatchError(
                "Content digest mismatch: expected "
                f"{expected[:12]}… got {validation.content_digest[:12]}…",
                expected=expected,
                actual=validation.content_digest,
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
        *,
        candidate_registry: Any | None = None,
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
        authentication_reset_names: list[str] = []
        if candidate_registry is not None:
            active = self._runtime_manager.active_snapshot()
            old_registry = active.registry
            for candidate_state in candidate_registry.get_all_states():
                if not candidate_state.enabled:
                    continue
                account_name = candidate_state.name
                old_state = old_registry.get_state(account_name)
                if old_state is None:
                    continue
                old_config = old_registry.get_account_config(account_name)
                new_config = candidate_registry.get_account_config(account_name)
                credential_changed = old_registry.get_api_key(
                    account_name
                ) != candidate_registry.get_api_key(account_name)
                config_identity_changed = old_registry.get_provider_for_account(
                    account_name
                ) != candidate_registry.get_provider_for_account(account_name) or (
                    old_config is not None
                    and new_config is not None
                    and old_config.api_key_env != new_config.api_key_env
                )
                reenabled = not old_state.enabled
                if credential_changed or config_identity_changed or reenabled:
                    authentication_reset_names.append(account_name)
        return PersistenceDelta(
            configured_providers=configured_providers,
            config_accounts=tuple(config_accounts),
            authentication_reset_names=tuple(sorted(authentication_reset_names)),
        )

    def _prepare_process_transitions(
        self,
        candidate_config: AppConfig,
        *,
        runtime_manager: RuntimeManager | None = None,
        generation_id: int = 0,
        config_digest: str = "",
        routing_trace_guard: Any | None = None,
    ) -> ProcessTransitionPlan:
        """Calculate process-supervisor task specs without applying them.

        Returns a :class:`ProcessTransitionPlan` that the commit step
        applies after publication.  Includes transitions for:

        - Task spec diff (process supervisor reconfiguration)
        - Routing-trace writer reconfiguration
        - Routing-trace guard reconfiguration
        - Effective state (app.state compatibility mirrors)
        """
        if self.TEST_INJECT_PROCESS_TRANSITION_PLAN is not None:
            return self.TEST_INJECT_PROCESS_TRANSITION_PLAN
        process = self._process
        process_supervisor = process.process_supervisor
        transitions: list[ProcessTransition] = []

        # Task spec transition
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
            transitions.append(
                TaskSpecTransition(
                    process_supervisor=process_supervisor,
                    candidate_specs=candidate_specs,
                    callback_factories=callback_factories,
                    process=process,
                )
            )
        else:
            candidate_specs = ()
            callback_factories: dict[str, Any] = {}

        # Routing-trace writer transition
        routing_trace_writer = getattr(process, "routing_trace_writer", None)
        if routing_trace_writer is not None:
            transitions.append(
                RoutingTraceWriterTransition(
                    writer=routing_trace_writer,
                    mode=candidate_config.routing.trace.mode,
                    sample_rate=candidate_config.routing.trace.sample_rate,
                )
            )

        # Routing-trace guard transition. The guard belongs to the candidate
        # generation, so applying this transition cannot mutate the active
        # generation or a process-global singleton.
        if routing_trace_guard is not None:
            transitions.append(
                RoutingTraceGuardTransition(
                    guard=routing_trace_guard,
                    threshold_ms=(
                        candidate_config.routing.trace.skip_above_lock_wait_p95_ms
                    ),
                    queue_occupancy_threshold=(
                        candidate_config.routing.trace.guard_queue_occupancy_threshold
                    ),
                    oldest_event_age_s=(
                        candidate_config.routing.trace.guard_oldest_event_age_s
                    ),
                    cooldown_s=candidate_config.routing.trace.guard_cooldown_s,
                )
            )

        # Effective state transition (app.state mirrors)
        if self._app is not None:
            transitions.append(
                EffectiveStateTransition(
                    app_state=getattr(self._app, "state", None),
                    config=candidate_config,
                    config_digest=config_digest,
                    generation_id=generation_id,
                )
            )

        return ProcessTransitionPlan(
            task_specs=candidate_specs,
            callback_factories=callback_factories,
            transitions=tuple(transitions),
        )

    async def _apply_persistence_delta(
        self,
        delta: PersistenceDelta,
        *,
        nested: bool = False,
    ) -> None:
        """Apply a prepared persistence delta inside a SQLite transaction.

        When *nested* is ``True`` the caller owns the outer transaction;
        this method executes the writes without opening its own
        ``BEGIN IMMEDIATE`` block, allowing the outer transaction to
        commit or roll back atomically.  When ``False`` (the default)
        the method creates its own transaction for backward
        compatibility with direct callers.
        """
        from eggpool.db.repositories import (  # noqa: PLC0415
            AccountBackoffRepository,
            AccountRepository,
            ProviderRepository,
        )

        db = self._process.db
        try:
            if nested:
                # Piggyback on the caller's transaction — the ContextVar
                # nesting logic in Database.transaction() handles this.
                provider_repo = ProviderRepository(db)
                await provider_repo.sync_from_config(delta.configured_providers)

                account_repo = AccountRepository(db)
                await account_repo.sync_from_config(list(delta.config_accounts))
                backoff_repo = AccountBackoffRepository(db)
                for account_name in delta.authentication_reset_names:
                    account_id = await account_repo.get_id_by_name(account_name)
                    if account_id is not None:
                        await backoff_repo.clear_authentication(account_id)
            else:
                async with db.transaction():
                    provider_repo = ProviderRepository(db)
                    await provider_repo.sync_from_config(delta.configured_providers)

                    account_repo = AccountRepository(db)
                    await account_repo.sync_from_config(list(delta.config_accounts))
                    backoff_repo = AccountBackoffRepository(db)
                    for account_name in delta.authentication_reset_names:
                        account_id = await account_repo.get_id_by_name(account_name)
                        if account_id is not None:
                            await backoff_repo.clear_authentication(account_id)
        except Exception as exc:
            logger.exception("Persistence delta application failed")
            raise ReloadReconciliationError(
                f"Failed to apply persistence delta: {exc!r}"
            ) from exc

    async def _apply_process_transitions(
        self,
        plan: ProcessTransitionPlan,
        *,
        result: TransitionApplyResult | None = None,
    ) -> TransitionApplyResult:
        """Apply prepared process transitions.

        Called after publication so the process supervisor is only
        reconfigured when the new generation is already live.
        Each transition's ``apply()`` method is called in order,
        regardless of whether task specs are present — independent
        transitions (routing-trace writer/guard, effective-state) still
        execute even when the process supervisor is absent.

        Returns a :class:`TransitionApplyResult` for rollback tracking.

        Plan 017 Workstream B: on partial failure the
        :class:`ProcessTransitionApplyError` carries a reference to
        the partial result so callers can perform rollback without
        losing the old-state snapshots captured during apply.

        Plan 018 Workstream A1: the caller can provide a pre-created
        :class:`TransitionApplyResult` to own the result lifecycle
        before ``apply_all()`` is called.
        """
        if result is None:
            result = TransitionApplyResult(plan)
        try:
            await result.apply_all()
        except ProcessTransitionApplyError as exc:
            exc.transition_result = result
            raise
        return result

    async def _abort_precommit_reload(
        self,
        *,
        txn: ReloadTransaction,
        pending_swap: Any,
        transition_result: TransitionApplyResult | None,
        candidate: RuntimeGenerationCandidate | None,
        cause: BaseException,
        error_stage: str = "unknown",
    ) -> PrecommitAbortOutcome:
        """Shared precommit cleanup owner for the reload transaction.

        Plan 017 Workstream C: every precommit cleanup path
        (``except Exception`` and ``except asyncio.CancelledError``)
        funnels through this helper so the cleanup semantics are
        identical regardless of the failure class.  The helper is
        idempotent — pending swap rollback, transition rollback,
        candidate abort, and lease-gate clearing are all safe to
        repeat.

        Plan 019 Workstream F2: accepted transactions must never
        enter precommit cleanup.  A defensive assertion rejects
        the call immediately.

        Order of operations:

        0. Reject accepted transactions (defensive invariant).
        1. Rollback the staged pending swap so the lease gate is
           reopened and the old slot is restored as active.
        2. Rollback any applied process transitions in reverse order.
           Aggregation errors are logged but do not mask the primary
           cause.
        3. Abort candidate resources if the candidate has not been
           transferred to the runtime manager.
        4. Verify old generation is still active and admission is open.
        5. Capture cleanup diagnostics.
        6. Return structured outcome.

        Plan 017 Workstream B: transition_result may carry a partial
        result even when the caller did not receive it (e.g. the
        ProcessTransitionApplyError was caught and the result attached
        to it).  Rollback uses the structured
        :class:`TransitionRollbackOutcome` return type.
        """
        # Plan 019 Workstream F2: accepted reloads cannot enter
        # precommit cleanup.  This is a correctness assertion.
        if txn.reload_accepted:
            raise TransactionStateError(
                "accepted reload cannot enter precommit cleanup"
            )
        swap_rollback_attempted = False
        swap_rollback_succeeded = False
        transition_rollback_outcome: TransitionRollbackOutcome | None = None
        candidate_abort_attempted = False
        candidate_abort_succeeded = False
        candidate_cleanup_diag: CleanupDiagnostics | None = None
        admission_reopened = False
        degraded = False

        # 1. Rollback staged swap so the lease gate is reopened.
        if (
            pending_swap is not None
            and pending_swap.staged
            and not pending_swap.committed
        ):
            swap_rollback_attempted = True
            try:
                await pending_swap.rollback()
                swap_rollback_succeeded = True
            except RuntimeManagerSwapStateError:
                # Idempotent — already rolled back or terminal.
                swap_rollback_succeeded = True
                logger.debug("Pending swap rollback skipped; not in staged state")
            except Exception as exc:
                degraded = True
                logger.warning("Pending swap rollback raised: %r", exc, exc_info=True)

        # 2. Rollback applied process transitions in reverse order.
        if transition_result is not None:
            try:
                transition_rollback_outcome = await transition_result.rollback_applied()
                if transition_rollback_outcome.failures:
                    degraded = True
                    logger.warning(
                        "Process transition rollback errors during precommit abort: %s",
                        [
                            (name, f"{type(exc).__name__}: {exc}")
                            for name, exc in transition_rollback_outcome.failures
                        ],
                    )
            except Exception as exc:
                degraded = True
                logger.warning(
                    "Transition rollback aggregation failed: %r",
                    exc,
                    exc_info=True,
                )

            # 3. Abort candidate resources if not yet transferred.
        #    Use getattr for backward compat with CandidateGeneration
        #    mocks that lack ownership_state / abort.
        if candidate is not None:
            candidate_abort_fn = getattr(candidate, "abort", None)
            candidate_state = getattr(candidate, "ownership_state", None)
            if candidate_abort_fn is not None:
                from eggpool.runtime_manager import (  # noqa: PLC0415
                    CandidateOwnershipState,
                )

                should_abort = False
                if isinstance(candidate_state, CandidateOwnershipState):
                    if candidate_state not in (
                        CandidateOwnershipState.TRANSFERRED,
                        CandidateOwnershipState.ABORTED,
                    ):
                        should_abort = True
                else:
                    # Plan 020 Workstream F5: normalize once, then compare
                    # against canonical lowercase values.
                    state_value = getattr(candidate_state, "value", candidate_state)
                    if state_value not in ("transferred", "aborted"):
                        should_abort = True

                if should_abort:
                    candidate_abort_attempted = True
                    try:
                        candidate_cleanup_diag = await candidate.abort(
                            cause=cause,
                            failure_stage=error_stage,
                        )
                        candidate_abort_succeeded = True
                        self._last_cleanup_diagnostics = candidate_cleanup_diag
                    except Exception as exc:
                        degraded = True
                        logger.warning("Candidate abort failed: %r", exc, exc_info=True)
                        # Fall back to candidate.diagnostics if available.
                        candidate_cleanup_diag = getattr(candidate, "diagnostics", None)
                        if candidate_cleanup_diag is not None:
                            self._last_cleanup_diagnostics = candidate_cleanup_diag

        # 4. Verify old generation is active and admission is open.
        try:
            self._runtime_manager.active_snapshot()
            admission_reopened = True
        except Exception:
            admission_reopened = False
            degraded = True

        return PrecommitAbortOutcome(
            swap_rollback_attempted=swap_rollback_attempted,
            swap_rollback_succeeded=swap_rollback_succeeded,
            transition_rollback_outcome=transition_rollback_outcome,
            candidate_abort_attempted=candidate_abort_attempted,
            candidate_abort_succeeded=candidate_abort_succeeded,
            candidate_cleanup_diagnostics=candidate_cleanup_diag,
            admission_reopened=admission_reopened,
            degraded=degraded,
            primary_error=str(cause),
        )

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
        pending_swap: Any | None = None,
    ) -> bool:
        """Attempt to compensate for a post-publication failure.

        Plan 016 Workstream E2 (commit acceptance): once the
        candidate slot is the active slot (``is_candidate_slot_active``
        on the pending swap), the reload has been *accepted* — the
        new generation is live and accepting leases.  Compensation
        must therefore NOT attempt rollback; it only retries
        post-publication bookkeeping that may have failed.

        The compensation strategy is:

        1. If the failure was in process transitions, retry applying
           them — the process supervisor can safely reconfigure after
           publication.
        2. Accept the new generation regardless — the persistence
           delta is idempotent and will be re-synced on the next
           reload.
        3. If the pending swap reports its candidate slot is already
           active, the reload was accepted even when post-publication
           housekeeping failed; warn but do not attempt rollback.

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

        # Plan 016 Workstream E2: detect "candidate already active"
        # so the compensation path does not duplicate work the
        # ``PendingGenerationSwap`` already finalized.  This catches
        # the race where ``commit()`` succeeded and the swap moved
        # to ``COMMITTED`` state but a subsequent bookkeeping step
        # raised — in that case the new generation is live and
        # accepting leases, so compensation is a no-op.
        swap_already_active = pending_swap is not None and getattr(
            pending_swap, "is_candidate_slot_active", False
        )
        if swap_already_active:
            logger.info(
                "Post-publication compensation: candidate slot is "
                "already active for generation %d; reload accepted; "
                "treating post-publication failure as housekeeping",
                txn.generation_id,
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
            candidate_already_active=swap_already_active,
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
        *,
        pending_swap: Any | None = None,
        transition_result: TransitionApplyResult | None = None,
    ) -> None:
        """Atomically publish the candidate generation.

        The normal compatibility path is split into three explicit phases:

        1. ``_prepare_swap`` — capture the active generation identity
           and the candidate generation object.  Failures here abort
           the candidate before ownership transfers.
        2. ``_commit_publication`` — invoke
           :meth:`RuntimeManager.install_candidate` which performs the
           pointer swap.  Failures here leave the SQLite transaction
           rolled back (persistence + publication live inside the same
           transaction).
        3. ``_finalize_retirement_handling`` — transfer candidate
           ownership to the runtime manager (so the candidate's
           registered closeables are never re-closed) and mirror the
           new generation onto ``app.state`` for synchronous consumers.

        Splitting these phases lets the transaction state machine
        record publication facts as soon as each step succeeds, even
        if a later step fails.  See the ``prepared-swap protocol``
        section of :mod:`architecture.reload`.

        The live reload path supplies a staged swap and transition result;
        in that mode this seam applies process transitions inside the
        transaction and the caller commits the staged swap afterward.
        """
        if self.TEST_INJECT_PUBLISH_FAILURE is not None:
            raise self.TEST_INJECT_PUBLISH_FAILURE
        if pending_swap is not None:
            if transition_result is None:
                raise ReloadCommitError("A pending swap requires a transition result")
            if self.TEST_INJECT_TRANSITION_APPLY_FAILURE is not None:
                raise self.TEST_INJECT_TRANSITION_APPLY_FAILURE
            await transition_result.apply_all()
            return
        swap = self._prepare_swap(candidate)
        # At this point the active generation identity and the
        # candidate generation are captured but no pointer swap has
        # occurred.  Any exception raised in ``_commit_publication``
        # below propagates as a ``ReloadCommitError`` and leaves the
        # SQLite transaction to roll back atomically.
        await self._commit_publication(swap)
        self._finalize_retirement_handling(swap)

    def _prepare_swap(
        self,
        candidate: CandidateGeneration | RuntimeGenerationCandidate,
    ) -> _PreparedSwap:
        """Capture publication inputs without mutating any state."""
        generation: RuntimeGeneration | None = getattr(
            candidate, "generation", None
        ) or getattr(candidate, "_built_generation", None)  # pyright: ignore[reportPrivateUsage]
        if generation is None:
            raise ReloadCommitError(
                "Candidate has no generation; was mark_prepared() called?"
            )
        active = self._runtime_manager.active_snapshot()
        return _PreparedSwap(
            candidate=candidate,
            generation=generation,
            active_generation_id=active.generation_id,
            drain_timeout_s=self._drain_timeout_s,
        )

    async def _commit_publication(
        self,
        swap: _PreparedSwap,
    ) -> None:
        """Swap the active slot and retire the previous generation."""
        try:
            await self._runtime_manager.install_candidate(
                swap.generation,
                drain_timeout_s=swap.drain_timeout_s,
                expected_active_generation_id=swap.active_generation_id,
            )
        except Exception as exc:
            logger.exception("Generation publication failed")
            raise ReloadCommitError(f"Failed to publish generation: {exc!r}") from exc

    def _finalize_retirement_handling(
        self,
        swap: _PreparedSwap,
    ) -> None:
        """Transfer candidate ownership and mirror onto ``app.state``."""
        transfer_fn = getattr(swap.candidate, "transfer_to_runtime_manager", None)
        if transfer_fn is not None:
            transfer_fn()
        if self._app is not None:
            from eggpool.app import (  # noqa: PLC0415
                mirror_generation_on_app_state,
            )

            mirror_generation_on_app_state(self._app, swap.generation)


__all__ = [
    "AcceptedReloadFinalization",
    "CandidateGeneration",
    "PrecommitAbortOutcome",
    "ReloadAcceptanceState",
    "ReloadCommitError",
    "ReloadCounters",
    "ReloadDiagnosticResult",
    "ReloadInProgressError",
    "ReloadManager",
    "ReloadObserver",
    "ReloadOperationResult",
    "ReloadOperationStage",
    "ReloadOperationState",
    "ReloadPreparationError",
    "ReloadReconciliationError",
    "ReloadResultCategory",
    "ReloadRetirementStatus",
    "ReloadTerminalStage",
    # Phase 6 transaction types (re-exported for convenience)
    "PersistenceDelta",
    "ProcessTransitionPlan",
    "ReloadTransaction",
    "TransactionState",
]
