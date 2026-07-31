"""Database recovery controller (Plan 027).

The :class:`DatabaseRecoveryController` is the process-owned owner of
all database recovery attempts.  It is created once per process and
bound to the primary :class:`Database` instance; reloads do not
recreate it (it is not generation-owned).

Responsibilities:

1. Receive invalidation notifications from ``Database``.
2. Stop admission of new correctness-critical writes.
3. Mark readiness false for the duration of recovery.
4. Detach and close the suspect write connection with a bounded
   timeout.
5. Open a fresh write connection using the same validated
   configuration.
6. Reapply pragmas and verify schema/migration compatibility.
7. Run read and rollback-only writable probes.
8. Reconcile ambiguous operations.
9. Restore write admission and readiness.

The controller is single-flight: concurrent requests observing
invalidation all join the same recovery attempt.  When the recovery
attempt fails, the controller retries with bounded backoff.  When
retries are exhausted, the database enters ``failed_closed`` state
and readiness reports degraded.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.db.connection import (
    AmbiguousDatabaseOperation,
    DatabaseLifecycleState,
)
from eggpool.request.terminal_status import (
    REQUEST_PENDING_STATUSES,
    REQUEST_TERMINAL_STATUSES,
    RESERVATION_TERMINAL_STATUSES,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.health.writable_probe import DatabaseWritableProbe
    from eggpool.models.config import DatabaseRecoveryConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryAttemptResult:
    """Immutable result of a single recovery attempt.

    Fields:

    - ``state``: the resulting lifecycle state (``READY`` on success,
      ``FAILED_CLOSED`` on exhaustion, ``RECOVERING`` if a retry is
      pending).
    - ``attempt_number``: 1-indexed; reaches ``max_attempts`` on
      exhaustion.
    - ``started_at_monotonic`` / ``completed_at_monotonic``: timing
      facts for ``recovery_metrics``.
    - ``error_class`` / ``error_message``: the failing exception's
      class name and repr-sanitized message; ``None`` on success.
    - ``replacement_epoch``: the epoch value after the replacement
      connection was opened.  ``None`` if no replacement was
      attempted.
    """

    state: DatabaseLifecycleState
    attempt_number: int
    started_at_monotonic: float
    completed_at_monotonic: float
    error_class: str | None = None
    error_message: str | None = None
    replacement_epoch: int | None = None
    ambiguous_resolved: int = 0
    ambiguous_failed: int = 0


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """Outcome of reconciling a single ambiguous operation."""

    operation_id: str
    strategy: str
    outcome: str  # committed/absent or an unresolved_* category
    duration_ms: float
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Immutable snapshot of the recovery controller's state."""

    lifecycle_state: DatabaseLifecycleState
    total_invalidation_count: int
    invalidation_reasons_by_class: tuple[tuple[str, int], ...]
    recovery_attempts: int
    successful_recoveries: int
    failed_recoveries: int
    last_attempt: RecoveryAttemptResult | None
    active_waiters: int
    pending_ambiguous_operations: int
    active_recovery: bool
    last_completed_at_monotonic: float | None
    time_to_recover_s: float | None
    failed_closed_reason: str | None
    admission_admitted: bool


