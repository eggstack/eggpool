"""Tests for the canonical thinking capability schema."""

from __future__ import annotations

from eggpool.catalog.capabilities import (
    ModelCapabilities,
    ThinkingCapability,
    ThinkingClientControls,
    aggregate_model_capabilities,
    aggregate_thinking_capabilities,
    aggregate_thinking_status,
    client_requests_thinking,
    has_thinking_support,
    merge_model_capabilities,
    merge_thinking_capabilities,
    serialize_model_capabilities,
    serialize_thinking_for_models,
)

# ---------------------------------------------------------------------------
# Default construction
# ---------------------------------------------------------------------------


class TestDefaultConstruction:
    def test_thinking_capability_defaults(self) -> None:
        tc = ThinkingCapability()
        assert tc.status == "unknown"
        assert tc.source == "unknown"
        assert tc.native_protocols == []
        assert tc.client_controls == {}
        assert tc.budget_tokens_min is None
        assert tc.budget_tokens_max is None
        assert tc.effort_to_budget_tokens is None
        assert tc.notes is None

    def test_model_capabilities_defaults(self) -> None:
        mc = ModelCapabilities()
        assert mc.thinking.status == "unknown"
        assert mc.thinking.source == "unknown"

    def test_thinking_client_controls_defaults(self) -> None:
        tcc = ThinkingClientControls()
        assert tcc.request_fields == []
        assert tcc.response_fields == []
        assert tcc.stream_delta_fields == []
        assert tcc.response_block_types == []

    def test_thinking_capability_with_values(self) -> None:
        tc = ThinkingCapability(
            status="supported",
            source="provider_catalog",
            native_protocols=["anthropic"],
            client_controls={
                "anthropic": ThinkingClientControls(
                    request_fields=["thinking"],
                    response_block_types=["thinking"],
                ),
            },
            budget_tokens_min=1024,
            budget_tokens_max=100000,
        )
        assert tc.status == "supported"
        assert tc.source == "provider_catalog"
        assert tc.native_protocols == ["anthropic"]
        assert "anthropic" in tc.client_controls
        assert tc.budget_tokens_min == 1024
        assert tc.budget_tokens_max == 100000


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------


