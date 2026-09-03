"""ThinkingControlContract schema and inference tests."""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
    dict_to_model_capabilities,
    infer_control_contract,
    merge_thinking_capabilities,
    model_capabilities_to_dict,
    serialize_thinking_for_models,
    thinking_override_to_capability,
)


class TestThinkingControlContract:
    """ThinkingControlContract schema tests."""

    def test_default_contract_is_unknown(self) -> None:
        contract = ThinkingControlContract()
        assert contract.mode == "unknown"
        assert contract.request_fields == []
        assert contract.accepted_efforts == []
        assert contract.effort_aliases == {}
        assert contract.effort_to_budget_tokens is None
        assert contract.explicit_budget_min is None
        assert contract.explicit_budget_max is None
        assert contract.historical_reasoning_content == "unknown"
        assert contract.source == "unknown"

    def test_fixed_contract(self) -> None:
        contract = ThinkingControlContract(
            mode="fixed",
            source="manual_override",
        )
        assert contract.mode == "fixed"
        assert contract.accepted_efforts == []

    def test_effort_contract(self) -> None:
        contract = ThinkingControlContract(
            mode="effort",
            request_fields=["reasoning_effort"],
            accepted_efforts=["low", "medium", "high"],
            effort_aliases={"med": "medium"},
            source="provider_catalog",
        )
        assert contract.mode == "effort"
        assert "low" in contract.accepted_efforts
        assert contract.effort_aliases["med"] == "medium"

    def test_budget_contract(self) -> None:
        contract = ThinkingControlContract(
            mode="budget",
            request_fields=["thinking"],
            explicit_budget_min=1024,
            explicit_budget_max=128000,
            source="manual_override",
        )
        assert contract.mode == "budget"
        assert contract.explicit_budget_min == 1024
        assert contract.explicit_budget_max == 128000

    def test_effort_or_budget_contract(self) -> None:
        contract = ThinkingControlContract(
            mode="effort_or_budget",
            request_fields=["thinking"],
            accepted_efforts=["low", "medium", "high"],
            effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 16384},
            explicit_budget_min=1024,
            explicit_budget_max=128000,
            source="provider_catalog",
        )
        assert contract.mode == "effort_or_budget"
        assert contract.effort_to_budget_tokens is not None
        assert contract.effort_to_budget_tokens["high"] == 16384

    def test_none_contract(self) -> None:
        contract = ThinkingControlContract(
            mode="none",
            source="manual_override",
        )
        assert contract.mode == "none"


class TestInferControlContract:
    """Tests for infer_control_contract helper."""

    def test_explicit_contract_preserved(self) -> None:
        explicit = ThinkingControlContract(mode="fixed", source="manual_override")
        cap = ThinkingCapability(
            status="supported",
            control_contract=explicit,
        )
        result = infer_control_contract(cap)
        assert result.mode == "fixed"
        assert result.source == "manual_override"

    def test_unsupported_infers_none(self) -> None:
        cap = ThinkingCapability(status="unsupported")
        result = infer_control_contract(cap)
        assert result.mode == "none"

    def test_supported_with_efforts_infers_effort(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            supported_efforts=["low", "medium", "high"],
            effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 16384},
            budget_tokens_min=512,
            budget_tokens_max=65536,
        )
        result = infer_control_contract(cap)
        assert result.mode == "effort"
        assert result.accepted_efforts == ["low", "medium", "high"]
        assert result.effort_to_budget_tokens == {
            "low": 1024,
            "medium": 4096,
            "high": 16384,
        }
        assert result.explicit_budget_min == 512
        assert result.explicit_budget_max == 65536

    def test_budget_bounds_only_infers_budget(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            budget_tokens_min=1024,
            budget_tokens_max=128000,
        )
        result = infer_control_contract(cap)
        assert result.mode == "budget"
        assert result.explicit_budget_min == 1024
        assert result.explicit_budget_max == 128000

    def test_supported_no_controls_infers_unknown(self) -> None:
        cap = ThinkingCapability(status="supported")
        result = infer_control_contract(cap)
        assert result.mode == "unknown"

    def test_unknown_status_infers_unknown(self) -> None:
        cap = ThinkingCapability(status="unknown")
        result = infer_control_contract(cap)
        assert result.mode == "unknown"


class TestThinkingRequestIntent:
    """ThinkingRequestIntent dataclass tests."""

    def test_defaults(self) -> None:
        intent = ThinkingRequestIntent()
        assert intent.requested_effort is None
        assert intent.requested_budget_tokens is None
        assert intent.request_fields == ()
        assert intent.has_historical_reasoning_content is False
        assert intent.client_requests_new_reasoning is False

    def test_frozen(self) -> None:
        intent = ThinkingRequestIntent(requested_effort="high")
        with pytest.raises(AttributeError):
            intent.requested_effort = "low"  # type: ignore[misc]


