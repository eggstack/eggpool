"""Unit tests for model-info tiered identity matching.

Pins the 10 required scenarios from the matching plan using a fake
repo and inline SourceModelRecord fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eggpool.model_info.matching import (
    ModelInfoMatchingConfig,
    build_candidate_index,
    resolve_source_record_tiered,
)
from eggpool.model_info.types import SourceModelRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _record(
    source_model_id: str,
    *,
    display_name: str | None = None,
    source: str = "openrouter",
    confidence: float = 0.9,
) -> SourceModelRecord:
    return SourceModelRecord(
        source=source,
        source_model_id=source_model_id,
        observed_at=_NOW,
        raw_hash=source_model_id,
        raw_payload={},
        normalized={},
        display_name=display_name or source_model_id,
        confidence=confidence,
    )


class FakeRepo:
    """Minimal async repo stub for tiered resolver tests."""

    def __init__(self) -> None:
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

    async def get_aliases_for_model(
        self, model_id: str, *, source: str | None = None
    ) -> list[str]:
        rows = await self.list_alias_rows_for_model(model_id, source=source)
        return [r["alias"] for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfiguredAliasWinsOverNormalized:
    @pytest.mark.asyncio()
    async def test_tier0_wins(self) -> None:
        """A configured alias resolves at tier 0 even when normalized
        exact would also match."""
        or_record = _record("minimax/minimax-m3")
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()
        repo.seed_alias("minimax-m3", "minimax/minimax-m3", "openrouter")

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="minimax-m3",
            provider_id="opencode-go",
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.match_method == "configured_exact_alias"
        assert decision.record is or_record


class TestExactSourceIdMatch:
    @pytest.mark.asyncio()
    async def test_direct_source_model_id(self) -> None:
        """model_id == source_model_id is an exact hit at tier 1."""
        or_record = _record("claude-sonnet-4.5")
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="claude-sonnet-4.5",
            provider_id="anthropic",
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.match_method == "exact_source_id"
        assert decision.record is or_record


class TestNormalizedExactMinimax:
    @pytest.mark.asyncio()
    async def test_minimax_m3_matches_minimax_slash(self) -> None:
        """'minimax-m3' normalizes to 'minimaxm3', matching the model
        segment of 'minimax/minimax-m3'."""
        or_record = _record("minimax/minimax-m3")
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="minimax-m3",
            provider_id="opencode-go",
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.match_method == "normalized_exact"
        assert decision.record is or_record


class TestDisplayNameDuplicateVendorCollapse:
    @pytest.mark.asyncio()
    async def test_display_name_matches(self) -> None:
        """'MiniMax: MiniMax M3' display name normalizes the same as
        'minimax-m3' and matches via normalized exact."""
        or_record = _record("minimax/minimax-m3", display_name="MiniMax: MiniMax M3")
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="minimax-m3",
            provider_id="opencode-go",
            display_name="MiniMax: MiniMax M3",
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.match_method in ("normalized_exact", "exact_source_id")
        assert decision.record is or_record


class TestProviderNamespaceNotVendorNamespace:
    @pytest.mark.asyncio()
    async def test_opencode_go_namespace_stripped(self) -> None:
        """'opencode-go/minimax-m3' is NOT treated as a vendor match.
        The model segment 'minimax-m3' normalizes to 'minimaxm3' and
        matches via normalized exact."""
        or_record = _record("minimax/minimax-m3")
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()
        # Provider-catalog alias for the aggregator.
        repo.seed_alias(
            "opencode-go/minimax-m3",
            "opencode-go/minimax-m3",
            "provider_catalog",
        )

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="opencode-go/minimax-m3",
            provider_id="opencode-go",
            display_name=None,
            repo=repo,
            candidate_index=index,
            known_provider_namespaces={"opencode-go"},
        )
        assert decision.matched is True
        assert decision.record is or_record


class TestRegexFamilyRuleSafeVariant:
    @pytest.mark.asyncio()
    async def test_claude_sonnet_matches_same_family(self) -> None:
        """Regex rule matches 'claude-sonnet-4.5' to an anthropic record
        when variant tokens are compatible."""
        or_record = _record(
            "anthropic/claude-sonnet-4.5", display_name="Claude Sonnet 4.5"
        )
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="claude-sonnet-4.5",
            provider_id="anthropic",
            display_name=None,
            repo=repo,
            candidate_index=index,
            config=ModelInfoMatchingConfig(
                normalized_exact=False,
                regex_rules=True,
                similarity=False,
            ),
        )
        assert decision.matched is True
        assert decision.match_method == "regex_rule"
        assert decision.record is or_record


class TestSimilarityHighScoreAndGap:
    @pytest.mark.asyncio()
    async def test_similarity_accepted(self) -> None:
        """Similarity match accepted when score >= 0.92 and gap >= 0.05.

        Uses a slightly different model_id ('deepseek-v3-r20') that
        does not match any exact source ID or normalized key in the
        index, but is very close to 'deepseek-v3-r10'.  The vendor
        token 'deepseek' matches in both local and candidate.
        """
        or_record = _record("deepseek/deepseek-v3-r10")
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="deepseek-v3-r20",
            provider_id="deepseek",
            display_name=None,
            repo=repo,
            candidate_index=index,
            config=ModelInfoMatchingConfig(
                normalized_exact=False,
                regex_rules=False,
                similarity=True,
                similarity_threshold=0.92,
                similarity_min_gap=0.05,
            ),
        )
        assert decision.matched is True
        assert decision.match_method == "similarity_guarded"
        assert decision.record is or_record


class TestSimilarityVariantTokenRejection:
    @pytest.mark.asyncio()
    async def test_mini_variant_not_matching_non_mini(self) -> None:
        """'claude-sonnet-4.5-mini' should NOT match 'claude-sonnet-4.5'
        because 'mini' is a critical variant token."""
        or_record = _record(
            "anthropic/claude-sonnet-4.5", display_name="Claude Sonnet 4.5"
        )
        index = build_candidate_index("openrouter", [or_record])

        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="claude-sonnet-4.5-mini",
            provider_id="anthropic",
            display_name=None,
            repo=repo,
            candidate_index=index,
            config=ModelInfoMatchingConfig(
                normalized_exact=False,
                regex_rules=False,
                similarity=True,
                similarity_threshold=0.92,
                similarity_min_gap=0.05,
            ),
        )
        assert decision.matched is False
        assert decision.match_method in ("no_match", "ambiguous_candidates")


class TestAmbiguousCandidatesNoMatch:
    @pytest.mark.asyncio()
    async def test_multiple_plausible_candidates(self) -> None:
        """Multiple candidates with the same normalized key and no
        vendor tie-break produce ambiguous_candidates."""
        rec_a = _record("vendor-a/model-x", display_name="Model X")
        rec_b = _record("vendor-b/model-x", display_name="Model X Alt")
        index = build_candidate_index("openrouter", [rec_a, rec_b])

        repo = FakeRepo()

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


class TestSimilarityDisabledByDefault:
    def test_config_defaults(self) -> None:
        """ModelInfoMatchingConfig defaults to similarity=False."""
        config = ModelInfoMatchingConfig()
        assert config.similarity is False
        assert config.normalized_exact is True
        assert config.regex_rules is True
        assert config.similarity_threshold == 0.92
        assert config.similarity_min_gap == 0.05
        assert config.persist_discovered_aliases is True
        assert config.max_candidates_per_model == 20
