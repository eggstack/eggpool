"""Accepted-reload finalization job (Plan 018/019/020).

Every accepted reload creates exactly one process-owned
:class:`AcceptedReloadFinalizationJob` before the first
post-acceptance await.  The job retains strong references to all
state needed for retry and executes idempotent finalization steps
in a declared order.

Design principles
-----------------

- Completed steps are not repeated on retry.
- Failure at one step leaves the job registered at that exact step.
- Cancellation leaves the job registered and the transaction accepted.
- No preacceptance cleanup is invoked from any finalization step.
- Retirement scheduling is exactly once through pending-swap
  idempotence.
- Progress and health are separate concepts: the progress cursor
  identifies the next required step; health records whether the
  latest attempt failed.
- Only ``COMPLETED`` progress is terminal; there is no degraded
  completion state for retryable operational work.
- Single-flight execution is provided by a process-owned retained
  task wrapper.  Concurrent ``run()`` callers share one executed
  task.  Waiter cancellation does not cancel the retained task.
- An unknown or invalid progress state raises
  :class:`AcceptedFinalizationInvariantError` and is retained as
  unresolved -- never silently converted to ``COMPLETED``.
- Distinct counters separate attempts, failures, retry attempts,
  and retirement-step retry attempts so operators can attribute
  reload health without ambiguity.
- Active error fields are cleared after a successful recovery;
  prior failure information is retained only as bounded,
  copy-on-record diagnostic data included in the lightweight
  :class:`AcceptedFinalizationRecord`.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.errors import AcceptedFinalizationInvariantError

if TYPE_CHECKING:
    from eggpool.reload_transaction import ReloadTransaction, TransitionApplyResult
    from eggpool.runtime_manager import (
        RuntimeGeneration,
        RuntimeGenerationCandidate,
    )

logger = logging.getLogger(__name__)

FINALIZATION_HISTORY_MAX: int = 32


class AcceptedFinalizationStep(enum.Enum):
    """Monotonic progress cursor of accepted-reload finalization.

    The job advances through these steps in order.  Once a step
    succeeds the next ``run()`` resumes from the following step.
    Failure does not advance the cursor.
    """

    REGISTERED = "registered"
    OWNERSHIP_TRANSFERRED = "ownership_transferred"
    MIRROR_UPDATED = "mirror_updated"
    TRANSITIONS_FINALIZED = "transitions_finalized"
    OBSERVER_REPORTED = "observer_reported"
    RETIREMENT_SCHEDULING = "retirement_scheduling"
    RETIREMENT_SCHEDULED = "retirement_scheduled"
    TRANSACTION_COMPLETED = "transaction_completed"
    COMPLETED = "completed"


class AcceptedFinalizationHealth(enum.Enum):
    """Attempt outcome independent of progress.

    Health records whether the latest attempt failed.  It does not
    participate in progress guards or completion checks.
    """

    READY = "ready"
    RUNNING = "running"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"


class FinalizationStatus(enum.Enum):
    """Operator-visible finalization status.

    Plan 020 Workstream D2: distinguishes accepted-and-fully-completed
    reloads from accepted-but-pending reloads.
    """

    COMPLETED = "completed"
    RETRY_PENDING = "retry_pending"
    RETIREMENT_SCHEDULE_FAILED = "retirement_schedule_failed"
    SHUTDOWN_ADOPTED = "shutdown_adopted"
    INVARIANT_FAILED = "invariant_failed"


@dataclass(frozen=True)
class AcceptedFinalizationRecord:
    """Lightweight immutable diagnostic record for completed jobs.

    Contains only scalar or immutable diagnostic data.  No live
    runtime objects are retained.
    """

    request_id: str
    generation_id: int
    old_generation_id: int | None
    completion_status: str
    attempts: int
    failure_count: int
    retry_attempt_count: int
    retirement_retry_attempt_count: int
    last_failed_step: str | None
    last_error_class: str | None
    last_error_message: str | None
    completed_at: float
    duration_s: float


@dataclass(frozen=True)
class AcceptedFinalizationOutcome:
    """Structured scalar-only outcome of a finalization attempt.

    Plan 020 Workstream B2: callers receive a complete view of the
    finalization state so they can reconcile counters and history
    without having to inspect the live job object.
    """

    completed: bool
    next_step: str | None
    attempt_count: int
    failure_count: int
    retry_attempt_count: int
    retirement_retry_attempt_count: int
    failed_step: str | None
    error_class: str | None
    error_message: str | None
    retry_permitted: bool
    status: FinalizationStatus


@dataclass
class AcceptedReloadFinalizationJob:
    """Process-owned finalization job for accepted reloads.

    Created synchronously before the first post-acceptance await.
    Runs idempotently -- completed steps are not repeated.

    The job retains strong references to all state needed for retry:

    - transaction
    - candidate container
    - committed pending swap
    - transition result
    - published generation
    - old generation ID and slot ownership through the pending swap
    - app compatibility mirror target
    - observer/reporting metadata

    Plan 019 Workstream A: progress and health are separate.  The
    progress cursor identifies the next required step; health records
    whether the latest attempt failed.  Only ``COMPLETED`` progress
    is terminal.

    Plan 019 Workstream A3: single-flight execution via a retained
    task wrapper.  Concurrent ``run()`` callers share one actual
    attempt; waiter cancellation does not cancel the retained task.

    Plan 019 Workstream C: operational references are released
    after completion via :meth:`release_references`.

    Plan 020 Workstream B: attempt, failure, retry, and retirement
    retry counters are distinct.  Active error fields are cleared
    after successful recovery.  Unknown progress becomes an
    invariant error and is never treated as completion.
    """

    request_id: str
    generation_id: int
    old_generation_id: int | None
    transaction: ReloadTransaction
    candidate: RuntimeGenerationCandidate
    pending_swap: Any  # PendingGenerationSwap
    transition_result: TransitionApplyResult | None
    published_generation: RuntimeGeneration
    app: Any | None
    observer: Any  # ReloadObserver
    #: Test-only seam -- when set on the reload manager, the job
    #: raises it after ownership transfer (for testing post-acceptance
    #: cancellation).  Checked dynamically at runtime so clearing the
    #: seam on the manager takes effect immediately.
    _reload_manager: Any = field(default=None, repr=False)

    _step: AcceptedFinalizationStep = field(
        default=AcceptedFinalizationStep.REGISTERED,
        repr=False,
    )
    _health: AcceptedFinalizationHealth = field(
        default=AcceptedFinalizationHealth.READY,
        repr=False,
    )
    _attempt_count: int = field(default=0, repr=False)
    _failure_count: int = field(default=0, repr=False)
    _retry_attempt_count: int = field(default=0, repr=False)
    _retirement_retry_attempt_count: int = field(default=0, repr=False)
    _last_error_step: str | None = field(default=None, repr=False)
    _last_error_class: str | None = field(default=None, repr=False)
    _last_error_message: str | None = field(default=None, repr=False)
    _completed_at: float | None = field(default=None, repr=False)
    _started_at: float | None = field(default=None, repr=False)
    _reconciled: bool = field(default=False, repr=False)
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _run_task: asyncio.Task[AcceptedFinalizationOutcome] | None = field(
        default=None,
        repr=False,
    )
    _released: bool = field(default=False, repr=False)

    # -- public properties ------------------------------------------------

    @property
    def step(self) -> AcceptedFinalizationStep:
        """Current finalization progress cursor."""
        return self._step

    @property
    def health(self) -> AcceptedFinalizationHealth:
        """Current attempt health."""
        return self._health

    @property
    def attempts(self) -> int:
        """Number of actual finalization attempts."""
        return self._attempt_count

    @property
    def failure_count(self) -> int:
        """Number of failed attempts."""
        return self._failure_count

    @property
    def retry_attempt_count(self) -> int:
        """Number of attempts after the first (whether failed or succeeded)."""
        return self._retry_attempt_count

    @property
    def retirement_retry_attempt_count(self) -> int:
        """Number of retry attempts that started at ``RETIREMENT_SCHEDULING``."""
        return self._retirement_retry_attempt_count

    @property
    def last_error_step(self) -> str | None:
        """Step that failed last, or ``None``."""
        return self._last_error_step

    @property
    def last_error_class(self) -> str | None:
        """Class name of last error, or ``None``."""
        return self._last_error_class

    @property
    def last_error_message(self) -> str | None:
        """Message of last error, or ``None``."""
        return self._last_error_message

    @property
    def is_complete(self) -> bool:
        """True only when every required step completed."""
        return self._step is AcceptedFinalizationStep.COMPLETED

    @property
    def is_unresolved(self) -> bool:
        """True when the job is not yet complete."""
        return not self.is_complete

    @property
    def status(self) -> FinalizationStatus:
        """Operator-visible finalization status."""
        if self.is_complete:
            return FinalizationStatus.COMPLETED
        if self._step is AcceptedFinalizationStep.RETIREMENT_SCHEDULING:
            return FinalizationStatus.RETIREMENT_SCHEDULE_FAILED
        if self._last_error_class == "AcceptedFinalizationInvariantError":
            return FinalizationStatus.INVARIANT_FAILED
        return FinalizationStatus.RETRY_PENDING

    # -- single-flight execution ------------------------------------------

    async def run(self) -> AcceptedFinalizationOutcome:
        """Execute incomplete steps idempotently.

        Plan 020 Workstream B1: a single retained task owns each
        attempt.  Concurrent callers share the same task via
        ``asyncio.shield``; waiter cancellation does not cancel the
        task.  The lock exists only to serialize task creation and
        cleanup, not to serialize the attempt itself.

        Returns a structured :class:`AcceptedFinalizationOutcome`.
        """
        if self.is_complete:
            return self._completed_outcome()
        async with self._run_lock:
            task = self._run_task
            if task is None or task.done():
                task = asyncio.create_task(self._run_attempt())
                self._run_task = task
        return await asyncio.shield(task)

    async def _run_attempt(self) -> AcceptedFinalizationOutcome:
        """Single-shot attempt that owns the retained task lifecycle.

        Plan 020 Workstream B3: callers always wait through
        ``asyncio.shield`` so cancelling a waiter does not cancel
        this task.  Timeout propagates to the caller but the
        attempt continues to completion.
        """
        if self._reconciled:
            return self._completed_outcome()
        self._attempt_count += 1
        if self._started_at is None:
            self._started_at = time.monotonic()
        previous_step = self._step
        is_retry = self._attempt_count > 1
        if is_retry:
            self._retry_attempt_count += 1
            if previous_step is AcceptedFinalizationStep.RETIREMENT_SCHEDULING:
                self._retirement_retry_attempt_count += 1
        self._health = AcceptedFinalizationHealth.RUNNING

        _step_dispatch: dict[
            AcceptedFinalizationStep,
            tuple[str, Any],
        ] = {
            AcceptedFinalizationStep.REGISTERED: (
                "ownership_transfer",
                self._step_ownership_transfer,
            ),
            AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED: (
                "mirror_update",
                self._step_mirror_update,
            ),
            AcceptedFinalizationStep.MIRROR_UPDATED: (
                "transitions_finalization",
                self._step_transitions_finalization,
            ),
            AcceptedFinalizationStep.TRANSITIONS_FINALIZED: (
                "observer_report",
                self._step_observer_report,
            ),
            AcceptedFinalizationStep.OBSERVER_REPORTED: (
                "retirement_scheduling",
                self._step_retirement_scheduling,
            ),
            AcceptedFinalizationStep.RETIREMENT_SCHEDULED: (
                "transaction_completion",
                self._step_transaction_completion,
            ),
        }

        try:
            while self._step is not AcceptedFinalizationStep.COMPLETED:
                dispatch = _step_dispatch.get(self._step)
                if dispatch is None:
                    raise AcceptedFinalizationInvariantError(
                        "No dispatch for accepted-finalization step "
                        f"{self._step.value!r}",
                        step=self._step.value,
                        request_id=self.request_id,
                        generation_id=self.generation_id,
                    )
                step_name, step_fn = dispatch
                self._last_error_step = step_name
                await step_fn()
                # Plan 020 Workstream C4: clear active error fields
                # after a successful step so completed history does
                # not carry stale error context.
                self._last_error_step = None
                self._last_error_class = None
                self._last_error_message = None
        except AcceptedFinalizationInvariantError as exc:
            # Plan 020 Workstream B5: an invariant failure is not a
            # normal retry.  Record failure state and re-raise so the
            # caller observes the typed error.  The job stays in the
            # active registry with status=invariant_failed.
            self._failure_count += 1
            self._last_error_class = type(exc).__name__
            self._last_error_message = str(exc)
            self._health = AcceptedFinalizationHealth.RETRY_PENDING
            logger.warning(
                "Accepted finalization invariant for generation %d: %r",
                self.generation_id,
                exc,
                exc_info=True,
            )
            raise
        except Exception as exc:
            self._failure_count += 1
            self._last_error_class = type(exc).__name__
            self._last_error_message = str(exc)
            self._health = AcceptedFinalizationHealth.RETRY_PENDING
            logger.warning(
                "Accepted finalization step %s failed for generation %d: %r",
                self._last_error_step,
                self.generation_id,
                exc,
                exc_info=True,
            )
            return self._outcome(retry_permitted=True)

        self._health = AcceptedFinalizationHealth.COMPLETED
        self._completed_at = time.monotonic()
        return self._outcome(retry_permitted=False)

    def _outcome(self, *, retry_permitted: bool) -> AcceptedFinalizationOutcome:
        """Build a structured outcome from the current job state."""
        completed = self.is_complete
        return AcceptedFinalizationOutcome(
            completed=completed,
            next_step=None if completed else self._step.value,
            attempt_count=self._attempt_count,
            failure_count=self._failure_count,
            retry_attempt_count=self._retry_attempt_count,
            retirement_retry_attempt_count=self._retirement_retry_attempt_count,
            failed_step=self._last_error_step,
            error_class=self._last_error_class,
            error_message=self._last_error_message,
            retry_permitted=retry_permitted,
            status=FinalizationStatus.COMPLETED if completed else self.status,
        )

    def _completed_outcome(self) -> AcceptedFinalizationOutcome:
        """Return a completed-only outcome for retry-after-completion callers."""
        return AcceptedFinalizationOutcome(
            completed=True,
            next_step=None,
            attempt_count=self._attempt_count,
            failure_count=self._failure_count,
            retry_attempt_count=self._retry_attempt_count,
            retirement_retry_attempt_count=self._retirement_retry_attempt_count,
            failed_step=None,
            error_class=None,
            error_message=None,
            retry_permitted=False,
            status=FinalizationStatus.COMPLETED,
        )

    # -- individual steps ---------------------------------------------------

    async def _step_ownership_transfer(self) -> None:
        """Transfer candidate ownership to the runtime manager."""
        if self._step != AcceptedFinalizationStep.REGISTERED:
            return
        transfer_fn = getattr(self.candidate, "transfer_to_runtime_manager", None)
        if transfer_fn is not None:
            transfer_fn()
        self.transaction.accepted_finalization.candidate_ownership_transferred = True
        self.transaction.mark_ownership_transferred()
        self._step = AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED

    async def _step_mirror_update(self) -> None:
        """Update app.state compatibility mirror."""
        if self._step != AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED:
            return
        # Test seam: inject post-acceptance cancellation after ownership
        # transfer but before mirror/finalize.  Checked dynamically so
        # clearing the seam on the manager takes effect immediately.
        if self._reload_manager is not None:
            inject = getattr(
                self._reload_manager, "TEST_INJECT_FINALIZATION_CANCEL", None
            )
            if inject is not None:
                raise inject
        if self.app is not None:
            from eggpool.app import mirror_generation_on_app_state  # noqa: PLC0415

            mirror_generation_on_app_state(self.app, self.published_generation)
        self.transaction.accepted_finalization.compatibility_mirror_updated = True
        self._step = AcceptedFinalizationStep.MIRROR_UPDATED

    async def _step_transitions_finalization(self) -> None:
        """Finalize process transitions -- release captured old-state snapshots.

        Plan 019 Workstream B1: inspect ``TransitionFinalizeOutcome``.
        If ``remaining`` is non-empty, the step does not advance --
        observer and retirement wait for successful transition
        finalization.
        """
        if self._step != AcceptedFinalizationStep.MIRROR_UPDATED:
            return
        if self.transition_result is not None:
            outcome = await self.transition_result.finalize_all()
            if outcome.remaining:
                raise TransitionFinalizationPendingError(
                    f"Transitions still pending: {outcome.remaining!r}",
                    attempted=outcome.attempted,
                    finalized=outcome.finalized,
                    failures=outcome.failures,
                    remaining=outcome.remaining,
                )
        self.transaction.accepted_finalization.transitions_finalized = True
        # Advance the transaction state machine through the intermediate
        # states that must precede retirement scheduling.
        self.transaction.mark_process_transitions_applied()
        self.transaction.mark_persistence_committed()
        self.transaction.mark_observable_state_updated()
        self._step = AcceptedFinalizationStep.TRANSITIONS_FINALIZED

    async def _step_observer_report(self) -> None:
        """Report publication and retirement through safe observer wrappers.

        Plan 020 Workstream A3: both ``on_publish_complete`` and
        ``on_retirement_started`` are invoked here as safe,
        non-authoritative calls.  Observer failure is logged and
        diagnosed but does not block transition finalization or
        retirement.
        """
        if self._step != AcceptedFinalizationStep.TRANSITIONS_FINALIZED:
            return
        try:
            await self.observer.on_publish_complete(
                generation_id=self.generation_id,
                digest_prefix=self.transaction.digest_prefix,
            )
        except Exception as exc:
            logger.warning(
                "Observer on_publish_complete failed for generation %d: %r",
                self.generation_id,
                exc,
                exc_info=True,
            )
        try:
            await self.observer.on_retirement_started(
                generation_id=self.generation_id,
                digest_prefix=self.transaction.digest_prefix,
                old_generation_id=self.old_generation_id,
            )
        except Exception as exc:
            logger.warning(
                "Observer on_retirement_started failed for generation %d: %r",
                self.generation_id,
                exc,
                exc_info=True,
            )
        self._step = AcceptedFinalizationStep.OBSERVER_REPORTED

    async def _step_retirement_scheduling(self) -> None:
        """Schedule old-generation retirement through pending swap.

        Plan 019 Workstream D1: the fault seam
        ``TEST_INJECT_RETIREMENT_FAILURE`` is wired here at the real
        production boundary, immediately before
        ``finalize_retirement()``.
        """
        if self._step != AcceptedFinalizationStep.OBSERVER_REPORTED:
            return
        # Plan 019 Workstream D1: retirement fault injection seam.
        # The seam is instance-scoped, one-shot, test-only.
        if self._reload_manager is not None:
            inject = getattr(
                self._reload_manager, "TEST_INJECT_RETIREMENT_FAILURE", None
            )
            if inject is not None:
                # One-shot: clear so retry can succeed.
                self._reload_manager.TEST_INJECT_RETIREMENT_FAILURE = None
                raise inject
        await self.pending_swap.finalize_retirement()
        self.transaction.accepted_finalization.retirement_scheduled = True
        self.transaction.mark_retirement_scheduled()
        self._step = AcceptedFinalizationStep.RETIREMENT_SCHEDULED

    async def _step_transaction_completion(self) -> None:
        """Mark the transaction as fully completed."""
        if self._step != AcceptedFinalizationStep.RETIREMENT_SCHEDULED:
            return
        self.transaction.mark_completed()
        self.transaction.accepted_finalization.transaction_completed = True
        self._step = AcceptedFinalizationStep.TRANSACTION_COMPLETED
        # Final step: mark COMPLETED only after all real work is done.
        self._step = AcceptedFinalizationStep.COMPLETED

    # -- shutdown adoption -------------------------------------------------

    async def adopt_for_shutdown(self) -> None:
        """Mark the job as adopted by shutdown cleanup.

        Plan 020 Workstream E2: when shutdown cannot drain the job
        within the bound, deterministic ownership recovery transfers
        the committed pending swap to runtime shutdown.  This method
        records adoption state without rewriting the progress cursor.

        The job remains in the active registry until the reload
        manager prunes it; the manager uses ``adopted_for_shutdown``
        to guarantee idempotent close semantics.
        """
        # Idempotent: a single marker is enough.
        self._released = True

    @property
    def adopted_for_shutdown(self) -> bool:
        """True once shutdown has adopted ownership."""
        return self._released

    # -- reference lifecycle -----------------------------------------------

    def release_references(self) -> None:
        """Release operational references after completion.

        Plan 019 Workstream C3: after successful completion and before
        dropping from the active registry, clear strong references to
        operational objects.  The job retains only diagnostic scalars.
        """
        if self._released:
            return
        self._released = True
        self.candidate = None  # type: ignore[assignment]
        self.pending_swap = None  # type: ignore[assignment]
        self.transition_result = None
        self.published_generation = None  # type: ignore[assignment]
        self.app = None
        self.observer = None  # type: ignore[assignment]
        self.transaction = None  # type: ignore[assignment]

    def mark_reconciled(self) -> None:
        """Mark the job as reconciled by the manager.

        Plan 020 Workstream C2: the manager's reconciliation step
        calls this once it has updated all counters and history so
        that no future attempt can double-count completion.
        """
        self._reconciled = True

    # -- diagnostics --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return diagnostic snapshot of the finalization job."""
        duration_s: float | None = None
        if self._started_at is not None:
            end = (
                self._completed_at
                if self._completed_at is not None
                else time.monotonic()
            )
            duration_s = end - self._started_at
        return {
            "request_id": self.request_id,
            "generation_id": self.generation_id,
            "old_generation_id": self.old_generation_id,
            "step": self._step.value,
            "health": self._health.value,
            "status": self.status.value,
            "is_complete": self.is_complete,
            "attempt_count": self._attempt_count,
            "failure_count": self._failure_count,
            "retry_attempt_count": self._retry_attempt_count,
            "retirement_retry_attempt_count": self._retirement_retry_attempt_count,
            "last_error_step": self._last_error_step,
            "last_error_class": self._last_error_class,
            "last_error_message": self._last_error_message,
            "completed_at": self._completed_at,
            "duration_s": duration_s,
            "released": self._released,
            "reconciled": self._reconciled,
        }

    def to_record(self) -> AcceptedFinalizationRecord:
        """Create an immutable diagnostic record for history.

        Plan 019 Workstream C2: lightweight record with no live
        runtime references.
        """
        duration_s: float = 0.0
        if self._started_at is not None:
            end = (
                self._completed_at
                if self._completed_at is not None
                else time.monotonic()
            )
            duration_s = end - self._started_at
        return AcceptedFinalizationRecord(
            request_id=self.request_id,
            generation_id=self.generation_id,
            old_generation_id=self.old_generation_id,
            completion_status=self.status.value,
            attempts=self._attempt_count,
            failure_count=self._failure_count,
            retry_attempt_count=self._retry_attempt_count,
            retirement_retry_attempt_count=self._retirement_retry_attempt_count,
            last_failed_step=self._last_error_step,
            last_error_class=self._last_error_class,
            last_error_message=self._last_error_message,
            completed_at=self._completed_at or 0.0,
            duration_s=duration_s,
        )


class TransitionFinalizationPendingError(Exception):
    """Raised when transition finalization has remaining work."""

    def __init__(
        self,
        message: str,
        *,
        attempted: tuple[str, ...] = (),
        finalized: tuple[str, ...] = (),
        failures: tuple[tuple[str, Exception], ...] = (),
        remaining: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.attempted = attempted
        self.finalized = finalized
        self.failures = failures
        self.remaining = remaining


__all__ = [
    "AcceptedFinalizationHealth",
    "AcceptedFinalizationOutcome",
    "AcceptedFinalizationRecord",
    "AcceptedFinalizationStep",
    "AcceptedReloadFinalizationJob",
    "FinalizationStatus",
    "TransitionFinalizationPendingError",
    "FINALIZATION_HISTORY_MAX",
]