@dataclass
class DatabaseRecoveryController:
    """Single-flight owner of database recovery attempts.

    The controller is bound to a single :class:`Database` instance
    and survives the request lifecycle.  Reloads do not recreate it
    because the underlying database connection is process-owned.

    Fields:

    - ``db``: the primary write connection.
    - ``config``: recovery controls (max_attempts, backoff, etc.).
    - ``readiness_probe``: optional process-owned probe that is
      notified when readiness should change.
    - ``on_recovery_complete``: optional callback invoked when a
      recovery cycle finishes (success or failure).  Used by the
      ``/readyz`` handler and dashboard to refresh state.
    """

    db: Database
    config: DatabaseRecoveryConfig
    readiness_probe: DatabaseWritableProbe | None = None
    on_recovery_complete: Any = None  # noqa: ANN401

    _state: DatabaseLifecycleState = field(
        init=False, default=DatabaseLifecycleState.DISCONNECTED
    )
    _recovery_lock: threading.Lock = field(init=False, default_factory=threading.Lock)
    _waiter_count: int = field(init=False, default=0)
    _waiters_event: asyncio.Event = field(init=False, default_factory=asyncio.Event)
    _total_invalidation_count: int = field(init=False, default=0)
    _recovery_attempts: int = field(init=False, default=0)
    _successful_recoveries: int = field(init=False, default=0)
    _failed_recoveries: int = field(init=False, default=0)
    _last_attempt: RecoveryAttemptResult | None = field(init=False, default=None)
    _last_completed_at_monotonic: float | None = field(init=False, default=None)
    _last_time_to_recover_s: float | None = field(init=False, default=None)
    _failed_closed_reason: str | None = field(init=False, default=None)
    _active_recovery_task: asyncio.Task[RecoveryAttemptResult] | None = field(
        init=False, default=None
    )
    _invalidation_reasons_by_class: collections.Counter[str] = field(
        init=False, default_factory=lambda: collections.Counter()
    )
    _admission_admitted: bool = field(init=False, default=False)
    _shutdown_in_progress: bool = field(init=False, default=False)
    _reconciler_registry: dict[str, Any] = field(init=False, default_factory=lambda: {})

    def __post_init__(self) -> None:
        # Bind the controller to the database so the database can
        # notify us on invalidation.  This is a one-way binding;
        # the database does not retain a strong reference to the
        # controller (the runtime manager owns the controller).
        self.db.attach_recovery_controller(self)
        # Set up the initial state from the database's current state.
        if self.db._conn is not None:  # type: ignore[reportPrivateUsage]
            self._state = DatabaseLifecycleState.READY
            self._admission_admitted = True
        else:
            self._state = DatabaseLifecycleState.DISCONNECTED
            self._admission_admitted = False
        # Register built-in reconcilers.  The dispatch reconciler
        # requires the production dispatch_repository to be
        # importable; the finalization reconciler is implemented as
        # a pure state-predicates check.
        self._register_builtin_reconcilers()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> DatabaseLifecycleState:
        return self._state

    @property
    def admission_admitted(self) -> bool:
        """Whether new correctness-critical writes are admitted."""
        return self._admission_admitted

    def snapshot(self) -> RecoverySnapshot:
        """Return an immutable snapshot of the controller's state."""
        reasons = sorted(self._invalidation_reasons_by_class.items())
        with self._recovery_lock:
            return RecoverySnapshot(
                lifecycle_state=self._state,
                total_invalidation_count=self._total_invalidation_count,
                invalidation_reasons_by_class=tuple(reasons),
                recovery_attempts=self._recovery_attempts,
                successful_recoveries=self._successful_recoveries,
                failed_recoveries=self._failed_recoveries,
                last_attempt=self._last_attempt,
                active_waiters=self._waiter_count,
                pending_ambiguous_operations=len(
                    self.db.pending_ambiguous_operations()
                ),
                active_recovery=self._active_recovery_task is not None
                and not self._active_recovery_task.done(),
                last_completed_at_monotonic=self._last_completed_at_monotonic,
                time_to_recover_s=self._last_time_to_recover_s,
                failed_closed_reason=self._failed_closed_reason,
                admission_admitted=self._admission_admitted,
            )

    async def wait_for_ready(self, timeout_s: float = 30.0) -> bool:
        """Wait until the database is ready or the timeout elapses.

        Returns True on success, False on timeout.  Multiple concurrent
        callers join the same wait; the controller's single-flight
        recovery is shared by all of them.
        """
        current = self._state
        if current is DatabaseLifecycleState.READY:
            return True
        if current is DatabaseLifecycleState.FAILED_CLOSED:
            return False
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    current = self._state
                    if current is DatabaseLifecycleState.READY:
                        return True
                    if current is DatabaseLifecycleState.FAILED_CLOSED:
                        return False
                    if self._shutdown_in_progress:
                        return False
                    self._waiter_count += 1
                    try:
                        await self._waiters_event.wait()
                    finally:
                        self._waiter_count = max(0, self._waiter_count - 1)
                    self._waiters_event.clear()
        except TimeoutError:
            return False

    async def handle_invalidation(self, reason: str, reason_class: str) -> None:
        """React to an invalidation notification from the database.

        Updates internal counters, transitions to the
        ``INVALIDATED`` state, and starts a single-flight recovery
        attempt if one is not already in progress.

        This is the entry point used by
        :meth:`Database._invalidate_connection`.  Concurrent
        invocations all funnel through the same recovery task.
        """
        if self._shutdown_in_progress:
            return
        self._total_invalidation_count += 1
        self._invalidation_reasons_by_class[reason_class] += 1
        self._state = DatabaseLifecycleState.INVALIDATED
        self._admission_admitted = False
        # Spawn a recovery attempt if one is not already active.
        existing = self._active_recovery_task
        if existing is None or existing.done():
            self._active_recovery_task = asyncio.create_task(
                self._recover_with_retries(reason=reason),
                name="eggpool:database_recovery",
            )
        # Wake any waiters so they can check the new state.
        self._waiters_event.set()

    async def recover_blocking(self, timeout_s: float = 30.0) -> bool:
        """Block until recovery completes (or fails).

        Used by callers that need to wait for the replacement
        connection before issuing a new request.  Returns True on
        success, False on failure.
        """
        existing = self._active_recovery_task
        if existing is not None and not existing.done():
            try:
                await asyncio.wait_for(asyncio.shield(existing), timeout=timeout_s)
            except TimeoutError:
                return False
            except Exception:
                return False
        return self._state is DatabaseLifecycleState.READY

    async def shutdown(self) -> None:
        """Stop the recovery controller and cancel any active attempt.

        Idempotent.  Sets the shutdown flag so concurrent invocations
        do not start new attempts.  Any active recovery attempt is
        cancelled (with bounded timeout) so the database does not
        leave the controller in a wedged state.
        """
        self._shutdown_in_progress = True
        self._admission_admitted = False
        existing = self._active_recovery_task
        if existing is not None and not existing.done():
            existing.cancel()
            try:
                async with asyncio.timeout(5.0):
                    await existing
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        self._waiters_event.set()

    # ------------------------------------------------------------------
    # Internal — recovery loop
    # ------------------------------------------------------------------

    def _register_builtin_reconcilers(self) -> None:
        """Register the production reconcilers.

        The dispatch reconciler is delegated to the existing
        ``dispatch_repository.reconcile_ambiguous_commit`` helper.
        The finalization reconciler is a state-predicates check that
        respects Plan 026 invariants.
        """
        self._reconciler_registry["dispatch"] = _reconcile_dispatch
        self._reconciler_registry["request_finalization"] = (
            _reconcile_request_finalization
        )
        self._reconciler_registry["attempt_finalization"] = (
            _reconcile_attempt_finalization
        )
        # Only supports descriptors buffered by pre-061 code in the current
        # process.  Production writers use the explicit strategies above.
        self._reconciler_registry["finalization"] = _reconcile_legacy_finalization

    def _backoff_for_attempt(self, attempt: int) -> float:
        """Return the backoff (seconds) for the given attempt number."""
        if attempt <= 0:
            return 0.0
        backoff_ms = min(
            self.config.max_backoff_ms,
            self.config.initial_backoff_ms * (2 ** (attempt - 1)),
        )
        return backoff_ms / 1000.0

    async def _recover_with_retries(self, *, reason: str) -> RecoveryAttemptResult:
        """Bounded retry loop over ``_attempt_recovery``."""
        max_attempts = self.config.max_attempts
        last_result: RecoveryAttemptResult | None = None
        for attempt in range(1, max_attempts + 1):
            self._state = DatabaseLifecycleState.RECOVERING
            self._recovery_attempts += 1
            last_result = await self._attempt_recovery(attempt=attempt, reason=reason)
            self._last_attempt = last_result
            self._last_completed_at_monotonic = last_result.completed_at_monotonic
            if last_result.state is DatabaseLifecycleState.READY:
                self._successful_recoveries += 1
                self._state = DatabaseLifecycleState.READY
                self._admission_admitted = True
                self.db._writes_admitted = True  # type: ignore[reportPrivateUsage]
                self.db._reads_admitted = True  # type: ignore[reportPrivateUsage]
                self.db._writes_admitted_event.set()  # type: ignore[reportPrivateUsage]
                self.db._generation_replaced_at = time.monotonic()  # type: ignore[reportPrivateUsage]
                self.db._recovery_count += 1  # type: ignore[reportPrivateUsage]
                self._fire_recovery_complete(success=True)
                self._waiters_event.set()
                return last_result
            self._failed_recoveries += 1
            if attempt < max_attempts:
                backoff = self._backoff_for_attempt(attempt)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break
        # Exhausted: enter failed_closed state.
        assert last_result is not None
        self._failed_closed_reason = last_result.error_message or "exhausted"
        self._state = DatabaseLifecycleState.FAILED_CLOSED
        self._admission_admitted = False
        self._fire_recovery_complete(success=False)
        self._waiters_event.set()
        return last_result

    async def _attempt_recovery(
        self, *, attempt: int, reason: str
    ) -> RecoveryAttemptResult:
        """Run a single recovery attempt.

        Steps:

        1. Mark invalidation state on the database.
        2. Close the suspect connection (already done by the
           invalidation path).
        3. Open a replacement connection.
        4. Verify schema/migration compatibility (skip for in-memory
           databases which lose all state on reconnect).
        5. Run a writable probe.
        6. Reconcile ambiguous operations.
        """
        started_at = time.monotonic()
        is_memory_db = self.db._path == ":memory:"  # type: ignore[reportPrivateUsage]
        try:
            # 1-2. The connection is already detached and closed.
            # Confirm the database has no live connection.
            if self.db._conn is not None:  # type: ignore[reportPrivateUsage]
                # Defensive: close any stray connection.
                with contextlib.suppress(Exception):
                    await self.db.disconnect()

            # 3. Open a replacement connection.
            await self.db.connect(admit=False)
            replacement_epoch = self.db.connection_epoch

            # 4. For in-memory databases, re-run migrations since the
            # new connection starts with an empty schema.  For
            # file-backed databases, verify schema compatibility.
            if is_memory_db:
                from eggpool.db.migrations import MigrationRunner  # noqa: PLC0415

                await MigrationRunner(self.db).run(internal_recovery=True)
            else:
                await self._verify_schema_compatibility()

            # 5. Run a writable probe.
            writable = await self._probe_writable()
            if not writable:
                raise RuntimeError("Writable probe failed on replacement connection")

            # 6. Reconcile ambiguous operations.
            ambiguous_ops = self.db.pending_ambiguous_operations()
            resolved, failed = await self._reconcile_ambiguous_operations(ambiguous_ops)
            if failed:
                raise RuntimeError(
                    f"{failed} ambiguous database operation(s) unresolved"
                )

            completed_at = time.monotonic()
            return RecoveryAttemptResult(
                state=DatabaseLifecycleState.READY,
                attempt_number=attempt,
                started_at_monotonic=started_at,
                completed_at_monotonic=completed_at,
                error_class=None,
                error_message=None,
                replacement_epoch=replacement_epoch,
                ambiguous_resolved=resolved,
                ambiguous_failed=failed,
            )
        except asyncio.CancelledError:
            await self.db._close_failed_connection()  # type: ignore[reportPrivateUsage]
            self.db._writes_admitted = False  # type: ignore[reportPrivateUsage]
            self.db._reads_admitted = False  # type: ignore[reportPrivateUsage]
            self.db._writes_admitted_event.clear()  # type: ignore[reportPrivateUsage]
            raise
        except Exception as exc:
            await self.db._close_failed_connection()  # type: ignore[reportPrivateUsage]
            self.db._writes_admitted = False  # type: ignore[reportPrivateUsage]
            self.db._reads_admitted = False  # type: ignore[reportPrivateUsage]
            self.db._writes_admitted_event.clear()  # type: ignore[reportPrivateUsage]
            self.db._transition_state(DatabaseLifecycleState.FAILED_CLOSED)  # type: ignore[reportPrivateUsage]
            completed_at = time.monotonic()
            return RecoveryAttemptResult(
                state=DatabaseLifecycleState.FAILED_CLOSED,
                attempt_number=attempt,
                started_at_monotonic=started_at,
                completed_at_monotonic=completed_at,
                error_class=type(exc).__qualname__,
                error_message=repr(exc),
                replacement_epoch=None,
                ambiguous_resolved=0,
                ambiguous_failed=0,
            )

    async def _verify_schema_compatibility(self) -> None:
        """Verify the replacement connection is on the same schema.

        The check is intentionally lightweight: we read the
        ``_migrations`` table and confirm that the highest version
        matches the expected schema.  Failing this check is
        catastrophic -- the database file may have been corrupted
        or replaced out-of-band.
        """
        try:
            async with self.db.transaction(_internal_recovery=True):
                rows = await self.db.fetch_all(
                    "SELECT MAX(version) AS max_version FROM _migrations"
                )
        except Exception as exc:
            raise RuntimeError(
                f"Cannot verify schema on replacement connection: {exc}"
            ) from exc
        if not rows:
            return  # No migrations table -- treat as fresh schema
        max_version = rows[0]["max_version"]
        if max_version is None:
            return  # No migrations recorded
        # Best-effort: the migrations runner applied these versions
        # on the suspect connection.  If the replacement connection
        # sees a different max version, the database file changed
        # under us.
        from eggpool.db.migrations import EXPECTED_SCHEMA_VERSION  # noqa: PLC0415

        if int(max_version) < EXPECTED_SCHEMA_VERSION:
            raise RuntimeError(
                f"Replacement connection has schema version "
                f"{int(max_version)}; expected >= {EXPECTED_SCHEMA_VERSION}"
            )

    async def _probe_writable(self) -> bool:
        """Run a writable probe on the replacement connection."""
        try:
            return await self.db.probe_writable()
        except Exception:
            return False

    async def _reconcile_ambiguous_operations(
        self,
        ops: tuple[AmbiguousDatabaseOperation, ...],
    ) -> tuple[int, int]:
        """Reconcile ambiguous operations against the replacement connection.

        Operations are acknowledged individually only after a
        definitive ``committed`` or explicitly valid ``absent`` result.
        Every other result remains in the ambiguity buffer and blocks
        readiness.
        """
        if not ops:
            return 0, 0
        self._state = DatabaseLifecycleState.RECONCILING
        resolved = 0
        failed = 0
        for op in ops:
            try:
                async with asyncio.timeout(self.config.reconciliation_timeout_s):
                    outcome = await self._dispatch_reconciler(op)
                if outcome.outcome in ("committed", "absent"):
                    resolved += 1
                    self.db.acknowledge_ambiguous_operation(op)
                else:
                    failed += 1
            except Exception:
                failed += 1
        return resolved, failed

    async def _dispatch_reconciler(
        self, op: AmbiguousDatabaseOperation
    ) -> ReconciliationOutcome:
        """Dispatch an ambiguous operation to the appropriate reconciler."""
        started = time.monotonic()
        reconciler = self._reconciler_registry.get(op.reconciliation_strategy)
        if reconciler is None:
            return ReconciliationOutcome(
                operation_id=op.operation_id,
                strategy=op.reconciliation_strategy,
                outcome="unresolved_unknown_strategy",
                duration_ms=(time.monotonic() - started) * 1000,
                error_class="UnknownStrategy",
            )
        try:
            async with self.db.transaction(_internal_recovery=True):
                result = await reconciler(self.db, op)
            return ReconciliationOutcome(
                operation_id=op.operation_id,
                strategy=op.reconciliation_strategy,
                outcome=result,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:
            return ReconciliationOutcome(
                operation_id=op.operation_id,
                strategy=op.reconciliation_strategy,
                outcome="unresolved_error",
                duration_ms=(time.monotonic() - started) * 1000,
                error_class=type(exc).__qualname__,
            )

    def _fire_recovery_complete(self, *, success: bool) -> None:
        """Notify the readiness probe and any completion callback."""
        if self.readiness_probe is not None:
            # Force an immediate probe so readiness reflects the
            # new connection.  Failures are swallowed because the
            # recovery cycle itself has already been decided.
            with contextlib.suppress(Exception):
                self.readiness_probe.force_probe_nowait()
        if self.on_recovery_complete is not None:
            try:
                cb_result = self.on_recovery_complete(
                    success=success, snapshot=self.snapshot()
                )
                if asyncio.iscoroutine(cb_result):
                    asyncio.create_task(cb_result)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Built-in reconcilers
# ---------------------------------------------------------------------------


async def _reconcile_dispatch(db: Database, op: AmbiguousDatabaseOperation) -> str:
    """Reconcile a dispatch selection against the durable state.

    Delegates to :func:`eggpool.db.dispatch_repository.reconcile_ambiguous_commit`
    when the import is available.  The reconciler returns one of
    ``"committed"``, ``"absent"``, ``"conflicting"``.
    """
    from eggpool.db.dispatch_repository import (  # noqa: PLC0415
        DispatchAmbiguousCommitError,
        reconcile_ambiguous_commit,
    )

    proxy_request_id = op.operation_id
    attempt_number = 0
    for key, value in op.idempotency_keys:
        if key == "attempt_number":
            attempt_number = int(value)
    try:
        await reconcile_ambiguous_commit(
            db,
            proxy_request_id=proxy_request_id,
            attempt_number=attempt_number,
        )
        return "committed"
    except DispatchAmbiguousCommitError:
        # Check if the operation should be retried or rolled back.
        return "absent"
    except Exception:
        return "unresolved_conflict"


def _operation_value(op: AmbiguousDatabaseOperation, name: str) -> str | None:
    """Read one explicit identity from an ambiguous-operation descriptor."""

    for key, value in op.idempotency_keys:
        if key == name:
            return str(value)
    return None


async def _reconcile_request_finalization(
    db: Database, op: AmbiguousDatabaseOperation
) -> str:
    """Reconcile a request finalization using its complete identity tuple."""

    request_id = _operation_value(op, "request_id") or op.operation_id
    attempt_id = _operation_value(op, "attempt_id")
    reservation_id = _operation_value(op, "reservation_id")
    try:
        request = await db.fetch_one(
            "SELECT id, status FROM requests WHERE id = ?",
            (request_id,),
        )
        if request is None:
            return "absent"
        status = request["status"]
        if status not in REQUEST_TERMINAL_STATUSES:
            if status in REQUEST_PENDING_STATUSES:
                return "absent"
            return "unresolved_conflict"

        if attempt_id is not None:
            attempt = await db.fetch_one(
                "SELECT id, request_id, completed_at "
                "FROM request_attempts WHERE id = ?",
                (attempt_id,),
            )
            if attempt is None or str(attempt["request_id"]) != str(request["id"]):
                return "unresolved_conflict"
            if attempt["completed_at"] is None:
                return "unresolved_conflict"
        if reservation_id is not None:
            reservation = await db.fetch_one(
                "SELECT request_id, status FROM reservations WHERE id = ?",
                (reservation_id,),
            )
            if (
                reservation is None
                or str(reservation["request_id"]) != str(request["id"])
                or reservation["status"] not in RESERVATION_TERMINAL_STATUSES
            ):
                return "unresolved_conflict"
    except Exception:
        return "unresolved_conflict"
    return "committed"


async def _reconcile_attempt_finalization(
    db: Database, op: AmbiguousDatabaseOperation
) -> str:
    """Reconcile an attempt and its reservation by explicit durable IDs."""

    attempt_id = _operation_value(op, "attempt_id") or op.operation_id
    request_id = _operation_value(op, "request_id")
    reservation_id = _operation_value(op, "reservation_id")
    if request_id is None or reservation_id is None:
        return "unresolved_conflict"
    try:
        attempt = await db.fetch_one(
            "SELECT id, request_id, completed_at FROM request_attempts WHERE id = ?",
            (attempt_id,),
        )
        if attempt is None:
            return "absent"
        if str(attempt["request_id"]) != request_id:
            return "unresolved_conflict"
        request = await db.fetch_one(
            "SELECT id, status FROM requests WHERE id = ?",
            (request_id,),
        )
        reservation = await db.fetch_one(
            "SELECT request_id, status FROM reservations WHERE id = ?",
            (reservation_id,),
        )
        if request is None or reservation is None:
            return "unresolved_conflict"
        if str(reservation["request_id"]) != request_id:
            return "unresolved_conflict"
        if attempt["completed_at"] is None:
            return "unresolved_conflict"
        if reservation["status"] not in RESERVATION_TERMINAL_STATUSES:
            return "unresolved_conflict"
        if (
            request["status"]
            not in REQUEST_TERMINAL_STATUSES | REQUEST_PENDING_STATUSES
        ):
            return "unresolved_conflict"
    except Exception:
        return "unresolved_conflict"
    return "committed"


async def _reconcile_legacy_finalization(
    db: Database, op: AmbiguousDatabaseOperation
) -> str:
    """Bounded compatibility for already-buffered pre-061 descriptors."""

    if op.operation_kind == "attempt_finalization":
        return await _reconcile_attempt_finalization(db, op)
    return await _reconcile_request_finalization(db, op)


# Kept as a private compatibility name for existing diagnostics/tests.
_reconcile_finalization = _reconcile_legacy_finalization