class TestMergeThinkingCapabilities:
    def test_merge_empty_over_default(self) -> None:
        base = ThinkingCapability()
        override = ThinkingCapability()
        result = merge_thinking_capabilities(base, override)
        assert result.status == "unknown"
        assert result.source == "unknown"

    def test_merge_override_wins_when_base_is_unknown(self) -> None:
        base = ThinkingCapability()
        override = ThinkingCapability(status="supported", source="model_info")
        result = merge_thinking_capabilities(base, override)
        assert result.status == "supported"
        assert result.source == "model_info"

    def test_merge_higher_priority_wins(self) -> None:
        base = ThinkingCapability(status="unsupported", source="provider_catalog")
        override = ThinkingCapability(status="supported", source="model_info")
        result = merge_thinking_capabilities(base, override)
        assert result.status == "supported"
        assert result.source == "model_info"

    def test_merge_equal_priority_prefers_override(self) -> None:
        base = ThinkingCapability(status="supported", source="provider_catalog")
        override = ThinkingCapability(status="supported", source="model_info")
        result = merge_thinking_capabilities(base, override)
        assert result.status == "supported"
        assert result.source == "model_info"

    def test_merge_base_wins_when_override_is_unknown(self) -> None:
        base = ThinkingCapability(status="supported", source="provider_catalog")
        override = ThinkingCapability()
        result = merge_thinking_capabilities(base, override)
        assert result.status == "supported"
        assert result.source == "provider_catalog"

    def test_merge_native_protocols_union(self) -> None:
        base = ThinkingCapability(native_protocols=["openai"])
        override = ThinkingCapability(
            status="supported",
            native_protocols=["anthropic"],
        )
        result = merge_thinking_capabilities(base, override)
        assert set(result.native_protocols) == {"openai", "anthropic"}

    def test_merge_client_controls_override_wins(self) -> None:
        base_controls = ThinkingClientControls(request_fields=["old"])
        override_controls = ThinkingClientControls(request_fields=["new"])
        base = ThinkingCapability(
            client_controls={"openai": base_controls},
        )
        override = ThinkingCapability(
            status="supported",
            client_controls={"openai": override_controls},
        )
        result = merge_thinking_capabilities(base, override)
        assert result.client_controls["openai"].request_fields == ["new"]

    def test_merge_client_controls_base_fills_gaps(self) -> None:
        base_controls = ThinkingClientControls(request_fields=["base"])
        base = ThinkingCapability(
            client_controls={"openai": base_controls},
        )
        override = ThinkingCapability(
            status="supported",
            client_controls={
                "anthropic": ThinkingClientControls(
                    request_fields=["override"],
                )
            },
        )
        result = merge_thinking_capabilities(base, override)
        assert result.client_controls["openai"].request_fields == ["base"]
        assert result.client_controls["anthropic"].request_fields == ["override"]

    def test_merge_budget_tokens_override_wins(self) -> None:
        base = ThinkingCapability(budget_tokens_min=100, budget_tokens_max=50000)
        override = ThinkingCapability(
            status="supported",
            budget_tokens_min=200,
            budget_tokens_max=80000,
        )
        result = merge_thinking_capabilities(base, override)
        assert result.budget_tokens_min == 200
        assert result.budget_tokens_max == 80000

    def test_merge_budget_tokens_partial_override(self) -> None:
        base = ThinkingCapability(budget_tokens_min=100, budget_tokens_max=50000)
        override = ThinkingCapability(status="supported", budget_tokens_max=80000)
        result = merge_thinking_capabilities(base, override)
        assert result.budget_tokens_min == 100
        assert result.budget_tokens_max == 80000

    def test_merge_effort_to_budget_tokens_override_wins(self) -> None:
        base = ThinkingCapability(
            effort_to_budget_tokens={"low": 1000, "high": 10000},
        )
        override = ThinkingCapability(
            status="supported",
            effort_to_budget_tokens={"low": 2000},
        )
        result = merge_thinking_capabilities(base, override)
        assert result.effort_to_budget_tokens == {"low": 2000}

    def test_merge_notes_override_wins(self) -> None:
        base = ThinkingCapability(notes="base note")
        override = ThinkingCapability(status="supported", notes="override note")
        result = merge_thinking_capabilities(base, override)
        assert result.notes == "override note"

    def test_merge_preserves_base_notes_when_override_empty(self) -> None:
        base = ThinkingCapability(notes="base note")
        override = ThinkingCapability()
        result = merge_thinking_capabilities(base, override)
        assert result.notes == "base note"


class TestMergeModelCapabilities:
    def test_merge_delegates_to_thinking(self) -> None:
        base = ModelCapabilities(
            thinking=ThinkingCapability(status="unknown"),
        )
        override = ModelCapabilities(
            thinking=ThinkingCapability(status="supported", source="model_info"),
        )
        result = merge_model_capabilities(base, override)
        assert result.thinking.status == "supported"
        assert result.thinking.source == "model_info"


# ---------------------------------------------------------------------------
# Aggregate semantics
# ---------------------------------------------------------------------------


class TestAggregateThinkingStatus:
    def test_empty_list(self) -> None:
        assert aggregate_thinking_status([]) == "unknown"

    def test_all_unknown(self) -> None:
        assert aggregate_thinking_status(["unknown", "unknown"]) == "unknown"

    def test_all_supported(self) -> None:
        assert aggregate_thinking_status(["supported", "supported"]) == "supported"

    def test_all_unsupported(self) -> None:
        assert (
            aggregate_thinking_status(["unsupported", "unsupported"]) == "unsupported"
        )

    def test_mixed_supported_and_unsupported(self) -> None:
        assert aggregate_thinking_status(["supported", "unsupported"]) == "mixed"

    def test_mixed_supported_and_unknown(self) -> None:
        assert aggregate_thinking_status(["supported", "unknown"]) == "mixed"

    def test_mixed_unsupported_and_unknown(self) -> None:
        assert aggregate_thinking_status(["unsupported", "unknown"]) == "mixed"

    def test_conflicting_takes_precedence_over_mixed(self) -> None:
        assert (
            aggregate_thinking_status(["supported", "unsupported", "conflicting"])
            == "conflicting"
        )

    def test_single_supported(self) -> None:
        assert aggregate_thinking_status(["supported"]) == "supported"

    def test_single_unknown(self) -> None:
        assert aggregate_thinking_status(["unknown"]) == "unknown"


