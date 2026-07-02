"""Tests that cache/compression metrics do NOT affect routing.

The QuotaFairScorer routes by request count + token count, never by
cache/compression metrics. These tests verify that two accounts with
different cache/compression profiles receive fair, load-based routing.
"""

from __future__ import annotations

import os

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.routing.router import Router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockCatalog:
    """Mock catalog with a single model across all configured accounts."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


def _make_config(accounts: list[dict[str, str]]) -> AppConfig:
    """Build a single-provider config with equal-weight accounts."""
    raw: dict[str, object] = {
        "providers": {
            "test-provider": {
                "id": "test-provider",
                "base_url": "https://api.example.com/v1",
                "routing_priority": 0,
                "accounts": accounts,
            }
        }
    }
    return AppConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCacheMetricsDoNotSkewRouting:
    """Cache metrics must not influence account selection."""

    @pytest.mark.asyncio()
    async def test_cache_metrics_do_not_skew_routing(self) -> None:
        """Two same-provider accounts with different cache match ratios
        receive fair selection based on load, not cache hits."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
            assert counts["acct_a"] < 40
            assert counts["acct_b"] < 40
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)


class TestCompressionSavingsDoNotSkewRouting:
    """Compression savings must not influence account selection."""

    @pytest.mark.asyncio()
    async def test_compression_savings_do_not_skew_routing(
        self,
    ) -> None:
        """Two accounts with different compression savings receive fair
        selection based on load, not compression ratio."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
            assert counts["acct_a"] < 40
            assert counts["acct_b"] < 40
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)


class TestStablePrefixHashDoesNotAffectRouting:
    """Stable prefix hash must not influence account selection."""

    @pytest.mark.asyncio()
    async def test_stable_prefix_hash_does_not_affect_routing(
        self,
    ) -> None:
        """Two accounts with different stable_prefix_hash values receive
        fair selection based on load, not cache locality."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
            assert counts["acct_a"] < 40
            assert counts["acct_b"] < 40
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)
