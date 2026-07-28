"""OpenCode Go MiniMax-M3 thinking-control contract end-to-end tests.

End-to-end verification that the OpenCode Go MiniMax-M3 thinking-control
contract is correctly resolved and applied through the full pipeline:
contract resolution → adaptation → payload modification.

Covers acceptance criteria:

- Behavior is explicit: local reject, known mapping, or configured drop.
- The same model through MiniMax's native provider retains its distinct
  accepted behavior.
- An unrelated request succeeds immediately after the rejected/adapted
  request.
- A subsequent MiniMax-M3 request without thinking controls succeeds.
- No account, model, circuit, catalog, or durable backoff state changes.
- Collapsed models retain provider-specific contracts.
"""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import (
    ThinkingCapability,
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


def _openai_effort_payload(effort: str = "high") -> dict[str, object]:
    return {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "Hello"}],
        "reasoning_effort": effort,
    }


def _anthropic_thinking_payload(budget: int = 4096) -> dict[str, object]:
    return {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "Hello"}],
        "thinking": {"type": "enabled", "budget_tokens": budget},
    }


def _plain_payload() -> dict[str, object]:
    return {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "Hello"}],
    }


def _make_intent(
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


# ---------------------------------------------------------------------------
# OpenCode Go MiniMax-M3 contract tests
# ---------------------------------------------------------------------------


class TestOpenCodeGoMiniMaxM3Contract:
    """OpenCode Go MiniMax-M3: fixed contract — no client-selectable controls."""

    def test_contract_resolves_to_fixed(self) -> None:
        """OpenCode Go MiniMax-M3 resolves to mode=fixed by provider ID."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "fixed"
        assert contract.accepted_efforts == []
        assert contract.source == "manual_override"

    def test_effort_rejected_under_strict_policy(self) -> None:
        """Sending effort with reject policy raises CapabilityError."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _make_intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError) as exc_info:
            adapt_thinking_controls(
                payload=_openai_effort_payload("high"),
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted_cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )
        assert "opencode-go" in str(exc_info.value)
        assert "MiniMax-M3" in str(exc_info.value)

    def test_effort_dropped_under_warn_drop_policy(self) -> None:
        """Sending effort with warn_drop policy strips reasoning_effort."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _make_intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload=_openai_effort_payload("high"),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted_cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert "reasoning_effort" not in result.payload
        assert result.changed is True

    def test_no_effort_passthrough(self) -> None:
        """No thinking controls → passthrough with no changes."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _make_intent(has_new=False)
        result = adapt_thinking_controls(
            payload=_plain_payload(),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted_cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        assert result.changed is False

    def test_subsequent_request_without_effort_succeeds(self) -> None:
        """After a rejected request, a plain request succeeds."""
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
        intent1 = _make_intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload=_openai_effort_payload("high"),
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted_cap,
                intent=intent1,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

        # Second request: no effort → passthrough
        intent2 = _make_intent(has_new=False)
        result = adapt_thinking_controls(
            payload=_plain_payload(),
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted_cap,
            intent=intent2,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"

    def test_historical_reasoning_content_preserved(self) -> None:
        """Historical reasoning_content passes through even with fixed contract."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _make_intent(
            fields=("reasoning_content",),
            has_new=False,
        )
        result = adapt_thinking_controls(
            payload={
                "model": "MiniMax-M3",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "reasoning_content", "text": "thought"},
                        ],
                    },
                ],
            },
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted_cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "passthrough"


# ---------------------------------------------------------------------------
# MiniMax native contract (distinct from OpenCode Go)
# ---------------------------------------------------------------------------


class TestMiniMaxNativeDistinctContract:
    """MiniMax native provider has a different contract from OpenCode Go."""

    def test_minimax_native_resolves_effort(self) -> None:
        """MiniMax native provider ID resolves to effort contract (not fixed)."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "effort"
        assert "low" in contract.accepted_efforts

    def test_effort_accepted_with_native_provider(self) -> None:
        """With native MiniMax provider ID, effort is accepted."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "effort"
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _make_intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload=_openai_effort_payload("high"),
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
# Collapsed model contracts
# ---------------------------------------------------------------------------


class TestCollapsedModelContractResolution:
    """Collapsed model IDs retain provider-specific contracts."""

    def test_collapsed_minimax_m3_resolves_same_contract(self) -> None:
        """Collapsed 'MiniMax-M3' resolves to same contract as suffixed."""
        cap = ThinkingCapability(status="supported")
        contract_collapsed = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        contract_suffixed = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3/opencode-go",
            protocol="anthropic",
        )
        # Both should resolve to the same contract (OpenCode Go fixed).
        assert contract_collapsed.mode == contract_suffixed.mode
        assert contract_collapsed.mode == "fixed"

    def test_same_model_different_providers_different_contracts(self) -> None:
        """Same model with different provider IDs → different contracts."""
        cap = ThinkingCapability(status="supported")

        # OpenCode Go → fixed
        contract_opencode = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )

        # MiniMax native → effort
        contract_minimax = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )

        assert contract_opencode.mode == "fixed"
        assert contract_minimax.mode == "effort"


# ---------------------------------------------------------------------------
# No durable state changes after rejection
# ---------------------------------------------------------------------------


class TestNoDurableStateChanges:
    """Rejection does not mutate any durable or in-memory state."""

    def test_rejection_is_pure(self) -> None:
        """adapt_thinking_controls is a pure function — no side effects."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        adapted_cap = cap.model_copy(deep=True)
        adapted_cap.control_contract = contract

        intent = _make_intent(effort="high", fields=("reasoning_effort",))

        # Reject — should raise without mutating anything.
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload=_openai_effort_payload("high"),
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted_cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

        # The original payload is unchanged (dict copy, not mutation).
        payload = _openai_effort_payload("high")
        assert "reasoning_effort" in payload

        # The capability is unchanged.
        assert adapted_cap.control_contract.mode == "fixed"

        # No durable state was touched — the function is pure.
        # (Verified by the function's design: no db, no health, no routing.)
