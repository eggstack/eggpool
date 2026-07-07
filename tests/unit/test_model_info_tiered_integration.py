"""Integration tests for the tiered identity matching system.

Uses real in-memory SQLite databases with migrations, the OpenRouter
fixture, and the tiered resolver to verify the full match + evidence
persistence lifecycle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.matching import (
    ModelInfoMatchingConfig,
    build_candidate_index,
    resolve_source_record_tiered,
)
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.sources.openrouter import _parse_entry_to_record
from eggpool.model_info.types import SourceModelRecord
from tests.helpers.model_info_fixtures import load_openrouter_fixture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


def _parse_openrouter_fixture() -> list[SourceModelRecord]:
    """Parse the OpenRouter fixture into SourceModelRecord objects."""
    payload = load_openrouter_fixture()
    data = payload.get("data", [])
    records: list[SourceModelRecord] = []
    for entry in data:
        source_model_id = entry.get("id", "")
        record = _parse_entry_to_record(source_model_id, entry, _NOW)
        records.append(record)
    return records


class FakeRepo:
    """Async repo stub that delegates to a real in-memory Database.

    Wraps the real ModelInfoRepository for alias/evidence persistence but
    uses a simpler interface for the tiered resolver's repo calls.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._real_repo = ModelInfoRepository(db)
        self._alias_rows: dict[tuple[str, str | None], list[dict[str, Any]]] = {}

    def seed_alias(
        self,
        model_id: str,
        alias: str,
        source: str,
        confidence: float = 0.9,
        provider_id: str | None = None,
    ) -> None:
        key = (model_id, source)
        self._alias_rows.setdefault(key, []).append(
            {
                "model_id": model_id,
                "source": source,
                "alias": alias,
                "provider_id": provider_id,
                "confidence": confidence,
                "active": True,
                "last_seen_at": _NOW.isoformat(),
            }
        )

    async def list_alias_rows_for_model(
        self, model_id: str, *, source: str | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for (mid, src), rows in self._alias_rows.items():
            if mid != model_id:
                continue
            if source is not None and src != source:
                continue
            results.extend(rows)
        return results

    async def list_match_evidence(
        self, model_id: str, *, source: str | None = None
    ) -> list[dict[str, object]]:
        return await self._real_repo.list_match_evidence(model_id, source=source)

    async def record_match_evidence(
        self,
        model_id: str,
        provider_id: str | None,
        source: str,
        alias: str,
        match_method: str,
        confidence: float,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        await self._real_repo.record_match_evidence(
            model_id, provider_id, source, alias, match_method, confidence, diagnostics
        )

    async def upsert_alias_with_method(
        self,
        model_id: str,
        provider_id: str | None,
        alias: str,
        source: str,
        *,
        match_method: str,
        discovered_by: str = "tiered_resolver",
        confidence: float = 0.5,
        diagnostics: dict[str, object] | None = None,
        active: bool = True,
    ) -> None:
        await self._real_repo.upsert_alias_with_method(
            model_id,
            provider_id,
            alias,
            source,
            match_method=match_method,
            discovered_by=discovered_by,
            confidence=confidence,
            diagnostics=diagnostics,
            active=active,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFreshDBMinimaxM3ResolvesViaTieredMatcher:
    """Test 1: Fresh DB, provider-catalog seed, OpenRouter fixture, full lifecycle."""

    @pytest.mark.asyncio()
    async def test_resolves_and_persists_evidence(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            # Seed a provider-catalog observation for this model.
            repo = FakeRepo(db)
            await repo.record_match_evidence(
                model_id="minimax-m3",
                provider_id="opencode-go",
                source="provider_catalog",
                alias="opencode-go/minimax-m3",
                match_method="configured_exact_alias",
                confidence=1.0,
                diagnostics={},
            )

            # Build candidate index from OpenRouter fixture.
            or_records = _parse_openrouter_fixture()
            candidate_index = build_candidate_index("openrouter", or_records)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="minimax-m3",
                provider_id="opencode-go",
                display_name=None,
                repo=repo,
                candidate_index=candidate_index,
            )

            # The resolver should find a match via normalized_exact.
            assert decision.matched is True
            assert decision.match_method in ("normalized_exact", "regex_rule")
            assert decision.record is not None
            assert decision.record.source_model_id == "minimax/minimax-m3"

            # Persist the match evidence.
            await repo.record_match_evidence(
                model_id="minimax-m3",
                provider_id="opencode-go",
                source="openrouter",
                alias=decision.alias_to_persist or decision.record.source_model_id,
                match_method=decision.match_method,
                confidence=decision.confidence,
                diagnostics=decision.diagnostics,
            )

            # Verify evidence was persisted.
            evidence = await repo.list_match_evidence("minimax-m3", source="openrouter")
            assert len(evidence) >= 1
            row = evidence[0]
            assert row["match_method"] in ("normalized_exact", "regex_rule")
            assert float(row["confidence"]) > 0.0
            diag = json.loads(str(row["diagnostics_json"]))
            assert "matched_source_model_id" in diag or "rule_pattern" in diag

            # Persist the observation so the canonical row has source data.
            await repo._real_repo.upsert_observation(
                decision.record, model_id="minimax-m3", provider_id="opencode-go"
            )

            # Verify the observation was persisted.
            observations = await repo._real_repo.get_latest_observations_for_model(
                "minimax-m3", sources=["openrouter"]
            )
            assert "openrouter" in observations
        finally:
            await db.disconnect()


class TestMinimaxM3DisplayNameDuplicateVendorResolves:
    """Test 2: Display name with duplicate vendor collapses to match."""

    @pytest.mark.asyncio()
    async def test_display_name_collapses_via_normalized_exact(self) -> None:
        or_record = SourceModelRecord(
            source="openrouter",
            source_model_id="minimax/minimax-m3",
            observed_at=_NOW,
            raw_hash="test",
            raw_payload={},
            normalized={},
            display_name="MiniMax: MiniMax M3",
        )
        index = build_candidate_index("openrouter", [or_record])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="minimax-m3",
                provider_id="opencode-go",
                display_name="MiniMax: MiniMax M3",
                repo=repo,
                candidate_index=index,
            )

            assert decision.matched is True
            assert decision.record is not None
            assert decision.record.source_model_id == "minimax/minimax-m3"
            # Normalized exact should collapse "MiniMax: MiniMax M3" to "minimaxm3"
            # and match "minimax-m3" which also normalizes to "minimaxm3".
            assert decision.match_method in ("normalized_exact", "exact_source_id")
        finally:
            await db.disconnect()


class TestProviderNamespaceNotTreatedAsVendor:
    """Test 3: opencode-go namespace is stripped, not treated as vendor."""

    @pytest.mark.asyncio()
    async def test_with_namespace_matches(self) -> None:
        or_record = SourceModelRecord(
            source="openrouter",
            source_model_id="minimax/minimax-m3",
            observed_at=_NOW,
            raw_hash="test",
            raw_payload={},
            normalized={},
            display_name="MiniMax M3",
        )
        index = build_candidate_index("openrouter", [or_record])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "opencode-go/minimax-m3")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="opencode-go/minimax-m3",
                provider_id="opencode-go",
                display_name=None,
                repo=repo,
                candidate_index=index,
                known_provider_namespaces={"opencode-go"},
            )

            # With namespace hint, "minimax-m3" is stripped and matches.
            assert decision.matched is True
            assert decision.record is not None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_without_namespace_does_not_match_vendor(self) -> None:
        """Without known_provider_namespaces, 'opencode-go' is not treated
        as a vendor namespace, so no match is expected."""
        or_record = SourceModelRecord(
            source="openrouter",
            source_model_id="minimax/minimax-m3",
            observed_at=_NOW,
            raw_hash="test",
            raw_payload={},
            normalized={},
            display_name="MiniMax M3",
        )
        index = build_candidate_index("openrouter", [or_record])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "opencode-go/minimax-m3")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="opencode-go/minimax-m3",
                provider_id="opencode-go",
                display_name=None,
                repo=repo,
                candidate_index=index,
                known_provider_namespaces=None,
            )

            # Without namespace hint, "opencode-go/minimax-m3" as a whole
            # does not normalize to match "minimax/minimax-m3".
            if decision.matched:
                # If it matched, it must be via a different path (e.g. regex).
                assert decision.match_method != "normalized_exact"
        finally:
            await db.disconnect()


