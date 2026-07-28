"""Native provider normalization tests.

Verifies that provider-bound thinking control adaptation runs correctly
on native (non-transcoded) request paths, including the skip logic for
unknown contracts and the full adaptation pipeline for known contracts.
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


def _capability(
    *,
    status: str = "supported",
    mode: str = "unknown",
    accepted_efforts: list[str] | None = None,
    effort_aliases: dict[str, str] | None = None,
) -> ThinkingCapability:
    contract = ThinkingControlContract(
        mode=mode,  # type: ignore[arg-type]
        accepted_efforts=accepted_efforts or [],
        effort_aliases=effort_aliases or {},
        source="manual_override",
    )
    return ThinkingCapability(status=status, control_contract=contract)  # type: ignore[arg-type]


def _intent(
    *,
    effort: str | None = None,
    budget: int | None = None,
    fields: tuple[str, ...] = (),
    has_history: bool = False,
    has_new: bool = True,
    protocol: str = "openai",
) -> ThinkingRequestIntent:
    return ThinkingRequestIntent(
        requested_effort=effort,
        requested_budget_tokens=budget,
        request_fields=fields,
        has_historical_reasoning_content=has_history,
        client_requests_new_reasoning=has_new,
        client_protocol=protocol,
    )


# ---------------------------------------------------------------------------
# Native path with known contracts
# ---------------------------------------------------------------------------


class TestNativeKnownContract:
    """Adaptation runs for native paths when the contract is known."""

    def test_native_effort_contract_known_effort_passthrough(self) -> None:
        """Native path + effort contract + accepted effort → passthrough."""
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="high", fields=("reasoning_effort",), protocol="openai")
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision in ("passthrough", "mapped")
        assert "reasoning_effort" in result.emitted_controls

    def test_native_fixed_contract_rejects_effort(self) -> None:
        """Native path + fixed contract + effort sent → rejected."""
        cap = _capability(mode="fixed")
        intent = _intent(effort="high", fields=("reasoning_effort",), protocol="openai")
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

    def test_native_fixed_contract_warn_drops_effort(self) -> None:
        """Native path + fixed contract + warn_drop → effort dropped."""
        cap = _capability(mode="fixed")
        intent = _intent(effort="high", fields=("reasoning_effort",), protocol="openai")
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert "reasoning_effort" not in result.payload

    def test_native_none_contract_rejects(self) -> None:
        """Native path + none contract + effort sent → rejected."""
        cap = _capability(mode="none")
        intent = _intent(effort="high", fields=("reasoning_effort",), protocol="openai")
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

    def test_native_effort_or_budget_preserves_budget(self) -> None:
        """Native path + effort_or_budget + budget → passthrough."""
        cap = _capability(mode="effort_or_budget")
        intent = _intent(budget=4096, fields=("thinking",), protocol="anthropic")
        result = adapt_thinking_controls(
            payload={
                "model": "test",
                "thinking": {"budget_tokens": 4096},
            },
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        thinking = result.payload.get("thinking")
        assert isinstance(thinking, dict)
        assert thinking.get("budget_tokens") == 4096


# ---------------------------------------------------------------------------
# Native path with unknown contract (skip logic)
# ---------------------------------------------------------------------------


class TestNativeUnknownContractSkip:
    """Native path + unknown contract → adaptation skipped at the coordinator level.

    The pure ``adapt_thinking_controls`` function still processes the
    request (it's the coordinator's ``_adapt_provider_thinking_controls``
    that applies the native-path-skip guard).  These tests verify the
    behavior the skip logic relies on.
    """

    def test_unknown_contract_rejects_under_strict_policy(self) -> None:
        """Unknown contract + strict → CapabilityError (coordinator would skip)."""
        cap = _capability(mode="unknown")
        intent = _intent(effort="high", fields=("reasoning_effort",), protocol="openai")
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unknown_contract="reject"),
            )

    def test_unknown_contract_allow_with_warning(self) -> None:
        """Unknown contract + allow_with_warning → passthrough with warning."""
        cap = _capability(mode="unknown")
        intent = _intent(effort="high", fields=("reasoning_effort",), protocol="openai")
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unknown_contract="allow_with_warning"),
        )
        assert result.decision == "passthrough"
        assert len(result.warnings) == 1
        assert result.warnings[0].kind == "unknown_contract_forwarded"


# ---------------------------------------------------------------------------
# Resolve control contract for native paths
# ---------------------------------------------------------------------------


class TestNativeContractResolution:
    """Verify contract resolution for native provider URLs."""

    def test_opencode_go_native_resolves_fixed(self) -> None:
        """OpenCode Go MiniMax-M3 → fixed contract (by provider ID)."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "fixed"

    def test_minimax_native_resolves_effort(self) -> None:
        """MiniMax native provider ID → effort contract."""
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "effort"

    def test_unknown_provider_infers_from_legacy(self) -> None:
        """Unknown provider + legacy fields → inferred contract."""
        cap = ThinkingCapability(
            status="supported",
            supported_efforts=["low", "medium", "high"],
        )
        contract = resolve_control_contract(
            capability=cap,
            provider_base_url="https://unknown.example.com/v1",
            model_id="some-model",
            protocol="openai",
        )
        assert contract.mode == "effort"
        assert contract.accepted_efforts == ["low", "medium", "high"]

    def test_explicit_override_always_wins(self) -> None:
        """Explicit override on capability wins over built-in."""
        cap = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="budget",
                source="manual_override",
            ),
        )
        contract = resolve_control_contract(
            capability=cap,
            provider_base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            protocol="openai",
        )
        # Built-in says effort, but override says budget.
        assert contract.mode == "budget"
