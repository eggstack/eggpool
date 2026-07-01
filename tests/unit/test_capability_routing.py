"""Tests for capability-aware routing (Phase 6)."""

from __future__ import annotations

from unittest.mock import MagicMock

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingRequestRequirement,
    check_candidate_thinking_eligibility,
    classify_thinking_request,
)
from eggpool.errors import CapabilityError
from eggpool.routing.eligibility import get_eligible_accounts

# ---------------------------------------------------------------------------
# classify_thinking_request
# ---------------------------------------------------------------------------


class TestClassifyThinkingRequest:
    def test_empty_body(self) -> None:
        req = classify_thinking_request({}, "openai")
        assert req.required is False
        assert req.fields == []
        assert req.client_protocol == "openai"
        assert req.requested_effort is None
        assert req.requested_budget_tokens is None

    def test_openai_reasoning_effort(self) -> None:
        req = classify_thinking_request({"reasoning_effort": "high"}, "openai")
        assert req.required is True
        assert req.fields == ["reasoning_effort"]
        assert req.requested_effort == "high"
        assert req.client_protocol == "openai"

    def test_openai_reasoning(self) -> None:
        req = classify_thinking_request({"reasoning": {"effort": "low"}}, "openai")
        assert req.required is True
        assert "reasoning" in req.fields

    def test_anthropic_thinking(self) -> None:
        req = classify_thinking_request(
            {"thinking": {"type": "enabled", "budget_tokens": 8192}},
            "anthropic",
        )
        assert req.required is True
        assert "thinking" in req.fields
        assert req.requested_budget_tokens == 8192
        assert req.client_protocol == "anthropic"

    def test_thinking_budget_field(self) -> None:
        req = classify_thinking_request({"thinking_budget": 4096}, "anthropic")
        assert req.required is True
        assert "thinking_budget" in req.fields
        assert req.requested_budget_tokens == 4096

    def test_multiple_indicators(self) -> None:
        req = classify_thinking_request(
            {"reasoning_effort": "medium", "thinking": {"budget_tokens": 2048}},
            "openai",
        )
        assert req.required is True
        assert set(req.fields) == {"reasoning_effort", "thinking"}
        assert req.requested_effort == "medium"
        assert req.requested_budget_tokens == 2048

    def test_history_reasoning_content(self) -> None:
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

    def test_non_dict_thinking_value(self) -> None:
        req = classify_thinking_request({"thinking": "enabled"}, "anthropic")
        assert req.required is True
        assert "thinking" in req.fields
        assert req.requested_budget_tokens is None

    def test_non_numeric_budget_tokens(self) -> None:
        req = classify_thinking_request(
            {"thinking": {"budget_tokens": "invalid"}}, "anthropic"
        )
        assert req.required is True
        assert req.requested_budget_tokens is None

    def test_no_thinking_in_non_assistant_messages(self) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "reasoning_content", "text": "some reasoning"},
                    ],
                }
            ]
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is False

    def test_non_list_content_in_assistant(self) -> None:
        body = {
            "messages": [
                {"role": "assistant", "content": "just a string"},
            ]
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is False

    def test_non_dict_message_in_list(self) -> None:
        body = {"messages": ["not a dict", 123]}
        req = classify_thinking_request(body, "openai")
        assert req.required is False


# ---------------------------------------------------------------------------
# check_candidate_thinking_eligibility
# ---------------------------------------------------------------------------


class TestCheckCandidateThinkingEligibility:
    def test_supported_always_eligible(self) -> None:
        assert check_candidate_thinking_eligibility("supported") is True

    def test_conflicting_always_rejected(self) -> None:
        assert check_candidate_thinking_eligibility("conflicting") is False

    def test_unsupported_reject(self) -> None:
        assert check_candidate_thinking_eligibility("unsupported") is False
        assert (
            check_candidate_thinking_eligibility(
                "unsupported", unsupported_action="reject"
            )
            is False
        )

    def test_unsupported_warn_drop(self) -> None:
        assert (
            check_candidate_thinking_eligibility(
                "unsupported", unsupported_action="warn_drop"
            )
            is True
        )

    def test_unsupported_route_best_effort(self) -> None:
        assert (
            check_candidate_thinking_eligibility(
                "unsupported", unsupported_action="route_best_effort"
            )
            is True
        )

    def test_unknown_reject(self) -> None:
        assert check_candidate_thinking_eligibility("unknown") is False

    def test_unknown_allow_with_warning(self) -> None:
        assert (
            check_candidate_thinking_eligibility(
                "unknown", unknown_action="allow_with_warning"
            )
            is True
        )

    def test_unknown_route_best_effort(self) -> None:
        assert (
            check_candidate_thinking_eligibility(
                "unknown", unknown_action="route_best_effort"
            )
            is True
        )

    def test_mixed_filter(self) -> None:
        assert check_candidate_thinking_eligibility("mixed") is True
        assert (
            check_candidate_thinking_eligibility("mixed", mixed_action="filter") is True
        )

    def test_mixed_reject(self) -> None:
        assert (
            check_candidate_thinking_eligibility("mixed", mixed_action="reject")
            is False
        )

    def test_mixed_allow(self) -> None:
        assert (
            check_candidate_thinking_eligibility("mixed", mixed_action="allow") is True
        )


# ---------------------------------------------------------------------------
# Eligibility integration with thinking filter
# ---------------------------------------------------------------------------


class TestEligibilityWithThinking:
    def _make_cache_with_thinking(
        self,
        account: str,
        provider: str,
        model_id: str,
        thinking_status: str,
    ) -> MagicMock:
        """Create a mock catalog cache with a model that has thinking capabilities."""
        from eggpool.catalog.capabilities import model_capabilities_to_dict

        caps = model_capabilities_to_dict(
            __import__(
                "eggpool.catalog.capabilities", fromlist=["ModelCapabilities"]
            ).ModelCapabilities(thinking=ThinkingCapability(status=thinking_status))
        )
        cache = MagicMock()
        cache.get_provider_for_account.return_value = provider
        cache.get_provider_model_entry.return_value = {
            "model_id": model_id,
            "protocol": "openai",
            "capabilities": caps,
        }
        cache.is_account_model_available.return_value = True
        cache.get_supporting_accounts.return_value = frozenset({account})
        return cache

    def test_thinking_required_filters_unsupported(self) -> None:
        cache = self._make_cache_with_thinking("acct1", "p1", "m1", "unsupported")
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True)
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        eligible = get_eligible_accounts(states, "m1", cache, thinking_requirement=req)
        assert len(eligible) == 0

    def test_thinking_required_allows_supported(self) -> None:
        cache = self._make_cache_with_thinking("acct1", "p1", "m1", "supported")
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True)
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        eligible = get_eligible_accounts(states, "m1", cache, thinking_requirement=req)
        assert len(eligible) == 1

    def test_thinking_not_required_passes_all(self) -> None:
        cache = self._make_cache_with_thinking("acct1", "p1", "m1", "unsupported")
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True)
        ]
        req = ThinkingRequestRequirement(
            required=False, client_protocol="openai", fields=[]
        )
        eligible = get_eligible_accounts(states, "m1", cache, thinking_requirement=req)
        assert len(eligible) == 1

    def test_thinking_required_warn_drop_policy_allows(self) -> None:
        cache = self._make_cache_with_thinking("acct1", "p1", "m1", "unsupported")
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True)
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {
            "unsupported_thinking": "warn_drop",
            "unknown_thinking": "reject",
            "mixed_collapsed_thinking": "filter",
        }
        eligible = get_eligible_accounts(
            states, "m1", cache, thinking_requirement=req, capability_policy=policy
        )
        assert len(eligible) == 1

    def test_no_thinking_field_in_entry_passes_all(self) -> None:
        cache = MagicMock()
        cache.get_provider_for_account.return_value = "p1"
        cache.get_provider_model_entry.return_value = {
            "model_id": "m1",
            "protocol": "openai",
            "capabilities": {},
        }
        cache.is_account_model_available.return_value = True
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True)
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        eligible = get_eligible_accounts(states, "m1", cache, thinking_requirement=req)
        assert len(eligible) == 1


