"""Plan 020 Workstream F4 — Deterministic database outcome matrix.

Provides separate tests for each commit failure branch:
  1. Commit failure + confirmed rollback success → outcome="rolled_back"
  2. Commit failure + rollback failure → outcome="indeterminate"
  3. Commit failure + in_transaction=False → outcome="indeterminate"
  4. Connection invalidation persists after indeterminate state
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.errors import DatabaseCommitError, DatabaseConnectionInvalidatedError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# F4.1: Commit failure with confirmed rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_commit_failure_rollback_success(
    reload_harness: ReloadHarness,
) -> None:
    """F4.1: When commit fails but rollback succeeds, outcome is 'rolled_back'.

    The connection is clean and reusable after the rollback.
    """
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("simulated commit failure"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.rollback_attempted is True
    assert err.rollback_succeeded is True
    assert err.outcome == "rolled_back"
    # Connection is reusable.
    async with db.transaction():
        result = await db.execute_returning("SELECT 42")
        assert result is not None


# ---------------------------------------------------------------------------
# F4.2: Commit failure with indeterminate state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_commit_failure_indeterminate_state(
    reload_harness: ReloadHarness,
) -> None:
    """F4.2: When commit fails and rollback cannot determine state,
    outcome is 'indeterminate'.

    The connection is invalidated and must not be reused.
    """
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("diagnostic test failure"))
    db.set_test_inject_rollback_call(RuntimeError("rollback unavailable"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.rollback_attempted is True
    assert err.rollback_succeeded is False
    assert err.outcome == "indeterminate"
    assert err.connection_invalidated is True
    assert db._invalidated is True
    # Subsequent transaction raises DatabaseConnectionInvalidatedError.
    with pytest.raises(DatabaseConnectionInvalidatedError):
        async with db.transaction():
            await db.execute_returning("SELECT 1")


# ---------------------------------------------------------------------------
# F4.3: Diagnostic fields are always present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_commit_failure_diagnostic_fields(
    reload_harness: ReloadHarness,
) -> None:
    """F4.3: DatabaseCommitError always exposes all diagnostic fields."""
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("diagnostic completeness test"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert isinstance(err.rollback_attempted, bool)
    assert isinstance(err.rollback_succeeded, bool)
    assert isinstance(err.connection_invalidated, bool)
    assert err.outcome == "rolled_back"
    assert err.rollback_attempted is True


# ---------------------------------------------------------------------------
# F4.4: Connection state summary after invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_connection_state_after_invalidation(
    reload_harness: ReloadHarness,
) -> None:
    """F4.4: Database connection_state reports invalidation."""
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("state summary test"))
    db.set_test_inject_rollback_call(RuntimeError("rollback unavailable"))

    with pytest.raises(DatabaseCommitError):
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    state = db.diagnostics()
    assert isinstance(state, dict)
    assert "connection_state" in state
    assert "reconnect_required" in state

    assert db._invalidated is True
    assert state["reconnect_required"] is True
    assert state["connection_state"] == "invalidated"
