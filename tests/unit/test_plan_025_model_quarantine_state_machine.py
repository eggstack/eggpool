"""Plan 025 — Model quarantine state machine tests.

Validates the bounded quarantine lifecycle: healthy → suspected →
quarantined → terminal_withdrawn, with TTL expiry, corroboration,
success clearing, and catalog reappearance clearing.

Run with::

    uv run pytest tests/unit/test_plan_025_model_quarantine_state_machine.py -v
"""

from __future__ import annotations

from eggpool.failure.quarantine import (
    EvidenceProvenance,
    ModelQuarantine,
    QuarantineState,
)


def _entry_args(
    *,
    provider_id: str = "openai",
    account_id: str = "acct-1",
    canonical_model_id: str = "gpt-4o",
    upstream_model_id: str | None = None,
    upstream_protocol: str = "openai",
) -> dict[str, str | None]:
    return dict(
        provider_id=provider_id,
        account_id=account_id,
        canonical_model_id=canonical_model_id,
        upstream_model_id=upstream_model_id,
        upstream_protocol=upstream_protocol,
    )


class TestQuarantineStateMachine:
    """State transitions and TTL expiry."""

    def test_healthy_by_default(self) -> None:
        q = ModelQuarantine()
        assert q.is_model_quarantined(**_entry_args()) is False

    def test_first_observation_creates_suspected(self) -> None:
        q = ModelQuarantine()
        entry = q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert entry.state == QuarantineState.SUSPECTED
        assert entry.observation_count == 1
        assert entry.first_observed == 1000.0
        assert entry.expiry == 1000.0 + q.suspected_ttl

    def test_suspected_is_quarantined(self) -> None:
        q = ModelQuarantine()
        assert q.is_model_quarantined(**_entry_args(), now=1000.0) is False
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert q.is_model_quarantined(**_entry_args(), now=1000.0) is True

    def test_second_observation_promotes_to_quarantined(self) -> None:
        q = ModelQuarantine(promotion_threshold=2)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entry = q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1001.0,
        )
        assert entry.state == QuarantineState.QUARANTINED
        assert entry.observation_count == 2
        assert entry.expiry == 1001.0 + q.quarantined_ttl

    def test_suspected_expiry_restores_healthy(self) -> None:
        q = ModelQuarantine(suspected_ttl=60.0)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert q.is_model_quarantined(**_entry_args(), now=1059.0) is True
        assert q.is_model_quarantined(**_entry_args(), now=1060.0) is False

    def test_quarantined_expiry_restores_healthy(self) -> None:
        q = ModelQuarantine(suspected_ttl=60.0, quarantined_ttl=120.0)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1001.0,
        )
        # Still quarantined before expiry
        assert q.is_model_quarantined(**_entry_args(), now=1120.0) is True
        # Expired
        assert q.is_model_quarantined(**_entry_args(), now=1121.0) is False

    def test_terminal_withdrawn_not_quarantined(self) -> None:
        q = ModelQuarantine()
        q.set_terminal_withdrawn(
            **_entry_args(),
            reason="catalog_absence",
            now=1000.0,
        )
        # Terminal is a separate routing concern, not quarantine
        assert q.is_model_quarantined(**_entry_args(), now=1000.0) is False

    def test_terminal_withdrawn_entry_exists(self) -> None:
        q = ModelQuarantine()
        entry = q.set_terminal_withdrawn(
            **_entry_args(),
            reason="catalog_absence",
            now=1000.0,
        )
        assert entry.state == QuarantineState.TERMINAL_WITHDRAWN
        assert entry.expiry is None


