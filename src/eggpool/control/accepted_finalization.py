"""Accepted-reload finalization job (Plan 018 Workstream C).

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
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.reload_transaction import ReloadTransaction, TransitionApplyResult
    from eggpool.runtime_manager import (
        RuntimeGeneration,
        RuntimeGenerationCandidate,
    )

logger = logging.getLogger(__name__)


class AcceptedFinalizationStep(enum.Enum):
    """Monotonic steps of accepted-reload finalization.

    The job advances through these steps in order.  Once a step
    succeeds the next ``run()`` resumes from the following step.
    """

    REGISTERED = "registered"
    OWNERSHIP_TRANSFERRED = "ownership_transferred"
    MIRROR_UPDATED = "mirror_updated"
    TRANSITIONS_FINALIZED = "transitions_finalized"
    OBSERVER_REPORTED = "observer_reported"
    RETIREMENT_SCHEDULED = "retirement_scheduled"
    COMPLETED = "completed"
    DEGRADED = "degraded"


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
    _attempts: int = field(default=0, repr=False)
    _last_error_step: str | None = field(default=None, repr=False)
    _last_error_class: str | None = field(default=None, repr=False)
    _last_error_message: str | None = field(default=None, repr=False)

    @property
    def step(self) -> AcceptedFinalizationStep:
        """Current finalization step."""
        return self._step

    @property
    def attempts(self) -> int:
        """Number of ``run()`` invocations."""
        return self._attempts

    @property
    def last_error_step(self) -> str | None:
        """Step that failed last, or ``None``."""
        return self._last_error_step

    @property
    def is_complete(self) -> bool:
        """True when the job has reached a terminal state."""
        return self._step in (
            AcceptedFinalizationStep.COMPLETED,
            AcceptedFinalizationStep.DEGRADED,
        )

    async def run(self) -> AcceptedFinalizationStep:
        """Execute incomplete steps idempotently.

        Returns the terminal step: ``COMPLETED`` when every step
        succeeded, ``DEGRADED`` when a step raised.
        """
        self._attempts += 1

        try:
            await self._step_ownership_transfer()
            await self._step_mirror_update()
            await self._step_transitions_finalization()
            await self._step_observer_report()
            await self._step_retirement_scheduling()
            await self._step_transaction_completion()
            self._step = AcceptedFinalizationStep.COMPLETED
        except Exception as exc:
            self._last_error_class = type(exc).__name__
            self._last_error_message = str(exc)
            logger.warning(
                "Accepted finalization step %s failed for generation %d: %r",
                self._step.value,
                self.generation_id,
                exc,
                exc_info=True,
            )
            self._step = AcceptedFinalizationStep.DEGRADED

        return self._step

    # -- individual steps ---------------------------------------------------

    async def _step_ownership_transfer(self) -> None:
        """Transfer candidate ownership to the runtime manager."""
        if self._step != AcceptedFinalizationStep.REGISTERED:
            return
        self._last_error_step = "ownership_transfer"
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
        self._last_error_step = "mirror_update"
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
        """Finalize process transitions -- release captured old-state snapshots."""
        if self._step != AcceptedFinalizationStep.MIRROR_UPDATED:
            return
        self._last_error_step = "transitions_finalization"
        if self.transition_result is not None:
            await self.transition_result.finalize_all()
        self.transaction.accepted_finalization.transitions_finalized = True
        # Advance the transaction state machine through the intermediate
        # states that must precede retirement scheduling.
        self.transaction.mark_process_transitions_applied()
        self.transaction.mark_persistence_committed()
        self.transaction.mark_observable_state_updated()
        self._step = AcceptedFinalizationStep.TRANSITIONS_FINALIZED

    async def _step_observer_report(self) -> None:
        """Report publication completion through a safe observer wrapper."""
        if self._step != AcceptedFinalizationStep.TRANSITIONS_FINALIZED:
            return
        self._last_error_step = "observer_report"
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
        self._step = AcceptedFinalizationStep.OBSERVER_REPORTED

    async def _step_retirement_scheduling(self) -> None:
        """Schedule old-generation retirement through pending swap."""
        if self._step != AcceptedFinalizationStep.OBSERVER_REPORTED:
            return
        self._last_error_step = "retirement_scheduling"
        await self.pending_swap.finalize_retirement()
        self.transaction.accepted_finalization.retirement_scheduled = True
        self.transaction.mark_retirement_scheduled()
        self._step = AcceptedFinalizationStep.RETIREMENT_SCHEDULED

    async def _step_transaction_completion(self) -> None:
        """Mark the transaction as fully completed."""
        if self._step != AcceptedFinalizationStep.RETIREMENT_SCHEDULED:
            return
        self._last_error_step = "transaction_completion"
        self.transaction.mark_completed()
        self.transaction.accepted_finalization.transaction_completed = True
        self._step = AcceptedFinalizationStep.COMPLETED

    # -- diagnostics --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return diagnostic snapshot of the finalization job."""
        return {
            "request_id": self.request_id,
            "generation_id": self.generation_id,
            "old_generation_id": self.old_generation_id,
            "step": self._step.value,
            "attempts": self._attempts,
            "last_error_step": self._last_error_step,
            "last_error_class": self._last_error_class,
            "last_error_message": self._last_error_message,
        }


__all__ = [
    "AcceptedFinalizationStep",
    "AcceptedReloadFinalizationJob",
]
