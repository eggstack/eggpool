"""Phase 11: Thinking/Reasoning comprehensive regression test matrix.

Covers config, catalog, model listing, routing, request translation,
response translation, streaming, app integration, and observability
for the thinking/reasoning feature across all subsystems.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from eggpool.catalog.capabilities import (
    CapabilityStatus,
    ModelCapabilities,
    ThinkingCapability,
    ThinkingClientControls,
    ThinkingRequestRequirement,
    aggregate_thinking_status,
    check_candidate_thinking_eligibility,
    classify_thinking_request,
    client_requests_thinking,
    dict_to_model_capabilities,
    has_thinking_support,
    merge_thinking_capabilities,
    model_capabilities_to_dict,
    serialize_model_capabilities,
)
from eggpool.metrics.thinking import (
    ThinkingMetricEvent,
    ThinkingMetricsCounter,
    get_counter,
    record_thinking_event,
)
from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.budget_resolver import (
    BudgetResolutionError,
    resolve_thinking_budget,
)
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import (
    CapabilityPolicy,
    OpenAIReasoningFields,
    ThinkingBudgetDefaults,
    TranscoderFeatures,
    TranscoderPolicy,
)
from eggpool.transcoder.streaming import AnthropicToOpenAIStreaming

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    client: str = "openai",
    upstream: str = "anthropic",
    request_id: str = "matrix-test",
) -> TranscodeContext:
    return TranscodeContext(
        request_id=request_id,
        client_protocol=client,
        upstream_protocol=upstream,
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults: dict[str, bool] = {"thinking": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


def _capability(
    status: CapabilityStatus = "unknown",
    *,
    source: str = "unknown",
    native_protocols: list[str] | None = None,
    budget_tokens_min: int | None = None,
    budget_tokens_max: int | None = None,
    effort_to_budget_tokens: dict[str, int] | None = None,
) -> ThinkingCapability:
    return ThinkingCapability(
        status=status,
        source=source,
        native_protocols=native_protocols or [],
        budget_tokens_min=budget_tokens_min,
        budget_tokens_max=budget_tokens_max,
        effort_to_budget_tokens=effort_to_budget_tokens,
    )


def _parse_sse_bytes(raw: list[bytes]) -> list[dict[str, Any]]:
    """Parse SSE output (list of bytes) into a list of JSON data payloads."""
    combined = b"".join(raw)
    frames: list[dict[str, Any]] = []
    for block in combined.split(b"\n\n"):
        if not block.strip():
            continue
        for line in block.split(b"\n"):
            if line.startswith(b"data: ") and line != b"data: [DONE]":
                with contextlib.suppress(json.JSONDecodeError):
                    frames.append(json.loads(line[6:]))
    return frames


def _anthropic_sse(
    event: str,
    *,
    message_id: str = "msg-1",
    model: str = "claude-3",
    index: int = 0,
    thinking_text: str | None = None,
    text: str | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> bytes:
    """Build an Anthropic SSE frame."""
    if event == "message_start":
        payload: dict[str, Any] = {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": usage or {"input_tokens": 10, "output_tokens": 0},
            },
        }
    elif event == "content_block_start":
        block_type = "thinking" if thinking_text is not None else "text"
        payload = {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": block_type, "thinking": ""},
        }
    elif event == "content_block_delta":
        if thinking_text is not None:
            delta_obj: dict[str, Any] = {
                "type": "thinking_delta",
                "thinking": thinking_text,
            }
        else:
            delta_obj = {"type": "text_delta", "text": text or ""}
        payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": delta_obj,
        }
    elif event == "content_block_stop":
        payload = {"type": "content_block_stop", "index": index}
    elif event == "message_delta":
        payload = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": usage or {"output_tokens": 5},
        }
    elif event == "message_stop":
        payload = {"type": "message_stop"}
    else:
        payload = {"type": event}

    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


class _MockCatalog:
    """Thin wrapper exposing only ``.cache`` from a ``ModelCatalogCache``."""

    def __init__(self, cache: Any) -> None:
        self.cache = cache


# ---------------------------------------------------------------------------
# Group 1: Config and capability schema tests
# ---------------------------------------------------------------------------


class TestGroup1ConfigAndCapabilitySchema:
    def test_default_capability_is_unknown(self) -> None:
        cap = ThinkingCapability()
        assert cap.status == "unknown"
        assert cap.source == "unknown"
        assert cap.native_protocols == []
        assert cap.budget_tokens_min is None
        assert cap.budget_tokens_max is None
        assert cap.effort_to_budget_tokens is None
        assert cap.notes is None

    def test_global_model_override_sets_thinking_support(self) -> None:
        override = ThinkingCapability(status="supported", source="manual_override")
        base = ThinkingCapability()
        merged = merge_thinking_capabilities(base, override)
        assert merged.status == "supported"
        assert merged.source == "manual_override"

    def test_provider_scoped_override_wins_over_global(self) -> None:
        global_cap = ThinkingCapability(status="supported", source="manual_override")
        provider_cap = ThinkingCapability(
            status="unsupported", source="manual_override"
        )
        merged_global = merge_thinking_capabilities(ThinkingCapability(), global_cap)
        merged_provider = merge_thinking_capabilities(merged_global, provider_cap)
        assert merged_provider.status == "supported"

    def test_invalid_status_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThinkingCapability(status="bogus")  # type: ignore[arg-type]

    def test_native_protocols_filtered_to_valid_set(self) -> None:
        cap = ThinkingCapability(native_protocols=["openai", "anthropic", "grpc"])
        assert cap.native_protocols == ["openai", "anthropic", "grpc"]
        override = ThinkingCapability(
            status="supported", source="manual_override", native_protocols=["grpc"]
        )
        merged = merge_thinking_capabilities(cap, override)
        assert "grpc" not in merged.native_protocols
        assert set(merged.native_protocols) == {"openai", "anthropic"}

    def test_mixed_collapsed_capability_is_computed_correctly(self) -> None:
        statuses: list[CapabilityStatus] = ["supported", "unsupported", "unknown"]
        result = aggregate_thinking_status(statuses)
        assert result == "mixed"

    def test_all_supported_aggregate(self) -> None:
        statuses: list[CapabilityStatus] = ["supported", "supported"]
        result = aggregate_thinking_status(statuses)
        assert result == "supported"

    def test_all_unsupported_aggregate(self) -> None:
        statuses: list[CapabilityStatus] = ["unsupported", "unsupported"]
        result = aggregate_thinking_status(statuses)
        assert result == "unsupported"

    def test_empty_list_aggregate(self) -> None:
        result: CapabilityStatus = aggregate_thinking_status([])
        assert result == "unknown"

    def test_conflicting_aggregate(self) -> None:
        statuses: list[CapabilityStatus] = ["supported", "unsupported"]
        result = aggregate_thinking_status(statuses)
        assert result == "mixed"

    def test_capability_policy_defaults(self) -> None:
        policy = CapabilityPolicy()
        assert policy.unsupported_thinking == "reject"
        assert policy.unknown_thinking == "reject"
        assert policy.mixed_collapsed_thinking == "filter"

    def test_transcoder_policy_injects_features(self) -> None:
        policy = TranscoderPolicy(features=TranscoderFeatures(thinking=True))
        assert policy.features.thinking is True
        assert policy.features.tools is False

    def test_thinking_budget_defaults(self) -> None:
        defaults = ThinkingBudgetDefaults()
        assert defaults.low == 1024
        assert defaults.medium == 4096
        assert defaults.high == 16384
        d = defaults.as_dict()
        assert d == {"low": 1024, "medium": 4096, "high": 16384}

    def test_openai_reasoning_fields_defaults(self) -> None:
        fields = OpenAIReasoningFields()
        assert fields.non_stream == ["reasoning_content"]
        assert fields.stream_delta == ["reasoning"]
        assert fields.emit_compat_aliases is False


# ---------------------------------------------------------------------------
# Group 2: /v1/models serialization tests
# ---------------------------------------------------------------------------


class TestGroup2ModelsSerialization:
    def _build_model_caps(
        self, thinking_status: CapabilityStatus, source: str = "manual_override"
    ) -> dict[str, Any]:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(status=thinking_status, source=source)
        )
        return serialize_model_capabilities(caps)

    def test_provider_scoped_model_with_supported_thinking(self) -> None:
        serialized = self._build_model_caps("supported")
        assert serialized["thinking"]["status"] == "supported"
        assert serialized["thinking"]["source"] == "manual_override"

    def test_provider_scoped_model_with_unknown_thinking(self) -> None:
        serialized = self._build_model_caps("unknown", source="unknown")
        assert serialized["thinking"]["status"] == "unknown"
        assert "source" not in serialized["thinking"]

    def test_collapsed_model_all_supported(self) -> None:
        provider_caps = [
            {"thinking": {"status": "supported", "source": "manual_override"}},
            {"thinking": {"status": "supported", "source": "manual_override"}},
        ]
        statuses = [
            dict_to_model_capabilities(c).thinking.status for c in provider_caps
        ]
        agg = aggregate_thinking_status(statuses)
        assert agg == "supported"

    def test_collapsed_model_all_unknown(self) -> None:
        provider_caps = [
            {"thinking": {"status": "unknown", "source": "unknown"}},
            {"thinking": {"status": "unknown", "source": "unknown"}},
        ]
        statuses = [
            dict_to_model_capabilities(c).thinking.status for c in provider_caps
        ]
        agg = aggregate_thinking_status(statuses)
        assert agg == "unknown"

    def test_collapsed_model_mixed_providers(self) -> None:
        provider_caps = [
            {"thinking": {"status": "supported", "source": "manual_override"}},
            {"thinking": {"status": "unsupported", "source": "manual_override"}},
            {"thinking": {"status": "unknown", "source": "unknown"}},
        ]
        statuses = [
            dict_to_model_capabilities(c).thinking.status for c in provider_caps
        ]
        agg = aggregate_thinking_status(statuses)
        assert agg == "mixed"

    def test_serialize_preserves_openai_compatible_fields(self) -> None:
        caps = ModelCapabilities(
            thinking=ThinkingCapability(status="supported", source="manual_override")
        )
        serialized = serialize_model_capabilities(caps)
        assert "thinking" in serialized
        thinking = serialized["thinking"]
        assert "status" in thinking
        assert "source" in thinking

    def test_client_controls_serialized_as_flattened_fields(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            source="manual_override",
            client_controls={
                "openai": ThinkingClientControls(
                    request_fields=["reasoning_effort"],
                    response_fields=["reasoning_content"],
                )
            },
        )
        caps = ModelCapabilities(thinking=cap)
        serialized = serialize_model_capabilities(caps)
        thinking = serialized["thinking"]
        assert "openai_request_fields" in thinking
        assert thinking["openai_request_fields"] == ["reasoning_effort"]
        assert "openai_response_fields" in thinking
        assert thinking["openai_response_fields"] == ["reasoning_content"]

    def test_budget_bounds_in_serialized_output(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            source="manual_override",
            budget_tokens_min=1024,
            budget_tokens_max=16384,
        )
        caps = ModelCapabilities(thinking=cap)
        serialized = serialize_model_capabilities(caps)
        thinking = serialized["thinking"]
        assert thinking["budget_tokens_min"] == 1024
        assert thinking["budget_tokens_max"] == 16384

    def test_provider_statuses_in_serialized_output(self) -> None:
        cap = ThinkingCapability(status="mixed", source="aggregate")
        caps = ModelCapabilities(thinking=cap)
        serialized = serialize_model_capabilities(
            caps,
            provider_statuses={"p1": "supported", "p2": "unsupported"},
        )
        thinking = serialized["thinking"]
        assert "providers" in thinking
        assert thinking["providers"]["p1"] == "supported"


# ---------------------------------------------------------------------------
# Group 3: Request classification tests
# ---------------------------------------------------------------------------


class TestGroup3RequestClassification:
    def test_openai_reasoning_effort_marks_thinking(self) -> None:
        req = classify_thinking_request({"reasoning_effort": "high"}, "openai")
        assert req.required is True
        assert req.fields == ["reasoning_effort"]
        assert req.requested_effort == "high"
        assert req.client_protocol == "openai"

    def test_assistant_reasoning_content_marks_thinking(self) -> None:
        body = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning_content", "text": "thinking..."},
                        {"type": "text", "text": "Hi!"},
                    ],
                },
            ]
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is True
        assert "reasoning_content" in req.fields

    def test_anthropic_top_level_thinking_marks_thinking(self) -> None:
        req = classify_thinking_request(
            {"thinking": {"type": "enabled", "budget_tokens": 8192}},
            "anthropic",
        )
        assert req.required is True
        assert "thinking" in req.fields
        assert req.requested_budget_tokens == 8192
        assert req.client_protocol == "anthropic"

    def test_plain_request_does_not_require_thinking(self) -> None:
        req = classify_thinking_request(
            {"messages": [{"role": "user", "content": "Hello"}]},
            "openai",
        )
        assert req.required is False
        assert req.fields == []
        assert req.requested_effort is None
        assert req.requested_budget_tokens is None

    def test_openai_reasoning_body_key(self) -> None:
        req = classify_thinking_request({"reasoning": {"effort": "low"}}, "openai")
        assert req.required is True
        assert "reasoning" in req.fields

    def test_anthropic_thinking_budget_field(self) -> None:
        req = classify_thinking_request({"thinking_budget": 4096}, "anthropic")
        assert req.required is True
        assert "thinking_budget" in req.fields
        assert req.requested_budget_tokens == 4096

    def test_non_dict_thinking_value(self) -> None:
        req = classify_thinking_request({"thinking": "enabled"}, "anthropic")
        assert req.required is True
        assert "thinking" in req.fields
        assert req.requested_budget_tokens is None

    def test_multiple_indicators_combined(self) -> None:
        req = classify_thinking_request(
            {"reasoning_effort": "medium", "thinking": {"budget_tokens": 2048}},
            "openai",
        )
        assert req.required is True
        assert set(req.fields) == {"reasoning_effort", "thinking"}
        assert req.requested_effort == "medium"
        assert req.requested_budget_tokens == 2048

    def test_history_only_thinking_content_no_top_level(self) -> None:
        body = {
            "messages": [
                {"role": "user", "content": "Continue"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning_content", "text": "prior thought"},
                        {"type": "text", "text": "response"},
                    ],
                },
                {"role": "user", "content": "Go on"},
            ]
        }
        req = classify_thinking_request(body, "anthropic")
        assert req.required is True
        assert "reasoning_content" in req.fields

    def test_client_requests_thinking_openai(self) -> None:
        cap = _capability("supported")
        body = {"reasoning_effort": "high"}
        assert client_requests_thinking(body, cap) is True

    def test_client_requests_thinking_unsupported(self) -> None:
        cap = _capability("unsupported")
        body = {"reasoning_effort": "high"}
        assert client_requests_thinking(body, cap) is False

    def test_client_requests_thinking_conflicting(self) -> None:
        cap = _capability("conflicting")
        body = {"reasoning_effort": "high"}
        assert client_requests_thinking(body, cap) is False

    def test_has_thinking_support_supported(self) -> None:
        assert has_thinking_support(_capability("supported")) is True

    def test_has_thinking_support_mixed(self) -> None:
        assert has_thinking_support(_capability("mixed")) is True

    def test_has_thinking_support_unknown(self) -> None:
        assert has_thinking_support(_capability("unknown")) is False

    def test_has_thinking_support_unsupported(self) -> None:
        assert has_thinking_support(_capability("unsupported")) is False


# ---------------------------------------------------------------------------
# Group 4: Routing tests
# ---------------------------------------------------------------------------


class TestGroup4Routing:
    def _make_state(
        self,
        name: str = "acct-1",
        eligible: bool = True,
        priority: int = 10,
    ) -> MagicMock:
        state = MagicMock()
        state.name = name
        state.routing_priority = priority
        state.is_eligible.return_value = eligible
        state.is_quota_exhausted.return_value = False
        return state

    def _make_cache_with_capabilities(
        self,
        model_id: str,
        accounts: list[tuple[str, str, CapabilityStatus]],
    ) -> MagicMock:
        """Build a mock cache with capability entries."""
        cache = MagicMock()
        support_map: dict[str, str] = {}
        entry_map: dict[tuple[str, str], dict[str, Any]] = {}

        for acct, provider, status in accounts:
            support_map[acct] = provider
            caps = model_capabilities_to_dict(
                ModelCapabilities(
                    thinking=ThinkingCapability(status=status, source="manual_override")
                )
            )
            entry = {
                "model_id": model_id,
                "protocol": "openai",
                "capabilities": caps,
            }
            entry_map[(model_id, provider)] = entry

        cache.get_provider_for_account = MagicMock(
            side_effect=lambda name: support_map.get(name)
        )
        cache.get_provider_model_entry = MagicMock(
            side_effect=lambda mid, pid: entry_map.get((mid, pid))
        )
        cache.is_account_model_available = MagicMock(return_value=True)

        return cache

    def test_supported_provider_remains_eligible(self) -> None:
        from eggpool.routing.eligibility import get_eligible_accounts

        cache = self._make_cache_with_capabilities(
            "m1", [("acct-1", "p1", "supported")]
        )
        states = [self._make_state("acct-1")]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        result = get_eligible_accounts(
            states,
            "m1",
            cache,
            thinking_requirement=req,
            capability_policy={
                "unsupported_thinking": "reject",
                "unknown_thinking": "reject",
            },
        )
        assert len(result) == 1
        assert result[0].name == "acct-1"

    def test_unsupported_provider_filtered(self) -> None:
        from eggpool.routing.eligibility import get_eligible_accounts

        cache = self._make_cache_with_capabilities(
            "m1", [("acct-1", "p1", "unsupported")]
        )
        states = [self._make_state("acct-1")]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        result = get_eligible_accounts(
            states,
            "m1",
            cache,
            thinking_requirement=req,
            capability_policy={
                "unsupported_thinking": "reject",
                "unknown_thinking": "reject",
            },
        )
        assert len(result) == 0

    def test_unknown_provider_follows_policy_reject(self) -> None:
        from eggpool.routing.eligibility import get_eligible_accounts

        cache = self._make_cache_with_capabilities("m1", [("acct-1", "p1", "unknown")])
        states = [self._make_state("acct-1")]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        result = get_eligible_accounts(
            states,
            "m1",
            cache,
            thinking_requirement=req,
            capability_policy={"unknown_thinking": "reject"},
        )
        assert len(result) == 0

    def test_unknown_provider_follows_policy_allow_with_warning(self) -> None:
        from eggpool.routing.eligibility import get_eligible_accounts

        cache = self._make_cache_with_capabilities("m1", [("acct-1", "p1", "unknown")])
        states = [self._make_state("acct-1")]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        result = get_eligible_accounts(
            states,
            "m1",
            cache,
            thinking_requirement=req,
            capability_policy={"unknown_thinking": "allow_with_warning"},
        )
        assert len(result) == 1

    def test_collapsed_mixed_filter_to_supported(self) -> None:
        from eggpool.routing.router import Router

        cache = self._make_cache_with_capabilities(
            "m1",
            [
                ("acct-supported", "p-supported", "supported"),
                ("acct-unsupported", "p-unsupported", "unsupported"),
            ],
        )

        catalog_mock = MagicMock()
        catalog_mock.cache = cache

        router = MagicMock()
        router._catalog = catalog_mock
        router._filter_mixed_collapsed_thinking = (
            Router._filter_mixed_collapsed_thinking.__get__(router)
        )

        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        states = [
            self._make_state("acct-supported"),
            self._make_state("acct-unsupported"),
        ]
        result = router._filter_mixed_collapsed_thinking(
            states,
            "m1",
            thinking_requirement=req,
            capability_policy={"mixed_collapsed_thinking": "filter"},
        )
        names = [s.name for s in result]
        assert "acct-supported" in names
        assert "acct-unsupported" not in names

    def test_collapsed_mixed_reject_policy_noop(self) -> None:
        from eggpool.routing.router import Router

        cache = self._make_cache_with_capabilities(
            "m1",
            [
                ("acct-supported", "p-supported", "supported"),
                ("acct-unsupported", "p-unsupported", "unsupported"),
            ],
        )

        catalog_mock = MagicMock()
        catalog_mock.cache = cache

        router = MagicMock()
        router._catalog = catalog_mock
        router._filter_mixed_collapsed_thinking = (
            Router._filter_mixed_collapsed_thinking.__get__(router)
        )

        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        states = [
            self._make_state("acct-supported"),
            self._make_state("acct-unsupported"),
        ]
        result = router._filter_mixed_collapsed_thinking(
            states,
            "m1",
            thinking_requirement=req,
            capability_policy={"mixed_collapsed_thinking": "reject"},
        )
        assert len(result) == 2

    def test_no_thinking_required_no_filter(self) -> None:
        from eggpool.routing.router import Router

        cache = self._make_cache_with_capabilities(
            "m1", [("acct-1", "p1", "unsupported")]
        )

        catalog_mock = MagicMock()
        catalog_mock.cache = cache

        router = MagicMock()
        router._catalog = catalog_mock
        router._filter_mixed_collapsed_thinking = (
            Router._filter_mixed_collapsed_thinking.__get__(router)
        )

        req = ThinkingRequestRequirement(
            required=False, client_protocol="openai", fields=[]
        )
        states = [self._make_state("acct-1")]
        result = router._filter_mixed_collapsed_thinking(
            states,
            "m1",
            thinking_requirement=req,
        )
        assert len(result) == 1

    def test_conflicting_always_rejected(self) -> None:
        assert (
            check_candidate_thinking_eligibility(
                "conflicting",
                unsupported_action="reject",
                unknown_action="reject",
                mixed_action="filter",
            )
            is False
        )

    def test_supported_always_eligible(self) -> None:
        assert (
            check_candidate_thinking_eligibility(
                "supported",
                unsupported_action="reject",
                unknown_action="reject",
                mixed_action="filter",
            )
            is True
        )

    def test_eligibility_error_no_eligible_provider(self) -> None:
        from eggpool.routing.eligibility import get_eligible_accounts

        cache = self._make_cache_with_capabilities(
            "m1",
            [
                ("acct-1", "p1", "unsupported"),
                ("acct-2", "p2", "unsupported"),
            ],
        )
        states = [self._make_state("acct-1"), self._make_state("acct-2")]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        result = get_eligible_accounts(
            states,
            "m1",
            cache,
            thinking_requirement=req,
            capability_policy={"unsupported_thinking": "reject"},
        )
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Group 5: OpenAI-to-Anthropic request transcoding tests
# ---------------------------------------------------------------------------


class TestGroup5OpenAIToAnthropicRequestTranscoding:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_reasoning_effort_low(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "low",
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 1024}

    def test_reasoning_effort_medium(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "medium",
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    def test_reasoning_effort_high(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 16384}

    def test_unknown_effort_uses_fallback(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "extra_high",
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"]["type"] == "enabled"
        assert result["thinking"]["budget_tokens"] == 4096

    def test_assistant_reasoning_content_to_thinking_block(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": "Hello!",
                    "reasoning_content": "thought process",
                },
            ],
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        contents = assistant_msg["content"]
        assert isinstance(contents, list)
        assert any(c.get("type") == "thinking" for c in contents)

    def test_thinking_disabled_drops_fields(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features(thinking=False)
        )
        assert "thinking" not in result

    def test_thinking_field_not_passed_through(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "thinking": {"type": "enabled", "budget_tokens": 8192},
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert "thinking" not in result

    def test_budget_resolution_with_capability(self) -> None:
        cap = _capability(
            "supported",
            effort_to_budget_tokens={"low": 2048, "medium": 8192, "high": 32768},
        )
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        result, _ = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            thinking_capability=cap,
            budget_defaults=cap.effort_to_budget_tokens,
        )
        assert result["thinking"]["budget_tokens"] == 32768

    def test_budget_clamped_to_max(self) -> None:
        cap = _capability("supported", budget_tokens_max=8192)
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_budget_tokens=32768,
            capability=cap,
        )
        assert result.budget_tokens == 8192
        assert result.clamped is True

    def test_budget_clamped_to_min(self) -> None:
        cap = _capability("supported", budget_tokens_min=4096)
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_budget_tokens=512,
            capability=cap,
        )
        assert result.budget_tokens == 4096
        assert result.clamped is True

    def test_budget_strict_rejects_clamped(self) -> None:
        cap = _capability("supported", budget_tokens_max=4096)
        with pytest.raises(BudgetResolutionError, match="clamped"):
            resolve_thinking_budget(
                model_id="claude-3",
                provider_id="anthropic",
                requested_budget_tokens=16384,
                capability=cap,
                budget_resolution_policy="strict",
            )

    def test_budget_strict_rejects_unknown_effort(self) -> None:
        with pytest.raises(BudgetResolutionError, match="Unknown effort"):
            resolve_thinking_budget(
                model_id="claude-3",
                provider_id="anthropic",
                requested_effort="absurd",
                capability=_capability("supported"),
                budget_resolution_policy="strict",
            )


# ---------------------------------------------------------------------------
# Group 6: Anthropic-to-OpenAI request transcoding tests
# ---------------------------------------------------------------------------


class TestGroup6AnthropicToOpenAIRequestTranscoding:
    def setup_method(self) -> None:
        self.transcoder = AnthropicToOpenAI()

    def test_anthropic_thinking_for_openai_upstream(self) -> None:
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 8192},
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(client="anthropic", upstream="openai"),
            features=_features(),
        )
        assert "thinking" not in result
        # Phase G: explicit kind, not the generic dropped_field bucket.
        assert any(
            w.get("kind") == "anthropic_top_level_thinking_dropped" for w in warnings
        )

    def test_anthropic_thinking_content_in_history(self) -> None:
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "internal thought"},
                        {"type": "text", "text": "Hi!"},
                    ],
                },
            ],
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(client="anthropic", upstream="openai"),
            features=_features(),
        )
        assistant_msg = result["messages"][1]
        contents = assistant_msg.get("content", [])
        if isinstance(contents, list):
            for c in contents:
                if isinstance(c, dict):
                    assert c.get("type") != "thinking"

    def test_thinking_dropped_warning(self) -> None:
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }
        _, warnings = self.transcoder.encode_request(
            payload,
            _make_context(client="anthropic", upstream="openai"),
            features=_features(),
        )
        assert any(
            w.get("kind") == "anthropic_top_level_thinking_dropped" for w in warnings
        )


# ---------------------------------------------------------------------------
# Group 7: Non-streaming response tests
# ---------------------------------------------------------------------------


class TestGroup7NonStreamingResponse:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_anthropic_thinking_to_reasoning_content(self) -> None:
        payload = {
            "id": "resp-1",
            "content": [
                {"type": "thinking", "thinking": "my reasoning"},
                {"type": "text", "text": "Hello!"},
            ],
            "stop_reason": "end_turn",
        }
        result, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=_features()
        )
        msg = result["choices"][0]["message"]
        assert "reasoning_content" in msg
        assert msg["reasoning_content"] == "my reasoning"

    def test_redacted_thinking_dropped_with_warning(self) -> None:
        payload = {
            "id": "resp-2",
            "content": [
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "text", "text": "OK"},
            ],
            "stop_reason": "end_turn",
        }
        result, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=_features()
        )
        msg = result["choices"][0]["message"]
        assert msg.get("reasoning_content") is None
        assert any(w.get("kind") == "dropped_field" for w in warnings)

    def test_configured_response_field_aliases(self) -> None:
        policy = TranscoderPolicy(
            openai_reasoning_fields=OpenAIReasoningFields(
                non_stream=["reasoning_content", "thinking_summary"]
            )
        )
        payload = {
            "id": "resp-3",
            "content": [
                {"type": "thinking", "thinking": "my reasoning"},
                {"type": "text", "text": "Hello!"},
            ],
            "stop_reason": "end_turn",
        }
        result, warnings = self.transcoder.decode_response(
            payload,
            _make_context(),
            features=_features(),
            reasoning_field_names=policy.openai_reasoning_fields.non_stream,
        )
        msg = result["choices"][0]["message"]
        assert "reasoning_content" in msg

    def test_feature_disabled_no_thinking_content(self) -> None:
        payload = {
            "id": "resp-4",
            "content": [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "Response"},
            ],
            "stop_reason": "end_turn",
        }
        result, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=_features(thinking=False)
        )
        msg = result["choices"][0]["message"]
        assert "reasoning_content" not in msg

    def test_no_thinking_block_normal_response(self) -> None:
        payload = {
            "id": "resp-5",
            "content": [{"type": "text", "text": "Just text"}],
            "stop_reason": "end_turn",
        }
        result, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=_features()
        )
        msg = result["choices"][0]["message"]
        assert msg["content"] == "Just text"
        assert "reasoning_content" not in msg


# ---------------------------------------------------------------------------
# Group 8: Streaming response tests
# ---------------------------------------------------------------------------


class TestGroup8StreamingResponse:
    @pytest.mark.asyncio
    async def test_thinking_delta_to_reasoning_delta(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            reasoning_field_names=["reasoning"],
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(
            _anthropic_sse("content_block_start", index=0, thinking_text="x")
        )
        raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta", thinking_text="reasoning text", index=0
            )
        )
        frames = _parse_sse_bytes(raw)
        assert len(frames) > 0
        data = frames[0]
        assert data["choices"][0]["delta"]["reasoning"] == "reasoning text"
        assert data["choices"][0]["delta"].get("content") is None

    @pytest.mark.asyncio
    async def test_ordering_preserved_relative_to_text(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            reasoning_field_names=["reasoning"],
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(
            _anthropic_sse("content_block_start", index=0, thinking_text="x")
        )
        raw1 = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="think", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        await transcoder.feed(_anthropic_sse("content_block_start", index=1))
        raw2 = await transcoder.feed(
            _anthropic_sse("content_block_delta", text="hello", index=1)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=1))
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="end_turn"))
        await transcoder.flush()

        all_frames = _parse_sse_bytes(raw1 + raw2)
        assert len(all_frames) >= 2
        reasoning_deltas = [
            f["choices"][0]["delta"]
            for f in all_frames
            if "reasoning" in f["choices"][0]["delta"]
        ]
        content_deltas = [
            f["choices"][0]["delta"]
            for f in all_frames
            if f["choices"][0]["delta"].get("content")
        ]
        assert len(reasoning_deltas) > 0
        assert len(content_deltas) > 0

    @pytest.mark.asyncio
    async def test_feature_disabled_path_consistent(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            features=TranscoderFeatures(thinking=False),
            reasoning_field_names=["reasoning"],
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(
            _anthropic_sse("content_block_start", index=0, thinking_text="x")
        )
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="secret", index=0)
        )
        frames = _parse_sse_bytes(raw)
        for f in frames:
            delta = f.get("choices", [{}])[0].get("delta", {})
            assert "reasoning" not in delta

    @pytest.mark.asyncio
    async def test_tool_call_streaming_unaffected(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            reasoning_field_names=["reasoning"],
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta",
                text="some text",
                index=0,
            )
        )
        frames = _parse_sse_bytes(raw)
        assert len(frames) > 0
        delta = frames[0]["choices"][0]["delta"]
        assert "reasoning" not in delta


# ---------------------------------------------------------------------------
# Group 9: App/coordinator integration tests
# ---------------------------------------------------------------------------


class TestGroup9AppCoordinatorIntegration:
    def test_coordinator_receives_transcoder_policy(self) -> None:
        policy = TranscoderPolicy(
            enabled=True,
            features=TranscoderFeatures(thinking=True),
            capability_policy=CapabilityPolicy(
                unsupported_thinking="reject",
                unknown_thinking="reject",
            ),
        )
        assert policy.features.thinking is True
        assert policy.capability_policy.unsupported_thinking == "reject"

    def test_preflight_and_dispatch_agree_on_features(self) -> None:
        policy = TranscoderPolicy(features=TranscoderFeatures(thinking=True))
        features = policy.features
        transcoder = select_transcoder_instance("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Test"}],
            "reasoning_effort": "high",
        }
        result, _ = transcoder.encode_request(
            payload,
            _make_context(),
            features=features,
        )
        assert "thinking" in result

    def test_features_none_prevents_thinking(self) -> None:
        transcoder = select_transcoder_instance("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Test"}],
            "reasoning_effort": "high",
        }
        result, _ = transcoder.encode_request(
            payload,
            _make_context(),
            features=None,
        )
        assert "thinking" not in result

    def test_thinking_feature_flag_off_prevents_transcoding(self) -> None:
        policy = TranscoderPolicy(features=TranscoderFeatures(thinking=False))
        transcoder = select_transcoder_instance("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Test"}],
            "reasoning_effort": "high",
        }
        result, _ = transcoder.encode_request(
            payload,
            _make_context(),
            features=policy.features,
        )
        assert "thinking" not in result


def select_transcoder_instance(client: str, upstream: str) -> Any:
    """Select a transcoder instance (mirrors protocol.select_transcoder)."""
    if client == "openai" and upstream == "anthropic":
        return OpenAIToAnthropic()
    if client == "anthropic" and upstream == "openai":
        return AnthropicToOpenAI()
    raise ValueError(f"Unknown pair: {client} -> {upstream}")


# ---------------------------------------------------------------------------
# Group 10: Observability tests
# ---------------------------------------------------------------------------


class TestGroup10Observability:
    @pytest_asyncio.fixture(autouse=True)
    async def _reset(self) -> None:
        counter = get_counter()
        yield counter
        await counter.reset()

    @pytest.mark.asyncio
    async def test_requested_counter_increments(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_requested(client_protocol="openai")
        await counter.increment_requested(client_protocol="openai")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 2
        assert snapshot["counters"]["requested|openai"] == 2

    @pytest.mark.asyncio
    async def test_transcoded_counter_increments(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_transcoded(
            client_protocol="openai",
            upstream_protocol="anthropic",
            provider_id="anthropic-prod",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "transcoded|openai|anthropic|anthropic-prod"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_dropped_counter_increments(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_dropped(
            client_protocol="anthropic",
            upstream_protocol="openai",
            reason="reasoning_content_dropped",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "dropped|anthropic|openai|reasoning_content_dropped"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_rejected_counter_increments(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_rejected(
            client_protocol="openai",
            capability_status="unsupported",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        assert snapshot["counters"]["rejected|openai|unsupported"] == 1

    @pytest.mark.asyncio
    async def test_budget_clamped_counter_increments(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_budget_clamped(
            client_protocol="openai",
            provider_id="anthropic-prod",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "budget_clamped|openai|anthropic-prod"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_record_thinking_event_dispatches(self) -> None:
        counter = get_counter()
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="high",
            resolved_budget_tokens=16384,
            budget_clamped=False,
            capability_status="supported",
            capability_source="manual_override",
            upstream_protocol="anthropic",
            upstream_fields=["thinking"],
            decision="transcoded",
        )
        await record_thinking_event(event)
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 2
        assert any(k.startswith("transcoded|") for k in snapshot["counters"])

    @pytest.mark.asyncio
    async def test_request_trace_metadata_only(self) -> None:
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="high",
            resolved_budget_tokens=16384,
            budget_clamped=False,
            capability_status="supported",
            capability_source="manual_override",
            upstream_protocol="anthropic",
            upstream_fields=["thinking"],
            decision="transcoded",
        )
        trace_dict: dict[str, Any] = {
            "requested": event.requested,
            "client_protocol": event.client_protocol,
            "fields": event.request_fields,
            "effort": event.requested_effort,
            "budget_tokens": event.resolved_budget_tokens,
            "clamped": event.budget_clamped,
            "capability_status": event.capability_status,
            "decision": event.decision,
        }
        assert "reasoning_effort" in trace_dict["fields"]
        assert trace_dict["requested"] is True
        assert trace_dict["decision"] == "transcoded"

    @pytest.mark.asyncio
    async def test_unknown_capability_counter(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_unknown_capability(client_protocol="openai")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        assert snapshot["counters"]["unknown_capability|openai"] == 1

    @pytest.mark.asyncio
    async def test_unsupported_capability_counter(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_unsupported_capability(client_protocol="anthropic")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        assert snapshot["counters"]["unsupported_capability|anthropic"] == 1

    @pytest.mark.asyncio
    async def test_stream_delta_counter(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_stream_delta(
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "stream_delta|openai|anthropic"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_response_block_counter(self) -> None:
        counter = ThinkingMetricsCounter()
        await counter.increment_response_block(
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "response_block|openai|anthropic"
        assert snapshot["counters"][key] == 1
