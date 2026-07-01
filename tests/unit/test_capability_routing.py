"""Tests for capability-aware routing (Phase 6)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from eggpool.catalog.capabilities import (
    ModelCapabilities,
    ThinkingCapability,
    ThinkingRequestRequirement,
    check_candidate_thinking_eligibility,
    classify_thinking_request,
)
from eggpool.errors import CapabilityError
from eggpool.routing.eligibility import get_eligible_accounts

if TYPE_CHECKING:
    from eggpool.catalog.cache import ModelCatalogCache

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

    def test_top_level_reasoning_content_on_assistant_string(self) -> None:
        """Phase E: top-level ``reasoning_content`` on assistant messages.

        Some clients (e.g. OpenAI Responses-style) attach thinking text
        as a top-level ``reasoning_content`` string alongside ``content``.
        The classifier must detect this and mark the request as
        thinking-required so capability-aware routing applies.
        """
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "final answer",
                    "reasoning_content": "Let me think...",
                }
            ]
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is True
        assert "reasoning_content" in req.fields

    def test_top_level_reasoning_content_empty_string_does_not_trigger(self) -> None:
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "final answer",
                    "reasoning_content": "",
                }
            ]
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is False

    def test_top_level_reasoning_content_list(self) -> None:
        """Phase E: ``reasoning_content`` may also be a list of segments."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "done",
                    "reasoning_content": [
                        {"type": "text", "text": "step 1"},
                        {"type": "text", "text": "step 2"},
                    ],
                }
            ]
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is True
        assert "reasoning_content" in req.fields


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

    def test_no_thinking_field_in_entry_fails_to_unknown(self) -> None:
        """Phase A: missing thinking metadata is treated as ``unknown``.

        Default ``unknown_thinking="reject"`` removes the candidate so
        requests with no capability metadata fail closed. Operators must
        either provide capability metadata or set the policy to
        ``allow_with_warning``/``route_best_effort`` to keep the
        account eligible.
        """
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
        assert len(eligible) == 0

    def test_no_thinking_field_in_entry_allow_with_warning_passes(self) -> None:
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
        policy = {"unknown_thinking": "allow_with_warning"}
        eligible = get_eligible_accounts(
            states, "m1", cache, thinking_requirement=req, capability_policy=policy
        )
        assert len(eligible) == 1

    def test_no_entry_at_all_allow_with_warning_passes(self) -> None:
        """Phase A: completely missing entry is also ``unknown``."""
        cache = MagicMock()
        cache.get_provider_for_account.return_value = "p1"
        cache.get_provider_model_entry.return_value = None
        cache.is_account_model_available.return_value = True
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True)
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {"unknown_thinking": "allow_with_warning"}
        eligible = get_eligible_accounts(
            states, "m1", cache, thinking_requirement=req, capability_policy=policy
        )
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


# ---------------------------------------------------------------------------
# Mixed collapsed thinking filter
# ---------------------------------------------------------------------------


