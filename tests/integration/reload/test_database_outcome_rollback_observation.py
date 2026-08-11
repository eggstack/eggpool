"""Deterministic database commit-outcome closure tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.errors import DatabaseCommitError, DatabaseConnectionInvalidatedError
from tests.support.database_faults import fail_commit

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_commit_failure_driver_reports_no_active_transaction(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A false pre-rollback transaction observation is indeterminate."""
    db = reload_harness.db
    fail_commit(monkeypatch, db, RuntimeError("commit lost"), commit_first=True)

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    error = exc_info.value
    assert error.outcome == "indeterminate"
    assert error.rollback_attempted is True
    assert error.rollback_succeeded is False
    assert error.connection_invalidated is True
    with pytest.raises(DatabaseConnectionInvalidatedError):
        async with db.transaction():
            await db.execute_returning("SELECT 1")
