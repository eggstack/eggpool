"""Tests for the OpenRouter catalog resolver."""

from __future__ import annotations

import httpx
import pytest

from eggpool.catalog.catalog_resolvers import (
    CatalogConfig,
    CatalogEntry,
    CatalogFetchError,
    CatalogResolverPipeline,
    OpenRouterCatalogResolver,
    TTLCache,
)
from eggpool.catalog.pricing_aliases import (
    AliasLookupResult,
    PricingAlias,
)
from eggpool.catalog.pricing_resolver import (
    SOURCE_DETAIL_OPENROUTER,
    ResolvedPricing,
)


class _StubClient:
    def __init__(self, payload: dict | Exception) -> None:
        self._payload = payload
        self.calls = 0
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def get(
        self, path: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls += 1
        self.paths.append(path)
        self.headers.append(headers or {})
        if isinstance(self._payload, Exception):
            raise self._payload
        request = httpx.Request("GET", path)
        return httpx.Response(200, json=self._payload, request=request)


class _PriorityResolver:
    def __init__(self, name: str, priority: int) -> None:
        self.name = name
        self._priority = priority

    @property
    def priority(self) -> int:
        return self._priority

    async def fetch_catalog(self) -> dict[str, CatalogEntry]:
        return {}

    def to_resolved_pricing(
        self,
        *,
        entry: CatalogEntry,
        provider_id: str,
        model_id: str,
        alias: PricingAlias,
    ) -> ResolvedPricing:
        raise AssertionError("priority-order test should not fetch catalog entries")


class _FailingFetchResolver:
    name = "failing"

    @property
    def priority(self) -> int:
        return 10

    async def fetch_catalog(self) -> dict[str, CatalogEntry]:
        raise RuntimeError("bad catalog payload")

    def to_resolved_pricing(
        self,
        *,
        entry: CatalogEntry,
        provider_id: str,
        model_id: str,
        alias: PricingAlias,
    ) -> ResolvedPricing:
        raise AssertionError("fetch failure should skip conversion")


class _FailingConvertResolver:
    name = "failing"

    @property
    def priority(self) -> int:
        return 10

    async def fetch_catalog(self) -> dict[str, CatalogEntry]:
        return {"external": CatalogEntry(catalog_model_id="external")}

    def to_resolved_pricing(
        self,
        *,
        entry: CatalogEntry,
        provider_id: str,
        model_id: str,
        alias: PricingAlias,
    ) -> ResolvedPricing:
        raise RuntimeError("bad conversion")


class TestTTLCache:
    def test_fresh_after_store(self) -> None:
        cache = TTLCache(ttl_seconds=60)
        assert cache.is_fresh is False
        cache.store({"x": "y"})  # type: ignore[arg-type]
        assert cache.is_fresh is True
        assert cache.get("x") == "y"  # type: ignore[comparison-overlap]

    def test_expires_after_ttl(self) -> None:
        cache = TTLCache(ttl_seconds=0)
        cache.store({"x": "y"})  # type: ignore[arg-type]
        assert cache.is_fresh is False

    def test_invalidate(self) -> None:
        cache = TTLCache(ttl_seconds=60)
        cache.store({"x": "y"})  # type: ignore[arg-type]
        cache.invalidate()
        assert cache.get("x") is None
        assert cache.is_fresh is False

    def test_evicts_oldest_when_max_entries_exceeded(self) -> None:
        """Oldest entries are evicted when the cache exceeds max_entries."""
        cache = TTLCache(ttl_seconds=3600, max_entries=3)
        cache.store(
            {
                "a": CatalogEntry(catalog_model_id="a"),
                "b": CatalogEntry(catalog_model_id="b"),
                "c": CatalogEntry(catalog_model_id="c"),
                "d": CatalogEntry(catalog_model_id="d"),
            }
        )
        snap = cache.snapshot()
        assert len(snap) == 3
        assert "a" not in snap  # oldest evicted
        assert "d" in snap


class TestOpenRouterParseCatalog:
    def test_parses_minimal_catalog(self) -> None:
        payload = {
            "data": [
                {
                    "id": "xiaomi/mimo-v2.5",
                    "pricing": {
                        "prompt": "0.000000105",
                        "completion": "0.00000028",
                        "input_cache_read": "0.000000021",
                        "input_cache_write": "0.000000105",
                    },
                }
            ]
        }
        entries = OpenRouterCatalogResolver._parse_catalog(payload)
        entry = entries["xiaomi/mimo-v2.5"]
        # 0.000000105 per token → 105 microdollars/1M input
        assert entry.input_price_per_1k == pytest.approx(0.000105)
        assert entry.output_price_per_1k == pytest.approx(0.00028)
        # 0.000000021 per token → 21 microdollars/1M cache read
        assert entry.cache_read_per_million_microdollars == 21_000
        assert entry.cache_write_per_million_microdollars == 105_000

    def test_skips_rows_without_id(self) -> None:
        payload = {
            "data": [
                {"pricing": {"prompt": "0.001"}},  # no id
                {"id": "valid", "pricing": {"prompt": "0.002"}},
            ]
        }
        entries = OpenRouterCatalogResolver._parse_catalog(payload)
        assert "valid" in entries
        assert len(entries) == 1

    def test_handles_missing_pricing_block(self) -> None:
        payload = {"data": [{"id": "x"}]}
        entries = OpenRouterCatalogResolver._parse_catalog(payload)
        assert entries["x"].input_price_per_1k is None
        assert entries["x"].cache_read_per_million_microdollars is None

    def test_handles_non_dict_payload(self) -> None:
        assert OpenRouterCatalogResolver._parse_catalog([]) == {}
        assert OpenRouterCatalogResolver._parse_catalog({"no_data": 1}) == {}

    def test_skips_entry_with_negative_price(self) -> None:
        payload = {
            "data": [
                {
                    "id": "bad-model",
                    "pricing": {"prompt": "-0.001", "completion": "0.002"},
                },
                {
                    "id": "good-model",
                    "pricing": {"prompt": "0.001", "completion": "0.002"},
                },
            ]
        }
        entries = OpenRouterCatalogResolver._parse_catalog(payload)
        assert "good-model" in entries
        # Negative price returns None — entry is kept but priced as None
        assert entries["bad-model"].input_price_per_1k is None
        assert entries["good-model"].input_price_per_1k is not None

    def test_ignores_boolean_price_field(self) -> None:
        payload = {
            "data": [
                {
                    "id": "bool-model",
                    "pricing": {"prompt": True, "completion": "0.002"},
                },
                {
                    "id": "good-model",
                    "pricing": {"prompt": "0.001", "completion": "0.002"},
                },
            ]
        }
        entries = OpenRouterCatalogResolver._parse_catalog(payload)
        assert "good-model" in entries
        assert "bool-model" in entries
        assert entries["bool-model"].input_price_per_1k is None
        assert entries["bool-model"].output_price_per_1k == pytest.approx(2.0)

    def test_all_invalid_prices_keep_entries_with_no_prices(self) -> None:
        payload = {
            "data": [
                {"id": "a", "pricing": {"prompt": "-1"}},
                {"id": "b", "pricing": {"prompt": True}},
            ]
        }
        entries = OpenRouterCatalogResolver._parse_catalog(payload)
        # "a" has negative price → kept with None pricing
        # "b" has boolean price → kept with None pricing
        assert "a" in entries
        assert entries["a"].input_price_per_1k is None
        assert "b" in entries
        assert entries["b"].input_price_per_1k is None

    def test_invalid_field_does_not_drop_other_valid_fields(self) -> None:
        payload = {
            "data": [
                {
                    "id": "partial-model",
                    "pricing": {
                        "prompt": {"amount": "bad"},
                        "completion": "0.00000028",
                        "input_cache_read": ["bad"],
                        "input_cache_write": "0.000000105",
                    },
                }
            ]
        }

        entries = OpenRouterCatalogResolver._parse_catalog(payload)

        entry = entries["partial-model"]
        assert entry.input_price_per_1k is None
        assert entry.output_price_per_1k == pytest.approx(0.00028)
        assert entry.cache_read_per_million_microdollars is None
        assert entry.cache_write_per_million_microdollars == 105_000

    def test_negative_cache_field_does_not_drop_entry(self) -> None:
        payload = {
            "data": [
                {
                    "id": "negative-cache-model",
                    "pricing": {
                        "prompt": "0.0000001",
                        "input_cache_read": "-0.00000001",
                    },
                }
            ]
        }

        entries = OpenRouterCatalogResolver._parse_catalog(payload)

        entry = entries["negative-cache-model"]
        assert entry.input_price_per_1k == pytest.approx(0.0001)
        assert entry.cache_read_per_million_microdollars is None


class TestOpenRouterFetchCatalog:
    @pytest.mark.asyncio
    async def test_returns_cached_without_recalling(self) -> None:
        payload = {
            "data": [
                {
                    "id": "mimo",
                    "pricing": {
                        "prompt": "0.000000105",
                        "completion": "0.00000028",
                    },
                }
            ]
        }
        client = _StubClient(payload)
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        resolver = OpenRouterCatalogResolver(config=cfg, client=client)  # type: ignore[arg-type]
        first = await resolver.fetch_catalog()
        second = await resolver.fetch_catalog()
        assert first.keys() == second.keys() == {"mimo"}
        # Second call must hit the cache, not the network.
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_raises_catalog_fetch_error_on_http_error(self) -> None:
        client = _StubClient(httpx.ConnectError("boom"))
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        resolver = OpenRouterCatalogResolver(config=cfg, client=client)  # type: ignore[arg-type]
        with pytest.raises(CatalogFetchError):
            await resolver.fetch_catalog()

    @pytest.mark.asyncio
    async def test_fetch_uses_absolute_catalog_url_and_headers(self) -> None:
        client = _StubClient({"data": []})
        cfg = CatalogConfig(
            name="openrouter",
            base_url="https://openrouter.example/api/v1/",
            api_key="catalog-key",
        )
        resolver = OpenRouterCatalogResolver(config=cfg, client=client)  # type: ignore[arg-type]

        await resolver.fetch_catalog()

        assert client.paths == ["https://openrouter.example/api/v1/models"]
        assert client.headers == [
            {"User-Agent": "eggpool/1.0", "Authorization": "Bearer catalog-key"}
        ]

    @pytest.mark.asyncio
    async def test_wraps_unexpected_parse_failure_as_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _StubClient({"data": []})
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        resolver = OpenRouterCatalogResolver(config=cfg, client=client)  # type: ignore[arg-type]

        def _boom(payload: object) -> dict[str, CatalogEntry]:
            raise RuntimeError(f"unexpected payload: {payload!r}")

        monkeypatch.setattr(
            OpenRouterCatalogResolver, "_parse_catalog", staticmethod(_boom)
        )

        with pytest.raises(CatalogFetchError):
            await resolver.fetch_catalog()


class TestOpenRouterToResolvedPricing:
    def test_curated_alias_uses_curated_confidence(self) -> None:
        from eggpool.catalog.catalog_resolvers import CatalogEntry
        from eggpool.catalog.pricing_resolver import (
            CONFIDENCE_CURATED_ALIAS,
            SOURCE_DETAIL_OPENROUTER,
        )

        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        resolver = OpenRouterCatalogResolver(config=cfg, client=_StubClient({}))  # type: ignore[arg-type]
        entry = CatalogEntry(
            catalog_model_id="xiaomi/mimo-v2.5",
            input_price_per_1k=0.0001,
            output_price_per_1k=0.0002,
            cache_read_per_million_microdollars=20_000,
            cache_write_per_million_microdollars=100_000,
        )
        alias = PricingAlias(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
            catalog_model_id="xiaomi/mimo-v2.5",
            confidence="curated_alias",
        )
        result = resolver.to_resolved_pricing(
            entry=entry,
            provider_id="opencode-go",
            model_id="mimo-v2.5",
            alias=alias,
        )
        assert result.source_detail == SOURCE_DETAIL_OPENROUTER
        assert result.source_confidence == CONFIDENCE_CURATED_ALIAS
        assert result.source_model_id == "xiaomi/mimo-v2.5"


class _StubAliasResolver:
    def __init__(
        self, mapping: dict[tuple[str, str, str], PricingAlias | None]
    ) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, str, str]] = []

    async def lookup(
        self,
        *,
        provider_id: str,
        upstream_model_id: str,
        catalog_source: str,
    ) -> AliasLookupResult:
        self.calls.append((provider_id, upstream_model_id, catalog_source))
        return AliasLookupResult(
            resolved=self._mapping.get((provider_id, upstream_model_id, catalog_source))
        )


