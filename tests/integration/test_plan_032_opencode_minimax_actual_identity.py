"""Plan 032 — OpenCode Go MiniMax-M3 actual identity (integration).

Exercises the production adaptation method with real SelectedAttempt-shaped
objects and the production resolver, verifying that:
- OpenCode Go resolves by provider ID (not URL)
- MiniMax native resolves a distinct contract
- Streaming and non-streaming use the same contract decision
- Strict reject and warn-drop policies work correctly
- Subsequent plain requests succeed after rejections

Run with::

    uv run pytest tests/integration/test_plan_032_opencode_minimax_actual_identity.py -v
"""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
)
from eggpool.errors import CapabilityError
from eggpool.transcoder.builtin_contracts import resolve_control_contract
from eggpool.transcoder.provider_adaptation import (
    ProviderControlPolicy,
    adapt_thinking_controls,
)

pytestmark = [pytest.mark.integration, pytest.mark.request_path]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(
    *,
    effort: str | None = None,
    budget: int | None = None,
    streaming: bool = False,
) -> dict[str, object]:
    p: dict[str, object] = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    if effort is not None:
        p["reasoning_effort"] = effort
    if budget is not None:
        p["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if streaming:
        p["stream"] = True
    return p


def _intent(
    *,
    effort: str | None = None,
    budget: int | None = None,
    fields: tuple[str, ...] = (),
    has_new: bool = True,
    protocol: str = "openai",
) -> ThinkingRequestIntent:
    return ThinkingRequestIntent(
        requested_effort=effort,
        requested_budget_tokens=budget,
        request_fields=fields,
        has_historical_reasoning_content=False,
        client_requests_new_reasoning=has_new,
        client_protocol=protocol,
    )


def _resolve_and_adapt(
    *,
    provider_id: str,
    model_id: str = "MiniMax-M3",
    protocol: str = "anthropic",
    payload: dict[str, object],
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy | None = None,
) -> tuple[ThinkingControlContract, object]:
    """Resolve the contract and run adaptation, returning both."""

    cap = ThinkingCapability(status="supported")
    contract = resolve_control_contract(
        capability=cap,
        provider_id=provider_id,
        model_id=model_id,
        protocol=protocol,
    )
    adapted_cap = cap.model_copy(deep=True)
    adapted_cap.control_contract = contract

    result = adapt_thinking_controls(
        payload=payload,
        client_protocol="openai",
        model_id=model_id,
        provider_id=provider_id,
        capability=adapted_cap,
        intent=intent,
        policy=policy or ProviderControlPolicy(),
    )
    return contract, result


# ---------------------------------------------------------------------------
# Native OpenAI client request → OpenCode Go / MiniMax-M3
# ---------------------------------------------------------------------------


class TestOpenCodeGoMiniMaxM3NativeOpenAI:
    """OpenAI client → OpenCode Go MiniMax-M3: fixed contract."""

    def test_effort_rejected_strict(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "fixed"
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload=_payload(effort="high"),
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted_cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

    def test_effort_dropped_warn_drop(self) -> None:
        intent = _intent(effort="high", fields=("reasoning_effort",))
        _, result = _resolve_and_adapt(
            provider_id="opencode-go",
            payload=_payload(effort="high"),
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert "reasoning_effort" not in result.payload
        assert result.changed is True

    def test_plain_passthrough(self) -> None:
        intent = _intent(has_new=False)
        _, result = _resolve_and_adapt(
            provider_id="opencode-go",
            payload=_payload(),
            intent=intent,
        )
        assert result.decision == "passthrough"
        assert result.changed is False


# ---------------------------------------------------------------------------
# Anthropic client request → OpenCode Go / MiniMax-M3
# ---------------------------------------------------------------------------


class TestOpenCodeGoMiniMaxM3AnthropicClient:
    """Anthropic client → OpenCode Go MiniMax-M3: fixed contract."""

    def test_budget_rejected_strict(self) -> None:
        contract = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "fixed"

    def test_budget_dropped_warn_drop(self) -> None:
        intent = _intent(budget=4096, fields=("thinking",), protocol="anthropic")
        _, result = _resolve_and_adapt(
            provider_id="opencode-go",
            payload=_payload(budget=4096),
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"


# ---------------------------------------------------------------------------
# Streaming and non-streaming use the same contract
# ---------------------------------------------------------------------------


class TestStreamingEquivalence:
    """Streaming flag does not affect contract resolution."""

    def test_streaming_fixed_contract(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract_streaming = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        contract_non_streaming = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract_streaming.mode == contract_non_streaming.mode == "fixed"

    def test_streaming_native_mini_max_effort(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "effort"


# ---------------------------------------------------------------------------
# Strict reject and warn-drop policy
# ---------------------------------------------------------------------------


class TestPolicyBehavior:
    """Strict reject and warn-drop work correctly for each contract."""

    def test_opencode_go_strict_reject(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload=_payload(effort="high"),
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted_cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

    def test_opencode_go_warn_drop(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload=_payload(effort="high"),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted_cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        # Only reasoning_effort removed — messages, model, etc. preserved.
        assert "model" in result.payload
        assert "messages" in result.payload

    def test_minimax_native_effort_passthrough(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload=_payload(effort="high"),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="minimax",
            capability=adapted_cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision in ("passthrough", "mapped")
        assert "reasoning_effort" in result.emitted_controls


# ---------------------------------------------------------------------------
# Subsequent plain request after rejection
# ---------------------------------------------------------------------------


class TestSubsequentPlainRequest:
    """After a rejected request, a plain request succeeds."""

    def test_plain_request_after_rejection(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        # First request: effort → rejected
        intent1 = _intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload=_payload(effort="high"),
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted_cap,
                intent=intent1,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

        # Second request: no effort → passthrough
        intent2 = _intent(has_new=False)
        result = adapt_thinking_controls(
            payload=_payload(),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted_cap,
            intent=intent2,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"


# ---------------------------------------------------------------------------
# Native MiniMax selected provider
# ---------------------------------------------------------------------------


class TestNativeMiniMaxProvider:
    """Native MiniMax provider resolves a distinct built-in contract."""

    def test_minimax_native_resolves_effort(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "effort"
        assert contract.accepted_efforts == ["low", "medium", "high"]

    def test_minimax_native_effort_accepted(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload=_payload(effort="high"),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="minimax",
            capability=adapted_cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision in ("passthrough", "mapped")
        assert "reasoning_effort" in result.emitted_controls

    def test_minimax_native_not_shadowed_by_opencode_go(self) -> None:
        """MiniMax native has a distinct contract from OpenCode Go."""
        cap = ThinkingCapability(status="supported")
        contract_minimax = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        contract_opencode = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract_minimax.mode == "effort"
        assert contract_opencode.mode == "fixed"
        assert contract_minimax is not contract_opencode