class TestAggregateThinkingCapabilities:
    def test_empty_list(self) -> None:
        result = aggregate_thinking_capabilities([])
        assert result.status == "unknown"
        assert result.source == "unknown"
        assert result.native_protocols == []

    def test_single_capability(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            source="model_info",
            native_protocols=["anthropic"],
        )
        result = aggregate_thinking_capabilities([cap])
        assert result.status == "supported"
        assert result.native_protocols == ["anthropic"]

    def test_multiple_same_status(self) -> None:
        caps = [
            ThinkingCapability(status="supported", native_protocols=["openai"]),
            ThinkingCapability(status="supported", native_protocols=["anthropic"]),
        ]
        result = aggregate_thinking_capabilities(caps)
        assert result.status == "supported"
        assert set(result.native_protocols) == {"openai", "anthropic"}

    def test_multiple_mixed_status(self) -> None:
        caps = [
            ThinkingCapability(status="supported"),
            ThinkingCapability(status="unsupported"),
        ]
        result = aggregate_thinking_capabilities(caps)
        assert result.status == "mixed"

    def test_budget_conservative_merge(self) -> None:
        caps = [
            ThinkingCapability(budget_tokens_min=100, budget_tokens_max=80000),
            ThinkingCapability(budget_tokens_min=500, budget_tokens_max=120000),
        ]
        result = aggregate_thinking_capabilities(caps)
        assert result.budget_tokens_min == 500  # max of mins
        assert result.budget_tokens_max == 80000  # min of maxes

    def test_budget_invariant_violation_resets(self) -> None:
        caps = [
            ThinkingCapability(budget_tokens_min=100, budget_tokens_max=200),
            ThinkingCapability(budget_tokens_min=500, budget_tokens_max=600),
        ]
        result = aggregate_thinking_capabilities(caps)
        # min(500) > max(200) => invariant violated => None
        assert result.budget_tokens_min is None
        assert result.budget_tokens_max is None

    def test_effort_last_wins(self) -> None:
        caps = [
            ThinkingCapability(effort_to_budget_tokens={"low": 1000}),
            ThinkingCapability(effort_to_budget_tokens={"low": 2000, "high": 8000}),
        ]
        result = aggregate_thinking_capabilities(caps)
        assert result.effort_to_budget_tokens == {"low": 2000, "high": 8000}

    def test_client_controls_last_wins_per_protocol(self) -> None:
        caps = [
            ThinkingCapability(
                client_controls={
                    "openai": ThinkingClientControls(request_fields=["old"]),
                },
            ),
            ThinkingCapability(
                client_controls={
                    "openai": ThinkingClientControls(request_fields=["new"]),
                    "anthropic": ThinkingClientControls(request_fields=["a"]),
                },
            ),
        ]
        result = aggregate_thinking_capabilities(caps)
        assert result.client_controls["openai"].request_fields == ["new"]
        assert result.client_controls["anthropic"].request_fields == ["a"]


class TestAggregateModelCapabilities:
    def test_empty_list(self) -> None:
        result = aggregate_model_capabilities([])
        assert result.thinking.status == "unknown"

    def test_delegates_to_thinking(self) -> None:
        caps = [
            ModelCapabilities(
                thinking=ThinkingCapability(status="supported"),
            ),
            ModelCapabilities(
                thinking=ThinkingCapability(status="unsupported"),
            ),
        ]
        result = aggregate_model_capabilities(caps)
        assert result.thinking.status == "mixed"


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


