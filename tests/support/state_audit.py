"""Request-level state audit snapshot for Plan 023 error-isolation tests.

Captures every durable and in-memory ownership surface before and after
a single request so tests can assert that failed/cancelled requests do not
leak reservations, active counts, health state, or database rows.

Usage::

    before = await RequestStateAuditSnapshot.capture(db, coordinator)
    # ... run request ...
    after = await RequestStateAuditSnapshot.capture(db, coordinator)
    diff = before.diff(after)
    assert diff.request_history_changes  # expected: new rows
    assert not diff.runtime_ownership_changes  # forbidden: leaked state
    assert not diff.health_changes

The helper is deliberately scoped to a single request lifecycle — it does
not capture generation-level state (use ``RuntimeSnapshot`` for that).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DurableFacts:
    """Scalar, bounded facts about durable (SQLite) state."""

    request_rows: int = 0
    attempt_rows: int = 0
    reservation_rows: int = 0
    account_backoff_rows: int = 0
    account_event_rows: int = 0
    model_availability_rows: int = 0
    routing_decision_rows: int = 0
    finalization_retry_rows: int = 0


@dataclass(frozen=True)
class RuntimeFacts:
    """Scalar, bounded facts about in-memory runtime state."""

    active_request_count: int = 0
    reservation_count: int = 0
    reservation_tokens: int = 0
    estimated_cost: float = 0.0
    health_account_states: dict[str, str] = field(default_factory=dict)
    health_circuit_states: dict[str, str] = field(default_factory=dict)
    health_cooldown_remaining: dict[str, float] = field(default_factory=dict)
    disabled_models: dict[str, bool] = field(default_factory=dict)
    finalization_queue_depth: int = 0
    dispatch_writer_queue_depth: int = 0
    db_connection_state: str = "connected"
    db_invalidated: bool = False


@dataclass(frozen=True)
class StateAuditDiff:
    """Structured diff between two ``RequestStateAuditSnapshot`` captures.

    Categories distinguish expected request-history additions from
    forbidden shared-state changes.
    """

    request_history_changes: list[str] = field(default_factory=list)
    runtime_ownership_changes: list[str] = field(default_factory=list)
    health_changes: list[str] = field(default_factory=list)
    durable_backoff_changes: list[str] = field(default_factory=list)
    database_connection_changes: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when no unexpected shared-state changes occurred."""
        return not (
            self.runtime_ownership_changes
            or self.health_changes
            or self.durable_backoff_changes
            or self.database_connection_changes
        )


