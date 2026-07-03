"""Tests for the structured pricing resolver pipeline."""

from __future__ import annotations

from typing import Any

import pytest

from eggpool.catalog.pricing_resolver import (
    CONFIDENCE_AUTHORITATIVE,
    CONFIDENCE_HEURISTIC,
    CONFIDENCE_OPERATOR,
    SOURCE_CONFIG,
    SOURCE_DETAIL_OPERATOR_OVERRIDE,
    SOURCE_DETAIL_PROVIDER_METADATA,
    SOURCE_MIXED,
    SOURCE_UPSTREAM,
    ResolvedPricing,
    apply_snapshot_trust_gates,
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
        assert result.source_confidence == CONFIDENCE_HEURISTIC

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

    def test_minimax_style_bare_pricing_defaults_to_per_million(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="MiniMax-M3",
            provider_id="minimax",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "input": 0.2,
                        "output": 1.1,
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.0002)
        assert result.output_price_per_1k == pytest.approx(0.0011)


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

    def test_provider_native_cache_fields_inherit_per_million_cluster(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="minimax-m3",
            provider_id="minimax",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "input": 0.2,
                        "output": 1.1,
                        "cache_read": 0.02,
                        "cache_write": 0.2,
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.0002)
        assert result.output_price_per_1k == pytest.approx(0.0011)
        assert result.cache_read_per_million_microdollars == 20_000
        assert result.cache_write_per_million_microdollars == 200_000
        cache_read_detail = result.detail_for("cache_read")
        assert cache_read_detail is not None
        assert cache_read_detail.evidence == "numeric_scale"
        assert cache_read_detail.path == ("pricing", "cache_read")

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
        assert result.output_price_per_1k == pytest.approx(0.000015)
        assert any("input price" in record.message.lower() for record in caplog.records)

    def test_invalid_boolean_nan_and_empty_values_are_ignored(
        self,
        caplog: Any,
    ) -> None:
        with caplog.at_level("WARNING"):
            result = resolve_pricing_from_metadata(
                model_id="odd-model",
                provider_id="odd-provider",
                model_info={
                    "source_metadata": {
                        "pricing": {
                            "prompt": True,
                            "completion": "1.1 / 1M",
                            "cache_read": "",
                            "cache_write": float("nan"),
                        }
                    }
                },
                override_values={},
            )
        assert result is not None
        assert result.input_price_per_1k is None
        assert result.output_price_per_1k == pytest.approx(0.0011)
        assert result.cache_read_per_million_microdollars is None
        assert result.cache_write_per_million_microdollars is None
        assert len(caplog.records) >= 2


class TestUnitInferenceVariants:
    def test_explicit_suffix_wins_per_field(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="mixed",
            provider_id="provider",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.2 / 1M",
                        "completion": "0.0000011 / token",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.0002)
        assert result.output_price_per_1k == pytest.approx(0.0011)

    def test_field_name_units_cover_top_level_aliases(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="aliased-model",
            provider_id="provider",
            model_info={
                "source_metadata": {
                    "input_price_per_1k": 0.0002,
                    "output_usd_per_million": 1.1,
                    "cache_read_usd_per_million": 0.02,
                    "input_cache_write_per_million_microdollars": 200_000,
                }
            },
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.0002)
        assert result.output_price_per_1k == pytest.approx(0.0011)
        assert result.cache_read_per_million_microdollars == 20_000
        assert result.cache_write_per_million_microdollars == 200_000

    def test_ambiguous_single_bare_value_defaults_to_per_million(self) -> None:
        result = resolve_pricing_from_metadata(
            model_id="ambiguous",
            provider_id="provider",
            model_info={"source_metadata": {"pricing": {"input": 0.2}}},
            override_values={},
        )
        assert result is not None
        assert result.input_price_per_1k == pytest.approx(0.0002)
        assert result.source_confidence == CONFIDENCE_HEURISTIC
        detail = result.detail_for("input")
        assert detail is not None
        assert detail.evidence == "safe_default"


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


class TestSnapshotTrustGates:
    def test_rejects_implausible_category_rates_before_persistence(self) -> None:
        resolved = resolve_pricing_from_metadata(
            model_id="bad-model",
            provider_id="provider",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "input": 200000,
                        "output": 1.1,
                    }
                }
            },
            override_values={},
        )
        assert resolved is not None
        trusted = apply_snapshot_trust_gates(
            resolved,
            model_id="bad-model",
            provider_id="provider",
        )
        assert trusted is not None
        assert trusted.input_price_per_1k is None
        assert trusted.output_price_per_1k == pytest.approx(0.0011)

    def test_returns_none_when_every_category_is_rejected(self) -> None:
        resolved = resolve_pricing_from_metadata(
            model_id="bad-model",
            provider_id="provider",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "input": 200000,
                        "output": 900000,
                    }
                }
            },
            override_values={},
        )
        assert resolved is not None
        trusted = apply_snapshot_trust_gates(
            resolved,
            model_id="bad-model",
            provider_id="provider",
        )
        assert trusted is None


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
