"""SQLite connection manager using aiosqlite."""

from __future__ import annotations

import asyncio
import collections
import threading
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

from eggpool.errors import (
    DatabaseCommitError,
    DatabaseConnectionInvalidatedError,
    DatabaseError,
)


class _RollbackProbeError(Exception):
    """Sentinel exception for probe_writable to roll back without logging."""


@dataclass(slots=True)
class _TransactionState:
    """Mutable marker shared by tasks that inherit a transaction context."""

    active: bool = True


def _classify_op_kind(sql: str) -> str:
    """Classify a SQL statement into a coarse operator-kind bucket.

    The result feeds the in-memory ``operations_by_kind`` counter so
    operators can see whether the database is dominated by SELECTs,
    INSERTs, UPDATEs, DELETEs, or admin pragmas / schema migrations.
    Only the leading keyword is inspected.
    """
    stripped = sql.lstrip()
    upper = stripped.upper()
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
    if upper.startswith("BEGIN") or upper.startswith("COMMIT"):
        return "transaction"
    if upper.startswith("PRAGMA"):
        return "pragma"
    return "other"


def _classify_error_kind(exc: BaseException) -> str:
    """Classify a database exception into a coarse operator-kind bucket.

    The result feeds the in-memory ``last_operation_error_kind`` counter
    so operators can see whether the most recent failure was a lock
    conflict, schema error, integrity violation, or other class.
    """
    cls_name = type(exc).__qualname__.lower()
    if "lock" in cls_name or "busy" in cls_name:
        return "lock"
    if "integrity" in cls_name:
        return "integrity"
    if "operational" in cls_name:
        return "operational"
    if "syntax" in cls_name or "schema" in cls_name:
        return "schema"
    return "other"