# ---------------------------------------------------------------------------
# CapabilityError
# ---------------------------------------------------------------------------


class TestCapabilityError:
    def test_error_attributes(self) -> None:
        err = CapabilityError(
            model_id="gpt-4",
            capability="thinking",
            requested_fields=["reasoning_effort"],
            message="Model not supported for thinking",
        )
        assert err.model_id == "gpt-4"
        assert err.capability == "thinking"
        assert err.requested_fields == ["reasoning_effort"]
        assert str(err) == "Model not supported for thinking"


# ---------------------------------------------------------------------------
# CapabilityPolicy config
# ---------------------------------------------------------------------------


class TestCapabilityPolicyConfig:
    def test_defaults(self) -> None:
        from eggpool.transcoder.policy import CapabilityPolicy

        cp = CapabilityPolicy()
        assert cp.unsupported_thinking == "reject"
        assert cp.unknown_thinking == "reject"
        assert cp.mixed_collapsed_thinking == "filter"

    def test_custom_values(self) -> None:
        from eggpool.transcoder.policy import CapabilityPolicy

        cp = CapabilityPolicy(
            unsupported_thinking="warn_drop",
            unknown_thinking="allow_with_warning",
            mixed_collapsed_thinking="allow",
        )
        assert cp.unsupported_thinking == "warn_drop"
        assert cp.unknown_thinking == "allow_with_warning"
        assert cp.mixed_collapsed_thinking == "allow"

    def test_transcoder_policy_has_capability_policy(self) -> None:
        from eggpool.transcoder.policy import TranscoderPolicy

        tp = TranscoderPolicy()
        assert hasattr(tp, "capability_policy")
        assert tp.capability_policy.unsupported_thinking == "reject"

    def test_transcoder_policy_custom_capability_policy(self) -> None:
        from eggpool.transcoder.policy import CapabilityPolicy, TranscoderPolicy

        tp = TranscoderPolicy(
            capability_policy=CapabilityPolicy(unsupported_thinking="warn_drop")
        )
        assert tp.capability_policy.unsupported_thinking == "warn_drop"