class TestQuarantineClearing:
    """Clearing quarantine on success and catalog reappearance."""

    def test_exact_key_success_clears_suspected(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert q.is_model_quarantined(**_entry_args(), now=1000.0) is True
        cleared = q.clear_exact_key(
            **_entry_args(),
            reason="successful_request",
            now=1001.0,
        )
        assert cleared is True
        assert q.is_model_quarantined(**_entry_args(), now=1001.0) is False

    def test_exact_key_success_clears_quarantined(self) -> None:
        q = ModelQuarantine(promotion_threshold=2)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1001.0,
        )
        assert q.is_model_quarantined(**_entry_args(), now=1002.0) is True
        cleared = q.clear_exact_key(**_entry_args(), now=1002.0)
        assert cleared is True
        assert q.is_model_quarantined(**_entry_args(), now=1002.0) is False

    def test_catalog_reappearance_clears(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        cleared = q.clear_authoritative_reappearance(**_entry_args(), now=1001.0)
        assert cleared is True
        assert q.is_model_quarantined(**_entry_args(), now=1001.0) is False

    def test_manual_clear(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        cleared = q.manual_clear(**_entry_args(), now=1001.0)
        assert cleared is True
        assert q.is_model_quarantined(**_entry_args(), now=1001.0) is False

    def test_clear_nonexistent_returns_false(self) -> None:
        q = ModelQuarantine()
        assert q.clear_exact_key(**_entry_args(), now=1000.0) is False

    def test_clear_healthy_returns_false(self) -> None:
        q = ModelQuarantine()
        entry = q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entry.state = QuarantineState.HEALTHY
        assert q.clear_exact_key(**_entry_args(), now=1001.0) is False


class TestQuarantineScoping:
    """Quarantine is properly scoped by provider/account/model/protocol."""

    def test_different_accounts_are_independent(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(account_id="acct-1"),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert (
            q.is_model_quarantined(**_entry_args(account_id="acct-1"), now=1000.0)
            is True
        )
        assert (
            q.is_model_quarantined(**_entry_args(account_id="acct-2"), now=1000.0)
            is False
        )

    def test_different_models_are_independent(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(canonical_model_id="gpt-4o"),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert (
            q.is_model_quarantined(
                **_entry_args(canonical_model_id="gpt-4o"), now=1000.0
            )
            is True
        )
        assert (
            q.is_model_quarantined(
                **_entry_args(canonical_model_id="claude-3"), now=1000.0
            )
            is False
        )

    def test_different_providers_are_independent(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(provider_id="openai"),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert (
            q.is_model_quarantined(**_entry_args(provider_id="openai"), now=1000.0)
            is True
        )
        assert (
            q.is_model_quarantined(**_entry_args(provider_id="anthropic"), now=1000.0)
            is False
        )

    def test_different_protocols_are_independent(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(upstream_protocol="openai"),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert (
            q.is_model_quarantined(
                **_entry_args(upstream_protocol="openai"), now=1000.0
            )
            is True
        )
        assert (
            q.is_model_quarantined(
                **_entry_args(upstream_protocol="anthropic"), now=1000.0
            )
            is False
        )

    def test_different_upstream_models_are_independent(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(upstream_model_id="gpt-4o-2024"),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        assert (
            q.is_model_quarantined(
                **_entry_args(upstream_model_id="gpt-4o-2024"), now=1000.0
            )
            is True
        )
        assert (
            q.is_model_quarantined(
                **_entry_args(upstream_model_id="gpt-4o-2025"), now=1000.0
            )
            is False
        )


class TestQuarantinePersistence:
    """Hydration, pruning, and listing."""

    def test_list_entries(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entries = q.list_entries(now=1000.0)
        assert len(entries) == 1
        assert entries[0].canonical_model_id == "gpt-4o"

    def test_list_excludes_expired(self) -> None:
        q = ModelQuarantine(suspected_ttl=60.0)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entries = q.list_entries(now=1061.0)
        assert len(entries) == 0

    def test_list_includes_expired_when_requested(self) -> None:
        q = ModelQuarantine(suspected_ttl=60.0)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entries = q.list_entries(include_expired=True, now=1061.0)
        assert len(entries) == 1

    def test_prune_expired(self) -> None:
        q = ModelQuarantine(suspected_ttl=60.0)
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        removed = q.prune_expired(now=1061.0)
        assert removed == 1
        assert q.is_model_quarantined(**_entry_args(), now=1061.0) is False

    def test_hydrate_entry(self) -> None:
        from eggpool.failure.quarantine import QuarantineEntry

        q = ModelQuarantine(suspected_ttl=60.0)
        entry = QuarantineEntry(
            state=QuarantineState.SUSPECTED,
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.MIGRATION_LEGACY,
            reason="migration",
            first_observed=1000.0,
            last_observed=1000.0,
            observation_count=1,
            expiry=1100.0,
        )
        q.hydrate_entry(entry, now=1050.0)
        assert q.is_model_quarantined(**_entry_args(), now=1050.0) is True

    def test_hydrate_expired_entry_skipped(self) -> None:
        from eggpool.failure.quarantine import QuarantineEntry

        q = ModelQuarantine()
        entry = QuarantineEntry(
            state=QuarantineState.SUSPECTED,
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.MIGRATION_LEGACY,
            reason="migration",
            first_observed=1000.0,
            last_observed=1000.0,
            observation_count=1,
            expiry=1050.0,
        )
        q.hydrate_entry(entry, now=1100.0)
        assert q.is_model_quarantined(**_entry_args(), now=1100.0) is False

    def test_get_entry(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            **_entry_args(),
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entry = q.get_entry(**_entry_args())
        assert entry is not None
        assert entry.canonical_model_id == "gpt-4o"

    def test_get_entry_nonexistent(self) -> None:
        q = ModelQuarantine()
        assert q.get_entry(**_entry_args()) is None