class RequestStateAuditSnapshot:
    """Point-in-time capture of request-scoped state.

    Call :meth:`capture` before and after a request to produce a
    deterministic diff.  The capture is synchronous where possible and
    uses only scalar/bounded fields so snapshots are cheap.
    """

    def __init__(
        self,
        durable: DurableFacts,
        runtime: RuntimeFacts,
    ) -> None:
        self.durable = durable
        self.runtime = runtime

    @classmethod
    async def capture(
        cls,
        db: Any,
        coordinator: Any,
    ) -> RequestStateAuditSnapshot:
        """Capture current state from the database and coordinator.

        Args:
            db: ``Database`` instance with an active connection.
            coordinator: ``RequestCoordinator`` instance.
        """
        durable = await _capture_durable(db)
        runtime = _capture_runtime(coordinator)
        return cls(durable=durable, runtime=runtime)

    def diff(self, other: RequestStateAuditSnapshot) -> StateAuditDiff:
        """Compute a structured diff against another snapshot.

        ``self`` is the *before* snapshot, ``other`` is the *after*.
        """
        request_changes: list[str] = []
        runtime_changes: list[str] = []
        health_changes: list[str] = []
        backoff_changes: list[str] = []
        db_changes: list[str] = []

        # --- Request history (expected) ---
        if other.durable.request_rows > self.durable.request_rows:
            request_changes.append(
                f"request_rows: {self.durable.request_rows} → "
                f"{other.durable.request_rows}"
            )
        if other.durable.attempt_rows > self.durable.attempt_rows:
            request_changes.append(
                f"attempt_rows: {self.durable.attempt_rows} → "
                f"{other.durable.attempt_rows}"
            )
        if other.durable.reservation_rows != self.durable.reservation_rows:
            request_changes.append(
                f"reservation_rows: {self.durable.reservation_rows} → "
                f"{other.durable.reservation_rows}"
            )
        if other.durable.routing_decision_rows > self.durable.routing_decision_rows:
            request_changes.append(
                f"routing_decision_rows: "
                f"{self.durable.routing_decision_rows} → "
                f"{other.durable.routing_decision_rows}"
            )
        if other.durable.finalization_retry_rows > self.durable.finalization_retry_rows:
            request_changes.append(
                f"finalization_retry_rows: "
                f"{self.durable.finalization_retry_rows} → "
                f"{other.durable.finalization_retry_rows}"
            )

        # --- Runtime ownership (forbidden to leak) ---
        if other.runtime.active_request_count != self.runtime.active_request_count:
            runtime_changes.append(
                f"active_request_count: {self.runtime.active_request_count} → "
                f"{other.runtime.active_request_count}"
            )
        if other.runtime.reservation_count != self.runtime.reservation_count:
            runtime_changes.append(
                f"reservation_count: {self.runtime.reservation_count} → "
                f"{other.runtime.reservation_count}"
            )
        if other.runtime.reservation_tokens != self.runtime.reservation_tokens:
            runtime_changes.append(
                f"reservation_tokens: {self.runtime.reservation_tokens} → "
                f"{other.runtime.reservation_tokens}"
            )
        if (
            other.runtime.finalization_queue_depth
            != self.runtime.finalization_queue_depth
        ):
            runtime_changes.append(
                f"finalization_queue_depth: "
                f"{self.runtime.finalization_queue_depth} → "
                f"{other.runtime.finalization_queue_depth}"
            )
        if (
            other.runtime.dispatch_writer_queue_depth
            != self.runtime.dispatch_writer_queue_depth
        ):
            runtime_changes.append(
                f"dispatch_writer_queue_depth: "
                f"{self.runtime.dispatch_writer_queue_depth} → "
                f"{other.runtime.dispatch_writer_queue_depth}"
            )

        # --- Health state ---
        all_accounts = set(self.runtime.health_account_states) | set(
            other.runtime.health_account_states
        )
        for acct in sorted(all_accounts):
            before = self.runtime.health_account_states.get(acct, "unknown")
            after = other.runtime.health_account_states.get(acct, "unknown")
            if before != after:
                health_changes.append(f"health[{acct}]: {before} → {after}")

        all_circuits = set(self.runtime.health_circuit_states) | set(
            other.runtime.health_circuit_states
        )
        for acct in sorted(all_circuits):
            before = self.runtime.health_circuit_states.get(acct, "unknown")
            after = other.runtime.health_circuit_states.get(acct, "unknown")
            if before != after:
                health_changes.append(f"circuit[{acct}]: {before} → {after}")

        # --- Durable backoffs (compared by row count) ---
        if other.durable.account_backoff_rows != self.durable.account_backoff_rows:
            backoff_changes.append(
                f"account_backoff_rows: "
                f"{self.durable.account_backoff_rows} → "
                f"{other.durable.account_backoff_rows}"
            )

        # --- Database connection ---
        if other.runtime.db_invalidated != self.runtime.db_invalidated:
            db_changes.append(
                f"db_invalidated: {self.runtime.db_invalidated} → "
                f"{other.runtime.db_invalidated}"
            )
        if other.runtime.db_connection_state != self.runtime.db_connection_state:
            db_changes.append(
                f"db_connection_state: {self.runtime.db_connection_state} → "
                f"{other.runtime.db_connection_state}"
            )

        return StateAuditDiff(
            request_history_changes=request_changes,
            runtime_ownership_changes=runtime_changes,
            health_changes=health_changes,
            durable_backoff_changes=backoff_changes,
            database_connection_changes=db_changes,
        )


# ---------------------------------------------------------------------------
# Internal capture helpers
# ---------------------------------------------------------------------------