class TestSerializeThinkingForModels:
    def test_minimal(self) -> None:
        tc = ThinkingCapability()
        result = serialize_thinking_for_models(tc)
        assert result == {"status": "unknown"}

    def test_full(self) -> None:
        tc = ThinkingCapability(
            status="supported",
            source="model_info",
            native_protocols=["openai", "anthropic"],
            budget_tokens_min=1024,
            budget_tokens_max=100000,
            effort_to_budget_tokens={"low": 1000, "high": 10000},
        )
        result = serialize_thinking_for_models(tc)
        assert result["status"] == "supported"
        assert result["source"] == "model_info"
        assert result["native_protocols"] == ["openai", "anthropic"]
        assert result["budget_tokens_min"] == 1024
        assert result["budget_tokens_max"] == 100000
        assert result["effort_to_budget_tokens"] == {"low": 1000, "high": 10000}

    def test_omits_unknown_source(self) -> None:
        tc = ThinkingCapability(status="unknown", source="unknown")
        result = serialize_thinking_for_models(tc)
        assert "source" not in result

    def test_omits_none_budget(self) -> None:
        tc = ThinkingCapability(status="supported", source="model_info")
        result = serialize_thinking_for_models(tc)
        assert "budget_tokens_min" not in result
        assert "budget_tokens_max" not in result


class TestSerializeModelCapabilities:
    def test_empty(self) -> None:
        mc = ModelCapabilities()
        result = serialize_model_capabilities(mc)
        assert result == {"thinking": {"status": "unknown"}}

    def test_with_thinking(self) -> None:
        mc = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                source="provider_catalog",
            ),
        )
        result = serialize_model_capabilities(mc)
        assert result["thinking"]["status"] == "supported"
        assert result["thinking"]["source"] == "provider_catalog"


# ---------------------------------------------------------------------------
# Request-level helpers
# ---------------------------------------------------------------------------


class TestClientRequestsThinking:
    def test_no_thinking_key(self) -> None:
        cap = ThinkingCapability(status="supported")
        assert client_requests_thinking({"messages": []}, cap) is False

    def test_thinking_key(self) -> None:
        cap = ThinkingCapability(status="supported")
        assert client_requests_thinking({"thinking": {"type": "enabled"}}, cap) is True

    def test_reasoning_key(self) -> None:
        cap = ThinkingCapability(status="supported")
        assert client_requests_thinking({"reasoning": {"effort": "high"}}, cap) is True

    def test_reasoning_effort_key(self) -> None:
        cap = ThinkingCapability(status="supported")
        assert client_requests_thinking({"reasoning_effort": "high"}, cap) is True

    def test_thinking_budget_key(self) -> None:
        cap = ThinkingCapability(status="supported")
        assert client_requests_thinking({"thinking_budget": 5000}, cap) is True

    def test_unsupported_status(self) -> None:
        cap = ThinkingCapability(status="unsupported")
        assert client_requests_thinking({"thinking": {}}, cap) is False

    def test_unknown_status(self) -> None:
        cap = ThinkingCapability(status="unknown")
        assert client_requests_thinking({"thinking": {}}, cap) is False

    def test_conflicting_status(self) -> None:
        cap = ThinkingCapability(status="conflicting")
        assert client_requests_thinking({"thinking": {}}, cap) is False

    def test_mixed_status(self) -> None:
        cap = ThinkingCapability(status="mixed")
        assert client_requests_thinking({"thinking": {}}, cap) is True


class TestHasThinkingSupport:
    def test_supported(self) -> None:
        assert has_thinking_support(ThinkingCapability(status="supported")) is True

    def test_mixed(self) -> None:
        assert has_thinking_support(ThinkingCapability(status="mixed")) is True

    def test_unsupported(self) -> None:
        assert has_thinking_support(ThinkingCapability(status="unsupported")) is False

    def test_unknown(self) -> None:
        assert has_thinking_support(ThinkingCapability(status="unknown")) is False

    def test_conflicting(self) -> None:
        assert has_thinking_support(ThinkingCapability(status="conflicting")) is False


# ---------------------------------------------------------------------------
# Protocol compatibility does not imply thinking support
# ---------------------------------------------------------------------------


