"""SQLite connection manager using aiosqlite."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

from go_aggregator.errors import DatabaseError


class _RollbackProbeError(Exception):
    """Sentinel exception for probe_writable to roll back without logging."""


class Database:
    """Async wrapper around aiosqlite with pragma configuration.

    All SQL operations are serialized through a single connection lock.
    Transaction ownership is tracked per-task to allow nested calls
    within the same task while preventing child tasks from inheriting
    transaction ownership.
    """

    def __init__(
        self,
        path: str,
        busy_timeout_ms: int = 5000,
        wal: bool = True,
        synchronous: str = "NORMAL",
    ) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._wal = wal
        self._synchronous = synchronous
        self._conn: aiosqlite.Connection | None = None
        self._connection_lock = asyncio.Lock()
        self._transaction_depth: ContextVar[int] = ContextVar(
            "database_transaction_depth",
            default=0,
        )
        self._transaction_owner: asyncio.Task[object] | None = None

    async def connect(self) -> None:
        """Open the connection and set pragmas."""
        try:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            if self._wal:
                await self._conn.execute("PRAGMA journal_mode = WAL")
            await self._conn.execute(f"PRAGMA synchronous = {self._synchronous}")
            await self._conn.commit()
        except Exception as exc:
            raise DatabaseError(f"Failed to connect to database: {exc}") from exc

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
        """Check if the current asyncio task owns the active transaction."""
        return (
            self._transaction_owner is not None
            and self._transaction_owner is asyncio.current_task()
            and self._transaction_depth.get() > 0
        )

    @asynccontextmanager
    async def _connection_access(self) -> AsyncGenerator[None]:
        """Acquire the connection lock for a SQL operation.

        If the current task already owns a transaction, the lock is
        already held and this is a no-op.  Otherwise the lock is
        acquired for the duration of the ``yield``.
        """
        if self._current_task_owns_transaction():
            yield
            return

        async with self._connection_lock:
            yield

    async def probe_writable(self) -> bool:
        """Probe the database for write access using a transaction.

        The transaction is always rolled back; returns True if the
        insert succeeded, False otherwise.
        """
        try:
            async with self.transaction():
                await self.execute(
                    "INSERT INTO health_probe (probe_at) VALUES (CURRENT_TIMESTAMP)"
                )
                raise _RollbackProbeError
        except _RollbackProbeError:
            return True
        except Exception:
            return False

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Execute a SQL statement and return the cursor.

        Prefer the explicit helpers :meth:`execute_write`,
        :meth:`execute_insert`, or :meth:`execute_returning`.  This
        method exists for transaction-internal use where the caller
        needs the full cursor object.  Callers MUST consume the cursor
        before yielding control if they are not inside a transaction
        (the connection lock is released after this method returns).
        Within a transaction the lock is already held for the entire
        outer transaction lifetime, so the cursor may be safely
        consumed anywhere inside the ``async with db.transaction():``
        block.
        """
        async with self._connection_access():
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

        Acquires the connection lock for the duration of the statement
        if no transaction is owned by the current task; otherwise the
        call is a no-op with respect to lock acquisition.  The cursor
        is fully consumed before this method returns, so the returned
        rowcount is always valid.
        """
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                return int(cursor.rowcount or 0)
            except Exception as exc:
                raise DatabaseError(f"Execute write failed: {exc}") from exc

    async def execute_insert(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """Execute an INSERT and return lastrowid.

        Raises ``DatabaseError`` if the INSERT did not produce a
        ``lastrowid`` (for example, against a table that lacks an
        INTEGER PRIMARY KEY).  Acquires the connection lock for the
        duration of the statement when no transaction is owned.
        """
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                last_id = cursor.lastrowid
                if last_id is None:
                    raise DatabaseError("INSERT did not return lastrowid")
                return int(last_id)
            except DatabaseError:
                raise
            except Exception as exc:
                raise DatabaseError(f"Execute insert failed: {exc}") from exc

    async def execute_returning(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[aiosqlite.Row]:
        """Execute a statement (typically ``UPDATE ... RETURNING``) and
        return all rows.

        Acquires the connection lock for the duration of the fetch, so
        the returned rows are guaranteed to be observed under the same
        lock acquisition as the underlying statement.  When called
        inside a transaction the call is a no-op with respect to lock
        acquisition.
        """
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                rows = await cursor.fetchall()
                return list(rows)  # type: ignore[arg-type]
            except Exception as exc:
                raise DatabaseError(f"Execute returning failed: {exc}") from exc

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all matching rows while holding the connection lock."""
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                rows = await cursor.fetchall()
                return list(rows)  # type: ignore[arg-type]
            except Exception as exc:
                raise DatabaseError(f"Fetch all failed: {exc}") from exc

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        """Fetch a single row or None while holding the connection lock."""
        async with self._connection_access():
            try:
                cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
                row = await cursor.fetchone()
                return row  # type: ignore[return-value]
            except Exception as exc:
                raise DatabaseError(f"Fetch one failed: {exc}") from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        """Execute a serialized write transaction.

        Uses BEGIN IMMEDIATE to serialize writers predictably.
        Repository methods must NOT call commit inside this context;
        the caller owns commit boundaries.

        Supports nesting within the same task context: inner transactions
        inherit the outer commit boundary. Different tasks always get their
        own outer transaction.
        """
        depth = self._transaction_depth.get()
        owner = asyncio.current_task()

        # Nested detection: depth > 0 AND same task owns it
        if depth > 0 and self._transaction_owner is owner:
            token = self._transaction_depth.set(depth + 1)
            try:
                yield
            finally:
                self._transaction_depth.reset(token)
            return

        # Outer transaction: hold the connection lock for the entire
        # transaction lifetime so no other task can interleave SQL.
        async with self._connection_lock:
            token = self._transaction_depth.set(1)
            self._transaction_owner = owner
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    await self.connection.rollback()
                    raise
                else:
                    await self.connection.commit()
            finally:
                self._transaction_owner = None
                self._transaction_depth.reset(token)
