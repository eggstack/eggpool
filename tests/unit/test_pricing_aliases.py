"""Tests for the deterministic pricing alias registry."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from eggpool.catalog.pricing_aliases import (
    ALIAS_CONFIDENCE_AMBIGUOUS_SKIP,
    ALIAS_CONFIDENCE_CURATED_ALIAS,
    ALIAS_CONFIDENCE_EXACT,
    AliasLookupResult,
    PricingAlias,
    PricingAliasResolver,
    seed_default_aliases,
)


@dataclass
class _FakeRow:
    provider_id: str
    upstream_model_id: str
    catalog_source: str
    catalog_model_id: str
    confidence: str
    notes: str | None = None

    def __getitem__(self, key: str) -> str | None:
        return getattr(self, key)


class _FakeDB:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows
        self._writes: list[tuple[str, tuple]] = []
        self.transaction_count = 0

    class _Transaction:
        def __init__(self, db: _FakeDB) -> None:
            self._db = db

        async def __aenter__(self) -> None:
            self._db.transaction_count += 1

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

    def transaction(self) -> _Transaction:
        return self._Transaction(self)

    async def fetch_all(self, query: str) -> list[_FakeRow]:
        return list(self._rows)

    async def fetch_one(self, query: str, params: tuple) -> _FakeRow | None:
        provider_id, upstream_model_id, catalog_source = params
        for row in self._rows:
            if (
                row.provider_id == provider_id
                and row.upstream_model_id == upstream_model_id
                and row.catalog_source == catalog_source
            ):
                return row
        return None

    async def execute_write(self, query: str, params: tuple) -> None:
        self._writes.append((query, params))


def _alias(
    *,
    provider_id: str = "opencode-go",
    upstream_model_id: str = "mimo-v2.5",
    catalog_source: str = "openrouter",
    catalog_model_id: str = "xiaomi/mimo-v2.5",
    confidence: str = ALIAS_CONFIDENCE_CURATED_ALIAS,
) -> PricingAlias:
    return PricingAlias(
        provider_id=provider_id,
        upstream_model_id=upstream_model_id,
        catalog_source=catalog_source,
        catalog_model_id=catalog_model_id,
        confidence=confidence,
    )


class TestAliasLookup:
    @pytest.mark.asyncio
    async def test_exact_lookup_returns_alias(self) -> None:
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "xiaomi/mimo-v2.5",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_EXACT,
                )
            ]
        )
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        result = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="xiaomi/mimo-v2.5",
            catalog_source="openrouter",
        )
        assert result.resolved is not None
        assert result.resolved.catalog_model_id == "xiaomi/mimo-v2.5"
        assert result.resolved.confidence == ALIAS_CONFIDENCE_EXACT
        assert result.ambiguous == ()

    @pytest.mark.asyncio
    async def test_curated_alias_lookup(self) -> None:
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                )
            ]
        )
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        result = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
        )
        assert result.resolved is not None
        assert result.resolved.catalog_model_id == "xiaomi/mimo-v2.5"
        assert result.resolved.confidence == ALIAS_CONFIDENCE_CURATED_ALIAS

    @pytest.mark.asyncio
    async def test_missing_alias_returns_none(self) -> None:
        db = _FakeDB([])
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        result = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
        )
        assert result.resolved is None

    @pytest.mark.asyncio
    async def test_ambiguous_skip_not_returned_as_resolved(self) -> None:
        """An ``ambiguous_skip`` row exists for diagnostics but must not
        cause the resolver to return a resolved alias."""
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "mimo",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_AMBIGUOUS_SKIP,
                    notes="Could be v2.5 or v2.5-pro",
                )
            ]
        )
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        result = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="mimo",
            catalog_source="openrouter",
        )
        assert result.resolved is None
        # The ambiguous row is preserved for caller diagnostics.
        assert len(result.ambiguous) == 1
        assert result.ambiguous[0].catalog_model_id == "xiaomi/mimo-v2.5"


class TestMiMoSeparation:
    """MiMo 2.5 vs MiMo 2.5 Pro must never collapse to a single lookup."""

    @pytest.mark.asyncio
    async def test_two_curated_aliases_resolve_independently(self) -> None:
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                ),
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5-pro",
                    "openrouter",
                    "xiaomi/mimo-v2.5-pro",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                ),
            ]
        )
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        non_pro = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
        )
        pro = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5-pro",
            catalog_source="openrouter",
        )
        assert non_pro.resolved is not None
        assert pro.resolved is not None
        assert non_pro.resolved.catalog_model_id == "xiaomi/mimo-v2.5"
        assert pro.resolved.catalog_model_id == "xiaomi/mimo-v2.5-pro"
        # Sanity: catalog IDs must differ so downstream fetch picks the
        # right row.
        assert non_pro.resolved.catalog_model_id != pro.resolved.catalog_model_id

    @pytest.mark.asyncio
    async def test_bare_mimo_with_no_alias_does_not_match_either(self) -> None:
        """If only the v2.5 / v2.5-pro aliases exist, the bare ``mimo``
        ID has no resolver at all — never substring-falls-back."""
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                ),
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5-pro",
                    "openrouter",
                    "xiaomi/mimo-v2.5-pro",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                ),
            ]
        )
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        result = await resolver.lookup(
            provider_id="opencode-go",
            upstream_model_id="mimo",
            catalog_source="openrouter",
        )
        assert result.resolved is None


class TestProviderScopedAliases:
    """Aliases must not leak across providers."""

    @pytest.mark.asyncio
    async def test_alias_for_one_provider_does_not_resolve_another(self) -> None:
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                )
            ]
        )
        resolver = PricingAliasResolver(db)  # type: ignore[arg-type]
        result = await resolver.lookup(
            provider_id="some-other-provider",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
        )
        assert result.resolved is None


class TestSeedDefaultAliases:
    @pytest.mark.asyncio
    async def test_seeding_inserts_curated_defaults(self) -> None:
        db = _FakeDB([])
        inserted = await seed_default_aliases(db)  # type: ignore[arg-type]
        assert inserted == 4
        assert len(db._writes) == 4

    @pytest.mark.asyncio
    async def test_seeding_is_idempotent(self) -> None:
        # Pre-populate one of the rows so seed_default_aliases sees it
        db = _FakeDB(
            [
                _FakeRow(
                    "opencode-go",
                    "mimo-v2.5",
                    "openrouter",
                    "xiaomi/mimo-v2.5",
                    ALIAS_CONFIDENCE_CURATED_ALIAS,
                )
            ]
        )
        inserted = await seed_default_aliases(db)  # type: ignore[arg-type]
        # The pre-existing row was skipped; the other three were inserted.
        assert inserted == 3


class TestDetectAmbiguousCandidates:
    @pytest.mark.asyncio
    async def test_helper_returns_true_when_multiple_candidates(self) -> None:
        rows = [
            _alias(catalog_model_id="xiaomi/mimo-v2.5"),
            _alias(
                upstream_model_id="mimo-v2.5-pro",
                catalog_model_id="xiaomi/mimo-v2.5-pro",
            ),
        ]
        # The static helper sees the in-memory list; no DB roundtrip.
        assert PricingAliasResolver.detect_ambiguous_candidates(
            [PricingAlias(**vars(r)) for r in rows],
            catalog_source="openrouter",
        )


class TestAliasLookupResult:
    def test_default_ambiguous_is_empty(self) -> None:
        result = AliasLookupResult(resolved=None)
        assert result.ambiguous == ()
