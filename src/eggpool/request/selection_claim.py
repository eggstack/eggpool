"""Selection claim state machine for ``RequestCoordinator._select_and_persist_attempt``.

Milestone B (dispatch stability / selection-lock deconvoying) replaces
the coordinator's broad selection critical section — which previously
held the global ``_select_lock`` across the entire durable transaction
AND the runtime publication step — with a narrower, explicit
claim/persistence/publication state machine.  This module owns the
state-object half of that refactor.

The claim is a short-lived, in-memory record of one account selected
for one attempt.  It is *not* a durable row; durability lives in
``request_attempts`` / ``reservations`` / ``requests``.  The claim's
job is to make the ordering between the in-process side effects
(circuit-breaker slot, active-request counter, quota reservation,
``context.attempted_accounts``) and the durable commit explicit and
testable.

State transitions
-----------------

``PLAN`` → ``CLAIM`` → ``PERSIST`` → ``COMMITTED`` → ``PUBLISHED``

Failure transitions from any pre-PUBLISHED state go to ``ROLLED_BACK``;
PUBLISHED state has no rollback (the dispatch already has the claim's
identity) but exposes a ``compensate()`` helper that releases runtime
state when post-commit publication fails.

Invariant: state advances exactly once per claim token.  Each transition
method is idempotent — calling it twice with the same expected next
state is a no-op; calling it with a different expected next state is a
``SelectionClaimError``.  The token guards against accidental
double-publish or double-rollback under retry paths.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eggpool.routing.router import RoutingExclusion

if TYPE_CHECKING:
    from eggpool.health.health_manager import HealthManager


class SelectionClaimState(enum.Enum):
    """Lifecycle states for a ``SelectionClaim``.

    The state advances through PLAN -> CLAIM -> PERSIST -> COMMITTED ->
    PUBLISHED.  Any pre-COMMITTED transition can divert to ROLLED_BACK.
    PUBLISHED has no rollback path; use ``compensate_post_commit()``
    instead.
    """

    PLAN = "plan"
    CLAIM = "claim"
    PERSIST = "persist"
    COMMITTED = "committed"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"


class SelectionClaimError(RuntimeError):
    """Raised on invalid state transitions or duplicate token use.

    Used in tests to assert invariant violations.  Production cleanup
    paths catch these and fail closed by compensating any partial
    state already published.
    """


@dataclass(frozen=True, slots=True)
class SelectionClaim:
    """Short-lived record of one account selected for one attempt.

    ``token`` uniquely identifies this claim across all transitions.
    State transitions are not part of the frozen identity; the live
    ``_state`` field is mutated under ``_state_lock``.
    """

    proxy_request_id: str
    attempt_number: int
    account_name: str
    account_id: int
    provider_id: str
    model_id: str
    protocol: str
    estimated_tokens: int
    estimated_microdollars: int
    selected_score: float | None
    selected_tier: int | None
    token: str
    created_monotonic_ns: int = field(default_factory=time.perf_counter_ns)

    def __post_init__(self) -> None:
        if not self.account_name:
            raise ValueError("SelectionClaim.account_name must be non-empty")
        if not self.token:
            raise ValueError("SelectionClaim.token must be non-empty")


@dataclass(slots=True)
class SelectionClaimTracker:
    """Mutable state machine wrapper around a :class:`SelectionClaim`.

    Encapsulates the PLAN -> CLAIM -> PERSIST -> COMMITTED -> PUBLISHED
    lifecycle.  ``transition()`` advances to the next state and is
    idempotent on the same expected target state.  Invalid transitions
    raise :class:`SelectionClaimError` so test cases can pin invariants
    and production code can fail closed.

    The tracker is *not* thread-safe by design; ``RequestCoordinator``
    is the only owner and serializes tracker access through the
    coordinator's selection-claim lock.
    """

    claim: SelectionClaim
    _state: SelectionClaimState = SelectionClaimState.PLAN
    _exclusions: list[RoutingExclusion] = field(default_factory=list[RoutingExclusion])
    _committed_db_request_id: str | None = None
    _committed_attempt_id: int | None = None
    _committed_reservation_id: str | None = None
    _active_count_published: bool = False
    _quota_reservation_published: bool = False
    _health_slot_acquired: bool = False
    _rolled_back_at: int | None = None

    @property
    def state(self) -> SelectionClaimState:
        return self._state

    @property
    def exclusions(self) -> tuple[RoutingExclusion, ...]:
        return tuple(self._exclusions)

    def add_exclusion(self, exclusion: RoutingExclusion) -> None:
        """Record an account that was excluded during the claim phase.

        Called from the revalidation loop when a candidate is rejected
        by the circuit breaker.  Ordering is preserved so the final
        trace accurately reflects the walk-down order.
        """
        self._exclusions.append(exclusion)

    def mark_health_slot_acquired(self) -> None:
        """Mark that the circuit-breaker slot for this claim is held.

        The slot must be released exactly once on every failure /
        cancellation path; ``_release_health_slot_if_held`` enforces the
        ``release exactly once`` invariant.
        """
        self._health_slot_acquired = True

    def transition(self, expected: SelectionClaimState) -> None:
        """Advance to ``expected`` from the current state.

        Idempotent when ``expected`` already matches the current state.
        Raises :class:`SelectionClaimError` on any other attempt.
        """
        if self._state == expected:
            return
        if self._state == SelectionClaimState.ROLLED_BACK:
            raise SelectionClaimError(
                f"Cannot transition from ROLLED_BACK to {expected.value!r} "
                f"(token={self.claim.token!r})"
            )
        if self._state == SelectionClaimState.PUBLISHED:
            raise SelectionClaimError(
                f"Cannot transition from PUBLISHED to {expected.value!r} "
                f"(token={self.claim.token!r})"
            )
        order = [
            SelectionClaimState.PLAN,
            SelectionClaimState.CLAIM,
            SelectionClaimState.PERSIST,
            SelectionClaimState.COMMITTED,
            SelectionClaimState.PUBLISHED,
        ]
        if order.index(expected) != order.index(self._state) + 1:
            raise SelectionClaimError(
                f"Invalid selection-claim transition: "
                f"{self._state.value!r} -> {expected.value!r} "
                f"(token={self.claim.token!r})"
            )
        self._state = expected

    def mark_persisted(
        self,
        *,
        db_request_id: str,
        attempt_id: int,
        reservation_id: str,
    ) -> None:
        """Record durable IDs once the SQLite transaction commits.

        Idempotent: replaying with the same values is a no-op.  Calling
        with different IDs after a successful commit is a
        ``SelectionClaimError`` (would imply two commits under one
        claim).
        """
        self.transition(SelectionClaimState.COMMITTED)
        if self._committed_db_request_id is None:
            self._committed_db_request_id = db_request_id
            self._committed_attempt_id = attempt_id
            self._committed_reservation_id = reservation_id
            return
        if (
            self._committed_db_request_id != db_request_id
            or self._committed_attempt_id != attempt_id
            or self._committed_reservation_id != reservation_id
        ):
            raise SelectionClaimError(
                f"Cannot change durable IDs after commit (token={self.claim.token!r})"
            )

    def mark_published(
        self,
        *,
        active_count_increased: bool,
        quota_reservation_added: bool,
    ) -> None:
        """Record that runtime state has been published.

        After this transition, no rollback is possible; use
        ``compensate_post_commit()`` instead.
        """
        self.transition(SelectionClaimState.PUBLISHED)
        self._active_count_published = active_count_increased
        self._quota_reservation_published = quota_reservation_added

    def roll_back(self) -> None:
        """Move the claim to ROLLED_BACK.  Idempotent."""
        if self._state == SelectionClaimState.ROLLED_BACK:
            return
        if self._state in {
            SelectionClaimState.PUBLISHED,
            SelectionClaimState.COMMITTED,
        }:
            raise SelectionClaimError(
                f"Cannot roll back from {self._state.value!r} via roll_back(); "
                f"use compensate_post_commit() "
                f"(token={self.claim.token!r})"
            )
        self._state = SelectionClaimState.ROLLED_BACK
        self._rolled_back_at = time.perf_counter_ns()

    def compensate_post_commit(self) -> None:
        """Roll back a claim whose durable rows committed but whose
        runtime publication step failed.

        The claim moves to ROLLED_BACK regardless of which mid-publish
        state it was in.  Callers must still unwind the partial
        runtime state (active count, quota reservation, health slot)
        themselves; this method only updates the tracker.
        """
        if self._state == SelectionClaimState.ROLLED_BACK:
            return
        if self._state not in {
            SelectionClaimState.COMMITTED,
            SelectionClaimState.PUBLISHED,
        }:
            raise SelectionClaimError(
                f"Cannot compensate_post_commit from {self._state.value!r} "
                f"(token={self.claim.token!r})"
            )
        self._state = SelectionClaimState.ROLLED_BACK
        self._rolled_back_at = time.perf_counter_ns()

    @property
    def committed_db_request_id(self) -> str | None:
        return self._committed_db_request_id

    @property
    def committed_attempt_id(self) -> int | None:
        return self._committed_attempt_id

    @property
    def committed_reservation_id(self) -> str | None:
        return self._committed_reservation_id

    @property
    def active_count_published(self) -> bool:
        return self._active_count_published

    @property
    def quota_reservation_published(self) -> bool:
        return self._quota_reservation_published

    @property
    def health_slot_acquired(self) -> bool:
        return self._health_slot_acquired

    @property
    def rolled_back_at(self) -> int | None:
        return self._rolled_back_at

    def release_health_slot_if_held(
        self,
        health_manager: HealthManager | None,
    ) -> None:
        """Release the circuit-breaker slot exactly once.

        Safe to call from every failure path; subsequent calls are
        no-ops.  ``health_manager`` may be ``None`` (tests with no
        ``HealthManager`` wired).
        """
        if not self._health_slot_acquired:
            return
        if health_manager is None:
            self._health_slot_acquired = False
            return
        health_manager.release_request(self.claim.account_name)
        self._health_slot_acquired = False
