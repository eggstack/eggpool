"""Quarantine hydration tests.

Validates that quarantine entries can be hydrated from durable storage,
that expired entries are skipped during hydration, and that the state
machine reproduces the same unexpired state after restart.
"""

from __future__ import annotations

from eggpool.failure.quarantine import (
    EvidenceProvenance,
    ModelQuarantine,
    QuarantineEntry,
    QuarantineState,
)


def _make_entry(
    *,
    state: QuarantineState = QuarantineState.SUSPECTED,
    provider_id: str = "openai",
    account_id: str = "acct-1",
    canonical_model_id: str = "gpt-4o",
    upstream_model_id: str | None = None,
    upstream_protocol: str = "openai",
    first_observed: float = 1000.0,
    observation_count: int = 1,
    expiry: float | None = 1100.0,
    evidence_provenance: EvidenceProvenance = EvidenceProvenance.MIGRATION_LEGACY,
) -> QuarantineEntry:
    return QuarantineEntry(
        state=state,
        provider_id=provider_id,
        account_id=account_id,
        canonical_model_id=canonical_model_id,
        upstream_model_id=upstream_model_id,
        upstream_protocol=upstream_protocol,
        evidence_provenance=evidence_provenance,
        reason="hydration_test",
        first_observed=first_observed,
        last_observed=first_observed,
        observation_count=observation_count,
        expiry=expiry,
    )


class TestQuarantineHydration:
    """Hydration from durable storage."""

    def test_hydrate_suspected_entry(self) -> None:
        q = ModelQuarantine()
        entry = _make_entry(state=QuarantineState.SUSPECTED, expiry=1100.0)
        q.hydrate_entry(entry, now=1050.0)
        assert (
            q.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1050.0,
            )
            is True
        )

    def test_hydrate_quarantined_entry(self) -> None:
        q = ModelQuarantine()
        entry = _make_entry(state=QuarantineState.QUARANTINED, expiry=1200.0)
        q.hydrate_entry(entry, now=1050.0)
        assert (
            q.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1050.0,
            )
            is True
        )

    def test_hydrate_expired_entry_skipped(self) -> None:
        q = ModelQuarantine()
        entry = _make_entry(state=QuarantineState.SUSPECTED, expiry=1050.0)
        q.hydrate_entry(entry, now=1100.0)
        assert (
            q.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1100.0,
            )
            is False
        )

    def test_hydrate_terminal_entry_not_quarantined(self) -> None:
        q = ModelQuarantine()
        entry = _make_entry(
            state=QuarantineState.TERMINAL_WITHDRAWN,
            expiry=None,
        )
        q.hydrate_entry(entry, now=1050.0)
        # Terminal is a routing concern, not quarantine
        assert (
            q.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1050.0,
            )
            is False
        )
        # Terminal entries still appear in list_entries (diagnostic visibility)
        entries = q.list_entries(now=1050.0)
        assert len(entries) == 1
        assert entries[0].state == QuarantineState.TERMINAL_WITHDRAWN

    def test_hydrate_healthy_entry_noop(self) -> None:
        q = ModelQuarantine()
        entry = _make_entry(state=QuarantineState.HEALTHY, expiry=1100.0)
        q.hydrate_entry(entry, now=1050.0)
        assert q.list_entries(now=1050.0) == []

    def test_restart_reproduces_unexpired_state(self) -> None:
        """Simulate restart: hydrate entries, verify they remain active."""
        q = ModelQuarantine()
        entry = _make_entry(state=QuarantineState.SUSPECTED, expiry=1100.0)
        q.hydrate_entry(entry, now=1050.0)

        # Simulate process restart at t=1050
        q2 = ModelQuarantine()
        hydrated_entries = q.list_entries(now=1050.0)
        for e in hydrated_entries:
            q2.hydrate_entry(e, now=1050.0)

        assert (
            q2.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1050.0,
            )
            is True
        )

    def test_expired_entries_not_reappearing_after_restart(self) -> None:
        """Expired entries must not reappear after restart."""
        q = ModelQuarantine()
        entry = _make_entry(state=QuarantineState.SUSPECTED, expiry=1050.0)
        q.hydrate_entry(entry, now=1100.0)

        # After restart at t=1100, expired entry should not be hydrated
        assert q.list_entries(now=1100.0) == []