class TestMixedCollapsedThinkingFilter:
    """Tests for _filter_mixed_collapsed_thinking on Router."""

    def _make_router_with_multi_provider(
        self,
        accounts: list[tuple[str, str, str]],
    ) -> MagicMock:
        """Create a mock Router with multiple accounts/providers.

        accounts: list of (account_name, provider_id, thinking_status)
        """
        from eggpool.catalog.capabilities import model_capabilities_to_dict

        cache = MagicMock()
        support_map: dict[str, str] = {}
        entry_map: dict[tuple[str, str], dict] = {}
        support_accounts: dict[str, set[str]] = {}

        for acct, provider, status in accounts:
            support_map[acct] = provider
            caps = model_capabilities_to_dict(
                __import__(
                    "eggpool.catalog.capabilities", fromlist=["ModelCapabilities"]
                ).ModelCapabilities(thinking=ThinkingCapability(status=status))
            )
            entry = {
                "model_id": "m1",
                "protocol": "openai",
                "capabilities": caps,
            }
            entry_map[("m1", provider)] = entry
            support_accounts.setdefault("m1", set()).add(acct)

        def get_provider_for_account(name: str) -> str | None:
            return support_map.get(name)

        def get_provider_model_entry(model_id: str, provider_id: str) -> dict | None:
            return entry_map.get((model_id, provider_id))

        cache.get_provider_for_account = MagicMock(side_effect=get_provider_for_account)
        cache.get_provider_model_entry = MagicMock(side_effect=get_provider_model_entry)

        catalog = MagicMock()
        catalog.cache = cache

        router = MagicMock()
        router._catalog = catalog
        router._filter_mixed_collapsed_thinking = (
            __import__(
                "eggpool.routing.router", fromlist=["Router"]
            ).Router._filter_mixed_collasted_thinking.__get__(router)
            if False
            else None
        )

        # Bind the real method to the mock router
        from eggpool.routing.router import Router

        router._filter_mixed_collapsed_thinking = (
            Router._filter_mixed_collapsed_thinking.__get__(router)
        )

        return router

    def test_filter_drops_unsupported_providers(self) -> None:
        router = self._make_router_with_multi_provider(
            [
                ("acct1", "p1", "supported"),
                ("acct2", "p2", "unsupported"),
            ]
        )
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True),
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct2", enabled=True),
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {"mixed_collapsed_thinking": "filter"}
        result = router._filter_mixed_collapsed_thinking(
            states, "m1", thinking_requirement=req, capability_policy=policy
        )
        names = [s.name for s in result]
        assert "acct1" in names
        assert "acct2" not in names

    def test_filter_keeps_all_when_allow(self) -> None:
        router = self._make_router_with_multi_provider(
            [
                ("acct1", "p1", "supported"),
                ("acct2", "p2", "unsupported"),
            ]
        )
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True),
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct2", enabled=True),
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {"mixed_collapsed_thinking": "allow"}
        result = router._filter_mixed_collapsed_thinking(
            states, "m1", thinking_requirement=req, capability_policy=policy
        )
        names = [s.name for s in result]
        assert "acct1" in names
        assert "acct2" in names

    def test_filter_falls_through_when_no_supported(self) -> None:
        router = self._make_router_with_multi_provider(
            [
                ("acct1", "p1", "unsupported"),
                ("acct2", "p2", "unsupported"),
            ]
        )
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True),
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct2", enabled=True),
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {"mixed_collapsed_thinking": "filter"}
        result = router._filter_mixed_collapsed_thinking(
            states, "m1", thinking_requirement=req, capability_policy=policy
        )
        names = [s.name for s in result]
        assert "acct1" in names
        assert "acct2" in names

    def test_filter_noop_when_single_provider(self) -> None:
        router = self._make_router_with_multi_provider(
            [
                ("acct1", "p1", "unsupported"),
            ]
        )
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True),
        ]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {"mixed_collapsed_thinking": "filter"}
        result = router._filter_mixed_collapsed_thinking(
            states, "m1", thinking_requirement=req, capability_policy=policy
        )
        assert len(result) == 1
        assert result[0].name == "acct1"

    def test_filter_noop_when_thinking_not_required(self) -> None:
        router = self._make_router_with_multi_provider(
            [
                ("acct1", "p1", "supported"),
                ("acct2", "p2", "unsupported"),
            ]
        )
        states = [
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct1", enabled=True),
            __import__(
                "eggpool.accounts.state", fromlist=["AccountRuntimeState"]
            ).AccountRuntimeState(name="acct2", enabled=True),
        ]
        req = ThinkingRequestRequirement(
            required=False, client_protocol="openai", fields=[]
        )
        policy = {"mixed_collapsed_thinking": "filter"}
        result = router._filter_mixed_collapsed_thinking(
            states, "m1", thinking_requirement=req, capability_policy=policy
        )
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Conflicting status manual override
# ---------------------------------------------------------------------------


class TestConflictingStatusOverride:
    def test_conflicting_resolved_by_catalog_merge(self) -> None:
        """When an override sets status='supported', the merged status
        is 'supported' and check_candidate_thinking_eligibility allows it."""
        from eggpool.catalog.capabilities import apply_capability_overrides

        base = ModelCapabilities(thinking=ThinkingCapability(status="conflicting"))
        result = apply_capability_overrides(
            "m1",
            base,
            global_overrides={"m1": {"thinking": {"status": "supported"}}},
            provider_overrides={},
        )
        assert result.thinking.status == "supported"
        assert check_candidate_thinking_eligibility(result.thinking.status) is True

    def test_conflicting_stays_rejected_without_override(self) -> None:
        """Without an override, conflicting status is rejected."""
        assert check_candidate_thinking_eligibility("conflicting") is False


