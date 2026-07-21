"""Reload transaction state machine and typed deltas (Phase 6).

Introduces a monotonic state machine for the live-rehash transaction
and typed delta objects for persistence and process transitions.  The
transaction encapsulates the complete lifecycle:

    created → validated → diffed → candidate_prepared →
    persistence_prepared → process_transitions_prepared →
    commit_started → runtime_published →
    process_transitions_applied → persistence_committed →
    observable_state_updated → retirement_scheduled → completed

Or, on failure:

    * → aborting → aborted
    * → compensation_failed

Every state transition is monotonic and asserted in code.  The
transaction object is owned by :class:`ReloadManager` and carries all
prepared deltas so the commit window is narrow and side-effect-free
outside the commit guard.

Commit ordering
---------------

The authoritative commit ordering is:

1. Revalidate active generation and shutdown state.
2. Open SQLite transaction and apply prepared persistence delta.
3. Pre-apply only reversible process operations required before
   publication.
4. Publish candidate generation atomically through RuntimeManager.
5. Transfer candidate ownership to the runtime manager.
6. Apply remaining bounded process-owned transitions.
7. Update effective configuration and compatibility state.
8. Commit SQLite.
9. Finalize process transitions and schedule old-generation retirement.
10. Mark transaction completed.

Rationale: SQLite commit before publication means a persistence
failure leaves the runtime untouched.  If publication fails after
SQLite commit, the persistence delta is idempotent and the next
reload will re-sync.  This ordering has the smallest irrecoverable
window because every post-publication step either completes normally
or is idempotent/retryable.

Cancellation semantics
----------------------

Before the commit point (step 4), cancellation aborts the candidate
and leaves old state unchanged.  After the commit point, the bounded
commit is shielded, completed or compensated, then cancellation is
propagated according to control-protocol policy.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.config_reload_policy import ConfigChange, ConfigDiff
    from eggpool.config_validation import (
        ConfigValidationResult,
        ConfigValidationWarning,
    )
    from eggpool.runtime_manager import (
        RuntimeGeneration,
        RuntimeGenerationCandidate,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transaction state machine
# ---------------------------------------------------------------------------


class TransactionState(enum.Enum):
    """Monotonic states for a reload transaction.

    Transitions only move forward (to higher-indexed states) or to
    terminal states (``ABORTED``, ``COMPENSATION_FAILED``).  Every
    transition is asserted in :meth:`ReloadTransaction._transition_to`.
    """

    CREATED = "created"
    VALIDATED = "validated"
    DIFFED = "diffed"
    CANDIDATE_PREPARED = "candidate_prepared"
    PERSISTENCE_PREPARED = "persistence_prepared"
    PROCESS_TRANSITIONS_PREPARED = "process_transitions_prepared"
    COMMIT_STARTED = "commit_started"
    RUNTIME_PUBLISHED = "runtime_published"
    PROCESS_TRANSITIONS_APPLIED = "process_transitions_applied"
    PERSISTENCE_COMMITTED = "persistence_committed"
    OBSERVABLE_STATE_UPDATED = "observable_state_updated"
    RETIREMENT_SCHEDULED = "retirement_scheduled"
    COMPLETED = "completed"
    ABORTING = "aborting"
    ABORTED = "aborted"
    COMPENSATION_FAILED = "compensation_failed"


# Valid forward transitions.  From state X, only states listed in
# _VALID_TRANSITIONS[X] are reachable.
_VALID_TRANSITIONS: dict[TransactionState, frozenset[TransactionState]] = {
    TransactionState.CREATED: frozenset(
        {TransactionState.VALIDATED, TransactionState.ABORTING}
    ),
    TransactionState.VALIDATED: frozenset(
        {TransactionState.DIFFED, TransactionState.ABORTING}
    ),
    TransactionState.DIFFED: frozenset(
        {TransactionState.CANDIDATE_PREPARED, TransactionState.ABORTING}
    ),
    TransactionState.CANDIDATE_PREPARED: frozenset(
        {TransactionState.PERSISTENCE_PREPARED, TransactionState.ABORTING}
    ),
    TransactionState.PERSISTENCE_PREPARED: frozenset(
        {TransactionState.PROCESS_TRANSITIONS_PREPARED, TransactionState.ABORTING}
    ),
    TransactionState.PROCESS_TRANSITIONS_PREPARED: frozenset(
        {TransactionState.COMMIT_STARTED, TransactionState.ABORTING}
    ),
    TransactionState.COMMIT_STARTED: frozenset(
        {TransactionState.RUNTIME_PUBLISHED, TransactionState.ABORTING}
    ),
    TransactionState.RUNTIME_PUBLISHED: frozenset(
        {
            TransactionState.PROCESS_TRANSITIONS_APPLIED,
            TransactionState.ABORTING,
        }
    ),
    TransactionState.PROCESS_TRANSITIONS_APPLIED: frozenset(
        {
            TransactionState.PERSISTENCE_COMMITTED,
            TransactionState.ABORTING,
        }
    ),
    TransactionState.PERSISTENCE_COMMITTED: frozenset(
        {
            TransactionState.OBSERVABLE_STATE_UPDATED,
            TransactionState.ABORTING,
        }
    ),
    TransactionState.OBSERVABLE_STATE_UPDATED: frozenset(
        {TransactionState.RETIREMENT_SCHEDULED, TransactionState.ABORTING}
    ),
    TransactionState.RETIREMENT_SCHEDULED: frozenset(
        {TransactionState.COMPLETED, TransactionState.ABORTING}
    ),
    TransactionState.COMPLETED: frozenset(),
    TransactionState.ABORTING: frozenset(
        {TransactionState.ABORTED, TransactionState.COMPENSATION_FAILED}
    ),
    TransactionState.ABORTED: frozenset(),
    TransactionState.COMPENSATION_FAILED: frozenset(),
}


class TransactionStateError(Exception):
    """Raised on an invalid state transition."""


# ---------------------------------------------------------------------------
# Typed deltas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersistenceDelta:
    """Prepared but not-yet-committed persistence changes.

    Immutable snapshot of the providers and accounts to sync.  The
    commit step applies these inside a SQLite transaction.
    """

    configured_providers: dict[str, dict[str, Any]]
    config_accounts: tuple[dict[str, Any], ...]


class ProcessTransition:
    """A single reversible process-owned transition.

    Each transition supports ``preflight()``, ``apply()``, and
    ``rollback()``.  Transitions that cannot be safely preflighted
    and rolled back should be classified ``RESTART_REQUIRED`` in the
    config reload policy.

    The base class provides no-op defaults; subclasses override the
    methods to perform actual work.  The current implementation has
    one concrete subclass: :class:`TaskSpecTransition`.
    """

    def __init__(
        self,
        name: str,
        description: str,
        *,
        reversible: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.reversible = reversible

    async def preflight(self) -> None:
        """Verify that the transition can be safely applied.

        Must not perform any mutation.  Raises if preconditions are
        not met (e.g. process supervisor unavailable, specs invalid).
        """

    async def apply(self) -> None:
        """Apply the transition.

        Called after publication so the process supervisor is only
        reconfigured when the new generation is already live.
        """

    async def rollback(self) -> None:
        """Roll back the transition if it was partially applied.

        Called only when a failure occurs between ``apply()`` and
        commit completion.  For the current commit ordering (process
        transitions applied AFTER publication), rollback is not needed
        because compensation retries the transition instead.  This
        method exists for forward-compatibility if the ordering changes.
        """

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name={self.name!r} reversible={self.reversible}>"
        )


class TaskSpecTransition(ProcessTransition):
    """Concrete transition for process-supervisor task spec reconfiguration.

    Applies ``apply_spec_diff()`` with the candidate task specs and
    callback factories.  Rollback re-applies with the old specs
    captured at preflight time.

    The transition stores references to the process supervisor and the
    candidate/old specs so ``apply()`` and ``rollback()`` can perform
    the actual reconfiguration.
    """

    def __init__(
        self,
        *,
        process_supervisor: Any,
        candidate_specs: tuple[Any, ...],
        callback_factories: dict[str, Any],
        process: Any,
    ) -> None:
        super().__init__(
            name="task_spec_diff",
            description="Reconfigure process supervisor task specs",
            reversible=True,
        )
        self._process_supervisor = process_supervisor
        self._candidate_specs = candidate_specs
        self._callback_factories = callback_factories
        self._process = process
        self._old_specs: tuple[Any, ...] | None = None
        self._applied = False

    async def preflight(self) -> None:
        """Verify the process supervisor is available."""
        if self._process_supervisor is None:
            raise RuntimeError("Process supervisor is not available")

    async def apply(self) -> None:
        """Apply task spec diff with candidate specs."""
        if self._process_supervisor is None:
            raise RuntimeError("Process supervisor is not available")

        # Capture old specs for potential rollback.
        self._old_specs = tuple(getattr(self._process_supervisor, "_task_specs", ()))

        await self._process_supervisor.apply_spec_diff(
            self._candidate_specs,
            callback_factories=self._callback_factories,
            process=self._process,
        )
        self._applied = True

    async def rollback(self) -> None:
        """Roll back by re-applying the old specs."""
        if not self._applied:
            return
        if self._process_supervisor is None or self._old_specs is None:
            return

        try:
            await self._process_supervisor.apply_spec_diff(
                self._old_specs,
                callback_factories={},
                process=self._process,
            )
        except Exception:
            logger.warning(
                "TaskSpecTransition rollback failed; "
                "process supervisor may be in intermediate state"
            )
        finally:
            self._applied = False


@dataclass(frozen=True)
class ProcessTransitionPlan:
    """Prepared process transitions for a reload.

    Captures the task specs, callback factories, and transition
    metadata so the commit step can apply them atomically.
    """

    task_specs: tuple[Any, ...]
    callback_factories: dict[str, Any]
    transitions: tuple[ProcessTransition, ...]


@dataclass(frozen=True)
class CommitDiagnostics:
    """Diagnostics captured during the commit phase."""

    commit_started_at: float = 0.0
    sqlite_apply_duration_s: float = 0.0
    publication_duration_s: float = 0.0
    process_transition_duration_s: float = 0.0
    total_commit_duration_s: float = 0.0
    old_generation_id: int | None = None
    new_generation_id: int | None = None
    persistence_rows_affected: int = 0


# ---------------------------------------------------------------------------
# Reload transaction
# ---------------------------------------------------------------------------


class ReloadTransaction:
    """Encapsulates a complete live-rehash transaction.

    The transaction carries all prepared deltas and metadata through
    the lifecycle from creation to completion (or abort).  It is
    owned by :class:`ReloadManager` and not shared across threads.

    The transaction is **not** thread-safe — it is designed for
    single-event-loop use with ``asyncio``.
    """

    def __init__(
        self,
        *,
        request_id: str,
        validation: ConfigValidationResult,
        expected_digest: str | None = None,
    ) -> None:
        self._state = TransactionState.CREATED
        self._created_at = time.monotonic()
        self._request_id = request_id
        self._validation = validation
        self._expected_digest = expected_digest

        # Prepared data — populated during prepare stages
        self._diff: ConfigDiff | None = None
        self._candidate: RuntimeGenerationCandidate | None = None
        self._generation_id: int = 0
        self._digest_prefix: str = (
            validation.content_digest[:12] if validation.content_digest else "<empty>"
        )
        self._persistence_delta: PersistenceDelta | None = None
        self._process_transition_plan: ProcessTransitionPlan | None = None
        self._old_generation_id: int | None = None
        self._changed_sections: tuple[str, ...] = ()
        self._warnings: tuple[ConfigValidationWarning, ...] = validation.warnings
        self._restart_required: tuple[ConfigChange, ...] = ()

        # Commit-phase data
        self._commit_diagnostics = CommitDiagnostics()
        self._published_generation: RuntimeGeneration | None = None

        # Terminal state
        self._completed_at: float | None = None
        self._error: Exception | None = None

        # Transition history for diagnostics
        self._transition_history: list[tuple[TransactionState, float]] = [
            (TransactionState.CREATED, self._created_at),
        ]

    # -- Properties ---------------------------------------------------------

    @property
    def state(self) -> TransactionState:
        return self._state

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def generation_id(self) -> int:
        return self._generation_id

    @property
    def digest_prefix(self) -> str:
        return self._digest_prefix

    @property
    def diff(self) -> ConfigDiff | None:
        return self._diff

    @property
    def candidate(self) -> RuntimeGenerationCandidate | None:
        return self._candidate

    @property
    def persistence_delta(self) -> PersistenceDelta | None:
        return self._persistence_delta

    @property
    def process_transition_plan(self) -> ProcessTransitionPlan | None:
        return self._process_transition_plan

    @property
    def old_generation_id(self) -> int | None:
        return self._old_generation_id

    @property
    def changed_sections(self) -> tuple[str, ...]:
        return self._changed_sections

    @property
    def warnings(self) -> tuple[ConfigValidationWarning, ...]:
        return self._warnings

    @property
    def restart_required(self) -> tuple[ConfigChange, ...]:
        return self._restart_required

    @property
    def commit_diagnostics(self) -> CommitDiagnostics:
        return self._commit_diagnostics

    @property
    def published_generation(self) -> RuntimeGeneration | None:
        return self._published_generation

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._created_at

    # -- State transitions --------------------------------------------------

    def _transition_to(self, new_state: TransactionState) -> None:
        """Assert and perform a monotonic state transition."""
        valid = _VALID_TRANSITIONS.get(self._state)
        if valid is None or new_state not in valid:
            raise TransactionStateError(
                f"Invalid transition: {self._state.value} → {new_state.value}"
            )
        self._state = new_state
        self._transition_history.append((new_state, time.monotonic()))
        logger.debug(
            "Transaction %s: %s → %s",
            self._request_id[:8],
            self._transition_history[-2][0].value
            if len(self._transition_history) > 1
            else "<init>",
            new_state.value,
        )

    def mark_validated(self) -> None:
        """Transition: CREATED → VALIDATED."""
        self._transition_to(TransactionState.VALIDATED)

    def mark_diffed(
        self,
        diff: ConfigDiff,
        *,
        changed_sections: tuple[str, ...],
        restart_required: tuple[ConfigChange, ...],
    ) -> None:
        """Transition: VALIDATED → DIFFED."""
        self._diff = diff
        self._changed_sections = changed_sections
        self._restart_required = restart_required
        self._transition_to(TransactionState.DIFFED)

    def mark_candidate_prepared(
        self,
        candidate: RuntimeGenerationCandidate,
        generation_id: int,
    ) -> None:
        """Transition: DIFFED → CANDIDATE_PREPARED."""
        self._candidate = candidate
        self._generation_id = generation_id
        self._transition_to(TransactionState.CANDIDATE_PREPARED)

    def mark_persistence_prepared(
        self,
        delta: PersistenceDelta,
    ) -> None:
        """Transition: CANDIDATE_PREPARED → PERSISTENCE_PREPARED."""
        self._persistence_delta = delta
        self._transition_to(TransactionState.PERSISTENCE_PREPARED)

    def mark_process_transitions_prepared(
        self,
        plan: ProcessTransitionPlan,
    ) -> None:
        """Transition: PERSISTENCE_PREPARED → PROCESS_TRANSITIONS_PREPARED."""
        self._process_transition_plan = plan
        self._transition_to(TransactionState.PROCESS_TRANSITIONS_PREPARED)

    def mark_commit_started(
        self,
        old_generation_id: int,
    ) -> None:
        """Transition: PROCESS_TRANSITIONS_PREPARED → COMMIT_STARTED."""
        self._old_generation_id = old_generation_id
        self._commit_diagnostics = CommitDiagnostics(
            commit_started_at=time.monotonic(),
            old_generation_id=old_generation_id,
        )
        self._transition_to(TransactionState.COMMIT_STARTED)

    def mark_runtime_published(
        self,
        published_generation: RuntimeGeneration,
    ) -> None:
        """Transition: COMMIT_STARTED → RUNTIME_PUBLISHED."""
        self._published_generation = published_generation
        self._commit_diagnostics = CommitDiagnostics(
            commit_started_at=self._commit_diagnostics.commit_started_at,
            old_generation_id=self._commit_diagnostics.old_generation_id,
            new_generation_id=published_generation.generation_id,
            publication_duration_s=(
                time.monotonic() - self._commit_diagnostics.commit_started_at
            ),
        )
        self._transition_to(TransactionState.RUNTIME_PUBLISHED)

    def mark_process_transitions_applied(self) -> None:
        """Transition: RUNTIME_PUBLISHED → PROCESS_TRANSITIONS_APPLIED."""
        self._transition_to(TransactionState.PROCESS_TRANSITIONS_APPLIED)

    def mark_persistence_committed(self) -> None:
        """Transition: PROCESS_TRANSITIONS_APPLIED → PERSISTENCE_COMMITTED."""
        self._transition_to(TransactionState.PERSISTENCE_COMMITTED)

    def mark_observable_state_updated(self) -> None:
        """Transition: PERSISTENCE_COMMITTED → OBSERVABLE_STATE_UPDATED."""
        self._transition_to(TransactionState.OBSERVABLE_STATE_UPDATED)

    def mark_retirement_scheduled(self) -> None:
        """Transition: OBSERVABLE_STATE_UPDATED → RETIREMENT_SCHEDULED."""
        self._transition_to(TransactionState.RETIREMENT_SCHEDULED)

    def mark_completed(self) -> None:
        """Transition: RETIREMENT_SCHEDULED → COMPLETED."""
        self._completed_at = time.monotonic()
        self._commit_diagnostics = CommitDiagnostics(
            commit_started_at=self._commit_diagnostics.commit_started_at,
            old_generation_id=self._commit_diagnostics.old_generation_id,
            new_generation_id=self._commit_diagnostics.new_generation_id,
            sqlite_apply_duration_s=self._commit_diagnostics.sqlite_apply_duration_s,
            publication_duration_s=self._commit_diagnostics.publication_duration_s,
            process_transition_duration_s=(
                self._commit_diagnostics.process_transition_duration_s
            ),
            total_commit_duration_s=time.monotonic()
            - self._commit_diagnostics.commit_started_at,
        )
        self._transition_to(TransactionState.COMPLETED)

    def mark_aborting(self, error: Exception | None = None) -> None:
        """Transition to ABORTING from any non-terminal state."""
        if self._state in (
            TransactionState.COMPLETED,
            TransactionState.ABORTED,
            TransactionState.COMPENSATION_FAILED,
            TransactionState.ABORTING,
        ):
            return  # already terminal or aborting
        self._error = error
        self._transition_to(TransactionState.ABORTING)

    def mark_aborted(self) -> None:
        """Transition: ABORTING → ABORTED."""
        self._completed_at = time.monotonic()
        self._transition_to(TransactionState.ABORTED)

    def mark_compensation_failed(self) -> None:
        """Transition: ABORTING → COMPENSATION_FAILED."""
        self._completed_at = time.monotonic()
        self._transition_to(TransactionState.COMPENSATION_FAILED)

    # -- Diagnostics --------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self._state in (
            TransactionState.COMPLETED,
            TransactionState.ABORTED,
            TransactionState.COMPENSATION_FAILED,
        )

    @property
    def is_committing(self) -> bool:
        return self._state in (
            TransactionState.COMMIT_STARTED,
            TransactionState.RUNTIME_PUBLISHED,
            TransactionState.PROCESS_TRANSITIONS_APPLIED,
            TransactionState.PERSISTENCE_COMMITTED,
            TransactionState.OBSERVABLE_STATE_UPDATED,
            TransactionState.RETIREMENT_SCHEDULED,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return transaction state for diagnostics."""
        return {
            "state": self._state.value,
            "request_id": self._request_id,
            "generation_id": self._generation_id,
            "digest_prefix": self._digest_prefix,
            "created_at": self._created_at,
            "elapsed_s": self.elapsed_s,
            "old_generation_id": self._old_generation_id,
            "changed_sections": self._changed_sections,
            "restart_required_count": len(self._restart_required),
            "has_candidate": self._candidate is not None,
            "has_persistence_delta": self._persistence_delta is not None,
            "has_process_transitions": self._process_transition_plan is not None,
            "published_generation_id": (
                self._published_generation.generation_id
                if self._published_generation
                else None
            ),
            "commit_diagnostics": {
                "commit_started_at": self._commit_diagnostics.commit_started_at,
                "sqlite_apply_duration_s": (
                    self._commit_diagnostics.sqlite_apply_duration_s
                ),
                "publication_duration_s": (
                    self._commit_diagnostics.publication_duration_s
                ),
                "process_transition_duration_s": (
                    self._commit_diagnostics.process_transition_duration_s
                ),
                "total_commit_duration_s": (
                    self._commit_diagnostics.total_commit_duration_s
                ),
                "old_generation_id": self._commit_diagnostics.old_generation_id,
                "new_generation_id": self._commit_diagnostics.new_generation_id,
            },
            "error": str(self._error) if self._error else None,
            "completed_at": self._completed_at,
            "transition_history": [
                {"state": s.value, "at": t} for s, t in self._transition_history
            ],
        }


__all__ = [
    "CommitDiagnostics",
    "PersistenceDelta",
    "ProcessTransition",
    "ProcessTransitionPlan",
    "ReloadTransaction",
    "TaskSpecTransition",
    "TransactionState",
    "TransactionStateError",
]
