"""Tests for the model limit resolver and conservative merge."""

from __future__ import annotations

from typing import Any

from eggpool.catalog.limits import (
    EffectiveModelLimits,
    ModelLimitResolver,
    conservative_limits,
    extract_upstream_limits,
    extract_upstream_limits_with_source,
)
from eggpool.models.config import AppConfig


def _make_config(
    *,
    global_overrides: dict[str, Any] | None = None,
    provider_overrides: dict[str, Any] | None = None,
) -> AppConfig:
    data: dict[str, Any] = {
        "upstream": {"base_url": "https://example.com"},
    }
    if global_overrides:
        data["model_overrides"] = global_overrides
    if provider_overrides:
        data["providers"] = {
            "test-provider": {
                "id": "test-provider",
                "base_url": "https://example.com",
                "model_overrides": provider_overrides,
            }
        }
    return AppConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Upstream metadata extraction
# ---------------------------------------------------------------------------


class TestExtractUpstreamLimits:
    def test_context_from_capabilities(self) -> None:
        caps: dict[str, Any] = {"context_window": 200000}
        ctx, inp, out = extract_upstream_limits(caps, {})
        assert ctx == 200000
        assert inp is None
        assert out is None

    def test_context_from_source_metadata(self) -> None:
        ctx, inp, out = extract_upstream_limits({}, {"max_position_embeddings": 4096})
        assert ctx == 4096

    def test_capabilities_preferred_over_metadata(self) -> None:
        caps: dict[str, Any] = {"context_window": 200000}
        meta: dict[str, Any] = {"context_window": 100000}
        ctx, _, _ = extract_upstream_limits(caps, meta)
        assert ctx == 200000

    def test_all_keys(self) -> None:
        caps: dict[str, Any] = {
            "context_window": 200000,
            "max_input_tokens": 180000,
            "max_output_tokens": 16384,
        }
        ctx, inp, out = extract_upstream_limits(caps, {})
        assert ctx == 200000
        assert inp == 180000
        assert out == 16384

    def test_numeric_string_parses(self) -> None:
        caps: dict[str, Any] = {"context_window": "200000"}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx == 200000

    def test_boolean_rejected(self) -> None:
        caps: dict[str, Any] = {"context_window": True}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx is None

    def test_zero_rejected(self) -> None:
        caps: dict[str, Any] = {"context_window": 0}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx is None

    def test_negative_rejected(self) -> None:
        caps: dict[str, Any] = {"context_window": -100}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx is None

    def test_float_with_fractional_rejected(self) -> None:
        caps: dict[str, Any] = {"context_window": 100.5}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx is None

    def test_integral_float_accepted(self) -> None:
        caps: dict[str, Any] = {"context_window": 100.0}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx == 100

    def test_empty_string_rejected(self) -> None:
        caps: dict[str, Any] = {"context_window": "  "}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx is None

    def test_non_numeric_string_rejected(self) -> None:
        caps: dict[str, Any] = {"context_window": "large"}
        ctx, _, _ = extract_upstream_limits(caps, {})
        assert ctx is None


class TestExtractUpstreamLimitsWithSource:
    def test_context_from_capabilities(self) -> None:
        caps: dict[str, Any] = {"context_window": 200000}
        (ctx_val, ctx_src), _, _ = extract_upstream_limits_with_source(caps, {})
        assert ctx_val == 200000
        assert ctx_src == "upstream_metadata"

    def test_context_from_source_metadata(self) -> None:
        (ctx_val, ctx_src), _, _ = extract_upstream_limits_with_source(
            {}, {"context_window": 200000}
        )
        assert ctx_val == 200000
        assert ctx_src == "upstream_metadata"


# ---------------------------------------------------------------------------
# ModelLimitResolver
# ---------------------------------------------------------------------------


