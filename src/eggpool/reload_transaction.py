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
from dataclasses import dataclass, field
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
    PROCESS_TRANSITIONS_PREFLIGHTED = "process_transitions_preflighted"
    COMMIT_STARTED = "commit_started"
    RUNTIME_STAGED = "runtime_staged"
    RUNTIME_SWAP_COMMITTED = "runtime_swap_committed"
    RUNTIME_PUBLISHED = "runtime_published"
    PROCESS_TRANSITIONS_APPLIED = "process_transitions_applied"
    PERSISTENCE_COMMITTED = "persistence_committed"
    OBSERVABLE_STATE_UPDATED = "observable_state_updated"
    RETIREMENT_SCHEDULED = "retirement_scheduled"
    COMPLETED = "completed"
    ABORTING = "aborting"
    ABORTED = "aborted"
    COMPENSATION_FAILED = "compensation_failed"


class ReloadAcceptanceState(enum.Enum):
    """Plan 017 Workstream D5: explicit acceptance lifecycle.

    Tracks the boundary between pre-acceptance and post-acceptance
    phases.  ``NOT_ACCEPTED`` is the initial state.  After SQLite
    commit succeeds but before ``pending_swap.commit()``, the state is
    ``PERSISTENCE_COMMITTED_RUNTIME_PENDING``.  After both SQLite and
    runtime swap commit, the state is ``ACCEPTED``.
    """

    NOT_ACCEPTED = "not_accepted"
    PERSISTENCE_COMMITTED_RUNTIME_PENDING = "persistence_committed_runtime_pending"
    ACCEPTED = "accepted"


