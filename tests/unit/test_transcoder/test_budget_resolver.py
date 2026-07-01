"""Tests for Phase 7 — Thinking budget resolution."""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import ThinkingCapability
from eggpool.errors import AggregatorError, CapabilityError
from eggpool.transcoder.budget_resolver import (
    BudgetResolutionError,
    resolve_thinking_budget,
)
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures, TranscoderPolicy


def _make_context(
    client: str = "openai",
    upstream: str = "anthropic",
) -> TranscodeContext:
    return TranscodeContext(
        request_id="test-budget",
        client_protocol=client,
        upstream_protocol=upstream,
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"thinking": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


# ---------------------------------------------------------------------------
# resolve_thinking_budget — standalone unit tests
# ---------------------------------------------------------------------------


class TestResolveThinkingBudget:
    def test_explicit_budget_within_limits(self) -> None:
        cap = ThinkingCapability(budget_tokens_min=1024, budget_tokens_max=16384)
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_budget_tokens=8192,
            capability=cap,
        )
        assert result.budget_tokens == 8192
        assert result.source == "explicit_budget"
        assert result.clamped is False
        assert result.warnings == []

    def test_explicit_budget_clamped_to_min(self) -> None:
        cap = ThinkingCapability(budget_tokens_min=2048, budget_tokens_max=16384)
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_budget_tokens=512,
            capability=cap,
        )
        assert result.budget_tokens == 2048
        assert result.clamped is True
        assert any(w["kind"] == "budget_clamped" for w in result.warnings)

    def test_explicit_budget_clamped_to_max(self) -> None:
        cap = ThinkingCapability(budget_tokens_min=1024, budget_tokens_max=8192)
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_budget_tokens=32768,
            capability=cap,
        )
        assert result.budget_tokens == 8192
        assert result.clamped is True

    def test_explicit_budget_strict_clamp_rejects(self) -> None:
        cap = ThinkingCapability(budget_tokens_min=2048, budget_tokens_max=8192)
        with pytest.raises(BudgetResolutionError, match="clamped"):
            resolve_thinking_budget(
                model_id="claude-3",
                provider_id="anthropic",
                requested_budget_tokens=512,
                capability=cap,
                budget_resolution_policy="strict",
            )

    def test_effort_low_uses_hardcoded_fallback(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="low",
        )
        assert result.budget_tokens == 1024
        assert result.source == "hardcoded_fallback"

    def test_effort_medium_uses_hardcoded_fallback(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="medium",
        )
        assert result.budget_tokens == 4096
        assert result.source == "hardcoded_fallback"

    def test_effort_high_uses_hardcoded_fallback(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="high",
        )
        assert result.budget_tokens == 16384
        assert result.source == "hardcoded_fallback"

    def test_effort_uses_capability_mapping(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"low": 2048, "medium": 8192, "high": 32768},
        )
        result = resolve_thinking_budget(
            model_id="minimax-m3",
            provider_id="minimax",
            requested_effort="medium",
            capability=cap,
        )
        assert result.budget_tokens == 8192
        assert result.source == "capability_effort_mapping"

    def test_effort_uses_global_defaults(self) -> None:
        defaults = {"low": 512, "medium": 2048, "high": 8192}
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="high",
            budget_defaults=defaults,
        )
        assert result.budget_tokens == 8192
        assert result.source == "global_defaults"

    def test_effort_capability_over_global_defaults(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"high": 32768},
        )
        defaults = {"low": 512, "medium": 2048, "high": 8192}
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="high",
            capability=cap,
            budget_defaults=defaults,
        )
        assert result.budget_tokens == 32768
        assert result.source == "capability_effort_mapping"

    def test_effort_unknown_lenient_falls_back(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="ultra",
        )
        assert result.budget_tokens == 4096
        assert result.source == "unknown_effort_fallback"
        assert any(w["kind"] == "unknown_effort" for w in result.warnings)

    def test_effort_unknown_strict_rejects(self) -> None:
        with pytest.raises(BudgetResolutionError, match="Unknown effort"):
            resolve_thinking_budget(
                model_id="claude-3",
                provider_id="anthropic",
                requested_effort="ultra",
                budget_resolution_policy="strict",
            )

    def test_no_input_returns_default(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
        )
        assert result.budget_tokens == 4096
        assert result.source == "fallback_default"
        assert any(w["kind"] == "budget_resolution_no_input" for w in result.warnings)

    def test_effort_with_min_clamp(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"low": 512},
            budget_tokens_min=1024,
        )
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="low",
            capability=cap,
        )
        assert result.budget_tokens == 1024
        assert result.clamped is True

    def test_effort_with_max_clamp(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"high": 32768},
            budget_tokens_max=16384,
        )
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="high",
            capability=cap,
        )
        assert result.budget_tokens == 16384
        assert result.clamped is True

    def test_effort_case_insensitive(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_effort="HIGH",
        )
        assert result.budget_tokens == 16384

    def test_explicit_budget_no_capability(self) -> None:
        result = resolve_thinking_budget(
            model_id="claude-3",
            provider_id="anthropic",
            requested_budget_tokens=8192,
        )
        assert result.budget_tokens == 8192
        assert result.source == "explicit_budget"


