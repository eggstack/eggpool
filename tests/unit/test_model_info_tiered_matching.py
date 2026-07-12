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
        assert config.deployment_suffix_normalized_exact is True
        assert config.release_suffix_normalized_exact is True
        assert config.similarity_threshold == 0.92
        assert config.similarity_min_gap == 0.05
        assert config.persist_discovered_aliases is True
        assert config.max_candidates_per_model == 20


# ---------------------------------------------------------------------------
# Deployment-suffix tier (2b)
# ---------------------------------------------------------------------------


class TestDeploymentSuffixHighspeedMinimax:
    """Suite covering `MiniMax-M{x}-highspeed` resolving to base
    `minimax/minimax-m{x}` OpenRouter IDs through the new tier 2b."""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("variant_id", "expected_source_id"),
        [
            ("MiniMax-M2.1-highspeed", "minimax/minimax-m2.1"),
            ("MiniMax-M2.5-highspeed", "minimax/minimax-m2.5"),
            ("MiniMax-M2.7-highspeed", "minimax/minimax-m2.7"),
            ("MiniMax-M2.7-fast", "minimax/minimax-m2.7"),
            ("MiniMax-M2.7-turbo", "minimax/minimax-m2.7"),
            ("MiniMax-M2.7-lowlatency", "minimax/minimax-m2.7"),
            ("MiniMax-M2.7-lowlat", "minimax/minimax-m2.7"),
            (
                "minimax/MiniMax-M2.7-highspeed",
                "minimax/minimax-m2.7",
            ),
        ],
    )
    async def test_highspeed_strips_to_base_minimax(
        self, variant_id: str, expected_source_id: str
    ) -> None:
        records = [
            _record("minimax/minimax-m2.1"),
            _record("minimax/minimax-m2.5"),
            _record("minimax/minimax-m2.7"),
        ]
        index = build_candidate_index("openrouter", records)
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id=variant_id,
            provider_id="minimax",
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.match_method == "deployment_suffix_normalized_exact"
        assert decision.record is not None
        assert decision.record.source_model_id == expected_source_id


class TestDeploymentSuffixDoesNotStripSemanticVariants:
    """Safety: semantic variant tokens (pro, mini, flash, lite,
    plus, code, preview, etc.) must NEVER be stripped."""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "variant_id",
        [
            "MiniMax-M2.7-pro",
            "MiniMax-M2.7-mini",
            "MiniMax-M2.7-flash",
            "MiniMax-M2.7-lite",
            "mimo-v2.5-pro",
            "qwen3.7-plus",
            "kimi-k2.7-code",
            "hy3-preview",
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "MiniMax-M2.7-thinking",
        ],
    )
    async def test_no_strip_for_semantic_variant(self, variant_id: str) -> None:
        record = _record("minimax/minimax-m2.7")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id=variant_id,
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


class TestDeploymentSuffixAmbiguityGuard:
    """When multiple source candidates share the stripped normalized
    base, the resolver must NOT silently pick one — it must return
    no_match (or ambiguous_deployment_suffix_candidates)."""

    @pytest.mark.asyncio()
    async def test_two_candidate_base_keys_does_not_match(self) -> None:
        # Both records normalize (after strip) to the same base key
        # ``minimaxminimaxm27``.  Tier 2b cannot break the tie without
        # semantic info -- it must surface no-match rather than guess.
        records = [
            _record("minimax/minimax-m2.7"),
            _record("minimax/MiniMax M2.7"),  # casing/space variant
        ]
        index = build_candidate_index("openrouter", records)
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="MiniMax-M2.7-highspeed",
            provider_id="minimax",
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False
        assert decision.match_method in (
            "no_match",
            "ambiguous_deployment_suffix_candidates",
            "ambiguous_candidates",
        )


class TestDeploymentSuffixPersistsAlias:
    """Tier 2b matches must propagate the discovered alias through the
    resolver's MatchDecision.alias_to_persist contract."""

    @pytest.mark.asyncio()
    async def test_alias_to_persist_is_base_source_id(self) -> None:
        record = _record("minimax/minimax-m2.7")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="MiniMax-M2.7-highspeed",
            provider_id="minimax",
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.alias_to_persist == "minimax/minimax-m2.7"
        assert decision.diagnostics.get("stripped_variant") == "MiniMax-M2.7"


class TestDeploymentSuffixOffByConfig:
    """Disabling the deployment-suffix tier via config must prevent
    highspeed variants from binding to base source IDs."""

    @pytest.mark.asyncio()
    async def test_tier_disabled_returns_no_match(self) -> None:
        record = _record("minimax/minimax-m2.7")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        config = ModelInfoMatchingConfig(
            deployment_suffix_normalized_exact=False,
        )
        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="MiniMax-M2.7-highspeed",
            provider_id="minimax",
            display_name=None,
            repo=repo,
            candidate_index=index,
            config=config,
        )
        assert decision.matched is False


class TestReleaseSuffixNormalizedExact:
    """Dated source variants may resolve only when the base is unique."""

    @pytest.mark.asyncio()
    async def test_qwen_dated_variant_matches_stable_provider_alias(self) -> None:
        record = _record("qwen/qwen3.5-plus-02-15")
        index = build_candidate_index("openrouter", [record])
        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="qwen3.5-plus",
            provider_id="opencode-go",
            display_name=None,
            repo=FakeRepo(),
            candidate_index=index,
        )
        assert decision.matched is True
        assert decision.match_method == "release_suffix_normalized_exact"
        assert decision.record is record

    @pytest.mark.asyncio()
    async def test_ambiguous_dated_variants_do_not_guess(self) -> None:
        records = [
            _record("qwen/qwen3.5-plus-02-15"),
            _record("qwen/qwen3.5-plus-04-20"),
        ]
        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="qwen3.5-plus",
            provider_id="opencode-go",
            display_name=None,
            repo=FakeRepo(),
            candidate_index=build_candidate_index("openrouter", records),
        )
        assert decision.matched is False
        assert decision.match_method == "ambiguous_release_suffix_candidates"

    @pytest.mark.asyncio()
    async def test_semantic_variant_is_not_created_by_date_fallback(self) -> None:
        record = _record("qwen/qwen3.5-plus-02-15")
        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="qwen3.5",
            provider_id="opencode-go",
            display_name=None,
            repo=FakeRepo(),
            candidate_index=build_candidate_index("openrouter", [record]),
        )
        assert decision.matched is False