class TestHydrationDoesNotDemoteRuntimeState:
    """A stale durable row must not downgrade newer in-memory state."""

    def test_stale_suspected_row_does_not_demote_quarantined(self) -> None:
        q = ModelQuarantine()
        # Runtime observations promoted the entry to QUARANTINED.
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="http_404",
            now=1000.0,
        )
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="http_404",
            now=1001.0,
        )
        assert (
            q.get_entry("openai", "acct-1", "gpt-4o", None, "openai").state
            is QuarantineState.QUARANTINED
        )

        # A stale durable SUSPECTED row (count=1) arrives late.
        stale = _make_entry(
            state=QuarantineState.SUSPECTED, observation_count=1, expiry=1200.0
        )
        q.hydrate_entry(stale, now=1002.0)

        entry = q.get_entry("openai", "acct-1", "gpt-4o", None, "openai")
        assert entry.state is QuarantineState.QUARANTINED

    def test_more_advanced_durable_row_replaces_resident(self) -> None:
        q = ModelQuarantine()
        resident = _make_entry(
            state=QuarantineState.SUSPECTED, observation_count=1, expiry=1200.0
        )
        q.hydrate_entry(resident, now=1050.0)

        advanced = _make_entry(
            state=QuarantineState.QUARANTINED, observation_count=3, expiry=1300.0
        )
        q.hydrate_entry(advanced, now=1050.0)

        entry = q.get_entry("openai", "acct-1", "gpt-4o", None, "openai")
        assert entry.state is QuarantineState.QUARANTINED
        assert entry.observation_count == 3

    def test_runtime_cleared_entry_survives_late_hydration(self) -> None:
        q = ModelQuarantine()
        durable = _make_entry(
            state=QuarantineState.QUARANTINED, observation_count=5, expiry=1300.0
        )
        # Runtime cleared the entry before hydration ran.
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="http_404",
            now=1000.0,
        )
        q.clear_exact_key(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            reason="successful_request",
            now=1002.0,
        )

        q.hydrate_entry(durable, now=1003.0)

        entry = q.get_entry("openai", "acct-1", "gpt-4o", None, "openai")
        assert entry is not None
        assert entry.state is QuarantineState.HEALTHY


class TestLegacyMigration:
    """Legacy model_unavailable rows migrate as migration_legacy."""

    def test_legacy_entry_hydrated_as_migration_legacy(self) -> None:
        q = ModelQuarantine()
        entry = _make_entry(
            state=QuarantineState.SUSPECTED,
            evidence_provenance=EvidenceProvenance.MIGRATION_LEGACY,
            expiry=1100.0,
        )
        q.hydrate_entry(entry, now=1050.0)
        hydrated = q.list_entries(now=1050.0)
        assert len(hydrated) == 1
        assert hydrated[0].evidence_provenance == EvidenceProvenance.MIGRATION_LEGACY

    def test_explicit_operator_disable_remains_terminal(self) -> None:
        """Operator-disabled models must remain terminal even if legacy."""
        q = ModelQuarantine()
        entry = _make_entry(
            state=QuarantineState.TERMINAL_WITHDRAWN,
            evidence_provenance=EvidenceProvenance.OPERATOR_ACTION,
            expiry=None,
        )
        q.hydrate_entry(entry, now=1050.0)
        # Terminal entries are not quarantined but still visible in list_entries
        assert (
            q.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1050.0,
            )
            is False
        )
        entries = q.list_entries(now=1050.0)
        assert len(entries) == 1
        assert entries[0].evidence_provenance == EvidenceProvenance.OPERATOR_ACTION