class Database:
    """Async wrapper around aiosqlite with pragma configuration.

    All SQL operations are serialized through a single connection lock.
    Nesting is detected via the per-task ``_in_transaction_context``
    ContextVar, not via SQLite's connection-wide ``in_transaction``
    state. ContextVars are inherited at task creation, so calls
    inside ``asyncio.shield()`` or ``asyncio.create_task()`` from a
    parent already inside ``transaction()`` correctly piggyback on
    the outer transaction without issuing a second ``BEGIN`` against
    the single SQLite connection. Unrelated concurrent tasks (probe
    workers, healthcheck tasks, sibling requests) do not inherit
    that flag and therefore acquire the lock directly so they
    cannot piggyback on each other's transactions.
    """

    #: Test-only fault injection seam for the pre-commit boundary.
    #:
    #: When set on the class, every outermost ``transaction()`` exits
    #: by raising this exception *after* the inner work has yielded
    #: successfully but *before* the SQLite COMMIT is issued.  This
    #: simulates a process crash / power-loss between yield and commit
    #: so reload tests can verify that callers see the failure and
    #: run the rollback / compensation path.  Must default to ``None``
    #: in production; only tests should set this.
    TEST_INJECT_BEFORE_COMMIT_CALL: Exception | None = None

    def __init__(
        self,
        path: str,
        busy_timeout_ms: int = 5000,
        wal: bool = True,
        synchronous: str = "NORMAL",
        read_only: bool = False,
    ) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._wal = wal
        self._synchronous = synchronous
        self._read_only = read_only
        self._conn: aiosqlite.Connection | None = None
        self._connection_lock = asyncio.Lock()
        self._connection_lock_guard = threading.Lock()
        self._transaction_depth: ContextVar[int] = ContextVar(
            "database_transaction_depth",
            default=0,
        )
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
        # Tracks whether the current asyncio.Task is currently
        # executing inside a ``db.transaction()`` block (outermost
        # OR nested/piggyback). Used by ``_require_transaction_owner``
        # and ``vacuum()`` to gate writes and special operations.
        # ContextVars are inherited at task creation, so shielded
        # and ``create_task`` children see ``True`` while their
        # parent is still inside a transaction -- this is what lets
        # a shielded child do writes that piggyback on the parent's
        # transaction without re-issuing ``BEGIN``.
        self._in_transaction_context: ContextVar[bool] = ContextVar(
            "database_in_transaction_context",
            default=False,
        )
        self._transaction_state: ContextVar[_TransactionState | None] = ContextVar(
            "database_transaction_state",
            default=None,
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
        # Instance-scoped test-only injection hooks.  These override
        # the class-level seam for a single Database instance so tests
        # can target a specific connection without affecting others.
        self._test_inject_before_commit: Exception | None = None
        self._test_inject_commit_call: Exception | None = None
        self._test_inject_rollback_call: Exception | None = None
        self._test_inject_in_transaction_before_rollback: bool | None = None
        # Connection-invalidation state (Plan 018 Workstream E).
        # Set when a commit failure leaves the connection in an
        # indeterminate state; subsequent transaction() calls raise
        # DatabaseConnectionInvalidatedError until connect() is called.
        self._invalidated: bool = False
        self._invalidated_reason: str | None = None
        self._invalidated_at: float | None = None
        self._last_commit_outcome: str | None = None
        self._last_rollback_attempted: bool = False
        self._last_rollback_succeeded: bool = False
        self._last_in_transaction_before_rollback: bool | None = None
        self._last_in_transaction_after_rollback: bool | None = None

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def connect(self) -> None:
        """Open the connection and set pragmas."""
        if self._conn is not None:
            raise DatabaseError("Database already connected")
        self._invalidated = False
        self._invalidated_reason = None
        self._invalidated_at = None
        try:
            if self._read_only:
                # Use a read-only URI so SQLite refuses to change
                # journal mode, create WAL files, or apply migrations.
                uri, use_uri = self._build_read_only_uri(self._path)
                self._conn = await aiosqlite.connect(uri, uri=use_uri)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.execute(
                    f"PRAGMA busy_timeout = {self._busy_timeout_ms}"
                )
                return
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            if self._wal:
                await self._conn.execute("PRAGMA journal_mode = WAL")
            await self._conn.execute(f"PRAGMA synchronous = {self._synchronous}")
            await self._conn.commit()
        except asyncio.CancelledError:
            await self._close_failed_connection()
            raise
        except Exception as exc:
            await self._close_failed_connection()
            raise DatabaseError(f"Failed to connect to database: {exc}") from exc

    async def _close_failed_connection(self) -> None:
        """Close and forget a partially initialized connection."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    async def _commit_connection(self) -> None:
        """Execute the SQLite COMMIT. May be patched in tests."""
        await self._conn.commit()  # type: ignore[union-attr]

    def set_test_inject_before_commit(self, exc: Exception | None) -> None:
        """Instance-scoped test hook for pre-commit bypass injection."""
        self._test_inject_before_commit = exc

    def set_test_inject_commit_call(self, exc: Exception | None) -> None:
        """Instance-scoped test hook for commit-call failure injection."""
        self._test_inject_commit_call = exc

    def set_test_inject_rollback_call(self, exc: Exception | None) -> None:
        """Instance-scoped test hook for deterministic rollback failure."""
        self._test_inject_rollback_call = exc

    def set_test_inject_in_transaction_before_rollback(
        self,
        value: bool | None,
    ) -> None:
        """Override the observed transaction state for commit recovery tests."""
        self._test_inject_in_transaction_before_rollback = value

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
        return f"file:{path}?mode=ro", True

    async def disconnect(self) -> None:
        """Close the connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _invalidate_connection(self, reason: str) -> None:
        """Detach and close the connection after indeterminate state.

        Atomically removes the connection from ``_conn`` while the
        connection lock is held (the caller must already hold the lock),
        sets the invalidated flag, and closes the detached connection
        with bounded best-effort.  Future ``transaction()`` calls fail
        with ``DatabaseConnectionInvalidatedError`` until ``connect()``
        is called again.
        """
        if self._conn is None:
            return
        conn_to_close = self._conn
        self._conn = None
        self._invalidated = True
        self._invalidated_reason = reason
        self._invalidated_at = time.monotonic()
        with suppress(Exception):
            await asyncio.wait_for(conn_to_close.close(), timeout=5.0)

    def diagnostics(self) -> dict[str, Any]:
        """Return operational database diagnostics.

        Exposes connection state, invalidation facts, and the last
        commit/rollback outcome without SQL values, credentials, or
        file contents.
        """
        if self._invalidated:
            state = "invalidated"
        elif self._conn is None:
            state = "disconnected"
        else:
            state = "connected"
        return {
            "connection_state": state,
            "invalidated_reason": self._invalidated_reason,
            "invalidated_at": self._invalidated_at,
            "reconnect_required": self._invalidated,
            "last_commit_outcome": self._last_commit_outcome,
            "last_rollback_attempted": self._last_rollback_attempted,
            "last_rollback_succeeded": self._last_rollback_succeeded,
            "last_in_transaction_before_rollback": (
                self._last_in_transaction_before_rollback
            ),
            "last_in_transaction_after_rollback": (
                self._last_in_transaction_after_rollback
            ),
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
        performed inside a ``db.transaction()`` boundary. The check is
        per-task-context (``_in_transaction_context`` ContextVar),
        which is inherited across ``asyncio.shield()`` and
        ``asyncio.create_task()`` so shielded/child tasks can do
        writes that piggyback on the parent's transaction without
        raising. Unrelated tasks that have not entered a transaction
        block will raise.
        """
        if self._read_only:
            raise DatabaseError(
                "Database is opened read-only; writes are not permitted"
            )
        if not self._has_active_transaction_context():
            raise DatabaseError(
                "Database writes require an active transaction; "
                "use 'async with db.transaction():'"
            )

    def _has_active_transaction_context(self) -> bool:
        """Return whether this task inherited a still-active transaction."""
        state_context = getattr(self, "_transaction_state", None)
        if state_context is None:
            # Keep lightweight test doubles and legacy manually constructed
            # Database instances compatible with the pre-state marker.
            return self._in_transaction_context.get()
        state = state_context.get()
        return state is not None and state.active

    def _refresh_idle_connection_lock(self) -> None:
        """Recreate an idle connection lock if it was bound to another loop.

        ``asyncio.Lock`` binds itself to the first event loop that has
        to wait for it. TestClient and multi-loop hosts can reuse the
        same Database instance across event loops, so an idle lock from
        an old loop must not poison the next request.  Held locks are
        never replaced; serialization remains intact.
        """
        if self._connection_lock.locked():
            return
        current_loop = asyncio.get_running_loop()
        lock_loop = getattr(self._connection_lock, "_loop", None)
        if lock_loop is None or lock_loop is current_loop:
            return

        guard = getattr(self, "_connection_lock_guard", None)
        if guard is None:
            guard = threading.Lock()
            self._connection_lock_guard = guard
        with guard:
            if self._connection_lock.locked():
                return
            lock_loop = getattr(self._connection_lock, "_loop", None)
            if lock_loop is not None and lock_loop is not current_loop:
                self._connection_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connection_access(self) -> AsyncGenerator[None]:
        """Acquire the connection lock for a SQL operation.

        If a transaction is already open on this connection, the
        outermost ``transaction()`` caller holds ``_connection_lock``
        and SQL is serialized through aiosqlite's worker thread; this
        is a no-op so piggybacked reads/writes do not deadlock.
        Otherwise the lock is acquired for the duration of the
        ``yield``.

        Lock wait time is tracked in contention counters for
        runtime diagnostics.
        """
        if self._invalidated:
            raise DatabaseConnectionInvalidatedError(
                self._invalidated_reason
                or "Connection invalidated by indeterminate commit outcome"
            )
        # ContextVar inheritance is the transaction's intentional
        # piggyback signal.  A child task created inside the transaction
        # inherits ``_in_transaction_context`` but not the identity of the
        # task that issued BEGIN.  Checking only the owner task here would
        # make child reads/PRAGMAs wait on the lock held by their parent.
        if self._has_active_transaction_context():
            yield
            return

        t0 = time.monotonic()
        self._refresh_idle_connection_lock()
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
        if self._in_transaction_context.get():
            raise DatabaseError("VACUUM cannot run while a transaction is active")
        self._refresh_idle_connection_lock()
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

        Nesting semantics are gated on the per-task
        ``_in_transaction_context`` ContextVar, NOT on SQLite's
        per-connection ``conn.in_transaction``. ContextVars are
        inherited at task creation, so ``asyncio.shield()`` and
        ``asyncio.create_task()`` children of an outer caller that
        already entered ``transaction()`` see ``True`` and piggyback
        on the outer's commit boundary -- which avoids
        ``OperationalError: cannot start a transaction within
        a transaction`` and ``_connection_lock`` deadlocks.

        Tasks that did not inherit ``_in_transaction_context=True``
        (probe workers, healthcheck tasks, unrelated concurrent
        requests) fall through to acquire ``_connection_lock``
        directly. This keeps separate operations from piggybacking
        on each other's transactions.

        The outermost ``transaction()`` caller is the only one
        that acquires ``_connection_lock`` and issues
        ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``. Nested
        callers -- including task-spawned piggybackers -- simply
        yield and inherit the outer's commit boundary.
        """
        if self._invalidated:
            raise DatabaseConnectionInvalidatedError(
                self._invalidated_reason
                or "Connection invalidated by indeterminate commit outcome"
            )
        if self._conn is None:
            raise DatabaseError("Database not connected")

        # Fast path: piggyback on an existing transaction only when
        # the current task inherited the transaction context (via
        # ``asyncio.shield()`` / ``asyncio.create_task()`` from a
        # parent already inside ``transaction()``) or is the same
        # task that opened it. Unrelated tasks that lack that
        # inheritance must acquire the lock instead, so two
        # concurrent requests cannot piggyback on each other.
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
        self._refresh_idle_connection_lock()
        async with self._connection_lock:
            if self._invalidated:
                raise DatabaseConnectionInvalidatedError(
                    self._invalidated_reason
                    or "Connection invalidated by indeterminate commit outcome"
                )
            # Re-check under the lock. Another task may have raced
            # between our initial check and acquiring the lock.
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
            state = _TransactionState()
            state_context = getattr(self, "_transaction_state", None)
            if state_context is None:
                state_context = ContextVar[_TransactionState | None](
                    "database_transaction_state_compat",
                    default=None,
                )
                self._transaction_state = state_context
            state_token = state_context.set(state)
            ctx_token = self._in_transaction_context.set(True)
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                state.active = False
                state_context.reset(state_token)
                self._in_transaction_context.reset(ctx_token)
                self._transaction_owner.reset(owner_token)
                raise DatabaseError(f"Begin transaction failed: {exc}") from exc
            try:
                yield
            except BaseException:
                await self._conn.rollback()
                raise
            else:
                # Plan 016 Workstream F / Plan 017 Workstream E:
                # test-only fault-injection seam to simulate a process
                # crash *after* the inner work completed but *before*
                # the SQLite COMMIT is issued.  Instance-level
                # ``_test_inject_before_commit`` takes precedence over
                # the class-level ``TEST_INJECT_BEFORE_COMMIT_CALL``.
                # The injection MUST NOT swallow real database errors
                # that arise from the actual ``commit()`` call.
                injected = (
                    self._test_inject_before_commit
                    or Database.TEST_INJECT_BEFORE_COMMIT_CALL
                )
                if injected is not None:
                    # One-shot: clear both instance and class seams.
                    self._test_inject_before_commit = None
                    if Database.TEST_INJECT_BEFORE_COMMIT_CALL is not None:
                        Database.TEST_INJECT_BEFORE_COMMIT_CALL = None
                    await self._conn.rollback()
                    raise injected

                # Plan 017 Workstream E: catch exceptions from the
                # actual ``commit()`` call and attempt recovery.
                commit_exc: Exception | None = None
                try:
                    commit_injected = self._test_inject_commit_call
                    if commit_injected is not None:
                        self._test_inject_commit_call = None
                        raise commit_injected
                    await self._commit_connection()
                except Exception as exc:
                    commit_exc = exc

                if commit_exc is not None:
                    rollback_attempted = False
                    rollback_succeeded = False
                    connection_invalidated = False
                    in_transaction_before_rollback: bool | None = None
                    in_transaction_after_rollback: bool | None = None

                    try:
                        in_transaction_before_rollback = (
                            self._test_inject_in_transaction_before_rollback
                            if self._test_inject_in_transaction_before_rollback
                            is not None
                            else getattr(self._conn, "in_transaction", None)
                        )
                        self._test_inject_in_transaction_before_rollback = None
                        rollback_attempted = True
                        if (
                            in_transaction_before_rollback is not None
                            and in_transaction_before_rollback
                        ):
                            rollback_injected = self._test_inject_rollback_call
                            self._test_inject_rollback_call = None
                            if rollback_injected is not None:
                                raise rollback_injected
                            await self._conn.rollback()
                            in_transaction_after_rollback = getattr(
                                self._conn, "in_transaction", None
                            )
                            if in_transaction_after_rollback is False:
                                rollback_succeeded = True
                            else:
                                connection_invalidated = True
                        else:
                            # Commit raised but in_transaction is
                            # already false — indeterminate.
                            connection_invalidated = True
                    except Exception:
                        rollback_succeeded = False
                        connection_invalidated = True

                    outcome = "rolled_back" if rollback_succeeded else "indeterminate"
                    self._last_commit_outcome = outcome
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

                    raise DatabaseCommitError(
                        f"SQLite COMMIT failed: {commit_exc!r}",
                        rollback_attempted=rollback_attempted,
                        rollback_succeeded=rollback_succeeded,
                        transaction_still_active=transaction_still_active,
                        connection_invalidated=connection_invalidated,
                        outcome=outcome,
                    ) from commit_exc
            finally:
                state.active = False
                state_context.reset(state_token)
                self._in_transaction_context.reset(ctx_token)
                self._transaction_owner.reset(owner_token)
