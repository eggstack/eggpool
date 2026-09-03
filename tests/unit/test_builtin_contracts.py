"""Built-in provider contracts tests."""

from __future__ import annotations

from eggpool.catalog.capabilities import ThinkingCapability, ThinkingControlContract
from eggpool.transcoder.builtin_contracts import (
    BUILTIN_CONTRACTS,
    lookup_builtin_contract,
    resolve_control_contract,
    validate_no_ambiguous_contracts,
)


class TestLookupBuiltinContract:
    """Tests for lookup_builtin_contract."""

    def test_opencode_go_minimax_m3_effort_or_budget(self) -> None:
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is not None
        assert contract.mode == "effort_or_budget"

    def test_opencode_go_by_url_fallback(self) -> None:
        """OpenCode Go matches via URL fallback when provider_id is absent."""
        contract = lookup_builtin_contract(
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # URL-based rule for OpenCode Go matches the effort-or-budget contract.
        assert contract is not None
        assert contract.mode == "effort_or_budget"

    def test_minimax_native_effort(self) -> None:
        contract = lookup_builtin_contract(
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is not None
        assert contract.mode == "effort"

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


class TestValidateNoAmbiguousContracts:
    """Tests for validate_no_ambiguous_contracts."""

    def test_no_ambiguous_contracts(self) -> None:
        errors = validate_no_ambiguous_contracts()
        assert errors == []

    def test_all_contracts_have_distinct_patterns(self) -> None:
        """Each built-in contract has a distinguishable key."""
        seen: set[tuple[str | None, str | None, str | None, str, str, int]] = set()
        for entry in BUILTIN_CONTRACTS:
            k = entry.key
            key_tuple = (
                k.provider_id_pattern,
                k.provider_kind_pattern,
                k.provider_base_url_pattern,
                k.model_id_pattern,
                k.protocol,
                k.priority,
            )
            # Multiple entries can have the same key if they differ in
            # contract content, but we verify no two are truly identical.
            assert key_tuple not in seen, f"Duplicate key: {k}"
            seen.add(key_tuple)