# ---------------------------------------------------------------------------
# Transcoder integration — encode_request with resolver
# ---------------------------------------------------------------------------


class TestBudgetResolutionViaTranscoder:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_effort_high_with_custom_defaults(self) -> None:
        policy = TranscoderPolicy(
            features=_features(),
            thinking_budget_defaults={"low": 512, "medium": 2048, "high": 8192},
        )
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=policy.features,
            budget_defaults=policy.thinking_budget_defaults.as_dict(),
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 8192}

    def test_effort_with_capability_mapping(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 32768},
        )
        payload = {
            "model": "minimax-m3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            thinking_capability=cap,
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 32768}

    def test_effort_clamped_to_max(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"high": 32768},
            budget_tokens_max=16384,
        )
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            thinking_capability=cap,
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        assert any(w["kind"] == "budget_clamped" for w in warnings)

    def test_strict_rejects_clamped_budget(self) -> None:
        cap = ThinkingCapability(
            effort_to_budget_tokens={"high": 32768},
            budget_tokens_max=16384,
        )
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        with pytest.raises(BudgetResolutionError, match="clamped"):
            self.transcoder.encode_request(
                payload,
                _make_context(),
                features=_features(),
                thinking_capability=cap,
                budget_resolution_policy="strict",
            )

    def test_unknown_effort_lenient(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "ultra",
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        assert any(w["kind"] == "unknown_effort" for w in warnings)

    def test_backward_compat_no_capability(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "low",
        }
        result, warnings = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 1024}

    def test_thinking_disabled_still_drops(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        _, warnings = self.transcoder.encode_request(
            payload,
            _make_context(),
            features=None,
        )
        assert any(w.get("kind") == "dropped_field" for w in warnings)


class TestClosingPassBudgetResolutionError:
    """Phase B: BudgetResolutionError must be a CapabilityError subclass.

    The proxy layer already catches :class:`CapabilityError` and returns
    HTTP 400; the closing pass promotes
    :class:`BudgetResolutionError` so it flows through the same renderer
    instead of bubbling up as an unhandled 500.
    """

    def test_budget_resolution_error_is_capability_error(self) -> None:
        err = BudgetResolutionError(
            "strict rejection",
            model_id="claude-3",
            requested_budget_tokens=1024,
            resolved_budget_tokens=512,
            budget_resolution_policy="strict",
            reason="clamped",
        )
        assert isinstance(err, CapabilityError)
        # CapabilityError is rendered as HTTP 400 by the proxy layer;
        # this test pins that subclassing promotes the rendering
        # automatically (no manual mapping required).
        assert isinstance(err, AggregatorError)

    def test_strict_policy_rejection_via_resolver_is_capability_error(self) -> None:
        """Strict policy raises BudgetResolutionError on clamp."""
        capability = ThinkingCapability(
            status="supported",
            source="manual_override",
            budget_tokens_min=512,
            budget_tokens_max=1024,
        )
        with pytest.raises(BudgetResolutionError) as excinfo:
            resolve_thinking_budget(
                model_id="claude-3",
                provider_id="anthropic",
                requested_effort=None,
                requested_budget_tokens=2048,
                capability=capability,
                budget_defaults=None,
                budget_resolution_policy="strict",
            )
        # Catching as CapabilityError must work; this is the path the
        # proxy layer takes.
        try:
            raise excinfo.value
        except CapabilityError as ce:
            assert ce is excinfo.value
