"""Tests for model capability overrides (Phase 3).

Covers config validation, override conversion, 3-layer merge, dict
conversion, and a config→catalog integration flow.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eggpool.catalog.capabilities import (
    ModelCapabilities,
    ThinkingCapability,
    apply_capability_overrides,
    dict_to_model_capabilities,
    merge_model_capabilities,
    model_capabilities_override_to_config,
    model_capabilities_to_dict,
    thinking_override_to_capability,
)
from eggpool.errors import ConfigError
from eggpool.models.config import (
    AppConfig,
    ModelCapabilitiesOverrideConfig,
    ThinkingCapabilityOverrideConfig,
)

# ---------------------------------------------------------------------------
# Config validation: ThinkingCapabilityOverrideConfig
# ---------------------------------------------------------------------------


class TestThinkingCapabilityOverrideConfig:
    def test_default_construction(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig()
        assert cfg.status is None
        assert cfg.source is None
        assert cfg.native_protocols is None
        assert cfg.budget_tokens_min is None
        assert cfg.budget_tokens_max is None
        assert cfg.effort_to_budget_tokens is None
        assert cfg.notes is None

    def test_status_supported_defaults_source(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(status="supported")
        assert cfg.source == "manual_override"

    def test_explicit_source_overrides_default(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported", source="provider_catalog"
        )
        assert cfg.source == "provider_catalog"

    def test_valid_native_protocols(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported", native_protocols=["openai", "anthropic"]
        )
        assert cfg.native_protocols == ["openai", "anthropic"]

    def test_invalid_native_protocols_rejected(self) -> None:
        with pytest.raises(ConfigError, match="Unknown native protocol"):
            ThinkingCapabilityOverrideConfig(
                status="supported", native_protocols=["grpc"]
            )

    def test_budget_tokens_min_positive(self) -> None:
        with pytest.raises(ConfigError, match="budget_tokens_min must be > 0"):
            ThinkingCapabilityOverrideConfig(status="supported", budget_tokens_min=0)

    def test_budget_tokens_min_negative(self) -> None:
        with pytest.raises(ConfigError, match="budget_tokens_min must be > 0"):
            ThinkingCapabilityOverrideConfig(status="supported", budget_tokens_min=-1)

    def test_budget_tokens_max_positive(self) -> None:
        with pytest.raises(ConfigError, match="budget_tokens_max must be > 0"):
            ThinkingCapabilityOverrideConfig(status="supported", budget_tokens_max=0)

    def test_budget_tokens_max_negative(self) -> None:
        with pytest.raises(ConfigError, match="budget_tokens_max must be > 0"):
            ThinkingCapabilityOverrideConfig(status="supported", budget_tokens_max=-5)

    def test_budget_tokens_min_exceeds_max_rejected(self) -> None:
        with pytest.raises(ConfigError, match="budget_tokens_min.*exceeds"):
            ThinkingCapabilityOverrideConfig(
                status="supported", budget_tokens_min=10000, budget_tokens_max=1000
            )

    def test_budget_tokens_valid_range(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported", budget_tokens_min=100, budget_tokens_max=50000
        )
        assert cfg.budget_tokens_min == 100
        assert cfg.budget_tokens_max == 50000

    def test_effort_to_budget_tokens_positive_int(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported",
            effort_to_budget_tokens={"low": 1000, "high": 10000},
        )
        assert cfg.effort_to_budget_tokens == {"low": 1000, "high": 10000}

    def test_effort_to_budget_tokens_zero_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be > 0"):
            ThinkingCapabilityOverrideConfig(
                status="supported", effort_to_budget_tokens={"low": 0}
            )

    def test_effort_to_budget_tokens_negative_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be > 0"):
            ThinkingCapabilityOverrideConfig(
                status="supported", effort_to_budget_tokens={"low": -100}
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThinkingCapabilityOverrideConfig.model_validate(
                {"status": "supported", "bogus_field": True}
            )

    def test_status_none_clears_all_fields(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig.model_validate(
            {
                "status": None,
                "source": "provider_catalog",
                "native_protocols": ["openai"],
                "budget_tokens_min": 100,
                "budget_tokens_max": 5000,
                "effort_to_budget_tokens": {"low": 100},
                "notes": "should be cleared",
            }
        )
        assert cfg.status is None
        assert cfg.source is None
        assert cfg.native_protocols is None
        assert cfg.budget_tokens_min is None
        assert cfg.budget_tokens_max is None
        assert cfg.effort_to_budget_tokens is None
        assert cfg.notes is None


class TestThinkingCapabilityOverrideConfigStatusValues:
    """All 5 canonical status values are accepted."""

    @pytest.mark.parametrize(
        "status",
        ["supported", "unsupported", "unknown", "mixed", "conflicting"],
    )
    def test_valid_status(self, status: str) -> None:
        cfg = ThinkingCapabilityOverrideConfig(status=status)  # type: ignore[arg-type]
        assert cfg.status == status


class TestThinkingCapabilityOverrideConfigSourceValues:
    """All 6 canonical source values are accepted."""

    @pytest.mark.parametrize(
        "source",
        [
            "provider_catalog",
            "model_info",
            "manual_override",
            "heuristic",
            "aggregate",
            "unknown",
        ],
    )
    def test_valid_source(self, source: str) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported",
            source=source,  # type: ignore[arg-type]
        )
        assert cfg.source == source


# ---------------------------------------------------------------------------
# Config validation: ModelCapabilitiesOverrideConfig
# ---------------------------------------------------------------------------


class TestModelCapabilitiesOverrideConfig:
    def test_default_construction(self) -> None:
        cfg = ModelCapabilitiesOverrideConfig()
        assert cfg.thinking is None

    def test_wraps_thinking(self) -> None:
        inner = ThinkingCapabilityOverrideConfig(status="supported")
        cfg = ModelCapabilitiesOverrideConfig(thinking=inner)
        assert cfg.thinking is not None
        assert cfg.thinking.status == "supported"

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelCapabilitiesOverrideConfig.model_validate(
                {"thinking": {"status": "supported"}, "vision": True}
            )


# ---------------------------------------------------------------------------
# Override conversion functions
# ---------------------------------------------------------------------------


class TestThinkingOverrideToCapability:
    def test_none_input(self) -> None:
        cap = thinking_override_to_capability(None)
        assert cap.status == "unknown"
        assert cap.source == "unknown"
        assert cap.native_protocols == []

    def test_all_none_dict(self) -> None:
        cap = thinking_override_to_capability(
            {"status": None, "source": None, "native_protocols": None}
        )
        assert cap.status == "unknown"
        assert cap.source == "unknown"

    def test_empty_dict(self) -> None:
        cap = thinking_override_to_capability({})
        assert cap.status == "unknown"
        assert cap.source == "unknown"

    def test_status_only(self) -> None:
        cap = thinking_override_to_capability({"status": "supported"})
        assert cap.status == "supported"
        assert cap.source == "manual_override"

    def test_full_override(self) -> None:
        cap = thinking_override_to_capability(
            {
                "status": "supported",
                "source": "provider_catalog",
                "native_protocols": ["openai", "anthropic"],
                "budget_tokens_min": 512,
                "budget_tokens_max": 80000,
                "effort_to_budget_tokens": {"low": 500, "high": 8000},
                "notes": "custom override",
            }
        )
        assert cap.status == "supported"
        assert cap.source == "provider_catalog"
        assert cap.native_protocols == ["openai", "anthropic"]
        assert cap.budget_tokens_min == 512
        assert cap.budget_tokens_max == 80000
        assert cap.effort_to_budget_tokens == {"low": 500, "high": 8000}
        assert cap.notes == "custom override"

    def test_native_protocols_conversion(self) -> None:
        cap = thinking_override_to_capability(
            {"status": "supported", "native_protocols": ["anthropic"]}
        )
        assert cap.native_protocols == ["anthropic"]

    def test_budget_tokens_conversion(self) -> None:
        cap = thinking_override_to_capability(
            {
                "status": "supported",
                "budget_tokens_min": 256,
                "budget_tokens_max": 64000,
            }
        )
        assert cap.budget_tokens_min == 256
        assert cap.budget_tokens_max == 64000

    def test_effort_to_budget_tokens_conversion(self) -> None:
        cap = thinking_override_to_capability(
            {
                "status": "supported",
                "effort_to_budget_tokens": {"medium": 3000},
            }
        )
        assert cap.effort_to_budget_tokens == {"medium": 3000}

    def test_notes_conversion(self) -> None:
        cap = thinking_override_to_capability(
            {"status": "supported", "notes": "test note"}
        )
        assert cap.notes == "test note"

    def test_non_int_budget_tokens_ignored(self) -> None:
        cap = thinking_override_to_capability(
            {
                "status": "supported",
                "budget_tokens_min": "not_an_int",
                "budget_tokens_max": 3.14,
            }
        )
        assert cap.budget_tokens_min is None
        assert cap.budget_tokens_max is None


class TestModelCapabilitiesOverrideToConfig:
    def test_none_input(self) -> None:
        caps = model_capabilities_override_to_config(None)
        assert caps.thinking.status == "unknown"

    def test_empty_dict(self) -> None:
        caps = model_capabilities_override_to_config({})
        assert caps.thinking.status == "unknown"

    def test_thinking_sub_dict(self) -> None:
        caps = model_capabilities_override_to_config(
            {
                "thinking": {
                    "status": "supported",
                    "source": "manual_override",
                    "notes": "from config",
                }
            }
        )
        assert caps.thinking.status == "supported"
        assert caps.thinking.source == "manual_override"
        assert caps.thinking.notes == "from config"

    def test_missing_thinking_key(self) -> None:
        caps = model_capabilities_override_to_config({"something_else": True})
        assert caps.thinking.status == "unknown"

    def test_non_dict_thinking_value(self) -> None:
        caps = model_capabilities_override_to_config({"thinking": "invalid"})
        assert caps.thinking.status == "unknown"


# ---------------------------------------------------------------------------
# apply_capability_overrides (3-layer merge)
# ---------------------------------------------------------------------------


class TestApplyCapabilityOverrides:
    def _base(self) -> ModelCapabilities:
        return ModelCapabilities(
            thinking=ThinkingCapability(
                status="unknown",
                source="unknown",
                native_protocols=[],
            )
        )

    def test_no_overrides(self) -> None:
        result = apply_capability_overrides("gpt-4o", self._base(), {}, {})
        assert result.thinking.status == "unknown"
        assert result.thinking.source == "unknown"

    def test_global_override_only(self) -> None:
        global_overrides = {
            "gpt-4o": {
                "thinking": {"status": "unsupported", "source": "manual_override"}
            }
        }
        result = apply_capability_overrides(
            "gpt-4o", self._base(), global_overrides, {}
        )
        assert result.thinking.status == "unsupported"
        assert result.thinking.source == "manual_override"

    def test_provider_override_only_matching_provider(self) -> None:
        provider_overrides = {
            "gpt-4o": {"thinking": {"status": "mixed", "source": "provider_catalog"}}
        }
        result = apply_capability_overrides(
            "gpt-4o", self._base(), {}, provider_overrides, provider_id="openai"
        )
        assert result.thinking.status == "mixed"

    def test_both_global_and_provider_wins(self) -> None:
        global_overrides = {
            "gpt-4o": {
                "thinking": {"status": "unsupported", "source": "manual_override"}
            }
        }
        provider_overrides = {
            "gpt-4o": {
                "thinking": {"status": "supported", "source": "provider_catalog"}
            }
        }
        result = apply_capability_overrides(
            "gpt-4o",
            self._base(),
            global_overrides,
            provider_overrides,
            provider_id="openai",
        )
        assert result.thinking.status == "supported"
        assert result.thinking.source == "provider_catalog"

    def test_provider_id_none_skips_provider_layer(self) -> None:
        global_overrides = {
            "gpt-4o": {"thinking": {"status": "mixed", "source": "manual_override"}}
        }
        provider_overrides = {
            "gpt-4o": {
                "thinking": {"status": "unsupported", "source": "manual_override"}
            }
        }
        result = apply_capability_overrides(
            "gpt-4o",
            self._base(),
            global_overrides,
            provider_overrides,
            provider_id=None,
        )
        assert result.thinking.status == "mixed"

    def test_no_leak_across_providers(self) -> None:
        provider_overrides = {
            "gpt-4o": {
                "thinking": {"status": "unsupported", "source": "manual_override"}
            }
        }
        result = apply_capability_overrides(
            "gpt-4o",
            self._base(),
            {},
            provider_overrides,
            provider_id="openai",
        )
        other_result = apply_capability_overrides(
            "gpt-4o",
            self._base(),
            {},
            provider_overrides,
            provider_id="anthropic",
        )
        assert result.thinking.status == "unsupported"
        assert other_result.thinking.status == "unsupported"

    def test_unknown_model_id_returns_base(self) -> None:
        result = apply_capability_overrides("nonexistent-model", self._base(), {}, {})
        assert result.thinking.status == "unknown"
        assert result.thinking.source == "unknown"

    def test_global_only_without_provider_id(self) -> None:
        global_overrides = {
            "claude-3.5": {
                "thinking": {"status": "supported", "source": "manual_override"}
            }
        }
        result = apply_capability_overrides(
            "claude-3.5",
            self._base(),
            global_overrides,
            {},
        )
        assert result.thinking.status == "supported"
        assert result.thinking.source == "manual_override"


# ---------------------------------------------------------------------------
# Dict ↔ typed-model conversion
# ---------------------------------------------------------------------------


class TestDictToModelCapabilities:
    def test_empty_dict(self) -> None:
        caps = dict_to_model_capabilities({})
        assert caps.thinking.status == "unknown"
        assert caps.thinking.native_protocols == []

    def test_thinking_sub_dict(self) -> None:
        caps = dict_to_model_capabilities(
            {
                "thinking": {
                    "status": "supported",
                    "source": "provider_catalog",
                    "native_protocols": ["openai"],
                    "budget_tokens_min": 512,
                    "budget_tokens_max": 64000,
                    "effort_to_budget_tokens": {"low": 500},
                    "notes": "test",
                }
            }
        )
        assert caps.thinking.status == "supported"
        assert caps.thinking.source == "provider_catalog"
        assert caps.thinking.native_protocols == ["openai"]
        assert caps.thinking.budget_tokens_min == 512
        assert caps.thinking.budget_tokens_max == 64000
        assert caps.thinking.effort_to_budget_tokens == {"low": 500}
        assert caps.thinking.notes == "test"

    def test_missing_thinking_key(self) -> None:
        caps = dict_to_model_capabilities({"something": 1})
        assert caps.thinking.status == "unknown"

    def test_non_dict_thinking_value(self) -> None:
        caps = dict_to_model_capabilities({"thinking": "invalid"})
        assert caps.thinking.status == "unknown"

    def test_unknown_status_preserved(self) -> None:
        caps = dict_to_model_capabilities({"thinking": {"status": "unsupported"}})
        assert caps.thinking.status == "unsupported"
        assert caps.thinking.source == "unknown"

    def test_unknown_fields_ignored(self) -> None:
        caps = dict_to_model_capabilities(
            {"thinking": {"status": "supported", "future_field": "value"}}
        )
        assert caps.thinking.status == "supported"


class TestModelCapabilitiesToDict:
    def test_default(self) -> None:
        d = model_capabilities_to_dict(ModelCapabilities())
        assert d == {}

    def test_supported_thinking(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(status="supported", source="model_info")
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["status"] == "supported"
        assert d["thinking"]["source"] == "model_info"
        assert d["supports_tools"] is True

    def test_mixed_thinking(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(status="mixed", source="aggregate")
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["status"] == "mixed"
        assert d["supports_tools"] is True

    def test_unsupported_no_tools_key(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(status="unsupported", source="model_info")
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["status"] == "unsupported"
        assert "supports_tools" not in d

    def test_unknown_omits_thinking_status(self) -> None:
        caps = ModelCapabilities()
        d = model_capabilities_to_dict(caps)
        # Unknown status means no thinking dict entry
        assert "thinking" not in d

    def test_full_round_trip(self) -> None:
        original = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="manual_override",
                native_protocols=["openai", "anthropic"],
                budget_tokens_min=256,
                budget_tokens_max=80000,
                effort_to_budget_tokens={"low": 500, "high": 8000},
                notes="round-trip test",
            )
        )
        d = model_capabilities_to_dict(original)
        restored = dict_to_model_capabilities(d)
        assert restored.thinking.status == original.thinking.status
        assert restored.thinking.source == original.thinking.source
        assert restored.thinking.native_protocols == original.thinking.native_protocols
        assert (
            restored.thinking.budget_tokens_min == original.thinking.budget_tokens_min
        )
        assert (
            restored.thinking.budget_tokens_max == original.thinking.budget_tokens_max
        )
        assert (
            restored.thinking.effort_to_budget_tokens
            == original.thinking.effort_to_budget_tokens
        )
        assert restored.thinking.notes == original.thinking.notes

    def test_notes_included(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported", source="manual_override", notes="important"
            )
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["notes"] == "important"

    def test_native_protocols_included(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="manual_override",
                native_protocols=["anthropic"],
            )
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["native_protocols"] == ["anthropic"]

    def test_client_controls_included(self) -> None:
        from eggpool.catalog.capabilities import ThinkingClientControls

        caps = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="manual_override",
                client_controls={
                    "openai": ThinkingClientControls(
                        request_fields=["reasoning_effort"],
                        response_block_types=["reasoning"],
                    )
                },
            )
        )
        d = model_capabilities_to_dict(caps)
        assert "client_controls" in d["thinking"]
        assert d["thinking"]["client_controls"]["openai"]["request_fields"] == [
            "reasoning_effort"
        ]

    def test_budget_tokens_included(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="manual_override",
                budget_tokens_min=1024,
                budget_tokens_max=100000,
            )
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["budget_tokens_min"] == 1024
        assert d["thinking"]["budget_tokens_max"] == 100000

    def test_effort_to_budget_tokens_included(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="manual_override",
                effort_to_budget_tokens={"low": 1000},
            )
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["effort_to_budget_tokens"] == {"low": 1000}


# ---------------------------------------------------------------------------
# Integration: config → override → capabilities
# ---------------------------------------------------------------------------


class TestConfigToOverrideIntegration:
    def test_config_thinking_override_round_trip(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported",
            source="manual_override",
            native_protocols=["openai"],
            budget_tokens_min=512,
            budget_tokens_max=80000,
            effort_to_budget_tokens={"low": 500, "high": 8000},
            notes="config integration test",
        )
        d = cfg.model_dump(exclude_none=True)
        cap = thinking_override_to_capability(d)
        assert cap.status == "supported"
        assert cap.source == "manual_override"
        assert cap.native_protocols == ["openai"]
        assert cap.budget_tokens_min == 512
        assert cap.budget_tokens_max == 80000
        assert cap.effort_to_budget_tokens == {"low": 500, "high": 8000}
        assert cap.notes == "config integration test"

    def test_config_model_capabilities_override_round_trip(self) -> None:
        cfg = ModelCapabilitiesOverrideConfig(
            thinking=ThinkingCapabilityOverrideConfig(
                status="supported",
                source="manual_override",
                native_protocols=["anthropic"],
            )
        )
        d = cfg.model_dump(exclude_none=True)
        caps = model_capabilities_override_to_config(d)
        assert caps.thinking.status == "supported"
        assert caps.thinking.source == "manual_override"
        assert caps.thinking.native_protocols == ["anthropic"]

    def test_full_flow_config_to_override_chain(self) -> None:
        base = ModelCapabilities(
            thinking=ThinkingCapability(
                status="unknown", source="unknown", native_protocols=[]
            )
        )
        cfg = AppConfig.from_dict(
            {
                "model_capabilities": {
                    "gpt-4o": {
                        "thinking": {
                            "status": "supported",
                            "source": "manual_override",
                            "native_protocols": ["openai"],
                            "notes": "operator override",
                        }
                    }
                }
            }
        )
        override_dict = cfg.model_capabilities["gpt-4o"].model_dump(exclude_none=True)
        override_caps = model_capabilities_override_to_config(override_dict)
        result = merge_model_capabilities(base, override_caps)
        assert result.thinking.status == "supported"
        assert result.thinking.source == "manual_override"
        assert result.thinking.native_protocols == ["openai"]
        assert result.thinking.notes == "operator override"

    def test_empty_config_produces_no_overrides(self) -> None:
        cfg = AppConfig.from_dict({})
        assert cfg.model_capabilities == {}

    def test_provider_scoped_override_via_apply(self) -> None:
        base = ModelCapabilities(
            thinking=ThinkingCapability(status="unknown", source="unknown")
        )
        cfg = AppConfig.from_dict(
            {
                "model_capabilities": {
                    "claude-3.5-sonnet": {
                        "thinking": {
                            "status": "supported",
                            "source": "manual_override",
                            "native_protocols": ["anthropic"],
                        }
                    }
                }
            }
        )
        provider_overrides = {
            model_id: cap.model_dump(exclude_none=True)
            for model_id, cap in cfg.model_capabilities.items()
        }
        result = apply_capability_overrides(
            "claude-3.5-sonnet",
            base,
            global_overrides={},
            provider_overrides=provider_overrides,
            provider_id="anthropic",
        )
        assert result.thinking.status == "supported"
        assert result.thinking.source == "manual_override"
        assert result.thinking.native_protocols == ["anthropic"]

    def test_global_and_provider_separate_model_ids(self) -> None:
        base_gpt = ModelCapabilities(thinking=ThinkingCapability(status="unknown"))
        base_claude = ModelCapabilities(thinking=ThinkingCapability(status="unknown"))
        global_overrides = {
            "gpt-4o": {"thinking": {"status": "supported", "source": "manual_override"}}
        }
        provider_overrides = {
            "claude-3.5-sonnet": {
                "thinking": {"status": "unsupported", "source": "manual_override"}
            }
        }
        result_gpt = apply_capability_overrides(
            "gpt-4o",
            base_gpt,
            global_overrides,
            provider_overrides,
            provider_id="openai",
        )
        result_claude = apply_capability_overrides(
            "claude-3.5-sonnet",
            base_claude,
            global_overrides,
            provider_overrides,
            provider_id="anthropic",
        )
        assert result_gpt.thinking.status == "supported"
        assert result_claude.thinking.status == "unsupported"

    def test_multiple_models_independent_overrides(self) -> None:
        cfg = AppConfig.from_dict(
            {
                "model_capabilities": {
                    "gpt-4o": {
                        "thinking": {
                            "status": "supported",
                            "source": "manual_override",
                        }
                    },
                    "claude-3.5-sonnet": {
                        "thinking": {
                            "status": "unsupported",
                            "source": "manual_override",
                        }
                    },
                    "gemini-pro": {
                        "thinking": {
                            "status": "mixed",
                            "source": "provider_catalog",
                        }
                    },
                }
            }
        )
        assert len(cfg.model_capabilities) == 3

        base = ModelCapabilities(thinking=ThinkingCapability(status="unknown"))
        overrides = {
            model_id: cap.model_dump(exclude_none=True)
            for model_id, cap in cfg.model_capabilities.items()
        }
        result_gpt = apply_capability_overrides("gpt-4o", base, overrides, {})
        result_claude = apply_capability_overrides(
            "claude-3.5-sonnet", base, overrides, {}
        )
        result_gemini = apply_capability_overrides("gemini-pro", base, overrides, {})
        assert result_gpt.thinking.status == "supported"
        assert result_claude.thinking.status == "unsupported"
        assert result_gemini.thinking.status == "mixed"

    def test_dict_conversion_with_overrides(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="manual_override",
                native_protocols=["openai"],
                budget_tokens_min=256,
                budget_tokens_max=64000,
                effort_to_budget_tokens={"low": 500},
                notes="integration",
            )
        )
        d = model_capabilities_to_dict(caps)
        restored = dict_to_model_capabilities(d)
        assert restored.thinking.status == "supported"
        assert restored.thinking.budget_tokens_min == 256
        assert restored.thinking.notes == "integration"
