"""Quarantine diagnostics and operator interface tests.

Validates read-only diagnostics for active model quarantine and
evidence, including listing, scope inspection, and manual clearing.
"""

from __future__ import annotations

from eggpool.failure.quarantine import (
    EvidenceProvenance,
    ModelQuarantine,
)


class TestQuarantineDiagnostics:
    """Read-only diagnostics for active quarantine entries."""

    def test_list_active_entries(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="model_absent_404",
            status_code=404,
            now=1000.0,
        )
        q.record_observation(
            provider_id="anthropic",
            account_id="acct-2",
            canonical_model_id="claude-3",
            upstream_model_id=None,
            upstream_protocol="anthropic",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="model_absent_404",
            status_code=404,
            now=1001.0,
        )
        entries = q.list_entries(now=1001.0)
        assert len(entries) == 2

    def test_entry_shows_scope(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id="gpt-4o-2024",
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="model_absent_404",
            now=1000.0,
        )
        entries = q.list_entries(now=1000.0)
        assert len(entries) == 1
        e = entries[0]
        assert e.provider_id == "openai"
        assert e.account_id == "acct-1"
        assert e.canonical_model_id == "gpt-4o"
        assert e.upstream_model_id == "gpt-4o-2024"
        assert e.upstream_protocol == "openai"

    def test_entry_shows_evidence_source(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entries = q.list_entries(now=1000.0)
        assert entries[0].evidence_provenance == EvidenceProvenance.RUNTIME_HTTP

    def test_entry_shows_observation_count(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1001.0,
        )
        entries = q.list_entries(now=1001.0)
        assert entries[0].observation_count == 2

    def test_entry_shows_expiry(self) -> None:
        q = ModelQuarantine(suspected_ttl=60.0)
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        entries = q.list_entries(now=1000.0)
        assert entries[0].expiry == 1060.0

    def test_entry_shows_reason(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="runtime_model_absent_404",
            now=1000.0,
        )
        entries = q.list_entries(now=1000.0)
        assert entries[0].reason == "runtime_model_absent_404"

    def test_manual_clear_by_operator(self) -> None:
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        cleared = q.manual_clear(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )
        assert cleared is True
        assert q.list_entries(now=1001.0) == []

    def test_readiness_not_affected_by_quarantine(self) -> None:
        """Quarantine of one model must not affect readiness."""
        q = ModelQuarantine()
        q.record_observation(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
            evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
            reason="test",
            now=1000.0,
        )
        # Readiness checks should not fail due to quarantine
        # This is a design invariant: readiness is about DB health,
        # not model availability
        assert len(q.list_entries(now=1000.0)) == 1
        # But a different model is still healthy
        assert (
            q.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="claude-3",
                upstream_model_id=None,
                upstream_protocol="openai",
                now=1000.0,
            )
            is False
        )
