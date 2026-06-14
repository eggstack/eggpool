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

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Execute a SQL statement and return the cursor."""
        try:
            return await self.connection.execute(sql, params)  # type: ignore[return-value]
        except Exception as exc:
            raise DatabaseError(f"Execute failed: {exc}") from exc

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all matching rows."""
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
        inherit the outer commit boundary.
        """
        depth = self._transaction_depth.get()
        if depth > 0:
            token = self._transaction_depth.set(depth + 1)
            try:
                yield
            finally:
                self._transaction_depth.reset(token)
            return

        async with self._transaction_lock:
            token = self._transaction_depth.set(1)
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