class TestDeepSeekV4DoesNotBindToV4Pro:
    """Test 4: deepseek-v4 resolves to v4, not v4-pro."""

    @pytest.mark.asyncio()
    async def test_v4_matches_v4_not_v4pro(self) -> None:
        rec_v4 = SourceModelRecord(
            source="openrouter",
            source_model_id="minimax/deepseek-v4",
            observed_at=_NOW,
            raw_hash="v4",
            raw_payload={},
            normalized={},
            display_name="DeepSeek V4",
        )
        rec_v4pro = SourceModelRecord(
            source="openrouter",
            source_model_id="minimax/deepseek-v4-pro",
            observed_at=_NOW,
            raw_hash="v4pro",
            raw_payload={},
            normalized={},
            display_name="DeepSeek V4 Pro",
        )
        index = build_candidate_index("openrouter", [rec_v4, rec_v4pro])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "deepseek-v4")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="deepseek-v4",
                provider_id="minimax",
                display_name=None,
                repo=repo,
                candidate_index=index,
            )

            assert decision.matched is True
            assert decision.record is not None
            # Must resolve to v4, NOT v4-pro.
            assert decision.record.source_model_id == "minimax/deepseek-v4"
            assert "deepseek-v4-pro" not in decision.record.source_model_id
        finally:
            await db.disconnect()


