"""Plan 023 — State audit snapshot unit tests.

Validates that the ``RequestStateAuditSnapshot`` captures the right
surfaces and produces deterministic diffs.

Run with::

    uv run pytest tests/unit/test_plan_023_state_audit.py -v
"""

from __future__ import annotations

from tests.support.state_audit import (
    DurableFacts,
    RequestStateAuditSnapshot,
    RuntimeFacts,
    StateAuditDiff,
)

# ---------------------------------------------------------------------------
# DurableFacts
# ---------------------------------------------------------------------------


class TestDurableFacts:
    def test_default_values(self) -> None:
        facts = DurableFacts()
        assert facts.request_rows == 0
        assert facts.attempt_rows == 0
        assert facts.reservation_rows == 0

    def test_equality(self) -> None:
        a = DurableFacts(request_rows=5, attempt_rows=3)
        b = DurableFacts(request_rows=5, attempt_rows=3)
        assert a == b

    def test_inequality(self) -> None:
        a = DurableFacts(request_rows=5)
        b = DurableFacts(request_rows=6)
        assert a != b


# ---------------------------------------------------------------------------
# RuntimeFacts
# ---------------------------------------------------------------------------


class TestRuntimeFacts:
    def test_default_values(self) -> None:
        facts = RuntimeFacts()
        assert facts.active_request_count == 0
        assert facts.reservation_count == 0
        assert facts.db_invalidated is False

    def test_health_states_default_empty(self) -> None:
        facts = RuntimeFacts()
        assert facts.health_account_states == {}


# ---------------------------------------------------------------------------
# StateAuditDiff
# ---------------------------------------------------------------------------


class TestStateAuditDiff:
    def test_is_clean_when_empty(self) -> None:
        diff = StateAuditDiff()
        assert diff.is_clean

    def test_not_clean_with_runtime_changes(self) -> None:
        diff = StateAuditDiff(runtime_ownership_changes=["active_request_count: 0 → 1"])
        assert not diff.is_clean

    def test_not_clean_with_health_changes(self) -> None:
        diff = StateAuditDiff(health_changes=["health[acct1]: ok → cooldown"])
        assert not diff.is_clean

    def test_not_clean_with_db_changes(self) -> None:
        diff = StateAuditDiff(
            database_connection_changes=["db_invalidated: False → True"]
        )
        assert not diff.is_clean


# ---------------------------------------------------------------------------
# Snapshot diff
# ---------------------------------------------------------------------------


class TestSnapshotDiff:
    def test_identical_snapshots_produce_empty_diff(self) -> None:
        before = RequestStateAuditSnapshot(
            durable=DurableFacts(request_rows=1),
            runtime=RuntimeFacts(active_request_count=0),
        )
        after = RequestStateAuditSnapshot(
            durable=DurableFacts(request_rows=1),
            runtime=RuntimeFacts(active_request_count=0),
        )
        diff = before.diff(after)
        assert diff.is_clean
        assert not diff.request_history_changes
        assert not diff.runtime_ownership_changes

    def test_request_row_increase_detected(self) -> None:
        before = RequestStateAuditSnapshot(
            durable=DurableFacts(request_rows=0, attempt_rows=0),
            runtime=RuntimeFacts(),
        )
        after = RequestStateAuditSnapshot(
            durable=DurableFacts(request_rows=1, attempt_rows=1),
            runtime=RuntimeFacts(),
        )
        diff = before.diff(after)
        assert len(diff.request_history_changes) == 2
        assert any("request_rows" in c for c in diff.request_history_changes)
        assert any("attempt_rows" in c for c in diff.request_history_changes)

    def test_reservation_leak_detected(self) -> None:
        before = RequestStateAuditSnapshot(
            durable=DurableFacts(),
            runtime=RuntimeFacts(reservation_count=0),
        )
        after = RequestStateAuditSnapshot(
            durable=DurableFacts(),
            runtime=RuntimeFacts(reservation_count=1),
        )
        diff = before.diff(after)
        assert not diff.is_clean
        assert any("reservation_count" in c for c in diff.runtime_ownership_changes)

    def test_health_state_change_detected(self) -> None:
        before = RequestStateAuditSnapshot(
            durable=DurableFacts(),
            runtime=RuntimeFacts(
                health_account_states={"acct1": "ok"},
                health_circuit_states={"acct1": "CLOSED"},
            ),
        )
        after = RequestStateAuditSnapshot(
            durable=DurableFacts(),
            runtime=RuntimeFacts(
                health_account_states={"acct1": "cooldown"},
                health_circuit_states={"acct1": "OPEN"},
            ),
        )
        diff = before.diff(after)
        assert len(diff.health_changes) >= 2

    def test_db_invalidated_detected(self) -> None:
        before = RequestStateAuditSnapshot(
            durable=DurableFacts(),
            runtime=RuntimeFacts(db_invalidated=False, db_connection_state="connected"),
        )
        after = RequestStateAuditSnapshot(
            durable=DurableFacts(),
            runtime=RuntimeFacts(
                db_invalidated=True, db_connection_state="invalidated"
            ),
        )
        diff = before.diff(after)
        assert len(diff.database_connection_changes) == 2