# Valid forward transitions.  From state X, only states listed in
# _VALID_TRANSITIONS[X] are reachable.
#
# Plan 016 Workstream D5: the production state ordering is
#
#   PROCESS_TRANSITIONS_PREPARED
#     → PROCESS_TRANSITIONS_PREFLIGHTED
#     → COMMIT_STARTED
#     → RUNTIME_STAGED
#     → RUNTIME_SWAP_COMMITTED
#     → PROCESS_TRANSITIONS_APPLIED
#     → PERSISTENCE_COMMITTED
#     → OBSERVABLE_STATE_UPDATED
#     → RETIREMENT_SCHEDULED
#     → COMPLETED
#
# Preflight always runs before COMMIT_STARTED, which runs before the
# lease gate closes.  Each state appears exactly once as a key in the
# map below (prior versions had ``PROCESS_TRANSITIONS_PREFLIGHTED``
# listed twice, leaving the first definition dead and the second
# definition leaking transitions that should not be reachable from
# ``PROCESS_TRANSITIONS_PREFLIGHTED``).
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
        {
            TransactionState.PROCESS_TRANSITIONS_PREFLIGHTED,
            TransactionState.ABORTING,
        }
    ),
    TransactionState.PROCESS_TRANSITIONS_PREFLIGHTED: frozenset(
        {TransactionState.COMMIT_STARTED, TransactionState.ABORTING}
    ),
    TransactionState.COMMIT_STARTED: frozenset(
        {
            TransactionState.RUNTIME_STAGED,
            TransactionState.RUNTIME_PUBLISHED,
            TransactionState.ABORTING,
        }
    ),
    TransactionState.RUNTIME_STAGED: frozenset(
        {TransactionState.RUNTIME_SWAP_COMMITTED, TransactionState.ABORTING}
    ),
    TransactionState.RUNTIME_SWAP_COMMITTED: frozenset(
        {
            TransactionState.PROCESS_TRANSITIONS_APPLIED,
            TransactionState.ABORTING,
        }
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


class TransitionRollbackState(enum.Enum):
    """Tracks retryability of transition rollback.

    Plan 018 Workstream A3: rollback state is retryable — a partial
    rollback can be retried to restore the remaining transitions.
    """

    NOT_ATTEMPTED = "not_attempted"
    COMPLETE = "complete"
    PARTIAL = "partial"


class ProcessTransitionApplyError(Exception):
    """Typed error carrying context for a partial transition apply failure.

    Plan 016 Workstream D2: callers routing a partial transition
    failure need the failed transition name, its position in the
    plan, the list of transitions that already applied (so the
    rollback path is recoverable), and the original cause.  This
    error keeps that context for diagnostic classification without
    forcing the aggregator to walk message strings.

    Plan 017 Workstream B: the error carries a reference to the
    partial ``TransitionApplyResult`` so callers can perform rollback
    without losing the old-state snapshots captured during apply.
    """

    def __init__(
        self,
        *,
        failed_transition_name: str,
        failed_transition_index: int,
        applied_transition_names: tuple[str, ...],
        original_exception: BaseException,
    ) -> None:
        super().__init__(
            f"Process transition {failed_transition_name!r} "
            f"(index {failed_transition_index}) failed: "
            f"{type(original_exception).__name__}: {original_exception}"
        )
        self.failed_transition_name = failed_transition_name
        self.failed_transition_index = failed_transition_index
        self.applied_transition_names = applied_transition_names
        self.original_exception = original_exception
        self.transition_result: TransitionApplyResult | None = None


@dataclass(frozen=True)
class TransitionRollbackOutcome:
    """Structured result of rolling back partially-applied transitions.

    Plan 017 Workstream B: replaces the untyped
    ``list[tuple[str, str]]`` return from
    :meth:`TransitionApplyResult.rollback_applied` with a typed
    container that distinguishes attempted transitions, successfully
    restored transitions, and per-transition failures.
    """

    attempted: tuple[str, ...]
    """Names of transitions whose rollback() was invoked."""
    restored: tuple[str, ...]
    """Names of transitions whose rollback() completed without error."""
    failures: tuple[tuple[str, Exception], ...]
    """Pairs of (transition_name, exception) for rollbacks that failed."""


@dataclass(frozen=True)
class TransitionFinalizeOutcome:
    """Structured result of finalizing transitions after commit.

    Plan 018 Workstream A4: replaces the swallowed-failure logging in
    :meth:`TransitionApplyResult.finalize_all` with a typed container
    that surfaces attempted, finalized, failed, and remaining
    transitions so callers can retry or diagnose incomplete
    finalization.
    """

    attempted: tuple[str, ...]
    """Names of transitions whose finalize() was invoked."""
    finalized: tuple[str, ...]
    """Names of transitions whose finalize() completed without error."""
    failures: tuple[tuple[str, Exception], ...]
    """Pairs of (transition_name, exception) for finalizations that failed."""
    remaining: tuple[str, ...]
    """Names of transitions not yet finalized (resumable on next call)."""


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

    async def finalize(self) -> None:
        """Finalize the transition after successful commit.

        Called only after the transaction is accepted.  Releases
        captured old-state snapshots.  Idempotent.  Failure is treated
        as post-commit housekeeping.
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
        # Plan 016 Workstream D4: explicit lifecycle for rollback /
        # finalize idempotence.
        self._rolled_back = False
        self._finalized = False

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
        """Roll back by re-applying the old specs.

        Plan 016 Workstream D3: rollback failures must propagate to
        the aggregator (``TransitionApplyResult.rollback_applied``)
        so degraded-state diagnostics can classify them.  This method
        does not swallow exceptions; it logs at debug for traceability
        and re-raises so the aggregation layer captures the cause.

        Workstream D4: a second rollback after success is a no-op; a
        rollback after finalize raises ``TransactionStateError`` so
        diagnostic classification catches misuse.

        Plan 017 Workstream B: on rollback failure, ``_rolled_back``
        is NOT set so a subsequent rollback attempt can retry.  Only
        successful rollback marks the transition as rolled back.
        """
        if self._finalized:
            raise TransactionStateError(f"Cannot rollback {self.name} after finalize")
        if not self._applied or self._rolled_back:
            return
        if self._process_supervisor is None or self._old_specs is None:
            return

        try:
            await self._process_supervisor.apply_spec_diff(
                self._old_specs,
                callback_factories={},
                process=self._process,
            )
        except Exception as exc:
            logger.debug("TaskSpecTransition rollback failed: %r", exc, exc_info=True)
            raise
        self._applied = False
        self._rolled_back = True

    async def finalize(self) -> None:
        """Release captured old-state snapshots after commit.

        Plan 016 Workstream D4: finalize is idempotent and a no-op
        when the transition has already been finalized.
        """
        if self._finalized:
            return
        self._old_specs = None
        self._finalized = True


class RoutingTraceWriterTransition(ProcessTransition):
    """Transition for reconfiguring the process-owned routing-trace writer.

    Captures the old mode/sample_rate at preflight and applies the new
    configuration.  Rollback restores the previous settings.
    """

    def __init__(
        self,
        *,
        writer: Any,
        mode: str,
        sample_rate: float,
    ) -> None:
        super().__init__(
            name="routing_trace_writer",
            description="Reconfigure routing-trace writer mode and sample rate",
            reversible=True,
        )
        self._writer = writer
        self._mode = mode
        self._sample_rate = sample_rate
        self._old_mode: str | None = None
        self._old_sample_rate: float | None = None
        self._applied = False
        self._rolled_back = False
        self._finalized = False

    async def preflight(self) -> None:
        """Capture current writer configuration without mutation."""
        if self._writer is None:
            return
        self._old_mode = getattr(self._writer, "_mode", None) or getattr(
            self._writer, "mode", None
        )
        self._old_sample_rate = getattr(self._writer, "_sample_rate", None) or getattr(
            self._writer, "sample_rate", None
        )

    async def apply(self) -> None:
        """Apply new routing-trace writer configuration."""
        if self._writer is None:
            return
        self._writer.configure(mode=self._mode, sample_rate=self._sample_rate)
        self._applied = True

    async def rollback(self) -> None:
        """Restore previous writer configuration.

        Plan 016 Workstream D3: rollback failures propagate to the
        aggregator so degraded-state diagnostics can classify them.

        Workstream D4: a second rollback after success is a no-op; a
        rollback after finalize raises ``TransactionStateError``.
        """
        if self._finalized:
            raise TransactionStateError(f"Cannot rollback {self.name} after finalize")
        if not self._applied or self._rolled_back or self._writer is None:
            return
        if self._old_mode is not None and self._old_sample_rate is not None:
            try:
                self._writer.configure(
                    mode=self._old_mode, sample_rate=self._old_sample_rate
                )
            except Exception as exc:
                logger.debug(
                    "RoutingTraceWriterTransition rollback failed: %r",
                    exc,
                    exc_info=True,
                )
                raise
        self._applied = False
        self._rolled_back = True

    async def finalize(self) -> None:
        """Release captured old-state snapshots."""
        if self._finalized:
            return
        self._old_mode = None
        self._old_sample_rate = None
        self._finalized = True


class RoutingTraceGuardTransition(ProcessTransition):
    """Transition for reconfiguring the process-owned routing-trace guard.

    Captures the old guard settings at preflight and applies the new
    configuration.  Rollback restores the previous settings.
    """

    def __init__(
        self,
        *,
        guard: Any,
        threshold_ms: float,
        queue_occupancy_threshold: float,
        oldest_event_age_s: float,
        cooldown_s: float,
    ) -> None:
        super().__init__(
            name="routing_trace_guard",
            description="Reconfigure routing-trace guard thresholds",
            reversible=True,
        )
        self._guard = guard
        self._threshold_ms = threshold_ms
        self._queue_occupancy_threshold = queue_occupancy_threshold
        self._oldest_event_age_s = oldest_event_age_s
        self._cooldown_s = cooldown_s
        self._old_settings: dict[str, Any] | None = None
        self._applied = False
        self._rolled_back = False
        self._finalized = False

    async def preflight(self) -> None:
        """Capture current guard settings without mutation."""
        if self._guard is None:
            return
        self._old_settings = {
            "threshold_ms": getattr(self._guard, "_threshold_ms", None),
            "queue_occupancy_threshold": getattr(
                self._guard, "_queue_occupancy_threshold", None
            ),
            "oldest_event_age_s": getattr(self._guard, "_oldest_event_age_s", None),
            "cooldown_s": getattr(self._guard, "_cooldown_s", None),
        }

    async def apply(self) -> None:
        """Apply new guard configuration."""
        if self._guard is None:
            return
        self._guard.configure(
            threshold_ms=self._threshold_ms,
            queue_occupancy_threshold=self._queue_occupancy_threshold,
            oldest_event_age_s=self._oldest_event_age_s,
            cooldown_s=self._cooldown_s,
        )
        self._applied = True

    async def rollback(self) -> None:
        """Restore previous guard configuration.

        Plan 016 Workstream D3: rollback failures propagate to the
        aggregator so degraded-state diagnostics can classify them.

        Workstream D4: a second rollback after success is a no-op; a
        rollback after finalize raises ``TransactionStateError``.
        """
        if self._finalized:
            raise TransactionStateError(f"Cannot rollback {self.name} after finalize")
        if (
            not self._applied
            or self._rolled_back
            or self._guard is None
            or self._old_settings is None
        ):
            return
        try:
            self._guard.configure(**self._old_settings)
        except Exception as exc:
            logger.debug(
                "RoutingTraceGuardTransition rollback failed: %r", exc, exc_info=True
            )
            raise
        self._applied = False
        self._rolled_back = True

    async def finalize(self) -> None:
        """Release captured old-state snapshots."""
        if self._finalized:
            return
        self._old_settings = None
        self._finalized = True


_MISSING = object()
"""Sentinel distinguishing 'attribute absent' from 'attribute is None'."""


class EffectiveStateTransition(ProcessTransition):
    """Transition for updating app.state compatibility mirrors.

    Captures the previous effective state (config, config_digest,
    coordinator, catalog, etc.) at preflight and applies the new
    state at commit.  Rollback restores the previous state, correctly
    handling the case where an attribute was absent before preflight.
    """

    def __init__(
        self,
        *,
        app_state: Any,
        config: Any,
        config_digest: str,
        generation_id: int,
    ) -> None:
        super().__init__(
            name="effective_state",
            description="Update app.state compatibility mirrors",
            reversible=True,
        )
        self._app_state = app_state
        self._config = config
        self._config_digest = config_digest
        self._generation_id = generation_id
        self._old_config: Any = _MISSING
        self._old_config_digest: Any = _MISSING
        self._old_generation_id: Any = _MISSING
        self._applied = False
        self._rolled_back = False
        self._finalized = False

    async def preflight(self) -> None:
        """Capture previous effective state without mutation."""
        if self._app_state is None:
            return
        self._old_config = getattr(self._app_state, "config", _MISSING)
        self._old_config_digest = getattr(self._app_state, "config_digest", _MISSING)
        self._old_generation_id = getattr(self._app_state, "generation_id", _MISSING)

    async def apply(self) -> None:
        """Apply new effective state to app.state."""
        if self._app_state is None:
            return
        self._app_state.config = self._config
        self._app_state.config_digest = self._config_digest
        self._app_state.generation_id = self._generation_id
        self._applied = True

    async def rollback(self) -> None:
        """Restore previous effective state.

        Distinguishes three cases per attribute:
        - ``_MISSING``: attribute was absent before preflight → delete it
        - ``None``: attribute was present with value None → set to None
        - other: attribute had a real value → restore it

        Plan 016 Workstream D4: a second rollback after success is a
        no-op; a rollback after finalize raises ``TransactionStateError``.
        """
        if self._finalized:
            raise TransactionStateError(f"Cannot rollback {self.name} after finalize")
        if not self._applied or self._rolled_back or self._app_state is None:
            return
        self._restore_attr("config", self._old_config)
        self._restore_attr("config_digest", self._old_config_digest)
        self._restore_attr("generation_id", self._old_generation_id)
        self._applied = False
        self._rolled_back = True

    def _restore_attr(self, attr: str, old_value: Any) -> None:
        """Restore a single attribute on _app_state."""
        if old_value is _MISSING:
            try:  # noqa: SIM105
                delattr(self._app_state, attr)
            except AttributeError:
                pass
        else:
            setattr(self._app_state, attr, old_value)

    async def finalize(self) -> None:
        """Release captured old-state snapshots."""
        if self._finalized:
            return
        self._old_config = _MISSING
        self._old_config_digest = _MISSING
        self._old_generation_id = _MISSING
        self._finalized = True


@dataclass(frozen=True)
class ProcessTransitionPlan:
    """Prepared process transitions for a reload.

    Captures the task specs, callback factories, and transition
    metadata so the commit step can apply them atomically.
    """

    task_specs: tuple[Any, ...]
    callback_factories: dict[str, Any]
    transitions: tuple[ProcessTransition, ...]


async def preflight_all_transitions(plan: ProcessTransitionPlan) -> list[str]:
    """Run preflight on every transition in declared order.

    Must not mutate any process state.  Collects the exact transition
    that failed.  Raises RuntimeError if any preflight fails.
    """
    preflighted: list[str] = []
    for transition in plan.transitions:
        await transition.preflight()
        preflighted.append(transition.name)
    return preflighted


@dataclass
class TransitionApplyResult:
    """Tracks applied transitions for rollback and finalization.

    After a successful commit, call :meth:`finalize_all` to release
    captured old-state snapshots.  On failure, call
    :meth:`rollback_applied` to undo applied transitions in reverse
    order.

    Plan 016 Workstream D1: the result object owns the applied-stack
    lifecycle.  :meth:`apply_all` raises :class:`ProcessTransitionApplyError`
    carrying the failed transition's name and index plus the list of
    already-applied transitions.  Callers can then call
    :meth:`rollback_applied` against the same result instance without
    losing the partial stack to a helper that raised before returning.

    Plan 018 Workstream A3: rollback state is retryable — a partial
    rollback can be retried to restore remaining transitions.

    Plan 018 Workstream A4: finalization failures are surfaced in a
    :class:`TransitionFinalizeOutcome` and a second call resumes from
    the first incomplete transition.
    """

    _plan: ProcessTransitionPlan
    _applied: list[ProcessTransition] = field(
        default_factory=lambda: list[ProcessTransition]()
    )
    _rollback_state: TransitionRollbackState = TransitionRollbackState.NOT_ATTEMPTED
    _unrestored: list[ProcessTransition] = field(
        default_factory=lambda: list[ProcessTransition]()
    )
    _finalized_transitions: list[ProcessTransition] = field(
        default_factory=lambda: list[ProcessTransition]()
    )
    _finalized: bool = False

    async def apply_all(self) -> None:
        """Apply transitions in order.  Stop at first failure.

        On failure raises :class:`ProcessTransitionApplyError` so the
        caller has the failed transition's name and index plus the
        list of already-applied transitions for the rollback path.
        """
        for index, transition in enumerate(self._plan.transitions):
            try:
                await transition.apply()
            except Exception as exc:
                raise ProcessTransitionApplyError(
                    failed_transition_name=transition.name,
                    failed_transition_index=index,
                    applied_transition_names=tuple(t.name for t in self._applied),
                    original_exception=exc,
                ) from exc
            self._applied.append(transition)

    async def rollback_applied(self) -> TransitionRollbackOutcome:
        """Roll back applied transitions in reverse order.

        Continues after individual rollback failures so every
        transition gets a chance.  Returns a
        :class:`TransitionRollbackOutcome` with structured results.

        Plan 016 Workstream D3: per-transition rollback failures
        propagate from the concrete rollback methods; this aggregator
        catches each failure, logs it, and continues to the next
        transition so a partial-stack restore still runs to
        completion.  Aggregate failures are returned to the caller for
        degraded-state classification.

        Plan 017 Workstream B: return type is now
        :class:`TransitionRollbackOutcome` with explicit attempted,
        restored, and failures fields.

        Plan 018 Workstream A3: rollback state is retryable — a
        partial rollback can be retried to restore remaining
        transitions.  ``COMPLETE`` returns immediately; ``PARTIAL``
        retries only unrestored transitions.
        """
        if self._rollback_state is TransitionRollbackState.COMPLETE:
            return TransitionRollbackOutcome(
                attempted=(),
                restored=(),
                failures=(),
            )
        transitions_to_rollback: list[ProcessTransition]
        if self._rollback_state is TransitionRollbackState.PARTIAL and self._unrestored:
            transitions_to_rollback = list(reversed(self._unrestored))
        else:
            transitions_to_rollback = list(reversed(self._applied))
            self._unrestored = list(self._applied)
        attempted: list[str] = []
        restored: list[str] = []
        failures: list[tuple[str, Exception]] = []
        for transition in transitions_to_rollback:
            attempted.append(transition.name)
            try:
                await transition.rollback()
            except Exception as exc:
                failures.append((transition.name, exc))
                logger.warning(
                    "Process transition %r rollback failed: %s",
                    transition.name,
                    exc,
                    exc_info=True,
                )
            else:
                restored.append(transition.name)
                if transition in self._unrestored:
                    self._unrestored.remove(transition)
        if not self._unrestored:
            self._rollback_state = TransitionRollbackState.COMPLETE
        else:
            self._rollback_state = TransitionRollbackState.PARTIAL
        return TransitionRollbackOutcome(
            attempted=tuple(attempted),
            restored=tuple(restored),
            failures=tuple(failures),
        )

    async def finalize_all(self) -> TransitionFinalizeOutcome:
        """Finalize applied transitions after commit.

        Called only after the transaction is accepted.  Releases
        captured old-state snapshots.  Returns a
        :class:`TransitionFinalizeOutcome` with structured results.

        Plan 016 Workstream E2: finalize is a no-op once it has run
        and must never be invoked from a rollback path.

        Plan 018 Workstream A4: failures are surfaced in the outcome
        and a second call resumes from the first incomplete
        transition.  ``_finalized`` is set only when all transitions
        have been finalized.
        """
        if self._finalized:
            return TransitionFinalizeOutcome(
                attempted=(),
                finalized=(),
                failures=(),
                remaining=(),
            )
        already_finalized_names = {t.name for t in self._finalized_transitions}
        attempted: list[str] = []
        finalized_names: list[str] = []
        failures: list[tuple[str, Exception]] = []
        for transition in self._applied:
            if transition.name in already_finalized_names:
                continue
            attempted.append(transition.name)
            try:
                await transition.finalize()
            except Exception as exc:
                failures.append((transition.name, exc))
                logger.debug(
                    "Finalization of transition %s failed (housekeeping)",
                    transition.name,
                    exc_info=True,
                )
            else:
                finalized_names.append(transition.name)
                self._finalized_transitions.append(transition)
        remaining = tuple(
            t.name
            for t in self._applied
            if t.name not in already_finalized_names and t.name not in finalized_names
        )
        if not remaining:
            self._finalized = True
        return TransitionFinalizeOutcome(
            attempted=tuple(attempted),
            finalized=tuple(finalized_names),
            failures=tuple(failures),
            remaining=remaining,
        )

    @property
    def applied_count(self) -> int:
        """Number of transitions successfully applied."""
        return len(self._applied)

    @property
    def is_fully_applied(self) -> bool:
        """True if all transitions in the plan were applied."""
        return len(self._applied) == len(self._plan.transitions)

    @property
    def applied_transitions(self) -> tuple[ProcessTransition, ...]:
        """Immutable view of transitions that successfully applied."""
        return tuple(self._applied)

    @property
    def is_rolled_back(self) -> bool:
        """True if :meth:`rollback_applied` has been called successfully."""
        return self._rollback_state is not TransitionRollbackState.NOT_ATTEMPTED

    @property
    def rollback_state(self) -> TransitionRollbackState:
        """Current rollback state for diagnostics."""
        return self._rollback_state

    @property
    def is_finalized(self) -> bool:
        """True if :meth:`finalize_all` has been called."""
        return self._finalized


@dataclass
class AcceptedReloadFinalization:
    """Plan 017 Workstream D: tracks post-acceptance finalization steps.

    After a reload is accepted (SQLite committed + runtime swap
    committed), the remaining finalization steps are housekeeping that
    must not fail the reload.  This record tracks which steps have
    completed so diagnostics and retry logic can identify the current
    finalization state.
    """

    candidate_ownership_transferred: bool = False
    compatibility_mirror_updated: bool = False
    transitions_finalized: bool = False
    retirement_scheduled: bool = False
    transaction_completed: bool = False

    def first_incomplete_step(self) -> str | None:
        """Return the name of the next incomplete step, or None if done."""
        if not self.candidate_ownership_transferred:
            return "ownership_transfer"
        if not self.compatibility_mirror_updated:
            return "compatibility_mirror_update"
        if not self.transitions_finalized:
            return "transitions_finalization"
        if not self.retirement_scheduled:
            return "retirement_scheduling"
        if not self.transaction_completed:
            return "transaction_completion"
        return None

    def is_complete(self) -> bool:
        """True when all finalization steps have completed."""
        return self.first_incomplete_step() is None


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
        self._preflighted_transitions: tuple[str, ...] = ()
        self._staged_swap_generation_id: int | None = None
        self._swap_old_generation_id: int | None = None

        # Explicit publication facts (C4) — tracked independently of
        # state-machine transitions so diagnostics derive from facts.
        self.publication_attempted: bool = False
        self.publication_occurred: bool = False
        self.active_generation_before: int | None = None
        self.active_generation_after: int | None = None
        self.persistence_committed: bool = False
        self.process_transitions_applied: bool = False
        self.effective_state_updated: bool = False
        self.retirement_scheduled: bool = False

        # Plan 016 Workstream H2/H3: per-stage progress flags surfaced
        # in the diagnostic.  Each flips to ``True`` when the matching
        # post-publication boundary has been crossed.  The terminal
        # snapshot freezes the values so a reload that crashes mid-way
        # shows which step is still pending.
        self._lease_admission_gated_at_terminal: bool = False
        self._post_commit_finalization_pending: bool = True
        self._ownership_transfer_pending: bool = True
        self._mirror_update_pending: bool = True
        self._retirement_scheduling_pending: bool = True
        self._pending_swap_state_at_terminal: str | None = None
        self._publication_epoch: int = 0

        # Plan 017 Workstream D: explicit acceptance fact and finalization
        # record.  ``_reload_accepted`` flips to True only after both
        # SQLite commit and runtime swap commit succeed.  Post-acceptance
        # finalization failures must NOT call _abort_precommit_reload.
        self._reload_accepted: bool = False
        self._acceptance_state: ReloadAcceptanceState = (
            ReloadAcceptanceState.NOT_ACCEPTED
        )
        self._accepted_finalization: AcceptedReloadFinalization = (
            AcceptedReloadFinalization()
        )

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

    # -- Workstream H2/H3 diagnostic snapshot -------------------------------

    @property
    def pending_swap_state_at_terminal(self) -> str | None:
        """Plan 016 Workstream H2: pending swap state at finalization.

        Records the most recent ``PendingSwapState`` value seen by
        the manager during the transaction so the diagnostic shows
        whether the swap reached ``COMMITTED``, ``FINALIZED``, or
        was rolled back before reaching the post-publication steps.
        """
        return self._pending_swap_state_at_terminal

    @property
    def lease_admission_gated_at_terminal(self) -> bool:
        """Plan 016 Workstream H2: lease admission gate state.

        ``True`` if the runtime manager reported an active lease
        gate at finalization time.  Useful for distinguishing
        reloads that committed cleanly (gate cleared) from those
        that aborted before clearing the gate.
        """
        return self._lease_admission_gated_at_terminal

    @property
    def post_commit_finalization_pending(self) -> bool:
        """Plan 016 Workstream H3: post-commit finalization pending.

        ``True`` until the post-commit ``finalize_all()`` step has
        completed.  Flips to ``False`` when ``finalize_all()`` runs
        successfully.
        """
        return self._post_commit_finalization_pending

    @property
    def ownership_transfer_pending(self) -> bool:
        """Plan 016 Workstream H3: ownership transfer pending.

        ``True`` until the candidate's ownership has been
        transferred to the runtime manager.  Flips to ``False`` on
        successful publication.
        """
        return self._ownership_transfer_pending

    @property
    def mirror_update_pending(self) -> bool:
        """Plan 016 Workstream H3: mirror-update pending.

        ``True`` until ``mirror_generation_on_app_state`` has been
        called for the new generation.  Flips to ``False`` when
        observable state is updated.
        """
        return self._mirror_update_pending

    @property
    def retirement_scheduling_pending(self) -> bool:
        """Plan 016 Workstream H3: retirement scheduling pending.

        ``True`` until the old-generation retirement task has been
        scheduled.  Flips to ``False`` when retirement is scheduled.
        """
        return self._retirement_scheduling_pending

    @property
    def publication_epoch(self) -> int:
        """Plan 016 Workstream H2: monotonic publication epoch.

        Captures the runtime manager's ``publication_epoch`` at the
        moment of finalization so operators can correlate the
        diagnostic with the manager's monotonic counter.
        """
        return self._publication_epoch

    # -- Workstream D: acceptance fact -----------------------------------

    @property
    def reload_accepted(self) -> bool:
        """Plan 017 Workstream D: True after SQLite commit + runtime swap.

        Once accepted, post-acceptance finalization failures must NOT
        call ``_abort_precommit_reload()`` — the candidate remains
        authoritative.
        """
        return self._reload_accepted

    @property
    def acceptance_state(self) -> ReloadAcceptanceState:
        """Plan 017 Workstream D5: explicit acceptance lifecycle state."""
        return self._acceptance_state

    @property
    def accepted_finalization(self) -> AcceptedReloadFinalization:
        """Plan 017 Workstream D: post-acceptance finalization record."""
        return self._accepted_finalization

    def mark_persistence_committed_runtime_pending(self) -> None:
        """Mark that SQLite committed but runtime swap is still pending.

        Plan 017 Workstream D5: records the narrow boundary between
        SQLite commit success and runtime swap commit.
        """
        self._acceptance_state = (
            ReloadAcceptanceState.PERSISTENCE_COMMITTED_RUNTIME_PENDING
        )

    def mark_accepted(self) -> None:
        """Mark reload as accepted after SQLite commit + runtime swap commit.

        Must be called after the SQLite transaction has committed
        (db.transaction() exited) and the runtime swap has been
        committed (pending_swap.commit() succeeded).  The state machine
        should be at ``RUNTIME_SWAP_COMMITTED`` at this point.
        """
        if self._state not in (
            TransactionState.RUNTIME_SWAP_COMMITTED,
            TransactionState.RUNTIME_PUBLISHED,
            TransactionState.PROCESS_TRANSITIONS_APPLIED,
            TransactionState.PERSISTENCE_COMMITTED,
            TransactionState.OBSERVABLE_STATE_UPDATED,
            TransactionState.RETIREMENT_SCHEDULED,
            TransactionState.COMPLETED,
        ):
            raise TransactionStateError(
                f"Cannot mark accepted from state {self._state.value}"
            )
        if not self.publication_occurred:
            raise TransactionStateError(
                "Cannot mark accepted: publication did not occur"
            )
        self._reload_accepted = True
        self._acceptance_state = ReloadAcceptanceState.ACCEPTED

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

    def mark_process_transitions_preflighted(
        self,
        preflighted: list[str],
    ) -> None:
        """Transition: PROCESS_TRANSITIONS_PREPARED → PROCESS_TRANSITIONS_PREFLIGHTED.

        Records the list of transitions that passed preflight validation.
        """
        self._preflighted_transitions = tuple(preflighted)
        self._transition_to(TransactionState.PROCESS_TRANSITIONS_PREFLIGHTED)

    def mark_commit_started(
        self,
        old_generation_id: int,
    ) -> None:
        """Transition: PROCESS_TRANSITIONS_PREFLIGHTED → COMMIT_STARTED.

        Plan 016 Workstream D5: production ordering is
        PROCESS_TRANSITIONS_PREPARED → PROCESS_TRANSITIONS_PREFLIGHTED
        → COMMIT_STARTED, so preflight always completes before commit
        state is recorded.
        """
        self._old_generation_id = old_generation_id
        self.active_generation_before = old_generation_id
        self._commit_diagnostics = CommitDiagnostics(
            commit_started_at=time.monotonic(),
            old_generation_id=old_generation_id,
        )
        self._transition_to(TransactionState.COMMIT_STARTED)

    def mark_runtime_staged(
        self,
        pending_swap: Any,
    ) -> None:
        """Transition: COMMIT_STARTED → RUNTIME_STAGED."""
        self.publication_attempted = True
        self._staged_swap_generation_id = getattr(
            pending_swap, "candidate_generation_id", None
        )
        self._transition_to(TransactionState.RUNTIME_STAGED)

    def mark_runtime_swap_committed(
        self,
        old_generation_id: int | None,
        *,
        new_generation_id: int | None = None,
    ) -> None:
        """Transition: RUNTIME_STAGED → RUNTIME_SWAP_COMMITTED.

        Plan 016 Workstream H1: record the new active generation ID
        and publication timestamp so committed transaction diagnostics
        identify the new active generation.  When the caller passes
        ``new_generation_id``, ``active_generation_after`` is set
        immediately for downstream consumers.
        """
        self.publication_occurred = True
        self._swap_old_generation_id = old_generation_id
        if new_generation_id is not None:
            self.active_generation_after = new_generation_id
            self._commit_diagnostics = CommitDiagnostics(
                commit_started_at=self._commit_diagnostics.commit_started_at,
                old_generation_id=self._commit_diagnostics.old_generation_id,
                new_generation_id=new_generation_id,
            )
        # Plan 016 Workstream H3: ownership is transferred at swap
        # commit; mirror is updated at observable-state; retirement is
        # scheduled after the swap commits.
        self._pending_swap_state_at_terminal = "committed"
        self._lease_admission_gated_at_terminal = False
        self._transition_to(TransactionState.RUNTIME_SWAP_COMMITTED)

    def mark_runtime_published(
        self,
        published_generation: RuntimeGeneration,
    ) -> None:
        """Transition: COMMIT_STARTED → RUNTIME_PUBLISHED."""
        self._published_generation = published_generation
        self.publication_attempted = True
        self.publication_occurred = True
        self.active_generation_after = published_generation.generation_id
        self._commit_diagnostics = CommitDiagnostics(
            commit_started_at=self._commit_diagnostics.commit_started_at,
            old_generation_id=self._commit_diagnostics.old_generation_id,
            new_generation_id=published_generation.generation_id,
            publication_duration_s=(
                time.monotonic() - self._commit_diagnostics.commit_started_at
            ),
        )
        # Plan 016 Workstream H3: ownership is transferred once the
        # candidate is published.
        self._pending_swap_state_at_terminal = "committed"
        self._transition_to(TransactionState.RUNTIME_PUBLISHED)

    def mark_process_transitions_applied(self) -> None:
        """Mark process transitions applied (from SWAP_COMMITTED or PUBLISHED)."""
        self.process_transitions_applied = True
        self._transition_to(TransactionState.PROCESS_TRANSITIONS_APPLIED)

    def mark_persistence_committed(self) -> None:
        """Transition: PROCESS_TRANSITIONS_APPLIED → PERSISTENCE_COMMITTED."""
        self.persistence_committed = True
        self._transition_to(TransactionState.PERSISTENCE_COMMITTED)

    def mark_observable_state_updated(self) -> None:
        """Transition: PERSISTENCE_COMMITTED → OBSERVABLE_STATE_UPDATED.

        Plan 016 Workstream H3: the app-state mirror is updated at
        this boundary, so ``mirror_update_pending`` flips to False.
        """
        self.effective_state_updated = True
        self._mirror_update_pending = False
        self._transition_to(TransactionState.OBSERVABLE_STATE_UPDATED)

    def mark_retirement_scheduled(self) -> None:
        """Transition: OBSERVABLE_STATE_UPDATED → RETIREMENT_SCHEDULED.

        Plan 016 Workstream H3: retirement is scheduled at this
        boundary.
        """
        self.retirement_scheduled = True
        self._retirement_scheduling_pending = False
        self._transition_to(TransactionState.RETIREMENT_SCHEDULED)

    def mark_ownership_transferred(self) -> None:
        """Mark that candidate ownership has been transferred.

        Plan 018 Workstream B4: ownership transfer pending flag is
        cleared at the actual transfer point (``transfer_to_runtime_manager``),
        not at publication.
        """
        self._ownership_transfer_pending = False

    def mark_completed(self) -> None:
        """Transition: RETIREMENT_SCHEDULED → COMPLETED.

        Plan 016 Workstream H3: post-commit finalization completes
        here.
        """
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
        self._post_commit_finalization_pending = False
        self._transition_to(TransactionState.COMPLETED)

    def mark_aborting(self, error: Exception | None = None) -> None:
        """Transition to ABORTING from any non-terminal state.

        When aborting from a post-commit state, records that publication
        was attempted so diagnostics can distinguish pre-commit abort
        from post-publication compensation.

        Plan 016 Workstream H4: ``RUNTIME_STAGED`` records
        ``publication_attempted=True`` (publication was attempted but
        did not occur) and ``RUNTIME_SWAP_COMMITTED`` (or any later
        state) records ``publication_occurred=True`` so abort
        diagnostics derive from explicit facts rather than the broad
        ``is_committing`` state category.

        Plan 018 Workstream B2: accepted reloads cannot be aborted.
        Once ``_reload_accepted`` is True, ``mark_aborting`` raises
        ``TransactionStateError`` so callers catch the misuse early.
        """
        if self._reload_accepted:
            raise TransactionStateError("Accepted reload cannot be aborted")
        if self._state in (
            TransactionState.COMPLETED,
            TransactionState.ABORTED,
            TransactionState.COMPENSATION_FAILED,
            TransactionState.ABORTING,
        ):
            return  # already terminal or aborting
        self._error = error
        if self._state is TransactionState.RUNTIME_STAGED:
            self.publication_attempted = True
        elif self._state in (
            TransactionState.COMMIT_STARTED,
            TransactionState.RUNTIME_SWAP_COMMITTED,
            TransactionState.RUNTIME_PUBLISHED,
            TransactionState.PROCESS_TRANSITIONS_APPLIED,
            TransactionState.PERSISTENCE_COMMITTED,
            TransactionState.OBSERVABLE_STATE_UPDATED,
            TransactionState.RETIREMENT_SCHEDULED,
        ):
            self.publication_attempted = True
            self.publication_occurred = True
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
            TransactionState.RUNTIME_STAGED,
            TransactionState.RUNTIME_SWAP_COMMITTED,
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
            "publication_attempted": self.publication_attempted,
            "publication_occurred": self.publication_occurred,
            "active_generation_before": self.active_generation_before,
            "active_generation_after": self.active_generation_after,
            "persistence_committed": self.persistence_committed,
            "process_transitions_applied": self.process_transitions_applied,
            "effective_state_updated": self.effective_state_updated,
            "retirement_scheduled": self.retirement_scheduled,
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
            # Plan 017 Workstream D: acceptance and finalization facts.
            "reload_accepted": self._reload_accepted,
            "acceptance_state": self._acceptance_state.value,
            "accepted_finalization": {
                "candidate_ownership_transferred": (
                    self._accepted_finalization.candidate_ownership_transferred
                ),
                "compatibility_mirror_updated": (
                    self._accepted_finalization.compatibility_mirror_updated
                ),
                "transitions_finalized": (
                    self._accepted_finalization.transitions_finalized
                ),
                "retirement_scheduled": (
                    self._accepted_finalization.retirement_scheduled
                ),
                "transaction_completed": (
                    self._accepted_finalization.transaction_completed
                ),
            },
            "transition_history": [
                {"state": s.value, "at": t} for s, t in self._transition_history
            ],
        }


__all__ = [
    "AcceptedReloadFinalization",
    "CommitDiagnostics",
    "EffectiveStateTransition",
    "PersistenceDelta",
    "ProcessTransition",
    "ProcessTransitionApplyError",
    "ProcessTransitionPlan",
    "ReloadAcceptanceState",
    "ReloadTransaction",
    "RoutingTraceGuardTransition",
    "RoutingTraceWriterTransition",
    "TaskSpecTransition",
    "TransitionApplyResult",
    "TransitionFinalizeOutcome",
    "TransitionRollbackOutcome",
    "TransitionRollbackState",
    "TransactionState",
    "TransactionStateError",
    "preflight_all_transitions",
]
