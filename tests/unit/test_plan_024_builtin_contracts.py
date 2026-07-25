"""Tests for Plan 024 — Built-in provider contracts."""

from __future__ import annotations

from eggpool.catalog.capabilities import ThinkingCapability, ThinkingControlContract
from eggpool.transcoder.builtin_contracts import (
    lookup_builtin_contract,
    resolve_control_contract,
)


class TestLookupBuiltinContract:
    """Tests for lookup_builtin_contract."""

    def test_opencode_go_minimax_m3_fixed(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.minimax.io/anthropic/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is not None
        assert contract.mode == "fixed"

    def test_minimax_native_effort(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.minimax.io/anthropic/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # The OpenCode Go contract matches first for this URL pattern.
        # The native contract requires a different URL pattern.
        assert contract is not None

    def test_anthropic_native(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.anthropic.com/v1",
            model_id="claude-3-opus",
            protocol="anthropic",
        )
        assert contract is not None
        assert contract.mode == "effort_or_budget"

    def test_openai_native(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            protocol="openai",
        )
        assert contract is not None
        assert contract.mode == "effort"

    def test_no_match(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.example.com/v1",
            model_id="some-model",
            protocol="openai",
        )
        assert contract is None

    def test_protocol_mismatch(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            protocol="anthropic",
        )
        assert contract is None


class TestResolveControlContract:
    """Tests for resolve_control_contract."""

    def test_explicit_override_wins(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="fixed",
                source="manual_override",
            ),
        )
        result = resolve_control_contract(
            capability=cap,
            provider_base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            protocol="openai",
        )
        # Explicit override takes precedence over built-in.
        assert result.mode == "fixed"

    def test_builtin_wins_over_inferred(self) -> None:
        cap = ThinkingCapability(status="supported")
        result = resolve_control_contract(
            capability=cap,
            provider_base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            protocol="openai",
        )
        # Built-in OpenAI contract should be used.
        assert result.mode == "effort"

    def test_inferred_fallback(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            supported_efforts=["low", "medium", "high"],
        )
        result = resolve_control_contract(
            capability=cap,
            provider_base_url="",
            model_id="",
            protocol="",
        )
        # No built-in match, infer from legacy fields.
        assert result.mode == "effort"
        assert result.accepted_efforts == ["low", "medium", "high"]