# ---------------------------------------------------------------------------
# Warning logging for non-reject policies
# ---------------------------------------------------------------------------


class TestCapabilityWarningLogging:
    """get_eligible_accounts must emit structured warnings when warn_drop
    or allow_with_warning policies let a candidate through."""

    def _make_cache(
        self, account: str, provider: str, model_id: str, thinking_status: str
    ) -> MagicMock:
        from eggpool.catalog.capabilities import model_capabilities_to_dict

        caps = model_capabilities_to_dict(
            ModelCapabilities(thinking=ThinkingCapability(status=thinking_status))
        )
        cache = MagicMock()
        cache.get_provider_for_account.return_value = provider
        cache.get_provider_model_entry.return_value = {
            "model_id": model_id,
            "protocol": "openai",
            "capabilities": caps,
        }
        cache.is_account_model_available.return_value = True
        return cache

    def test_warn_drop_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        from eggpool.accounts.state import AccountRuntimeState

        cache = self._make_cache("acct1", "p1", "m1", "unsupported")
        states = [AccountRuntimeState(name="acct1", enabled=True)]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {
            "unsupported_thinking": "warn_drop",
            "unknown_thinking": "reject",
            "mixed_collapsed_thinking": "filter",
        }

        with caplog.at_level(logging.WARNING, logger="eggpool.routing.eligibility"):
            eligible = get_eligible_accounts(
                states,
                "m1",
                cache,
                thinking_requirement=req,
                capability_policy=policy,
            )

        assert len(eligible) == 1
        assert any(
            "capability_routing" in record.message
            and "thinking=unsupported" in record.message
            and "policy=warn_drop" in record.message
            for record in caplog.records
        )

    def test_allow_with_warning_emits_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from eggpool.accounts.state import AccountRuntimeState

        cache = MagicMock()
        cache.get_provider_for_account.return_value = "p1"
        cache.get_provider_model_entry.return_value = {
            "model_id": "m1",
            "protocol": "openai",
            "capabilities": {
                "thinking": {"status": "unknown", "source": "provider_catalog"},
            },
        }
        cache.is_account_model_available.return_value = True
        states = [AccountRuntimeState(name="acct1", enabled=True)]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {
            "unsupported_thinking": "reject",
            "unknown_thinking": "allow_with_warning",
            "mixed_collapsed_thinking": "filter",
        }

        with caplog.at_level(logging.WARNING, logger="eggpool.routing.eligibility"):
            eligible = get_eligible_accounts(
                states,
                "m1",
                cache,
                thinking_requirement=req,
                capability_policy=policy,
            )

        assert len(eligible) == 1
        assert any(
            "capability_routing" in record.message
            and "thinking=unknown" in record.message
            and "policy=allow_with_warning" in record.message
            for record in caplog.records
        )

    def test_reject_does_not_emit_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from eggpool.accounts.state import AccountRuntimeState

        cache = self._make_cache("acct1", "p1", "m1", "unsupported")
        states = [AccountRuntimeState(name="acct1", enabled=True)]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {
            "unsupported_thinking": "reject",
            "unknown_thinking": "reject",
            "mixed_collapsed_thinking": "filter",
        }

        with caplog.at_level(logging.WARNING, logger="eggpool.routing.eligibility"):
            eligible = get_eligible_accounts(
                states,
                "m1",
                cache,
                thinking_requirement=req,
                capability_policy=policy,
            )

        assert len(eligible) == 0
        assert not any(
            "capability_routing" in record.message for record in caplog.records
        )

    def test_route_best_effort_does_not_emit_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from eggpool.accounts.state import AccountRuntimeState

        cache = self._make_cache("acct1", "p1", "m1", "unsupported")
        states = [AccountRuntimeState(name="acct1", enabled=True)]
        req = ThinkingRequestRequirement(
            required=True, client_protocol="openai", fields=["reasoning_effort"]
        )
        policy = {
            "unsupported_thinking": "route_best_effort",
            "unknown_thinking": "route_best_effort",
            "mixed_collapsed_thinking": "filter",
        }

        with caplog.at_level(logging.WARNING, logger="eggpool.routing.eligibility"):
            eligible = get_eligible_accounts(
                states,
                "m1",
                cache,
                thinking_requirement=req,
                capability_policy=policy,
            )

        assert len(eligible) == 1
        assert not any(
            "capability_routing" in record.message for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Coordinator integration: CapabilityError raised end-to-end
# ---------------------------------------------------------------------------


class TestCoordinatorCapabilityError:
    """Coordinator._select_and_persist_attempt must raise CapabilityError
    when a thinking request has no eligible provider under the default
    reject policy."""

    @pytest.mark.asyncio()
    async def test_thinking_required_no_eligible_raises_capability_error(
        self,
    ) -> None:
        import httpx

        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.catalog.capabilities import model_capabilities_to_dict
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.db.repositories import (
            AttemptRepository,
            RequestRepository,
            ReservationRepository,
            RoutingDecisionRepository,
        )
        from eggpool.models.config import AppConfig
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
        )
        from eggpool.routing.router import Router
        from eggpool.transcoder.policy import TranscoderPolicy

        name = "0001"
        os.environ[f"K_{name}"] = "k"
        try:
            config = AppConfig.model_validate(
                {
                    "providers": {
                        "test-provider": {
                            "id": "test-provider",
                            "base_url": "https://api.example.com/v1",
                            "protocols": ["openai"],
                            "routing_priority": 0,
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key_env": f"K_{name}",
                                    "weight": 1.0,
                                }
                            ],
                        }
                    }
                }
            )
            registry = AccountRegistry(config)

            cache = ModelCatalogCache()
            caps = model_capabilities_to_dict(
                ModelCapabilities(thinking=ThinkingCapability(status="unsupported"))
            )
            cache.update_from_account(
                name,
                "test-provider",
                [
                    {
                        "model_id": "test-model",
                        "protocol": "openai",
                        "capabilities": caps,
                    }
                ],
            )

            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]
            db = Database(path=":memory:")
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()

                async with db.transaction():
                    await db.execute_insert(
                        "INSERT INTO models (model_id, display_name, protocol) "
                        "VALUES (?, ?, ?)",
                        ("test-model", "test-model", "openai"),
                    )
                    await db.execute_insert(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, ?)",
                        (name, f"K_{name}", 1.0),
                    )
                    row = await db.fetch_one(
                        "SELECT id FROM accounts WHERE name = ?", (name,)
                    )
                    assert row is not None
                    await db.execute_insert(
                        "INSERT INTO account_models "
                        "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                        (int(row["id"]), "test-model"),
                    )

                coordinator = RequestCoordinator(
                    registry=registry,
                    catalog=_MockCatalog(cache),  # type: ignore[arg-type]
                    router=router,
                    db=db,
                    client_pool=httpx.AsyncClient(),
                    request_repo=RequestRepository(db),
                    reservation_repo=ReservationRepository(db),
                    attempt_repo=AttemptRepository(db),
                    routing_decision_repo=RoutingDecisionRepository(db),
                    health_manager=None,
                    transcoder_policy=TranscoderPolicy(),
                )

                ctx = ProxyRequestContext(
                    request_id="req-thinking",
                    protocol="openai",
                    model_id="test-model",
                    streaming=False,
                    original_body=b'{"model":"test-model",'
                    b'"messages":[{"role":"user","content":"hi"}],'
                    b'"reasoning_effort":"high"}',
                    incoming_headers={},
                )
                with pytest.raises(CapabilityError) as exc_info:
                    await coordinator._select_and_persist_attempt(ctx, 1)
                assert exc_info.value.model_id == "test-model"
                assert exc_info.value.capability == "thinking"
                assert exc_info.value.requested_fields == ["reasoning_effort"]
            finally:
                await db.disconnect()
        finally:
            os.environ.pop(f"K_{name}", None)


class _MockCatalog:
    """Mock catalog service exposing only the ``cache`` attribute."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache
