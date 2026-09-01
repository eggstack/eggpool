"""SQLite connection manager using aiosqlite."""

from __future__ import annotations

import asyncio
import collections
import enum
import re
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

from eggpool.errors import (
    DatabaseCommitError,
    DatabaseConnectionInvalidatedError,
    DatabaseError,
    DatabaseRollbackError,
    DatabaseTransactionOwnershipError,
)


class DatabaseLifecycleState(enum.Enum):
    """Explicit lifecycle state for a :class:`Database` instance.

    The state machine is the single authoritative source for whether
    database operations remain admitted. ``FAILED_CLOSED`` is terminal for
    the worker; a supervisor restart creates a fresh :class:`Database`.
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    FAILED_CLOSED = "failed_closed"
    SHUTTING_DOWN = "shutting_down"


class _RollbackProbeError(Exception):
    """Sentinel exception for probe_writable to roll back without logging."""


def _classify_op_kind(sql: str) -> str:
    """Classify a SQL statement into a coarse operator-kind bucket.

    The result feeds the in-memory ``operations_by_kind`` counter so
    operators can see whether the database is dominated by SELECTs,
    INSERTs, UPDATEs, DELETEs, or admin pragmas / schema migrations.
    Only the leading keyword is inspected.
    """
    stripped = sql.lstrip()
    while True:
        if stripped.startswith("--"):
            newline = stripped.find("\n")
            stripped = stripped[newline + 1 :] if newline >= 0 else ""
        elif stripped.startswith("/*"):
            comment_end = stripped.find("*/", 2)
            stripped = stripped[comment_end + 2 :] if comment_end >= 0 else ""
        else:
            break
        stripped = stripped.lstrip()
    upper = _mask_sql_literals(stripped.upper())
    if upper.startswith("WITH"):
        # Find the first statement keyword outside CTE parentheses. This
        # keeps nested SELECTs from determining the operation kind.
        depth = 0
        for match in re.finditer(
            r"\(|\)|\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|PRAGMA)\b",
            upper,
        ):
            token = match.group(0)
            if token == "(":
                depth += 1
            elif token == ")":
                depth = max(0, depth - 1)
            elif depth == 0:
                upper = upper[match.start() :]
                break
    if upper.startswith("SELECT"):
        return "select"
    if upper.startswith("INSERT"):
        return "insert"
    if upper.startswith("UPDATE"):
        return "update"
    if upper.startswith("DELETE"):
        return "delete"
    if upper.startswith("REPLACE"):
        return "replace"
    if upper.startswith(("BEGIN", "COMMIT", "ROLLBACK")):
        return "transaction"
    if upper.startswith("PRAGMA"):
        return "pragma"
    return "other"


def _mask_sql_literals(sql: str) -> str:
    """Replace quoted SQL strings and identifiers with spaces."""
    chars = list(sql)
    quote: str | None = None
    index = 0
    while index < len(chars):
        char = chars[index]
        if quote is None:
            if char in {"'", '"'}:
                quote = char
                chars[index] = " "
        elif char == quote:
            chars[index] = " "
            if index + 1 < len(chars) and chars[index + 1] == quote:
                chars[index + 1] = " "
                index += 1
            else:
                quote = None
        else:
            chars[index] = " "
        index += 1
    return "".join(chars)


def _classify_error_kind(exc: BaseException) -> str:
    """Classify a database exception into a coarse operator-kind bucket.

    The result feeds the in-memory ``last_operation_error_kind`` counter
    so operators can see whether the most recent failure was a lock
    conflict, schema error, integrity violation, or other class.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return "busy"
    if primary_code in {
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_NOMEM,
    }:
        return "disk"
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return "corruption"
    if primary_code in {sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_ABORT}:
        return "interrupted"

    message = str(exc).lower()[:200]
    if "database is locked" in message or "database table is locked" in message:
        return "busy"
    if any(token in message for token in ("disk i/o error", "disk full", "readonly")):
        return "disk"
    if any(
        token in message
        for token in ("database disk image is malformed", "not a database")
    ):
        return "corruption"
    if "interrupted" in message or "cancelled" in message:
        return "interrupted"

    cls_name = type(exc).__qualname__.lower()
    if "integrity" in cls_name:
        return "integrity"
    if "operational" in cls_name:
        return "operational"
    if "syntax" in cls_name or "schema" in cls_name:
        return "schema"
    return "other"


def _is_fatal_database_error(exc: BaseException) -> bool:
    """Return whether a database failure makes correctness unprovable."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _classify_error_kind(current) in {"corruption", "disk"}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _format_pragma_integer(value: object, name: str) -> str:
    """Validate an integer PRAGMA value before SQL interpolation."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatabaseError(f"{name} must be an integer")
    return str(value)


def _format_pragma_synchronous(value: object) -> str:
    """Validate the configured SQLite synchronous mode."""
    allowed = {"OFF", "NORMAL", "FULL", "EXTRA"}
    if not isinstance(value, str) or value not in allowed:
        raise DatabaseError("synchronous must be OFF, NORMAL, FULL, or EXTRA")
    return value