class TestMergeThinkingCapabilities:
    """Tests for merge_thinking_capabilities with control_contract."""

    def test_override_wins_contract(self) -> None:
        base = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(mode="unknown"),
        )
        override = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="fixed",
                source="manual_override",
            ),
        )
        merged = merge_thinking_capabilities(base, override)
        assert merged.control_contract.mode == "fixed"
        assert merged.control_contract.source == "manual_override"

    def test_base_preserved_when_override_default(self) -> None:
        base = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="effort",
                accepted_efforts=["low", "high"],
            ),
        )
        override = ThinkingCapability()  # all defaults
        merged = merge_thinking_capabilities(base, override)
        assert merged.control_contract.mode == "effort"


class TestSerializeThinkingForModels:
    """Tests for serialize_thinking_for_models with control_contract."""

    def test_contract_emitted_when_not_unknown(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="effort",
                accepted_efforts=["low", "medium", "high"],
                source="provider_catalog",
            ),
        )
        result = serialize_thinking_for_models(cap)
        assert "control_contract" in result
        cc = result["control_contract"]
        assert isinstance(cc, dict)
        assert cc["effort"] == "supported"
        assert cc["toggle"] == "unknown"
        assert cc["accepted_efforts"] == ["low", "medium", "high"]

    def test_contract_omitted_when_unknown(self) -> None:
        cap = ThinkingCapability(status="unknown")
        result = serialize_thinking_for_models(cap)
        assert "control_contract" not in result


class TestDictRoundTrip:
    """Round-trip test for dict_to_model_capabilities / model_capabilities_to_dict."""

    def test_control_contract_round_trip(self) -> None:
        original = {
            "thinking": {
                "status": "supported",
                "source": "provider_catalog",
                "control_contract": {
                    "mode": "effort",
                    "accepted_efforts": ["low", "medium", "high"],
                    "effort_aliases": {"med": "medium"},
                    "effort_to_budget_tokens": {
                        "low": 1024,
                        "medium": 4096,
                        "high": 16384,
                    },
                    "explicit_budget_min": 512,
                    "explicit_budget_max": 65536,
                    "historical_reasoning_content": "accepted",
                    "source": "manual_override",
                },
            },
        }
        caps = dict_to_model_capabilities(original)
        assert caps.thinking.control_contract.mode == "effort"
        assert caps.thinking.control_contract.accepted_efforts == [
            "low",
            "medium",
            "high",
        ]
        assert caps.thinking.control_contract.effort_aliases == {"med": "medium"}
        assert caps.thinking.control_contract.effort_to_budget_tokens == {
            "low": 1024,
            "medium": 4096,
            "high": 16384,
        }
        assert caps.thinking.control_contract.explicit_budget_min == 512
        assert caps.thinking.control_contract.explicit_budget_max == 65536
        assert caps.thinking.control_contract.historical_reasoning_content == "accepted"

        # Round-trip back to dict.
        output = model_capabilities_to_dict(caps)
        assert "thinking" in output
        tc = output["thinking"]
        assert isinstance(tc, dict)
        assert "control_contract" in tc
        cc_out = tc["control_contract"]
        assert isinstance(cc_out, dict)
        assert cc_out["effort"] == "supported"
        assert cc_out["toggle"] == "unknown"
        assert cc_out["accepted_efforts"] == ["low", "medium", "high"]

    def test_legacy_capability_without_contract(self) -> None:
        """Existing capability records without control_contract still work."""
        original = {
            "thinking": {
                "status": "supported",
                "supported_efforts": ["low", "medium", "high"],
            },
        }
        caps = dict_to_model_capabilities(original)
        assert caps.thinking.status == "supported"
        assert caps.thinking.control_contract.mode == "unknown"
        # infer_control_contract should derive the correct mode.
        from eggpool.catalog.capabilities import infer_control_contract

        inferred = infer_control_contract(caps.thinking)
        assert inferred.mode == "effort"

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (
                "fixed",
                {
                    "toggle": "unsupported",
                    "effort": "unsupported",
                    "budget": "unsupported",
                },
            ),
            (
                "effort",
                {"toggle": "unknown", "effort": "supported", "budget": "unknown"},
            ),
            (
                "effort_or_budget",
                {"toggle": "unknown", "effort": "supported", "budget": "supported"},
            ),
        ],
    )
    def test_legacy_modes_decode_to_dimensions(
        self, mode: str, expected: dict[str, str]
    ) -> None:
        caps = dict_to_model_capabilities(
            {"thinking": {"status": "supported", "control_contract": {"mode": mode}}}
        )
        contract = caps.thinking.control_contract
        assert {name: getattr(contract, name) for name in expected} == expected

        serialized = model_capabilities_to_dict(caps)
        contract_dict = serialized["thinking"]["control_contract"]
        assert "mode" not in contract_dict


class TestThinkingOverrideToCapability:
    """Tests for thinking_override_to_capability with control_contract."""

    def test_override_with_contract(self) -> None:
        override = {
            "status": "supported",
            "control_contract": {
                "mode": "fixed",
                "source": "manual_override",
            },
        }
        cap = thinking_override_to_capability(override)
        assert cap.control_contract.mode == "fixed"
        assert cap.control_contract.source == "manual_override"

    def test_override_without_contract(self) -> None:
        override = {"status": "supported"}
        cap = thinking_override_to_capability(override)
        assert cap.control_contract.mode == "unknown"
