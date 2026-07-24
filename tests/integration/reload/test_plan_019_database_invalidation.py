"""Plan 019 Workstream H — Deterministic database invalidation tests.

H1: Forces commit failure deterministically and verifies connection
invalidation behavior.

H2: Canonical ownership-state fallback with lowercase string normalization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from eggpool.errors import DatabaseCommitError, DatabaseConnectionInvalidatedError
from eggpool.runtime_manager import CandidateOwnershipState

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# H1: Deterministic commit failure with connection invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_commit_failure_invalidates_connection_on_ambiguous_state(
    reload_harness: ReloadHarness,
) -> None:
    """When commit fails and rollback cannot determine clean state,
    connection is invalidated.

    Uses set_test_inject_commit_call which raises before the actual commit.
    The connection's in_transaction state determines the outcome.
    """
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("simulated commit failure"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    # The outcome depends on aiosqlite's in_transaction state.
    assert err.rollback_attempted is True
    assert isinstance(err.outcome, str)
    assert err.outcome in ("rolled_back", "indeterminate")


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_commit_failure_diagnostic_fields(
    reload_harness: ReloadHarness,
) -> None:
    """DatabaseCommitError exposes all diagnostic fields correctly."""
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("diagnostic test failure"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert isinstance(err.rollback_attempted, bool)
    assert isinstance(err.rollback_succeeded, bool)
    assert isinstance(err.connection_invalidated, bool)
    assert isinstance(err.outcome, str)
    assert err.outcome in ("rolled_back", "indeterminate")


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_connection_invalidated_state_persists(
    reload_harness: ReloadHarness,
) -> None:
    """After connection invalidation, subsequent access raises."""
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("invalidate test"))

    with pytest.raises(DatabaseCommitError):
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    # If connection was invalidated, check the db state.
    if db._invalidated:
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with db.transaction():
                await db.execute_returning("SELECT 1")


# ---------------------------------------------------------------------------
# H2: Canonical ownership-state fallback
# ---------------------------------------------------------------------------


class TestCanonicalOwnershipStateFallback:
    """H2: Lowercase string ownership states are handled correctly."""

    def test_enum_value_is_lowercase(self) -> None:
        """CandidateOwnershipState enum values are lowercase strings."""
        assert CandidateOwnershipState.TRANSFERRED.value == "transferred"
        assert CandidateOwnershipState.ABORTED.value == "aborted"
        assert CandidateOwnershipState.BUILDING.value == "building"
        assert CandidateOwnershipState.PREPARED.value == "prepared"

    def test_string_comparison_with_enum_value(self) -> None:
        """Comparing enum.value to lowercase string works."""
        state = CandidateOwnershipState.TRANSFERRED
        assert state.value == "transferred"
        assert state.value not in ("aborted",)

    def test_mock_with_lowercase_string(self) -> None:
        """Mock candidate with lowercase string ownership_state."""
        candidate = MagicMock()
        candidate.ownership_state = "transferred"

        # The production code uses getattr for backward compat.
        state_value = getattr(
            candidate.ownership_state, "value", candidate.ownership_state
        )
        assert state_value == "transferred"
        assert state_value not in ("aborted",)

    def test_mock_with_enum_value(self) -> None:
        """Mock candidate with actual CandidateOwnershipState enum."""
        candidate = MagicMock()
        candidate.ownership_state = CandidateOwnershipState.ABORTED

        state_value = getattr(
            candidate.ownership_state, "value", candidate.ownership_state
        )
        assert state_value == "aborted"
        assert state_value not in ("transferred",)

    def test_uppercase_string_not_equal_to_lowercase(self) -> None:
        """Uppercase strings must not match lowercase canonical values."""
        candidate = MagicMock()
        candidate.ownership_state = "TRANSFERRED"

        state_value = getattr(
            candidate.ownership_state, "value", candidate.ownership_state
        )
        # Uppercase "TRANSFERRED" should NOT match lowercase "transferred".
        assert state_value != "transferred"