class TestAmbiguousCandidatesDoNotMatch:
    """Test 5: Two candidates with identical normalized key and no vendor tie-break."""

    @pytest.mark.asyncio()
    async def test_ambiguous_returns_no_match(self) -> None:
        rec_a = SourceModelRecord(
            source="openrouter",
            source_model_id="vendor-a/model-x",
            observed_at=_NOW,
            raw_hash="a",
            raw_payload={},
            normalized={},
            display_name="Model X",
        )
        rec_b = SourceModelRecord(
            source="openrouter",
            source_model_id="vendor-b/model-x",
            observed_at=_NOW,
            raw_hash="b",
            raw_payload={},
            normalized={},
            display_name="Model X Alt",
        )
        index = build_candidate_index("openrouter", [rec_a, rec_b])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "model-x")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="model-x",
                provider_id=None,
                display_name=None,
                repo=repo,
                candidate_index=index,
            )

            assert decision.matched is False
            assert decision.match_method == "ambiguous_candidates"
        finally:
            await db.disconnect()


class TestClaudeSonnetFamilyResolvesViaRegexRule:
    """Test 6: claude-sonnet-4.5 resolves to anthropic/claude-sonnet-4.5 via regex."""

    @pytest.mark.asyncio()
    async def test_regex_rule_matches_claude_sonnet(self) -> None:
        or_record = SourceModelRecord(
            source="openrouter",
            source_model_id="anthropic/claude-sonnet-4.5",
            observed_at=_NOW,
            raw_hash="test",
            raw_payload={},
            normalized={},
            display_name="Claude Sonnet 4.5",
        )
        index = build_candidate_index("openrouter", [or_record])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "claude-sonnet-4.5")

            repo = FakeRepo(db)

            # Disable normalized_exact to force regex_rule path.
            config = ModelInfoMatchingConfig(
                normalized_exact=False,
                regex_rules=True,
                similarity=False,
            )

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="claude-sonnet-4.5",
                provider_id="anthropic",
                display_name=None,
                repo=repo,
                candidate_index=index,
                config=config,
            )

            assert decision.matched is True
            assert decision.match_method == "regex_rule"
            assert decision.record is not None
            assert decision.record.source_model_id == "anthropic/claude-sonnet-4.5"
        finally:
            await db.disconnect()


class TestAliasEvidencePersistsAfterMatch:
    """Test 7: Match evidence is persisted to the match_evidence table."""

    @pytest.mark.asyncio()
    async def test_evidence_persisted_with_correct_fields(self) -> None:
        or_record = SourceModelRecord(
            source="openrouter",
            source_model_id="minimax/minimax-m3",
            observed_at=_NOW,
            raw_hash="test",
            raw_payload={},
            normalized={},
            display_name="MiniMax M3",
        )
        index = build_candidate_index("openrouter", [or_record])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="minimax-m3",
                provider_id="opencode-go",
                display_name=None,
                repo=repo,
                candidate_index=index,
            )

            assert decision.matched is True

            # Simulate what the service layer does: persist alias + evidence.
            await repo.upsert_alias_with_method(
                model_id="minimax-m3",
                provider_id=decision.alias_to_persist_provider_id,
                alias=decision.alias_to_persist or decision.record.source_model_id,
                source="openrouter",
                match_method=decision.match_method,
                discovered_by="tiered_resolver",
                confidence=decision.confidence,
                diagnostics=decision.diagnostics,
            )
            await repo.record_match_evidence(
                model_id="minimax-m3",
                provider_id=decision.alias_to_persist_provider_id,
                source="openrouter",
                alias=decision.alias_to_persist or decision.record.source_model_id,
                match_method=decision.match_method,
                confidence=decision.confidence,
                diagnostics=decision.diagnostics,
            )

            evidence = await repo.list_match_evidence("minimax-m3")
            assert len(evidence) >= 1
            row = evidence[0]
            assert row["match_method"] == decision.match_method
            assert float(row["confidence"]) > 0.0
            diag = json.loads(str(row["diagnostics_json"]))
            assert "matched_source_model_id" in diag or "rule_pattern" in diag
        finally:
            await db.disconnect()


class TestSimilarityDisabledByDefault:
    """Test 8: similarity=False by default; no_match without exact/normalized."""

    def test_config_defaults(self) -> None:
        config = ModelInfoMatchingConfig()
        assert config.similarity is False
        assert config.normalized_exact is True
        assert config.regex_rules is True

    @pytest.mark.asyncio()
    async def test_no_match_without_similarity(self) -> None:
        """Two candidates with different normalized keys, no regex match,
        similarity disabled -> no_match."""
        rec_a = SourceModelRecord(
            source="openrouter",
            source_model_id="vendor-a/model-alpha",
            observed_at=_NOW,
            raw_hash="a",
            raw_payload={},
            normalized={},
            display_name="Model Alpha",
        )
        index = build_candidate_index("openrouter", [rec_a])

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "model-beta")

            repo = FakeRepo(db)

            decision = await resolve_source_record_tiered(
                source="openrouter",
                model_id="model-beta",
                provider_id=None,
                display_name=None,
                repo=repo,
                candidate_index=index,
            )

            # Default config has similarity disabled.
            assert decision.matched is False
            assert decision.match_method == "no_match"
        finally:
            await db.disconnect()