class TestModelLimitResolver:
    def test_provider_override_beats_global(self) -> None:
        config = _make_config(
            global_overrides={
                "m1": {"max_context_tokens": 100000},
            },
            provider_overrides={
                "m1": {"max_context_tokens": 200000},
            },
        )
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={},
            source_metadata={},
        )
        assert result.context_tokens == 200000
        assert result.context_source == "provider_override"

    def test_global_override_beats_upstream(self) -> None:
        config = _make_config(
            global_overrides={
                "m1": {"max_context_tokens": 100000},
            },
        )
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={"context_window": 500000},
            source_metadata={},
        )
        assert result.context_tokens == 100000
        assert result.context_source == "global_override"

    def test_missing_provider_falls_back_to_global(self) -> None:
        config = _make_config(
            global_overrides={
                "m1": {"max_output_tokens": 8192},
            },
        )
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={},
            source_metadata={},
        )
        assert result.output_tokens == 8192
        assert result.output_source == "global_override"

    def test_missing_all_overrides_uses_upstream(self) -> None:
        config = _make_config()
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={"context_window": 200000},
            source_metadata={},
        )
        assert result.context_tokens == 200000
        assert result.context_source == "upstream_metadata"

    def test_per_field_provenance(self) -> None:
        config = _make_config(
            global_overrides={
                "m1": {"max_output_tokens": 8192},
            },
            provider_overrides={
                "m1": {"max_context_tokens": 220000},
            },
        )
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={"max_input_tokens": 200000},
            source_metadata={},
        )
        assert result.context_tokens == 220000
        assert result.context_source == "provider_override"
        assert result.input_tokens == 200000
        assert result.input_source == "upstream_metadata"
        assert result.output_tokens == 8192
        assert result.output_source == "global_override"

    def test_enforce_default_true(self) -> None:
        config = _make_config()
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={},
            source_metadata={},
        )
        assert result.enforce is True

    def test_enforce_from_provider_override(self) -> None:
        config = _make_config(
            provider_overrides={
                "m1": {"max_context_tokens": 100000, "enforce_context_limit": False},
            },
        )
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={},
            source_metadata={},
        )
        assert result.enforce is False

    def test_enforce_from_global_override(self) -> None:
        config = _make_config(
            global_overrides={
                "m1": {"max_context_tokens": 100000, "enforce_context_limit": False},
            },
        )
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={},
            source_metadata={},
        )
        assert result.enforce is False

    def test_all_unknown_returns_none(self) -> None:
        config = _make_config()
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="nonexistent",
            capabilities={},
            source_metadata={},
        )
        assert result.context_tokens is None
        assert result.input_tokens is None
        assert result.output_tokens is None
        assert result.context_source == "unknown"

    def test_numeric_string_in_metadata(self) -> None:
        config = _make_config()
        resolver = ModelLimitResolver(config)
        result = resolver.resolve(
            provider_id="test-provider",
            model_id="m1",
            capabilities={"context_window": "200000"},
            source_metadata={},
        )
        assert result.context_tokens == 200000


# ---------------------------------------------------------------------------
# Conservative merge
# ---------------------------------------------------------------------------


class TestConservativeLimits:
    def test_selects_minimum(self) -> None:
        limits = [
            EffectiveModelLimits(
                context_tokens=200000,
                input_tokens=180000,
                output_tokens=16384,
                enforce=True,
                context_source="provider_override",
                input_source="provider_override",
                output_source="provider_override",
            ),
            EffectiveModelLimits(
                context_tokens=500000,
                input_tokens=400000,
                output_tokens=32768,
                enforce=False,
                context_source="upstream_metadata",
                input_source="upstream_metadata",
                output_source="upstream_metadata",
            ),
        ]
        result = conservative_limits(limits)
        assert result.context_tokens == 200000
        assert result.input_tokens == 180000
        assert result.output_tokens == 16384

    def test_ignores_none_when_known_exist(self) -> None:
        limits = [
            EffectiveModelLimits(
                context_tokens=200000,
                input_tokens=None,
                output_tokens=16384,
                enforce=True,
                context_source="provider_override",
                input_source="unknown",
                output_source="provider_override",
            ),
            EffectiveModelLimits(
                context_tokens=500000,
                input_tokens=400000,
                output_tokens=None,
                enforce=False,
                context_source="upstream_metadata",
                input_source="upstream_metadata",
                output_source="unknown",
            ),
        ]
        result = conservative_limits(limits)
        assert result.context_tokens == 200000
        assert result.input_tokens == 400000
        assert result.output_tokens == 16384

    def test_all_unknown_returns_none(self) -> None:
        limits = [
            EffectiveModelLimits(
                context_tokens=None,
                input_tokens=None,
                output_tokens=None,
                enforce=True,
                context_source="unknown",
                input_source="unknown",
                output_source="unknown",
            ),
        ]
        result = conservative_limits(limits)
        assert result.context_tokens is None
        assert result.input_tokens is None
        assert result.output_tokens is None
        assert result.context_source == "unknown"

    def test_enforce_true_if_any_enforces(self) -> None:
        limits = [
            EffectiveModelLimits(
                context_tokens=100,
                input_tokens=None,
                output_tokens=None,
                enforce=False,
                context_source="a",
                input_source="unknown",
                output_source="unknown",
            ),
            EffectiveModelLimits(
                context_tokens=200,
                input_tokens=None,
                output_tokens=None,
                enforce=True,
                context_source="b",
                input_source="unknown",
                output_source="unknown",
            ),
        ]
        result = conservative_limits(limits)
        assert result.enforce is True

    def test_enforce_false_when_none_enforce(self) -> None:
        limits = [
            EffectiveModelLimits(
                context_tokens=100,
                input_tokens=None,
                output_tokens=None,
                enforce=False,
                context_source="a",
                input_source="unknown",
                output_source="unknown",
            ),
        ]
        result = conservative_limits(limits)
        assert result.enforce is False

    def test_empty_iterable(self) -> None:
        result = conservative_limits([])
        assert result.context_tokens is None
        assert result.context_source == "unknown"

    def test_single_provider_passthrough(self) -> None:
        limits = [
            EffectiveModelLimits(
                context_tokens=220000,
                input_tokens=180000,
                output_tokens=16384,
                enforce=True,
                context_source="provider_override",
                input_source="provider_override",
                output_source="provider_override",
            ),
        ]
        result = conservative_limits(limits)
        assert result.context_tokens == 220000
        assert result.context_source == "conservative_provider_minimum"
