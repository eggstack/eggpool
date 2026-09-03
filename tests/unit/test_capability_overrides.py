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
    TranscodingCapabilities,
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
    MediaCapabilityOverrideConfig,
    ModelCapabilitiesOverrideConfig,
    MultimodalCapabilityOverrideConfig,
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

    def test_independent_control_overrides(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig(
            status="supported",
            toggle="supported",
            effort="unsupported",
            budget="supported",
        )
        assert cfg.toggle == "supported"
        assert cfg.effort == "unsupported"
        assert cfg.budget == "supported"

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

    def test_control_fact_does_not_require_status(self) -> None:
        cfg = ThinkingCapabilityOverrideConfig.model_validate(
            {
                "effort": "supported",
                "supported_efforts": ["low", "medium", "high"],
            }
        )
        assert cfg.status is None
        assert cfg.source == "manual_override"
        assert cfg.effort == "supported"
        assert cfg.supported_efforts == ["low", "medium", "high"]


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

    def test_wraps_provider_cache_contract(self) -> None:
        cfg = ModelCapabilitiesOverrideConfig(
            transcoding=TranscodingCapabilities(
                prompt_cache_breakpoints={
                    "openai": {
                        "dialect": "compatible_extension",
                        "supported_ttls": ["30m"],
                        "default_ttl": "30m",
                    }
                }
            )
        )
        assert cfg.transcoding is not None
        contract = cfg.transcoding.prompt_cache_capability("openai")
        assert contract is not None
        assert contract.dialect == "compatible_extension"

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

    def test_independent_control_override_conversion(self) -> None:
        cap = thinking_override_to_capability(
            {
                "status": "supported",
                "toggle": "supported",
                "effort": "unsupported",
                "budget": "supported",
            }
        )
        assert cap.control_contract.toggle == "supported"
        assert cap.control_contract.effort == "unsupported"
        assert cap.control_contract.budget == "supported"

    def test_explicit_empty_legacy_efforts_disable_effort_only(self) -> None:
        cap = thinking_override_to_capability(
            {"status": "supported", "supported_efforts": []}
        )
        assert cap.control_contract.effort == "unsupported"
        assert cap.control_contract.toggle == "unknown"
        assert cap.control_contract.budget == "unknown"

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
        # Phase F: ``supports_tools`` is no longer a top-level capability
        # surface; tool support is owned by transcoder features.
        assert "supports_tools" not in d

    def test_mixed_thinking(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(status="mixed", source="aggregate")
        )
        d = model_capabilities_to_dict(caps)
        assert d["thinking"]["status"] == "mixed"
        # Phase F: ``supports_tools`` is no longer a top-level capability
        # surface; tool support is owned by transcoder features, not
        # ``ModelCapabilities``. Asserting its absence here pins the
        # removal so a regression re-introducing it is caught.
        assert "supports_tools" not in d

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


# ---------------------------------------------------------------------------
# MediaCapabilityOverrideConfig validation
# ---------------------------------------------------------------------------


class TestMediaCapabilityOverrideConfig:
    def test_default_construction(self) -> None:
        cfg = MediaCapabilityOverrideConfig()
        assert cfg.base64 is None
        assert cfg.url is None
        assert cfg.max_source_bytes is None

    def test_enable_base64(self) -> None:
        cfg = MediaCapabilityOverrideConfig(base64=True)
        assert cfg.base64 is True

    def test_enable_url(self) -> None:
        cfg = MediaCapabilityOverrideConfig(url=True)
        assert cfg.url is True

    def test_false_treated_as_none(self) -> None:
        cfg = MediaCapabilityOverrideConfig(base64=False, url=False)
        assert cfg.base64 is None
        assert cfg.url is None

    def test_max_source_bytes_positive(self) -> None:
        cfg = MediaCapabilityOverrideConfig(max_source_bytes=1024)
        assert cfg.max_source_bytes == 1024

    def test_max_source_bytes_zero_rejected(self) -> None:
        with pytest.raises(ConfigError, match="max_source_bytes must be > 0"):
            MediaCapabilityOverrideConfig(max_source_bytes=0)

    def test_max_source_bytes_negative_rejected(self) -> None:
        with pytest.raises(ConfigError, match="max_source_bytes must be > 0"):
            MediaCapabilityOverrideConfig(max_source_bytes=-1)

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MediaCapabilityOverrideConfig.model_validate(
                {"base64": True, "unknown_field": "value"}
            )


# ---------------------------------------------------------------------------
# MultimodalCapabilityOverrideConfig validation
# ---------------------------------------------------------------------------


class TestMultimodalCapabilityOverrideConfig:
    def test_default_construction(self) -> None:
        cfg = MultimodalCapabilityOverrideConfig()
        assert cfg.image_input is None
        assert cfg.document_input is None
        assert cfg.audio_input is None
        assert cfg.non_text_tool_result is None
        assert cfg.max_serialized_request_bytes is None

    def test_image_input(self) -> None:
        img = MediaCapabilityOverrideConfig(base64=True, url=True)
        cfg = MultimodalCapabilityOverrideConfig(image_input=img)
        assert cfg.image_input is not None
        assert cfg.image_input.base64 is True
        assert cfg.image_input.url is True

    def test_document_input(self) -> None:
        doc = MediaCapabilityOverrideConfig(url=True)
        cfg = MultimodalCapabilityOverrideConfig(document_input=doc)
        assert cfg.document_input is not None
        assert cfg.document_input.url is True

    def test_audio_input(self) -> None:
        aud = MediaCapabilityOverrideConfig(base64=True)
        cfg = MultimodalCapabilityOverrideConfig(audio_input=aud)
        assert cfg.audio_input is not None
        assert cfg.audio_input.base64 is True

    def test_non_text_tool_result(self) -> None:
        cfg = MultimodalCapabilityOverrideConfig(non_text_tool_result=True)
        assert cfg.non_text_tool_result is True

    def test_max_serialized_request_bytes(self) -> None:
        cfg = MultimodalCapabilityOverrideConfig(max_serialized_request_bytes=1048576)
        assert cfg.max_serialized_request_bytes == 1048576

    def test_max_serialized_request_bytes_zero_rejected(self) -> None:
        with pytest.raises(
            ConfigError, match="max_serialized_request_bytes must be > 0"
        ):
            MultimodalCapabilityOverrideConfig(max_serialized_request_bytes=0)

    def test_max_serialized_request_bytes_negative_rejected(self) -> None:
        with pytest.raises(
            ConfigError, match="max_serialized_request_bytes must be > 0"
        ):
            MultimodalCapabilityOverrideConfig(max_serialized_request_bytes=-1)

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MultimodalCapabilityOverrideConfig.model_validate(
                {"non_text_tool_result": True, "unknown_field": "value"}
            )


# ---------------------------------------------------------------------------
# Multimodal override conversion
# ---------------------------------------------------------------------------


class TestMultimodalOverrideToCapability:
    def test_none_input(self) -> None:
        cap = model_capabilities_override_to_config(None)
        assert cap.multimodal.image_input.base64 is False
        assert cap.multimodal.image_input.url is False
        assert cap.multimodal.document_input.base64 is False

    def test_empty_dict(self) -> None:
        cap = model_capabilities_override_to_config({})
        assert cap.multimodal.image_input.base64 is False

    def test_multimodal_image_base64(self) -> None:
        override = {"multimodal": {"image_input": {"base64": True}}}
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.image_input.base64 is True
        assert cap.multimodal.image_input.url is False

    def test_multimodal_image_url(self) -> None:
        override = {"multimodal": {"image_input": {"url": True}}}
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.image_input.url is True
        assert cap.multimodal.image_input.base64 is False

    def test_multimodal_both_modalities(self) -> None:
        override = {
            "multimodal": {
                "image_input": {"base64": True, "url": True},
                "document_input": {"url": True},
            }
        }
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.image_input.base64 is True
        assert cap.multimodal.image_input.url is True
        assert cap.multimodal.document_input.url is True

    def test_multimodal_non_text_tool_result(self) -> None:
        override = {"multimodal": {"non_text_tool_result": True}}
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.non_text_tool_result is True

    def test_multimodal_max_serialized_request_bytes(self) -> None:
        override = {"multimodal": {"max_serialized_request_bytes": 2097152}}
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.max_serialized_request_bytes == 2097152

    def test_multimodal_with_max_source_bytes(self) -> None:
        override = {
            "multimodal": {"image_input": {"base64": True, "max_source_bytes": 5242880}}
        }
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.image_input.base64 is True
        assert cap.multimodal.image_input.max_source_bytes == 5242880

    def test_multimodal_mixed_with_thinking(self) -> None:
        override = {
            "thinking": {"status": "supported"},
            "multimodal": {"image_input": {"url": True}},
        }
        cap = model_capabilities_override_to_config(override)
        assert cap.thinking.status == "supported"
        assert cap.multimodal.image_input.url is True

    def test_unknown_multimodal_key_ignored(self) -> None:
        override = {"multimodal": {"unknown_modality": True}}
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.image_input.base64 is False

    def test_invalid_multimodal_dict_ignored(self) -> None:
        override = {"multimodal": "not_a_dict"}
        cap = model_capabilities_override_to_config(override)
        assert cap.multimodal.image_input.base64 is False


# ---------------------------------------------------------------------------
# Multimodal config → dict → ModelCapabilities roundtrip
# ---------------------------------------------------------------------------


class TestMultimodalConfigRoundtrip:
    def test_roundtrip_image_base64(self) -> None:
        override = {"multimodal": {"image_input": {"base64": True}}}
        cap = model_capabilities_override_to_config(override)
        d = model_capabilities_to_dict(cap)
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal.image_input.base64 is True
        assert restored.multimodal.image_input.url is False

    def test_roundtrip_full_multimodal(self) -> None:
        override = {
            "multimodal": {
                "image_input": {"base64": True, "url": True},
                "document_input": {"url": True, "max_source_bytes": 1048576},
                "audio_input": {"base64": True},
                "non_text_tool_result": True,
                "max_serialized_request_bytes": 2097152,
            }
        }
        cap = model_capabilities_override_to_config(override)
        d = model_capabilities_to_dict(cap)
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal.image_input.base64 is True
        assert restored.multimodal.image_input.url is True
        assert restored.multimodal.document_input.url is True
        assert restored.multimodal.document_input.max_source_bytes == 1048576
        assert restored.multimodal.audio_input.base64 is True
        assert restored.multimodal.non_text_tool_result is True
        assert restored.multimodal.max_serialized_request_bytes == 2097152

    def test_roundtrip_thinking_and_multimodal(self) -> None:
        override = {
            "thinking": {"status": "supported", "budget_tokens_min": 1024},
            "multimodal": {"image_input": {"url": True}},
        }
        cap = model_capabilities_override_to_config(override)
        d = model_capabilities_to_dict(cap)
        restored = dict_to_model_capabilities(d)
        assert restored.thinking.status == "supported"
        assert restored.thinking.budget_tokens_min == 1024
        assert restored.multimodal.image_input.url is True


# ---------------------------------------------------------------------------
# ModelCapabilitiesOverrideConfig with multimodal
# ---------------------------------------------------------------------------


class TestModelCapabilitiesOverrideConfigMultimodal:
    def test_default_construction(self) -> None:
        cfg = ModelCapabilitiesOverrideConfig()
        assert cfg.multimodal is None

    def test_wraps_multimodal(self) -> None:
        inner = MultimodalCapabilityOverrideConfig(
            image_input=MediaCapabilityOverrideConfig(base64=True)
        )
        cfg = ModelCapabilitiesOverrideConfig(multimodal=inner)
        assert cfg.multimodal is not None
        assert cfg.multimodal.image_input is not None
        assert cfg.multimodal.image_input.base64 is True

    def test_model_dump_roundtrip(self) -> None:
        cfg = ModelCapabilitiesOverrideConfig(
            multimodal=MultimodalCapabilityOverrideConfig(
                image_input=MediaCapabilityOverrideConfig(base64=True, url=True),
                non_text_tool_result=True,
            )
        )
        d = cfg.model_dump(exclude_none=True)
        assert "multimodal" in d
        assert d["multimodal"]["image_input"]["base64"] is True
        assert d["multimodal"]["image_input"]["url"] is True
        assert d["multimodal"]["non_text_tool_result"] is True

    def test_model_validate_roundtrip(self) -> None:
        data = {
            "multimodal": {
                "image_input": {"base64": True},
                "non_text_tool_result": True,
            }
        }
        cfg = ModelCapabilitiesOverrideConfig.model_validate(data)
        assert cfg.multimodal is not None
        assert cfg.multimodal.image_input is not None
        assert cfg.multimodal.image_input.base64 is True
        assert cfg.multimodal.non_text_tool_result is True

    def test_model_validate_invalid_multimodal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelCapabilitiesOverrideConfig.model_validate(
                {"multimodal": {"image_input": {"base64": "not_bool"}}}
            )


# ---------------------------------------------------------------------------
# MULTIMODAL_LOSS_KINDS constants
# ---------------------------------------------------------------------------


class TestMultimodalLossKinds:
    def test_loss_kinds_defined(self) -> None:
        from eggpool.transcoder.errors import MULTIMODAL_LOSS_KINDS

        assert len(MULTIMODAL_LOSS_KINDS) == 4

    def test_unsupported_modality(self) -> None:
        from eggpool.transcoder.errors import MULTIMODAL_LOSS_KINDS

        assert "unsupported_modality" in MULTIMODAL_LOSS_KINDS

    def test_unsupported_source_form(self) -> None:
        from eggpool.transcoder.errors import MULTIMODAL_LOSS_KINDS

        assert "unsupported_source_form" in MULTIMODAL_LOSS_KINDS

    def test_media_tool_result_flattened(self) -> None:
        from eggpool.transcoder.errors import MULTIMODAL_LOSS_KINDS

        assert "media_tool_result_flattened" in MULTIMODAL_LOSS_KINDS

    def test_document_media_type_unsupported(self) -> None:
        from eggpool.transcoder.errors import MULTIMODAL_LOSS_KINDS

        assert "document_media_type_unsupported" in MULTIMODAL_LOSS_KINDS

    def test_distinct_from_cache_control_kinds(self) -> None:
        from eggpool.transcoder.errors import (
            CACHE_CONTROL_LOSS_KINDS,
            MULTIMODAL_LOSS_KINDS,
        )

        overlap = MULTIMODAL_LOSS_KINDS & CACHE_CONTROL_LOSS_KINDS
        assert overlap == set(), (
            f"Multimodal and cache-control kinds overlap: {overlap}"
        )