async def _capture_durable(db: Any) -> DurableFacts:
    """Read durable state from SQLite. Returns zeroed facts on error."""
    request_rows = 0
    attempt_rows = 0
    reservation_rows = 0
    account_backoff_rows = 0
    account_event_rows = 0
    model_availability_rows = 0
    routing_decision_rows = 0
    finalization_retry_rows = 0

    with contextlib.suppress(Exception):
        async with db._connection_lock:
            if db._conn is None:
                return DurableFacts()
            rows = await db._conn.execute_fetchall("SELECT COUNT(*) FROM requests")
            if rows:
                request_rows = rows[0][0]
            rows = await db._conn.execute_fetchall(
                "SELECT COUNT(*) FROM request_attempts"
            )
            if rows:
                attempt_rows = rows[0][0]
            rows = await db._conn.execute_fetchall("SELECT COUNT(*) FROM reservations")
            if rows:
                reservation_rows = rows[0][0]

    with contextlib.suppress(Exception):
        async with db._connection_lock:
            if db._conn is None:
                return DurableFacts(
                    request_rows=request_rows,
                    attempt_rows=attempt_rows,
                    reservation_rows=reservation_rows,
                )
            rows = await db._conn.execute_fetchall(
                "SELECT COUNT(*) FROM account_backoffs"
            )
            if rows:
                account_backoff_rows = rows[0][0]

    with contextlib.suppress(Exception):
        async with db._connection_lock:
            if db._conn is None:
                return DurableFacts(
                    request_rows=request_rows,
                    attempt_rows=attempt_rows,
                    reservation_rows=reservation_rows,
                    account_backoff_rows=account_backoff_rows,
                )
            rows = await db._conn.execute_fetchall(
                "SELECT COUNT(*) FROM account_events"
            )
            if rows:
                account_event_rows = rows[0][0]

    with contextlib.suppress(Exception):
        async with db._connection_lock:
            if db._conn is None:
                return DurableFacts(
                    request_rows=request_rows,
                    attempt_rows=attempt_rows,
                    reservation_rows=reservation_rows,
                    account_backoff_rows=account_backoff_rows,
                    account_event_rows=account_event_rows,
                )
            try:
                rows = await db._conn.execute_fetchall(
                    "SELECT COUNT(*) FROM routing_decisions"
                )
                if rows:
                    routing_decision_rows = rows[0][0]
            except Exception:
                pass

    return DurableFacts(
        request_rows=request_rows,
        attempt_rows=attempt_rows,
        reservation_rows=reservation_rows,
        account_backoff_rows=account_backoff_rows,
        account_event_rows=account_event_rows,
        model_availability_rows=model_availability_rows,
        routing_decision_rows=routing_decision_rows,
        finalization_retry_rows=finalization_retry_rows,
    )


def _capture_runtime(coordinator: Any) -> RuntimeFacts:
    """Read in-memory runtime state from the coordinator. Never raises."""
    active_count = 0
    reservation_count = 0
    reservation_tokens = 0
    estimated_cost = 0.0
    health_states: dict[str, str] = {}
    circuit_states: dict[str, str] = {}
    cooldown_remaining: dict[str, float] = {}
    disabled_models: dict[str, bool] = {}
    finalization_depth = 0
    dispatch_depth = 0
    db_state = "connected"
    db_invalidated = False

    with contextlib.suppress(Exception):
        hm = getattr(coordinator, "health_manager", None)
        if hm is not None:
            accounts = getattr(hm, "_accounts", {})
            for name, acct_health in accounts.items():
                health_states[name] = getattr(acct_health, "health_state", "unknown")
                cb = getattr(acct_health, "circuit_breaker", None)
                if cb is not None:
                    circuit_states[name] = getattr(cb, "state", "unknown")

    with contextlib.suppress(Exception):
        qr = getattr(coordinator, "quota_estimator", None)
        if qr is not None:
            reservations = getattr(qr, "_reservations", {})
            reservation_count = len(reservations)
            for r in reservations.values():
                reservation_tokens += getattr(r, "tokens", 0)
                estimated_cost += getattr(r, "estimated_cost", 0.0)

    with contextlib.suppress(Exception):
        frq = getattr(coordinator, "finalization_retry_queue", None)
        if frq is not None:
            finalization_depth = len(getattr(frq, "_queue", []))

    with contextlib.suppress(Exception):
        dw = getattr(coordinator, "dispatch_writer", None)
        if dw is not None:
            q = getattr(dw, "_queue", None)
            if q is not None:
                dispatch_depth = q.qsize()

    with contextlib.suppress(Exception):
        db = getattr(coordinator, "db", None)
        if db is not None:
            db_invalidated = getattr(db, "_invalidated", False)
            if db_invalidated:
                db_state = "invalidated"
            elif getattr(db, "_conn", None) is None:
                db_state = "disconnected"

    return RuntimeFacts(
        active_request_count=active_count,
        reservation_count=reservation_count,
        reservation_tokens=reservation_tokens,
        estimated_cost=estimated_cost,
        health_account_states=health_states,
        health_circuit_states=circuit_states,
        health_cooldown_remaining=cooldown_remaining,
        disabled_models=disabled_models,
        finalization_queue_depth=finalization_depth,
        dispatch_writer_queue_depth=dispatch_depth,
        db_connection_state=db_state,
        db_invalidated=db_invalidated,
    )
