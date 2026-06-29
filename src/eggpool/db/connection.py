"""SQLite connection manager using aiosqlite."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

from eggpool.errors import DatabaseError


class _RollbackProbeError(Exception):
    """Sentinel exception for probe_writable to roll back without logging."""


class Database:
    """Async wrapper around aiosqlite with pragma configuration.

    All SQL operations are serialized through a single connection lock.
    Nesting is detected via SQLite's own connection state
    (``conn.in_transaction``), not task identity, so calls inside
    ``asyncio.shield()`` or ``asyncio.create_task()`` correctly
    piggyback on the outer transaction without issuing a second
    ``BEGIN`` against the single SQLite connection.
    """

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
        self._transaction_owner: ContextVar[asyncio.Task[object] | None] = ContextVar(
            "database_transaction_owner",
            default=None,
        )
        # Contention counters (in-memory only, never persisted)
        self._write_ops: int = 0
        self._read_ops: int = 0
        self._total_transactions: int = 0
        self._total_nested_transactions: int = 0
        self._last_operation_error_class: str | None = None
        self._cumulative_lock_wait_s: float = 0.0
        self._max_lock_wait_s: float = 0.0
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
        # Tracks which asyncio.Task issued ``BEGIN IMMEDIATE`` for
        # the active outermost transaction on this connection.
        # Used by ``vacuum()`` to refuse to run when the *current*
        # task is the lock holder (which would deadlock). Nested
        # detection in ``transaction()`` itself uses SQLite's
        # ``conn.in_transaction``, NOT this attribute.
        self._transaction_owner: ContextVar[asyncio.Task[object] | None] = ContextVar(
            "database_transaction_owner",
            default=None,
        )

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def connect(self) -> None:
        """Open the connection and set pragmas."""
        if self._conn is not None:
            raise DatabaseError("Database already connected")
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
        ``transaction()`` itself uses ``conn.in_transaction``,
        NOT this helper, because ``_transaction_owner`` is a
        per-task ContextVar and would misidentify shielded or
        ``create_task`` children as non-owners.
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
        if not self._in_transaction_context.get():
            raise DatabaseError(
                "Database writes require an active transaction; "
                "use 'async with db.transaction():'"
            )

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
        if self._current_task_owns_transaction():
            yield
            return

        t0 = time.monotonic()
        self._refresh_idle_connection_lock()
        async with self._connection_lock:
            elapsed = time.monotonic() - t0
            self._cumulative_lock_wait_s += elapsed
            if elapsed > self._max_lock_wait_s:
                self._max_lock_wait_s = elapsed
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
            return int(rowcount) if rowcount >= 0 else 0
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
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
            return int(rowcount) if rowcount >= 0 else 0
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
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
            return int(last_id)
        except DatabaseError:
            raise
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
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
            return list(rows)  # type: ignore[arg-type]
        except Exception as exc:
            self._last_operation_error_class = type(exc).__qualname__
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
        """
        return {
            "write_ops": self._write_ops,
            "read_ops": self._read_ops,
            "total_transactions": self._total_transactions,
            "total_nested_transactions": self._total_nested_transactions,
            "last_operation_error_class": self._last_operation_error_class,
            "cumulative_lock_wait_s": round(self._cumulative_lock_wait_s, 4),
            "max_lock_wait_s": round(self._max_lock_wait_s, 4),
        }

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all matching rows while holding the connection lock."""
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                rows = await cursor.fetchall()
                self._read_ops += 1
                return list(rows)  # type: ignore[arg-type]
            except Exception as exc:
                self._last_operation_error_class = type(exc).__qualname__
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
                return row  # type: ignore[return-value]
            except Exception as exc:
                self._last_operation_error_class = type(exc).__qualname__
                raise DatabaseError(f"Fetch one failed: {exc}") from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        """Execute a serialized write transaction.

        Uses ``BEGIN IMMEDIATE`` to serialize writers predictably.
        Repository methods must NOT call commit inside this context;
        the caller owns commit boundaries.

        Nesting semantics use SQLite's connection state
        (``conn.in_transaction``), NOT ``asyncio.current_task()``
        identity. This matters across ``asyncio.shield()`` and
        ``asyncio.create_task()`` boundaries: a wrapped coroutine
        entering ``transaction()`` while an outer caller already
        issued ``BEGIN IMMEDIATE`` will see ``in_transaction=True``
        and piggyback on the outer transaction's commit boundary,
        instead of failing with
        ``OperationalError: cannot start a transaction within
        a transaction`` or deadlocking on ``_connection_lock``.

        The outermost ``transaction()`` caller is the only one
        that acquires ``_connection_lock`` and issues
        ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``. Nested
        callers -- including task-spawned piggybackers -- simply
        yield and inherit the outer's commit boundary.
        """
        if self._conn is None:
            raise DatabaseError("Database not connected")

        # Fast path: piggyback on an existing transaction.
        # Reading conn.in_transaction does not require the lock --
        # it reflects SQLite's authoritative per-connection state,
        # which aiosqlite mutates only inside the worker thread
        # that serializes our SQL.
        if self._conn.in_transaction:
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
            # Re-check under the lock. Another task may have raced
            # between our initial check and acquiring the lock.
            if self._conn.in_transaction:
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
                raise DatabaseError(f"Begin transaction failed: {exc}") from exc
            try:
                yield
            except BaseException:
                await self._conn.rollback()
                raise
            else:
                await self._conn.commit()
            finally:
                self._in_transaction_context.reset(ctx_token)
                self._transaction_owner.reset(owner_token)
