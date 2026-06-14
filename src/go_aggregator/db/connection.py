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
    """Async wrapper around aiosqlite with pragma configuration."""

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
        self._transaction_lock = asyncio.Lock()
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

    async def _wait_for_connection_access(self) -> None:
        """Wait for connection access if another task owns the transaction."""
        owner = getattr(self, "_transaction_owner", None)
        if owner is None:
            return
        if owner is asyncio.current_task():
            return
        # Another task owns it — wait for the lock, then release immediately
        async with self._transaction_lock:
            pass

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Execute a SQL statement and return the cursor."""
        await self._wait_for_connection_access()
        try:
            return await self.connection.execute(sql, params)  # type: ignore[return-value]
        except Exception as exc:
            raise DatabaseError(f"Execute failed: {exc}") from exc

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all matching rows."""
        await self._wait_for_connection_access()
        try:
            cursor = await self.connection.execute(sql, params)  # type: ignore[union-attr]
            rows = await cursor.fetchall()
            return list(rows)  # type: ignore[arg-type]
        except Exception as exc:
            raise DatabaseError(f"Fetch all failed: {exc}") from exc

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        """Fetch a single row or None."""
        await self._wait_for_connection_access()
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

        # If depth > 0 but different task, wait for lock then start new outer tx
        if depth > 0:
            # Wait for the lock, then release it and fall through to acquire it properly
            async with self._transaction_lock:
                pass
            # Now start a fresh outer transaction
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
                self._transaction_depth.reset(token)
                self._transaction_owner = None
            return

        async with self._transaction_lock:
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
                self._transaction_depth.reset(token)
                self._transaction_owner = None