class Database:
    """Async wrapper around aiosqlite with pragma configuration.

    All SQL operations are serialized through a single connection lock.
    A transaction is owned by the one asyncio task that issued ``BEGIN``.
    ContextVar inheritance is deliberately not treated as transaction
    permission: a child task created inside a transaction fails before SQL
    execution instead of piggybacking on the parent's commit boundary.
    """

    def __init__(
        self,
        path: str,
        busy_timeout_ms: int = 5000,
        wal: bool = True,
        synchronous: str = "NORMAL",
        read_only: bool = False,
        journal_size_limit: int | None = None,
    ) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._wal = wal
        self._synchronous = synchronous
        self._read_only = read_only
        self._journal_size_limit = journal_size_limit
        self._conn: aiosqlite.Connection | None = None
        self._invalidation_close_task: asyncio.Task[None] | None = None
        self._connection_lock = asyncio.Lock()
        self._canonical_loop: asyncio.AbstractEventLoop | None = None
        # Contention counters (in-memory only, never persisted)
        self._write_ops: int = 0
        self._read_ops: int = 0
        self._total_transactions: int = 0
        self._total_nested_transactions: int = 0
        self._last_operation_error_class: str | None = None
        self._last_operation_error_kind: str | None = None
        self._operations_by_kind: dict[str, int] = {}
        self._cumulative_lock_wait_s: float = 0.0
        self._max_lock_wait_s: float = 0.0
        self._lock_wait_count: int = 0
        self._lock_wait_samples_s: collections.deque[float] = collections.deque(
            maxlen=512
        )
        # Compatibility marker for same-task nesting. The explicit owner
        # task below is authoritative; inherited child contexts are rejected.
        self._in_transaction_context: ContextVar[bool] = ContextVar(
            "database_in_transaction_context",
            default=False,
        )
        # Tracks which asyncio.Task issued ``BEGIN IMMEDIATE`` for
        # the active outermost transaction on this connection.
        # Used by ``vacuum()`` to refuse to run when the *current*
        # task is the lock holder (which would deadlock). Nested
        # detection in ``transaction()`` itself uses the per-task
        # ``_in_transaction_context`` ContextVar, NOT this attribute.
        self._transaction_owner: ContextVar[asyncio.Task[object] | None] = ContextVar(
            "database_transaction_owner",
            default=None,
        )
        self._fatal_handler: Any = None  # noqa: ANN401
        # Fail-closed diagnostics. These facts are retained after the
        # connection is detached so operators can distinguish a terminal
        # database failure from an orderly shutdown.
        self._invalidated_reason: str | None = None
        self._invalidated_at: float | None = None
        self._fatal_notified = False
        self._last_commit_outcome: str | None = None
        self._last_rollback_attempted: bool = False
        self._last_rollback_succeeded: bool = False
        self._last_in_transaction_before_rollback: bool | None = None
        self._last_in_transaction_after_rollback: bool | None = None

        # -- Process lifecycle state -----------------------------------------
        self._lifecycle_state: DatabaseLifecycleState = (
            DatabaseLifecycleState.DISCONNECTED
        )
        self._invalidated_reason_class: str | None = None
        # Cached facts make the readiness path a pure read and let background
        # writers drop work immediately after the fatal transition.
        self._writes_admitted: bool = False
        self._reads_admitted: bool = False
        self._rollback_failure_count: int = 0
        # Counter incremented on every successful rollback.  Bounded
        # by the lifetime of the connection.
        self._rollback_success_count: int = 0

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def lifecycle_state(self) -> DatabaseLifecycleState:
        """Return the current lifecycle state."""
        return self._lifecycle_state

    @property
    def writes_admitted(self) -> bool:
        """Return whether new correctness-critical writes are admitted."""
        return self._writes_admitted

    @property
    def reads_admitted(self) -> bool:
        """Return whether read-only stats queries remain safe."""
        return self._reads_admitted

    @property
    def rollback_failure_count(self) -> int:
        """Return the number of rollback failures that caused invalidation."""
        return self._rollback_failure_count

    @property
    def rollback_success_count(self) -> int:
        """Return the number of successful rollbacks."""
        return self._rollback_success_count

    def set_fatal_handler(self, handler: Any) -> None:  # noqa: ANN401
        """Install the process-owned callback for an indeterminate DB state."""
        self._fatal_handler = handler

    def _transition_state(self, new_state: DatabaseLifecycleState) -> None:
        """Transition the lifecycle state, validating the move.

        The transition is purely diagnostic: the caller's invariants
        (locks, writes-admitted flag) must be set independently.  The
        method is the single source of truth for the state field; all
        state changes must go through it.
        """
        # The transitions are loosely ordered; the only invariants
        # enforced here are:
        # - SHUTTING_DOWN is terminal-ish (no return to READY).
        # - FAILED_CLOSED is terminal for this instance.
        # - DISCONNECTED is the bottom state.
        self._lifecycle_state = new_state

    def _classify_invalidation_reason(self, reason: str | None) -> str:
        """Return a coarse category for the invalidation reason.

        Operators do not need the full repr; only the kind so
        invalidation-reason diagnostics can be filtered. Unknown kinds
        collapse to ``"other"``.
        """
        if not reason:
            return "unspecified"
        lower = reason.lower()
        if "rollback" in lower:
            return "rollback_failure"
        if "commit" in lower:
            return "commit_failure"
        if "lock" in lower:
            return "lock_failure"
        if "integrity" in lower:
            return "integrity_violation"
        if "operational" in lower:
            return "operational_error"
        return "other"

    async def connect(self) -> None:
        """Open the connection and set pragmas on the canonical event loop."""
        if self._conn is not None:
            raise DatabaseError("Database already connected")
        self._ensure_canonical_loop()
        if self._lifecycle_state in {
            DatabaseLifecycleState.FAILED_CLOSED,
            DatabaseLifecycleState.SHUTTING_DOWN,
        }:
            raise DatabaseConnectionInvalidatedError(
                "Database lifecycle is terminal; create a fresh connection"
            )
        if self._connection_lock.locked():
            raise DatabaseError("Database connection lock is still held")
        busy_timeout_ms = _format_pragma_integer(
            self._busy_timeout_ms, "busy_timeout_ms"
        )
        synchronous = _format_pragma_synchronous(self._synchronous)
        journal_size_limit = (
            _format_pragma_integer(self._journal_size_limit, "journal_size_limit")
            if self._journal_size_limit is not None
            else None
        )
        self._transition_state(DatabaseLifecycleState.CONNECTING)
        self._invalidated_reason = None
        self._invalidated_reason_class = None
        self._invalidated_at = None
        try:
            if self._read_only:
                # Use a read-only URI so SQLite refuses to change
                # journal mode, create WAL files, or apply migrations.
                uri, use_uri = self._build_read_only_uri(self._path)
                self._conn = await aiosqlite.connect(uri, uri=use_uri)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
                self._writes_admitted = False
                self._reads_admitted = True
                self._transition_state(DatabaseLifecycleState.READY)
                return
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            if self._wal:
                await self._conn.execute("PRAGMA journal_mode = WAL")
            await self._conn.execute(f"PRAGMA synchronous = {synchronous}")
            if journal_size_limit is not None:
                await self._conn.execute(
                    f"PRAGMA journal_size_limit = {journal_size_limit}"
                )
            await self._conn.commit()
            self._writes_admitted = True
            self._reads_admitted = True
            self._transition_state(DatabaseLifecycleState.READY)
        except asyncio.CancelledError:
            await self._close_failed_connection()
            self._transition_state(DatabaseLifecycleState.DISCONNECTED)
            raise
        except Exception as exc:
            await self._close_failed_connection()
            self._writes_admitted = False
            self._reads_admitted = False
            self._transition_state(DatabaseLifecycleState.FAILED_CLOSED)
            raise DatabaseError(f"Failed to connect to database: {exc}") from exc

    def _ensure_canonical_loop(self) -> None:
        """Reject use from an event loop other than the one that connected."""
        current_loop = asyncio.get_running_loop()
        if self._canonical_loop is None:
            self._canonical_loop = current_loop
        elif self._canonical_loop is not current_loop:
            raise DatabaseError(
                "Database accessed from a foreign event loop; create a fresh "
                "Database instance for each loop"
            )

    async def _close_failed_connection(self) -> None:
        """Close and forget a partially initialized connection."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    async def _safe_rollback(
        self,
    ) -> tuple[bool, bool, bool | None, Exception | None]:
        """Attempt rollback with bounded diagnostics.

        Returns ``(rollback_attempted, rollback_succeeded,
        in_transaction_after, rollback_exc)``:

        - ``rollback_attempted`` is True whenever the roll-back call
          actually executed (a ``RuntimeError`` plugin injection also
          counts).
        - ``rollback_succeeded`` is True when ``in_transaction``
          observed False after the rollback.
        - ``in_transaction_after`` is the raw observed value (None
          when the connection did not expose it).
        - ``rollback_exc`` is the exception raised by the rollback
          call itself, when one was raised.

        """
        rollback_attempted = False
        rollback_succeeded = False
        in_transaction_after: bool | None = None
        rollback_exc: Exception | None = None
        conn = self._conn
        if conn is None:
            return False, False, None, None
        try:
            in_transaction_before = getattr(conn, "in_transaction", None)
            if in_transaction_before is not None and not in_transaction_before:
                # Nothing to roll back; treat as success.
                return True, True, False, None
            rollback_attempted = True
            await conn.rollback()
            in_transaction_after = getattr(conn, "in_transaction", None)
            if in_transaction_after is False:
                rollback_succeeded = True
        except BaseException as rb_exc:  # noqa: BLE001
            rollback_exc = rb_exc  # type: ignore[assignment]
            rollback_succeeded = False
        return (
            rollback_attempted,
            rollback_succeeded,
            in_transaction_after,
            rollback_exc,
        )

    async def _commit_connection(self) -> None:
        """Execute the SQLite COMMIT. May be patched in tests."""
        await self._conn.commit()  # type: ignore[union-attr]

    @staticmethod
    def _build_read_only_uri(path: str) -> tuple[str, bool]:
        """Build a SQLite URI with read-only mode.

        In-memory databases cannot be opened in read-only mode; we
        fall back to the plain path in that case (the test fixtures
        rely on it).  Returns ``(path, use_uri)`` where *use_uri*
        indicates whether the path is a SQLite URI.
        """
        if path == ":memory:":
            return path, False
        if "://" in path:
            return path, True
        return f"file:{quote(path, safe='/:')}?mode=ro", True

    async def disconnect(self) -> None:
        """Close the connection."""
        self._ensure_canonical_loop()
        async with self._connection_lock:
            self._transition_state(DatabaseLifecycleState.SHUTTING_DOWN)
            conn_to_close, self._conn = self._conn, None
            self._writes_admitted = False
            self._reads_admitted = False

        # The lock protects the connection handoff, while the potentially
        # blocking aiosqlite close happens after the handoff. This prevents a
        # close from interleaving with an operation that already acquired the
        # lock and makes fixture teardown race-safe.
        if conn_to_close is not None:
            await self._close_connection(conn_to_close)
        close_task = self._invalidation_close_task
        if close_task is not None:
            with suppress(asyncio.CancelledError):
                await close_task

    @staticmethod
    async def _close_connection(conn: aiosqlite.Connection) -> None:
        """Close a detached connection with bounded best-effort cleanup."""
        with suppress(Exception):
            await asyncio.wait_for(conn.close(), timeout=5.0)

    def _detach_invalidated_connection(
        self, reason: str
    ) -> aiosqlite.Connection | None:
        """Mark the database failed-closed and detach its connection.

        The caller must hold ``_connection_lock``. Detaching is synchronous;
        the detached connection is closed by the caller after releasing the
        lock or by a scheduled close task when the caller is in a transaction.
        """
        if self._lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED:
            return None
        conn_to_close = self._conn
        self._conn = None
        self._invalidated_reason = reason
        self._invalidated_reason_class = self._classify_invalidation_reason(reason)
        self._invalidated_at = time.monotonic()
        self._writes_admitted = False
        self._reads_admitted = False
        self._transition_state(DatabaseLifecycleState.FAILED_CLOSED)
        return conn_to_close

    def _schedule_invalidation_close(self, conn: aiosqlite.Connection) -> None:
        """Close an invalidated connection after the current lock holder exits."""
        if self._invalidation_close_task is not None:
            return
        task = asyncio.create_task(self._close_connection(conn))
        self._invalidation_close_task = task

        def _clear_close_task(done: asyncio.Task[None]) -> None:
            if self._invalidation_close_task is done:
                self._invalidation_close_task = None
            with suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_clear_close_task)

    async def _invalidate_connection(self, reason: str) -> None:
        """Detach and close the connection after indeterminate state.

        Transaction failures detach synchronously while the transaction lock
        is held, then schedule bounded close work for after that lock is
        released. Direct callers acquire the lock before detaching and await
        the close themselves. Future ``transaction()`` calls fail
        with ``DatabaseConnectionInvalidatedError``; the deployment
        contract is a worker restart, not same-process reconnection.

        In addition to the original flag, the method transitions
        through ``FAILED_CLOSED`` so operators can distinguish a terminal
        database failure from an orderly shutdown.
        """
        if self._lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED:
            return
        lock_held = self._connection_lock.locked()
        if lock_held:
            # Internal transaction failures arrive while the lock is held.
            # Detach now and schedule close work so the lock can be released
            # by the surrounding context manager before aiosqlite shutdown.
            conn_to_close = self._detach_invalidated_connection(reason)
            if conn_to_close is not None:
                self._schedule_invalidation_close(conn_to_close)
        else:
            async with self._connection_lock:
                conn_to_close = self._detach_invalidated_connection(reason)
            if conn_to_close is not None:
                await self._close_connection(conn_to_close)
        if self._fatal_handler is not None and not self._fatal_notified:
            self._fatal_notified = True
            with suppress(Exception):
                self._fatal_handler(reason)

    def diagnostics(self) -> dict[str, Any]:
        """Return operational database diagnostics.

        Exposes connection state, invalidation facts, and the last
        commit/rollback outcome without SQL values, credentials, or
        file contents.
        """
        if self._lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED:
            state = "failed_closed"
        elif self._conn is None:
            state = "disconnected"
        else:
            state = "connected"
        return {
            "connection_state": state,
            "lifecycle_state": self._lifecycle_state.value,
            "writes_admitted": self._writes_admitted,
            "reads_admitted": self._reads_admitted,
            "invalidated_reason": self._invalidated_reason,
            "invalidated_reason_class": self._invalidated_reason_class,
            "invalidated_at": self._invalidated_at,
            "reconnect_required": self._lifecycle_state
            is DatabaseLifecycleState.FAILED_CLOSED,
            "last_commit_outcome": self._last_commit_outcome,
            "last_rollback_attempted": self._last_rollback_attempted,
            "last_rollback_succeeded": self._last_rollback_succeeded,
            "last_in_transaction_before_rollback": (
                self._last_in_transaction_before_rollback
            ),
            "last_in_transaction_after_rollback": (
                self._last_in_transaction_after_rollback
            ),
            "rollback_failure_count": self._rollback_failure_count,
            "rollback_success_count": self._rollback_success_count,
        }

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database not connected")
        return self._conn

    def _current_task_owns_transaction(self) -> bool:
        """Return True if the *current* task issued the active ``BEGIN``.

        Tracks the task that issued ``BEGIN IMMEDIATE`` for the
        active outermost transaction. Used by callers that need
        to distinguish "this task holds the transaction lock"
        (and therefore cannot acquire ``_connection_lock``
        without deadlocking) from "some other task holds the
        transaction lock". Nesting detection inside
        ``transaction()`` itself uses the per-task
        ``_in_transaction_context`` ContextVar, NOT this helper,
        because ``_transaction_owner`` is task-scoped and would
        misidentify shielded or ``create_task`` children (which
        inherit ``_in_transaction_context`` but not
        ``_transaction_owner``) as non-owners.
        """
        owner = self._transaction_owner.get()
        return owner is not None and owner is asyncio.current_task()

    def _require_transaction_owner(self) -> None:
        """Raise if the current code path is not inside a transaction.

        Every write through :meth:`execute_write`, :meth:`execute_insert`,
        :meth:`execute_returning`, or :meth:`_execute_cursor` MUST be
        performed inside a ``db.transaction()`` boundary owned by the
        current task. ContextVar inheritance does not grant a child task
        permission to execute SQL.
        """
        if self._read_only:
            raise DatabaseError(
                "Database is opened read-only; writes are not permitted"
            )
        if not self._current_task_owns_transaction():
            if self._transaction_owner.get() is not None:
                raise DatabaseTransactionOwnershipError(
                    "database transaction is owned by another asyncio task"
                )
            raise DatabaseError(
                "Database writes require an active transaction; "
                "use 'async with db.transaction():'"
            )

    def _has_active_transaction_context(self) -> bool:
        """Return whether the current task owns the active transaction."""
        return self._current_task_owns_transaction()

    @asynccontextmanager
    async def _connection_access(self) -> AsyncGenerator[None]:
        """Acquire the connection lock for a SQL operation.

        The transaction owner may use the already-held lock. A different
        task fails before waiting or issuing SQL; otherwise the lock is
        acquired for the duration of the operation.

        Lock wait time is tracked in contention counters for
        runtime diagnostics.
        """
        self._ensure_canonical_loop()
        if self._lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED:
            raise DatabaseConnectionInvalidatedError(
                self._invalidated_reason
                or "Connection invalidated by indeterminate commit outcome"
            )
        if not self._reads_admitted:
            raise DatabaseError("Database reads are not admitted")
        owner = self._transaction_owner.get()
        if owner is not None and owner is not asyncio.current_task():
            raise DatabaseTransactionOwnershipError(
                "database transaction is owned by another asyncio task"
            )
        if self._has_active_transaction_context():
            yield
            return

        t0 = time.monotonic()
        async with self._connection_lock:
            elapsed = time.monotonic() - t0
            self._cumulative_lock_wait_s += elapsed
            if elapsed > self._max_lock_wait_s:
                self._max_lock_wait_s = elapsed
            self._lock_wait_count += 1
            self._lock_wait_samples_s.append(elapsed)
            yield

    async def probe_writable(self) -> bool:
        """Probe the database for write access using a transaction.

        The transaction is always rolled back; returns True if the
        insert succeeded, False otherwise.
        """
        try:
            async with self.transaction():
                await self._execute_cursor(
                    "INSERT INTO health_probe (probe_at) VALUES (CURRENT_TIMESTAMP)"
                )
                raise _RollbackProbeError
        except _RollbackProbeError:
            return True
        except Exception:
            return False

    async def _execute_cursor(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Cursor:
        """Execute a SQL statement and return the raw cursor.

        This method is **transaction-owner-only**.  The caller MUST hold
        the connection lock (either by being inside ``async with
        db.transaction():`` or by consuming the cursor before yielding
        control).  Outside a transaction the lock is released when this
        method returns, so any subsequent use of the cursor would race
        with other concurrent tasks.

        Prefer :meth:`execute_write`, :meth:`execute_insert`, or
        :meth:`execute_returning` for all new code.
        """
        self._require_transaction_owner()
        try:
            return await self.connection.execute(sql, params)  # type: ignore[return-value]
        except Exception as exc:
            raise DatabaseError(f"Execute failed: {exc}") from exc

    async def execute_write(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """Execute a write statement and return the rowcount.

        Must be called inside ``async with db.transaction():`` owned
        by the current task.  The cursor is fully consumed before this
        method returns, so the returned rowcount is always valid.
        """
        self._require_transaction_owner()
        try:
            cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
            rowcount = cursor.rowcount
            self._write_ops += 1
            kind = _classify_op_kind(sql)
            self._operations_by_kind[kind] = self._operations_by_kind.get(kind, 0) + 1
            return int(rowcount) if rowcount >= 0 else 0
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
            self._last_operation_error_kind = _classify_error_kind(exc)
            raise DatabaseError(f"Execute write failed: {exc}") from exc

    async def execute_many(
        self,
        sql: str,
        params: Sequence[Sequence[Any]],
    ) -> int:
        """Execute one write statement for multiple parameter rows.

        Must be called inside an owned transaction. Batching avoids one
        aiosqlite worker-thread round trip per row while preserving the
        caller's transaction boundary.
        """
        self._require_transaction_owner()
        if not params:
            return 0
        try:
            cursor = await self.connection.executemany(sql, params)  # type: ignore[union-attr]
            rowcount = cursor.rowcount
            self._write_ops += len(params)
            kind = _classify_op_kind(sql)
            self._operations_by_kind[kind] = self._operations_by_kind.get(kind, 0) + 1
            return int(rowcount) if rowcount >= 0 else 0
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
            self._last_operation_error_kind = _classify_error_kind(exc)
            raise DatabaseError(f"Execute many failed: {exc}") from exc

    async def execute_insert(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """Execute an INSERT and return lastrowid.

        Must be called inside ``async with db.transaction():`` owned
        by the current task.  Raises ``DatabaseError`` if the INSERT
        did not produce a ``lastrowid`` (for example, against a table
        that lacks an INTEGER PRIMARY KEY).
        """
        self._require_transaction_owner()
        try:
            cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
            last_id = cursor.lastrowid
            if last_id is None:
                raise DatabaseError("INSERT did not return lastrowid")
            self._write_ops += 1
            kind = _classify_op_kind(sql)
            self._operations_by_kind[kind] = self._operations_by_kind.get(kind, 0) + 1
            return int(last_id)
        except DatabaseError:
            raise
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
            self._last_operation_error_kind = _classify_error_kind(exc)
            raise DatabaseError(f"Execute insert failed: {exc}") from exc

    async def execute_returning(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[aiosqlite.Row]:
        """Execute a statement (typically ``UPDATE ... RETURNING``) and
        return all rows.

        Must be called inside ``async with db.transaction():`` owned
        by the current task.  The returned rows are guaranteed to be
        observed under the same lock acquisition as the underlying
        statement.
        """
        self._require_transaction_owner()
        try:
            upper = sql.lstrip().upper()
            cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
            rows = await cursor.fetchall()
            if upper.startswith("SELECT"):
                self._read_ops += 1
            else:
                self._write_ops += 1
            kind = _classify_op_kind(sql)
            self._operations_by_kind[kind] = self._operations_by_kind.get(kind, 0) + 1
            return list(rows)  # type: ignore[arg-type]
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
            self._last_operation_error_kind = _classify_error_kind(exc)
            raise DatabaseError(f"Execute returning failed: {exc}") from exc

    async def vacuum(self) -> None:
        """Run ``VACUUM`` to rebuild the database file.

        ``VACUUM`` cannot run inside a transaction, so this method
        bypasses :meth:`transaction` and acquires the connection lock
        directly. The lock is required so no other task can start a
        transaction while vacuum is rebuilding the file.

        Preconditions:

        - the database is not opened in read-only mode;
        - the current task is not the owner of an active transaction.

        Failures are wrapped in :class:`DatabaseError`. The connection
        is left usable on success or failure.
        """
        if self._read_only:
            raise DatabaseError("VACUUM cannot run on a read-only database")
        if (
            self._in_transaction_context.get()
            or self._transaction_owner.get() is not None
        ):
            raise DatabaseError("VACUUM cannot run while a transaction is active")
        async with self._connection_lock:
            try:
                cursor = await self.connection.execute("VACUUM")
                await cursor.close()
            except Exception as exc:
                raise DatabaseError(f"VACUUM failed: {exc}") from exc

    async def execute_pragma(self, sql: str) -> list[aiosqlite.Row]:
        """Execute a PRAGMA statement safely.

        Only accepts SQL beginning with "PRAGMA " (case-insensitive,
        after whitespace normalization).  Holds the connection lock
        for execution and fetch, and consumes the cursor before
        releasing the lock.  Returns rows when the PRAGMA produces
        rows; empty list otherwise.
        """
        if not sql or not sql.lstrip().upper().startswith("PRAGMA "):
            raise DatabaseError(
                "execute_pragma() only accepts SQL beginning with 'PRAGMA '"
            )
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql)  # type: ignore[union-attr]
                rows = await cursor.fetchall()
                return list(rows)  # type: ignore[arg-type]
            except Exception as exc:
                raise DatabaseError(f"Execute pragma failed: {exc}") from exc

    def contention_snapshot(self) -> dict[str, Any]:
        """Return in-memory contention counters.

        Counters are best-effort and reset on process restart.  They
        are intended for runtime diagnostics, not billing or alerting.
        ``lock_wait_p50_ms`` / ``p95_ms`` / ``p99_ms`` are computed from
        a bounded ring buffer of recent samples; ``None`` when fewer
        than one sample has been observed.
        """
        snapshot: dict[str, Any] = {
            "write_ops": self._write_ops,
            "read_ops": self._read_ops,
            "total_transactions": self._total_transactions,
            "total_nested_transactions": self._total_nested_transactions,
            "last_operation_error_class": self._last_operation_error_class,
            "last_operation_error_kind": self._last_operation_error_kind,
            "operations_by_kind": dict(self._operations_by_kind),
            "cumulative_lock_wait_s": round(self._cumulative_lock_wait_s, 4),
            "max_lock_wait_s": round(self._max_lock_wait_s, 4),
            "lock_wait_count": self._lock_wait_count,
        }
        if self._lock_wait_samples_s:
            samples = sorted(self._lock_wait_samples_s)
            size = len(samples)
            snapshot["lock_wait_p50_ms"] = round(
                samples[int(0.50 * (size - 1))] * 1000, 3
            )
            snapshot["lock_wait_p95_ms"] = round(
                samples[int(0.95 * (size - 1))] * 1000, 3
            )
            snapshot["lock_wait_p99_ms"] = round(
                samples[min(int(0.99 * (size - 1)), size - 1)] * 1000, 3
            )
            snapshot["lock_wait_max_ms"] = round(samples[-1] * 1000, 3)
            snapshot["lock_wait_sample_count"] = size
        else:
            snapshot["lock_wait_p50_ms"] = None
            snapshot["lock_wait_p95_ms"] = None
            snapshot["lock_wait_p99_ms"] = None
            snapshot["lock_wait_max_ms"] = None
            snapshot["lock_wait_sample_count"] = 0
        return snapshot

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all matching rows while holding the connection lock."""
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                rows = await cursor.fetchall()
                self._read_ops += 1
                kind = _classify_op_kind(sql)
                cur = self._operations_by_kind
                cur[kind] = cur.get(kind, 0) + 1
                return list(rows)  # type: ignore[arg-type]
            except Exception as exc:
                self._last_operation_error_class = type(exc).__qualname__
                self._last_operation_error_kind = _classify_error_kind(exc)
                raise DatabaseError(f"Fetch all failed: {exc}") from exc

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        """Fetch a single row or None while holding the connection lock."""
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                row = await cursor.fetchone()
                self._read_ops += 1
                kind = _classify_op_kind(sql)
                cur = self._operations_by_kind
                cur[kind] = cur.get(kind, 0) + 1
                return row  # type: ignore[return-value]
            except Exception as exc:
                self._last_operation_error_class = type(exc).__qualname__
                self._last_operation_error_kind = _classify_error_kind(exc)
                raise DatabaseError(f"Fetch one failed: {exc}") from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        """Execute a serialized write transaction.

        Uses ``BEGIN IMMEDIATE`` to serialize writers predictably.
        Repository methods must NOT call commit inside this context;
        the caller owns commit boundaries.

        Nesting is permitted only for the same asyncio task. A child task
        with inherited context fails before acquiring the lock or issuing SQL.
        The outermost task alone issues ``BEGIN IMMEDIATE`` / ``COMMIT`` /
        ``ROLLBACK``.
        """
        self._ensure_canonical_loop()
        if self._lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED:
            raise DatabaseConnectionInvalidatedError(
                self._invalidated_reason
                or "Connection invalidated by indeterminate commit outcome"
            )
        if self._conn is None:
            raise DatabaseError("Database not connected")
        if self._read_only:
            raise DatabaseError("Database is read-only")
        if not self._writes_admitted:
            raise DatabaseError("Database writes are not admitted")

        # Fast path: reuse an existing transaction only in its owning task.
        owner = self._transaction_owner.get()
        if owner is not None and owner is not asyncio.current_task():
            raise DatabaseTransactionOwnershipError(
                "database transaction is owned by another asyncio task"
            )
        if self._has_active_transaction_context():
            self._total_nested_transactions += 1
            ctx_token = self._in_transaction_context.set(True)
            try:
                yield
            except BaseException:
                # Nested callers MUST NOT commit or roll back the
                # shared transaction. Re-raise so the outermost
                # caller observes the failure and decides whether
                # to roll the whole thing back.
                raise
            finally:
                self._in_transaction_context.reset(ctx_token)
            return

        # Outermost: serialize via the connection lock and own the
        # BEGIN / COMMIT boundaries.
        async with self._connection_lock:
            # Fatal transitions happen while this lock is held, so no other
            # task can change lifecycle state between the admission check and
            # this point.
            if self._has_active_transaction_context():
                self._total_nested_transactions += 1
                ctx_token = self._in_transaction_context.set(True)
                try:
                    yield
                except BaseException:
                    raise
                finally:
                    self._in_transaction_context.reset(ctx_token)
                return

            self._total_transactions += 1
            owner = asyncio.current_task()
            owner_token = self._transaction_owner.set(owner)
            ctx_token = self._in_transaction_context.set(True)
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                self._in_transaction_context.reset(ctx_token)
                self._transaction_owner.reset(owner_token)
                if _is_fatal_database_error(exc):
                    await self._invalidate_connection(f"begin failure: {exc!r}")
                raise DatabaseError(f"Begin transaction failed: {exc}") from exc
            try:
                yield
            except BaseException as exc:
                # Body raised: attempt rollback.  Distinguish a
                # successful rollback from a rollback failure so
                # callers see the right typed error.
                (
                    body_rollback_attempted,
                    body_rollback_succeeded,
                    body_in_transaction_after,
                    body_rollback_exc,
                ) = await self._safe_rollback()
                if body_rollback_succeeded:
                    self._rollback_success_count += 1
                    if _is_fatal_database_error(exc):
                        await self._invalidate_connection(
                            f"transaction failure: {exc!r}"
                        )
                if not body_rollback_succeeded and body_rollback_attempted:
                    # Rollback itself failed — the transaction state
                    # is unknown. Invalidate the connection and raise a typed
                    # ``DatabaseRollbackError`` so callers cannot
                    # continue to issue SQL on a poisoned connection.
                    self._rollback_failure_count += 1
                    await self._invalidate_connection(
                        f"rollback failure — {body_rollback_exc!r}"
                    )
                    raise DatabaseRollbackError(
                        f"SQLite ROLLBACK failed: {body_rollback_exc!r}",
                        rollback_attempted=body_rollback_attempted,
                        rollback_succeeded=body_rollback_succeeded,
                        transaction_still_active=body_in_transaction_after,
                        connection_invalidated=True,
                        original_exception=body_rollback_exc,
                    ) from body_rollback_exc
                raise
            else:
                # Catch exceptions from the actual ``commit()`` call and
                # determine whether rollback proved the connection clean.
                commit_exc: Exception | None = None
                try:
                    await self._commit_connection()
                except Exception as exc:
                    commit_exc = exc

                if commit_exc is not None:
                    rollback_attempted = False
                    rollback_succeeded = False
                    connection_invalidated = False
                    in_transaction_before_rollback: bool | None = None
                    in_transaction_after_rollback: bool | None = None
                    rollback_exc: Exception | None = None

                    try:
                        in_transaction_before_rollback = getattr(
                            self._conn, "in_transaction", None
                        )
                        rollback_attempted = True
                        if (
                            in_transaction_before_rollback is not None
                            and in_transaction_before_rollback
                        ):
                            await self._conn.rollback()
                            in_transaction_after_rollback = getattr(
                                self._conn, "in_transaction", None
                            )
                            if in_transaction_after_rollback is False:
                                rollback_succeeded = True
                                self._rollback_success_count += 1
                            else:
                                connection_invalidated = True
                        else:
                            # Commit raised but in_transaction is
                            # already false — indeterminate.
                            connection_invalidated = True
                    except Exception as rb_exc:
                        rollback_succeeded = False
                        rollback_exc = rb_exc
                        connection_invalidated = True
                        self._rollback_failure_count += 1

                    if _is_fatal_database_error(commit_exc):
                        connection_invalidated = True

                    outcome = "rolled_back" if rollback_succeeded else "indeterminate"
                    self._last_commit_outcome = outcome
                    # Durable request/attempt/reservation identities are
                    # reconciled at the next process start. Do not retain
                    # an in-process ambiguity queue after admission closes.
                    self._last_rollback_attempted = rollback_attempted
                    self._last_rollback_succeeded = rollback_succeeded
                    self._last_in_transaction_before_rollback = (
                        in_transaction_before_rollback
                    )
                    self._last_in_transaction_after_rollback = (
                        in_transaction_after_rollback
                    )
                    # Determine transaction_still_active from
                    # actual observation rather than leaving it None.
                    transaction_still_active: bool | None = (
                        in_transaction_after_rollback
                        if rollback_attempted
                        else in_transaction_before_rollback
                    )

                    if connection_invalidated:
                        await self._invalidate_connection(
                            f"commit failure — {outcome}: {commit_exc!r}"
                        )

                    # If rollback itself failed (separate from the
                    # commit failure), raise the typed
                    # ``DatabaseRollbackError`` so callers see the
                    # rollback failure distinctly.
                    if rollback_exc is not None:
                        raise DatabaseRollbackError(
                            f"SQLite ROLLBACK failed after commit failure: "
                            f"{rollback_exc!r}",
                            rollback_attempted=rollback_attempted,
                            rollback_succeeded=rollback_succeeded,
                            transaction_still_active=transaction_still_active,
                            connection_invalidated=connection_invalidated,
                            original_exception=rollback_exc,
                        ) from commit_exc

                    raise DatabaseCommitError(
                        f"SQLite COMMIT failed: {commit_exc!r}",
                        rollback_attempted=rollback_attempted,
                        rollback_succeeded=rollback_succeeded,
                        transaction_still_active=transaction_still_active,
                        connection_invalidated=connection_invalidated,
                        outcome=outcome,
                    ) from commit_exc
            finally:
                self._in_transaction_context.reset(ctx_token)
                self._transaction_owner.reset(owner_token)
