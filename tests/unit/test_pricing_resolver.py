"""Tests for the structured pricing resolver pipeline."""

from __future__ import annotations

from typing import Any

import pytest

from eggpool.catalog.pricing_resolver import (
    CONFIDENCE_AUTHORITATIVE,
    CONFIDENCE_OPERATOR,
    SOURCE_CONFIG,
    SOURCE_DETAIL_OPERATOR_OVERRIDE,
    SOURCE_DETAIL_PROVIDER_METADATA,
    SOURCE_MIXED,
    SOURCE_UPSTREAM,
    ResolvedPricing,
    resolve_pricing_from_metadata,
)


class TestResolveInputFromMetadata:
    """Upstream metadata resolution covers the OpenRouter and legacy shapes."""

    def test_openrouter_pricing_prompt_default_unit_token(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="mimo-v2.5",
            provider_id="opencode-go",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.000000105",
                        "completion": "0.00000028",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.000105)
        assert result.output_price_per_1k == pytest.approx(0.00028)
        assert result.source == SOURCE_UPSTREAM
        assert result.source_detail == SOURCE_DETAIL_PROVIDER_METADATA
        assert result.source_confidence == CONFIDENCE_AUTHORITATIVE

    def test_legacy_field_names(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="legacy-model",
            provider_id="opencode-go",
            model_info={
                "source_metadata": {
                    "input_price_per_1k": 0.003,
                    "output_price_per_1k": 0.015,
                    "cache_read_per_million_microdollars": 300_000,
                    "cache_write_per_million_microdollars": 3_750_000,
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == 0.003
        assert result.output_price_per_1k == 0.015
        assert result.cache_read_per_million_microdollars == 300_000
        assert result.cache_write_per_million_microdollars == 3_750_000

    def test_alternate_field_names(self) -> None:
        """Catalogs that surface prompt/completion instead of input/output."""
        result = resolve_pricing_from_metadata(
            model_id="alt-model",
            provider_id="opencode-go",
            model_info={
                "source_metadata": {
                    "prompt_price_per_1k": 0.001,
                    "completion_price_per_1k": 0.002,
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == 0.001
        assert result.output_price_per_1k == 0.002

    def test_pricing_input_and_output_keys(self) -> None:
        """Some catalogs use ``pricing.input`` / ``pricing.output``."""
        result = resolve_pricing_from_metadata(
            model_id="alt-model",
            provider_id="opencode-go",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "input": "0.000001",
                        "output": "0.000002",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.001)
        assert result.output_price_per_1k == pytest.approx(0.002)


class TestResolveCachePricingVariants:
    """OpenRouter-style cache fields and Anthropic-style flat fields."""

    def test_openrouter_cache_keys(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="mimo-v2.5",
            provider_id="opencode-go",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.000000105",
                        "completion": "0.00000028",
                        "input_cache_read": "0.000000021",
                        "input_cache_write": "0.000000105",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        # per-token cache read → 21 microdollars per million tokens
        assert result.cache_read_per_million_microdollars == 21_000
        # per-token cache write → 105 microdollars per million tokens
        assert result.cache_write_per_million_microdollars == 105_000

    def test_anthropic_cache_field_names(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="claude-3",
            provider_id="anthropic",
            model_info={
                "source_metadata": {
                    "cache_read_input_token_cost": "0.0000003",
                    "cache_creation_input_token_cost": "0.00000375",
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.cache_read_per_million_microdollars == 300_000
        assert result.cache_write_per_million_microdollars == 3_750_000

    def test_legacy_cache_field_names(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="model",
            provider_id="provider",
            model_info={
                "source_metadata": {
                    "input_cache_read_per_million_microdollars": 100_000,
                    "input_cache_write_per_million_microdollars": 500_000,
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.cache_read_per_million_microdollars == 100_000
        assert result.cache_write_per_million_microdollars == 500_000

    def test_invalid_price_strings_logged_and_ignored(self, caplog: Any) -> None:
        """Bad strings should not crash; the resolver returns None for that category."""
        with caplog.at_level("WARNING"):
            result = resolve_pricing_from_metadata(
                model_id="model",
                provider_id="provider",
                model_info={
                    "source_metadata": {
                        "pricing": {"prompt": "free"},  # invalid
                        "completion": "0.015",
                    }
                },
                override_values={},
            )
        assert result is not None
        assert result.input_price_per_1k is None
        assert result.output_price_per_1k == 0.015
        assert any("input price" in record.message.lower() for record in caplog.records)


class TestOverrideSemantics:
    """TOML overrides remain authoritative for the categories they set."""

    def test_full_config_override_source(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="model",
            provider_id="provider",
            model_info={"source_metadata": {}},
            override_values={
                "input": 0.003,
                "output": 0.015,
                "cache_read": 300_000,
                "cache_write": 3_750_000,
            },
        )
        assert result is not None
        assert result.source == SOURCE_CONFIG
        assert result.source_detail == SOURCE_DETAIL_OPERATOR_OVERRIDE
        assert result.source_confidence == CONFIDENCE_OPERATOR

    def test_partial_override_mixed_source(self) -> None:
        """Operator sets only input; metadata supplies output → mixed source."""
        result = resolve_pricing_from_metadata(
            model_id="model",
            provider_id="provider",
            model_info={
                "source_metadata": {
                    "output_price_per_1k": 0.015,
                    "cache_read_per_million_microdollars": 300_000,
                }
            },
            override_values={"input": 0.001},
        )
        assert result is not None
        assert result.source == SOURCE_MIXED
        assert result.input_price_per_1k == 0.001  # from override
        assert result.output_price_per_1k == 0.015  # from upstream
        assert result.cache_read_per_million_microdollars == 300_000  # from upstream

    def test_no_resolution_returns_none(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="model",
            provider_id="provider",
            model_info={"source_metadata": {}},
            override_values={},
        )
        assert result is None

    def test_invalid_override_value_ignored(self) -> None:
        """An override of None should not be treated as a present value."""
        result = resolve_pricing_from_metadata(
            model_id="model",
            provider_id="provider",
            model_info={
                "source_metadata": {"input_price_per_1k": 0.003},
            },
            override_values={"input": None, "output": 0.015},
        )
        assert result is not None
        # input falls through to upstream
        assert result.input_price_per_1k == 0.003
        assert result.output_price_per_1k == 0.015


class TestResolvedPricingDataclass:
    """ResolvedPricing exposes structured provenance fields."""

    def test_has_any(self) -> None:
        empty = ResolvedPricing(
            input_price_per_1k=None,
            output_price_per_1k=None,
            cache_read_per_million_microdollars=None,
            cache_write_per_million_microdollars=None,
            source=SOURCE_UPSTREAM,
            source_detail=SOURCE_DETAIL_PROVIDER_METADATA,
            source_confidence=CONFIDENCE_AUTHORITATIVE,
        )
        assert empty.has_any is False

        partial = ResolvedPricing(
            input_price_per_1k=0.003,
            output_price_per_1k=None,
            cache_read_per_million_microdollars=None,
            cache_write_per_million_microdollars=None,
            source=SOURCE_UPSTREAM,
            source_detail=SOURCE_DETAIL_PROVIDER_METADATA,
            source_confidence=CONFIDENCE_AUTHORITATIVE,
        )
        assert partial.has_any is True

    def test_frozen(self) -> None:
        result = ResolvedPricing(
            input_price_per_1k=0.003,
            output_price_per_1k=None,
            cache_read_per_million_microdollars=None,
            cache_write_per_million_microdollars=None,
            source=SOURCE_UPSTREAM,
            source_detail=SOURCE_DETAIL_PROVIDER_METADATA,
            source_confidence=CONFIDENCE_AUTHORITATIVE,
        )
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            result.input_price_per_1k = 0.5  # type: ignore[misc]
