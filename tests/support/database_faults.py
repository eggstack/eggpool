"""Test-only fault installation at the database callable boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from eggpool.db.connection import Database


def fail_commit(
    monkeypatch: MonkeyPatch,
    db: Database,
    exc: Exception,
    *,
    commit_first: bool = False,
) -> None:
    """Make the private commit boundary fail, optionally after COMMIT."""

    original_commit = db._commit_connection
    fired = False

    async def injected_commit() -> None:
        nonlocal fired
        if fired:
            await original_commit()
            return
        fired = True
        if commit_first:
            await db.connection.commit()
        raise exc

    monkeypatch.setattr(db, "_commit_connection", injected_commit)


def fail_rollback(monkeypatch: MonkeyPatch, db: Database, exc: Exception) -> None:
    """Make the underlying SQLite rollback call fail."""

    original_rollback = db.connection.rollback
    fired = False

    async def injected_rollback() -> None:
        nonlocal fired
        if fired:
            await original_rollback()
            return
        fired = True
        raise exc

    monkeypatch.setattr(db.connection, "rollback", injected_rollback)