class TestCatalogResolverPipeline:
    @pytest.mark.asyncio
    async def test_consults_resolvers_by_configured_priority(self) -> None:
        alias_resolver = _StubAliasResolver({})
        pipeline = CatalogResolverPipeline(
            resolvers=[
                _PriorityResolver("late", priority=100),
                _PriorityResolver("early", priority=10),
            ],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )

        result = await pipeline.resolve(provider_id="opencode-go", model_id="m1")

        assert result is None
        assert alias_resolver.calls == [
            ("opencode-go", "m1", "early"),
            ("opencode-go", "m1", "late"),
        ]

    @pytest.mark.asyncio
    async def test_resolves_via_alias(self) -> None:
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        payload = {
            "data": [
                {
                    "id": "xiaomi/mimo-v2.5",
                    "pricing": {"prompt": "0.000000105"},
                }
            ]
        }
        client = _StubClient(payload)
        resolver = OpenRouterCatalogResolver(config=cfg, client=client)  # type: ignore[arg-type]
        alias = PricingAlias(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
            catalog_model_id="xiaomi/mimo-v2.5",
            confidence="curated_alias",
        )
        alias_resolver = _StubAliasResolver(
            {
                ("opencode-go", "mimo-v2.5", "openrouter"): alias,
            }
        )
        pipeline = CatalogResolverPipeline(
            resolvers=[resolver],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )
        result = await pipeline.resolve(provider_id="opencode-go", model_id="mimo-v2.5")
        assert result is not None
        assert result.source_detail == SOURCE_DETAIL_OPENROUTER
        assert result.input_price_per_1k == pytest.approx(0.000105)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_alias(self) -> None:
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        resolver = OpenRouterCatalogResolver(config=cfg, client=_StubClient({}))  # type: ignore[arg-type]
        alias_resolver = _StubAliasResolver({})
        pipeline = CatalogResolverPipeline(
            resolvers=[resolver],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )
        result = await pipeline.resolve(provider_id="opencode-go", model_id="mimo-v2.5")
        assert result is None
        # Alias resolver was consulted but the catalog was not.
        assert ("opencode-go", "mimo-v2.5", "openrouter") in alias_resolver.calls

    @pytest.mark.asyncio
    async def test_catalog_entry_missing_for_alias_logs_and_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        # Catalog is empty; alias points at a model that is not there.
        resolver = OpenRouterCatalogResolver(
            config=cfg, client=_StubClient({"data": []})
        )  # type: ignore[arg-type]
        alias = PricingAlias(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
            catalog_model_id="xiaomi/mimo-v2.5",
            confidence="curated_alias",
        )
        alias_resolver = _StubAliasResolver(
            {("opencode-go", "mimo-v2.5", "openrouter"): alias}
        )
        pipeline = CatalogResolverPipeline(
            resolvers=[resolver],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )
        with caplog.at_level("WARNING"):
            result = await pipeline.resolve(
                provider_id="opencode-go", model_id="mimo-v2.5"
            )
        assert result is None
        assert any("no entry for alias" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unexpected_fetch_error_logs_and_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        alias = PricingAlias(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="failing",
            catalog_model_id="external",
            confidence="curated_alias",
        )
        alias_resolver = _StubAliasResolver(
            {("opencode-go", "mimo-v2.5", "failing"): alias}
        )
        pipeline = CatalogResolverPipeline(
            resolvers=[_FailingFetchResolver()],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )

        with caplog.at_level("ERROR"):
            result = await pipeline.resolve(
                provider_id="opencode-go", model_id="mimo-v2.5"
            )

        assert result is None
        assert any("failed unexpectedly" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unexpected_conversion_error_logs_and_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        alias = PricingAlias(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="failing",
            catalog_model_id="external",
            confidence="curated_alias",
        )
        alias_resolver = _StubAliasResolver(
            {("opencode-go", "mimo-v2.5", "failing"): alias}
        )
        pipeline = CatalogResolverPipeline(
            resolvers=[_FailingConvertResolver()],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )

        with caplog.at_level("ERROR"):
            result = await pipeline.resolve(
                provider_id="opencode-go", model_id="mimo-v2.5"
            )

        assert result is None
        assert any("failed to convert pricing" in r.message for r in caplog.records)


class TestOpenRouterFetchCatalogIntegration:
    """End-to-end style: stub client → resolver → ResolvedPricing."""

    @pytest.mark.asyncio
    async def test_minimo_v2_5_full_pipeline(self) -> None:
        """Realistic payload matching the OpenRouter schema for MiMo."""
        cfg = CatalogConfig(name="openrouter", base_url="https://example.invalid")
        payload = {
            "data": [
                {
                    "id": "xiaomi/mimo-v2.5",
                    "pricing": {
                        "prompt": "0.000000105",
                        "completion": "0.00000028",
                        "input_cache_read": "0.000000021",
                        "input_cache_write": "0.000000105",
                    },
                }
            ]
        }
        client = _StubClient(payload)
        resolver = OpenRouterCatalogResolver(config=cfg, client=client)  # type: ignore[arg-type]
        alias = PricingAlias(
            provider_id="opencode-go",
            upstream_model_id="mimo-v2.5",
            catalog_source="openrouter",
            catalog_model_id="xiaomi/mimo-v2.5",
            confidence="exact",
        )
        alias_resolver = _StubAliasResolver(
            {("opencode-go", "mimo-v2.5", "openrouter"): alias}
        )
        pipeline = CatalogResolverPipeline(
            resolvers=[resolver],
            alias_resolver=alias_resolver,  # type: ignore[arg-type]
        )
        result = await pipeline.resolve(provider_id="opencode-go", model_id="mimo-v2.5")
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.000105)
        assert result.output_price_per_1k == pytest.approx(0.00028)
        assert result.cache_read_per_million_microdollars == 21_000
        assert result.cache_write_per_million_microdollars == 105_000
        assert result.source == "upstream"
        assert result.source_detail == SOURCE_DETAIL_OPENROUTER
        assert result.source_model_id == "xiaomi/mimo-v2.5"

        # Sanity: do not pay $92 — at 30M tokens the cost is roughly $3.
        # input: 30M * 0.000105/1K = $3.15
        # output: 1M * 0.00028/1K = $0.28
        # total ≈ $3.43 → 3_430_000 microdollars
        cost_dollars = (30_000_000 * result.input_price_per_1k / 1000) + (
            1_000_000 * result.output_price_per_1k / 1000
        )
        cost_micro = int(round(cost_dollars * 1_000_000))
        # Should land between $3 and $5.
        assert 3_000_000 <= cost_micro <= 5_000_000