class TestProtocolDoesNotImplyThinking:
    def test_openai_protocol_without_thinking(self) -> None:
        mc = ModelCapabilities(
            thinking=ThinkingCapability(status="unknown"),
        )
        assert mc.thinking.status == "unknown"
        assert not has_thinking_support(mc.thinking)

    def test_anthropic_protocol_without_thinking(self) -> None:
        mc = ModelCapabilities(
            thinking=ThinkingCapability(status="unknown"),
        )
        assert mc.thinking.status == "unknown"
        assert not has_thinking_support(mc.thinking)

    def test_openai_with_thinking_support(self) -> None:
        mc = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                native_protocols=["openai"],
            ),
        )
        assert has_thinking_support(mc.thinking)
        assert "openai" in mc.thinking.native_protocols

    def test_anthropic_with_thinking_support(self) -> None:
        mc = ModelCapabilities(
            thinking=ThinkingCapability(
                status="supported",
                native_protocols=["anthropic"],
            ),
        )
        assert has_thinking_support(mc.thinking)
        assert "anthropic" in mc.thinking.native_protocols


# ---------------------------------------------------------------------------
# Serialization: client control fields
# ---------------------------------------------------------------------------


class TestSerializeThinkingClientControls:
    def test_client_control_fields_emitted(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            source="provider_catalog",
            native_protocols=["anthropic"],
            client_controls={
                "openai": ThinkingClientControls(
                    request_fields=["reasoning_effort"],
                    response_fields=["reasoning_content"],
                    stream_delta_fields=["reasoning"],
                ),
                "anthropic": ThinkingClientControls(
                    request_fields=["thinking"],
                    response_block_types=["thinking"],
                ),
            },
        )
        result = serialize_thinking_for_models(cap)
        assert result["status"] == "supported"
        assert result["openai_request_fields"] == ["reasoning_effort"]
        assert result["openai_response_fields"] == ["reasoning_content"]
        assert result["openai_stream_delta_fields"] == ["reasoning"]
        assert result["anthropic_request_fields"] == ["thinking"]
        assert result["anthropic_response_block_types"] == ["thinking"]

    def test_client_control_fields_omitted_when_empty(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            source="provider_catalog",
        )
        result = serialize_thinking_for_models(cap)
        assert "openai_request_fields" not in result
        assert "anthropic_request_fields" not in result

    def test_effort_to_budget_tokens_emitted(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 16384},
        )
        result = serialize_thinking_for_models(cap)
        assert result["effort_to_budget_tokens"] == {
            "low": 1024,
            "medium": 4096,
            "high": 16384,
        }


# ---------------------------------------------------------------------------
# Serialization: provider_statuses for collapsed entries
# ---------------------------------------------------------------------------


class TestSerializeThinkingProviderStatuses:
    def test_provider_statuses_emitted(self) -> None:
        cap = ThinkingCapability(
            status="mixed",
            source="aggregate",
        )
        result = serialize_thinking_for_models(
            cap,
            provider_statuses={"minimax": "supported", "openrouter": "unknown"},
        )
        assert result["status"] == "mixed"
        assert result["providers"] == {
            "minimax": "supported",
            "openrouter": "unknown",
        }

    def test_provider_statuses_omitted_when_none(self) -> None:
        cap = ThinkingCapability(status="supported", source="provider_catalog")
        result = serialize_thinking_for_models(cap)
        assert "providers" not in result

    def test_provider_statuses_omitted_when_empty(self) -> None:
        cap = ThinkingCapability(status="supported", source="provider_catalog")
        result = serialize_thinking_for_models(cap, provider_statuses={})
        assert "providers" not in result

    def test_model_capabilities_forwards_provider_statuses(self) -> None:
        mc = ModelCapabilities(
            thinking=ThinkingCapability(status="mixed", source="aggregate"),
        )
        result = serialize_model_capabilities(
            mc,
            provider_statuses={"p1": "supported", "p2": "unsupported"},
        )
        assert result["thinking"]["status"] == "mixed"
        assert result["thinking"]["providers"] == {
            "p1": "supported",
            "p2": "unsupported",
        }
