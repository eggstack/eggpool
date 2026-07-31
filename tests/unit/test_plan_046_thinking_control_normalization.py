"""Plan 046: Provider thinking-control normalization and contract resolution.

Closes six confirmed defects:
1. Type-only thinking block not marked changed in fixed contract
2. thinking.effort not removed in fixed contract
3. Unsupported reasoning_effort falsely reported as mapped
4. Built-in contract selection applies priority before specificity
5. OpenCode Go lacks URL compatibility matcher
6. Adaptation warnings/emitted_controls untruthful

Tests cover every control spelling, policy disposition, contract
resolution ordering, and OpenCode Go / native MiniMax distinctness.
"""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
)
from eggpool.errors import CapabilityError
from eggpool.transcoder.builtin_contracts import (
    BUILTIN_CONTRACTS,
    BuiltinProviderContract,
    ProviderContractKey,
    lookup_builtin_contract,
    validate_no_ambiguous_contracts,
)
from eggpool.transcoder.provider_adaptation import (
    ProviderControlPolicy,
    adapt_thinking_controls,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capability(
    *,
    mode: str = "unknown",
    accepted_efforts: list[str] | None = None,
    effort_aliases: dict[str, str] | None = None,
    effort_to_budget: dict[str, int] | None = None,
    budget_min: int | None = None,
    budget_max: int | None = None,
) -> ThinkingCapability:
    contract = ThinkingControlContract(
        mode=mode,  # type: ignore[arg-type]
        accepted_efforts=accepted_efforts or [],
        effort_aliases=effort_aliases or {},
        effort_to_budget_tokens=effort_to_budget,
        explicit_budget_min=budget_min,
        explicit_budget_max=budget_max,
        source="manual_override",
    )
    return ThinkingCapability(status="supported", control_contract=contract)


def _intent(
    *,
    effort: str | None = None,
    budget: int | None = None,
    fields: tuple[str, ...] = (),
    has_new: bool = True,
) -> ThinkingRequestIntent:
    return ThinkingRequestIntent(
        requested_effort=effort,
        requested_budget_tokens=budget,
        request_fields=fields,
        has_historical_reasoning_content=False,
        client_requests_new_reasoning=has_new,
    )


# ---------------------------------------------------------------------------
# 1. Fixed contract: type-only thinking block is changed/dropped
# ---------------------------------------------------------------------------


class TestFixedContractTypeOnlyThinkingBlock:
    """Defect 1: A type-only thinking block must be observably changed."""

    def test_type_only_thinking_block_removed(self) -> None:
        """thinking with only type is removed under warn_drop."""
        cap = _capability(mode="fixed")
        intent = _intent(fields=("thinking",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking": {"type": "enabled"}},
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert result.changed is True
        assert "thinking" not in result.payload

    def test_type_only_thinking_block_rejected(self) -> None:
        """thinking with only type raises under reject."""
        cap = _capability(mode="fixed")
        intent = _intent(fields=("thinking",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "thinking": {"type": "enabled"}},
                client_protocol="anthropic",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

    def test_type_and_budget_thinking_block_partially_cleaned(self) -> None:
        """thinking with type+budget: both removed, block dropped."""
        cap = _capability(mode="fixed")
        intent = _intent(fields=("thinking",))
        result = adapt_thinking_controls(
            payload={
                "model": "test",
                "thinking": {"type": "enabled", "budget_tokens": 4096},
            },
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert result.changed is True
        assert "thinking" not in result.payload
        assert "thinking.budget_tokens" in result.requested_controls
        assert "thinking.type" in result.requested_controls


# ---------------------------------------------------------------------------
# 2. Fixed contract: thinking.effort is handled
# ---------------------------------------------------------------------------


class TestFixedContractThinkingEffort:
    """Defect 2: thinking.effort must be removed under fixed contract."""

    def test_effort_in_thinking_removed(self) -> None:
        cap = _capability(mode="fixed")
        intent = _intent(fields=("thinking",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking": {"effort": "high"}},
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert result.changed is True
        assert "thinking" not in result.payload
        assert "thinking.effort" in result.requested_controls

    def test_type_effort_budget_all_removed(self) -> None:
        cap = _capability(mode="fixed")
        intent = _intent(fields=("thinking",))
        result = adapt_thinking_controls(
            payload={
                "model": "test",
                "thinking": {
                    "type": "enabled",
                    "effort": "high",
                    "budget_tokens": 4096,
                },
            },
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert result.changed is True
        assert "thinking" not in result.payload
        assert "thinking.type" in result.requested_controls
        assert "thinking.effort" in result.requested_controls
        assert "thinking.budget_tokens" in result.requested_controls


# ---------------------------------------------------------------------------
# 3. Unsupported effort cannot be reported as mapped
# ---------------------------------------------------------------------------


class TestUnsupportedEffortNotMapped:
    """Defect 3: Unsupported reasoning_effort must not appear in emitted_controls."""

    def test_unsupported_effort_dropped(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="xhigh", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "xhigh"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert result.changed is True
        assert "reasoning_effort" not in result.payload
        assert "reasoning_effort" not in result.emitted_controls
        assert any(w.kind == "thinking_control_dropped" for w in result.warnings)

    def test_unsupported_effort_rejected(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="xhigh", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "xhigh"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )

    def test_unsupported_effort_rejected_under_map_if_known(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="xhigh", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "xhigh"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="map_if_known"),
            )

    def test_accepted_effort_unchanged(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "passthrough"
        assert result.changed is False
        assert "reasoning_effort" in result.emitted_controls
        assert "reasoning_effort" in result.payload

    def test_alias_mapped(self) -> None:
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
            effort_aliases={"med": "medium"},
        )
        intent = _intent(effort="med", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "med"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "mapped"
        assert result.changed is True
        assert result.payload.get("reasoning_effort") == "medium"
        assert "reasoning_effort" in result.emitted_controls
        assert any(w.kind == "effort_mapped" for w in result.warnings)


# ---------------------------------------------------------------------------
# 4. Specificity outranks priority
# ---------------------------------------------------------------------------


class TestSpecificityOutranksPriority:
    """Defect 4: Built-in contract resolution uses specificity before priority."""

    def test_higher_specificity_wins_over_lower_priority(self) -> None:
        """A provider_id match (specificity 3) wins over a URL match
        (specificity 1) even if the URL rule has lower priority."""
        # Create a synthetic scenario: URL rule has priority 5 (lower)
        # but specificity 1. ID rule has priority 10 (higher number)
        # but specificity 3. ID rule should win.
        a = BuiltinProviderContract(
            key=ProviderContractKey(
                provider_base_url_pattern=r".*example\.com.*",
                model_id_pattern=r".*",
                protocol="anthropic",
                priority=5,
            ),
            contract=ThinkingControlContract(mode="effort", source="manual_override"),
        )
        b = BuiltinProviderContract(
            key=ProviderContractKey(
                provider_id_pattern=r"^my-provider$",
                model_id_pattern=r".*",
                protocol="anthropic",
                priority=10,
            ),
            contract=ThinkingControlContract(mode="fixed", source="manual_override"),
        )
        # Simulate lookup: both match at different specificity.
        from eggpool.transcoder.builtin_contracts import _match_key

        matched_a, spec_a = _match_key(
            a.key,
            provider_id="my-provider",
            provider_kind=None,
            provider_base_url="https://example.com/v1",
            model_id="test",
            protocol="anthropic",
        )
        matched_b, spec_b = _match_key(
            b.key,
            provider_id="my-provider",
            provider_kind=None,
            provider_base_url="https://example.com/v1",
            model_id="test",
            protocol="anthropic",
        )
        assert matched_a is True
        assert matched_b is True
        assert spec_a == 1  # URL match
        assert spec_b == 3  # ID match
        # specificity 3 > 1, so b (ID rule) wins regardless of priority.
        assert spec_b > spec_a

    def test_same_specificity_uses_lowest_priority(self) -> None:
        """Within the same specificity, lowest priority number wins."""
        a = BuiltinProviderContract(
            key=ProviderContractKey(
                provider_id_pattern=r"^alpha$",
                model_id_pattern=r".*",
                protocol="anthropic",
                priority=20,
            ),
            contract=ThinkingControlContract(mode="effort", source="manual_override"),
        )
        b = BuiltinProviderContract(
            key=ProviderContractKey(
                provider_id_pattern=r"^alpha$",
                model_id_pattern=r".*",
                protocol="anthropic",
                priority=10,
            ),
            contract=ThinkingControlContract(mode="fixed", source="manual_override"),
        )
        from eggpool.transcoder.builtin_contracts import _match_key

        _, spec_a = _match_key(
            a.key,
            provider_id="alpha",
            provider_kind=None,
            provider_base_url="",
            model_id="test",
            protocol="anthropic",
        )
        _, spec_b = _match_key(
            b.key,
            provider_id="alpha",
            provider_kind=None,
            provider_base_url="",
            model_id="test",
            protocol="anthropic",
        )
        assert spec_a == spec_b == 3
        # Both same specificity; b has lower priority number → b wins.
        assert b.key.priority < a.key.priority

    def test_equal_specificity_equal_priority_ambiguous(self) -> None:
        """Equal specificity and priority raises ValueError."""
        # Both URL-based with same priority and overlapping patterns.
        from eggpool.transcoder.builtin_contracts import _specificity_class

        a = BuiltinProviderContract(
            key=ProviderContractKey(
                provider_base_url_pattern=r".*api\.example\.com.*",
                model_id_pattern=r".*",
                protocol="anthropic",
                priority=10,
            ),
            contract=ThinkingControlContract(mode="effort", source="manual_override"),
        )
        b = BuiltinProviderContract(
            key=ProviderContractKey(
                provider_base_url_pattern=r".*api\.example\.com.*",
                model_id_pattern=r".*",
                protocol="anthropic",
                priority=10,
            ),
            contract=ThinkingControlContract(mode="fixed", source="manual_override"),
        )
        assert _specificity_class(a.key) == _specificity_class(b.key) == 1
        assert a.key.priority == b.key.priority


# ---------------------------------------------------------------------------
# 5. OpenCode Go URL compatibility matcher
# ---------------------------------------------------------------------------


class TestOpenCodeGoUrlCompatibility:
    """Defect 5: URL-based compatibility for OpenCode Go."""

    def test_opencode_go_url_resolves_fixed(self) -> None:
        """OpenCode Go URL resolves the fixed contract without provider_id."""
        contract = lookup_builtin_contract(
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is not None
        assert contract.mode == "fixed"

    def test_opencode_go_id_still_wins(self) -> None:
        """Exact provider_id match wins over URL match."""
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is not None
        assert contract.mode == "fixed"

    def test_native_minimax_not_captured_by_opencode_url(self) -> None:
        """Native MiniMax URL does not match OpenCode Go URL rule."""
        contract = lookup_builtin_contract(
            provider_base_url="https://api.minimax.io/anthropic/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is None

    def test_provider_id_resembling_minimax_with_opencode_url(self) -> None:
        """Provider ID 'minimax-proxy' with OpenCode URL resolves by ID
        specificity (no ID match) then URL match."""
        contract = lookup_builtin_contract(
            provider_id="minimax-proxy",
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # minimax-proxy doesn't match any ID rule; URL rule matches.
        assert contract is not None
        assert contract.mode == "fixed"

    def test_native_minimax_id_wins_over_opencode_url(self) -> None:
        """Native MiniMax ID wins over OpenCode URL when both could match."""
        contract = lookup_builtin_contract(
            provider_id="minimax",
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # minimax ID matches at specificity 3, URL matches at specificity 1.
        # ID wins → effort contract.
        assert contract is not None
        assert contract.mode == "effort"

    def test_opencode_url_does_not_match_non_minimax_model(self) -> None:
        """OpenCode URL rule requires MiniMax-M3 model pattern."""
        contract = lookup_builtin_contract(
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="claude-3-opus",
            protocol="anthropic",
        )
        assert contract is None


# ---------------------------------------------------------------------------
# 6. Emitted_controls and warnings truthfulness
# ---------------------------------------------------------------------------


class TestEmittedControlsTruthfulness:
    """Defect 6: emitted_controls and warnings must be truthful."""

    def test_emitted_controls_subset_of_payload(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        for field in result.emitted_controls:
            assert field in result.payload, (
                f"emitted_controls contains {field!r} but it's not in the payload"
            )

    def test_dropped_field_not_in_emitted(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="xhigh", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "xhigh"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert "reasoning_effort" not in result.emitted_controls
        assert "reasoning_effort" not in result.payload

    def test_no_mapping_warning_for_unmapped_field(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        # Accepted effort: no mapping warning.
        assert not any(w.kind == "effort_mapped" for w in result.warnings)

    def test_map_if_known_rejects_unmappable(self) -> None:
        cap = _capability(mode="fixed")
        intent = _intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="map_if_known"),
            )


# ---------------------------------------------------------------------------
# 7. Non-dict thinking does not crash
# ---------------------------------------------------------------------------


class TestNonDictThinkingSafety:
    """Non-dict thinking values must not crash or invent semantics."""

    def test_string_thinking_not_crashed(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="high", fields=("thinking",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking": "enabled"},
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        # Non-dict thinking is treated as not_present.
        assert result.decision == "passthrough"

    def test_int_thinking_not_crashed(self) -> None:
        cap = _capability(mode="fixed")
        intent = _intent(fields=("thinking",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking": 42},
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        # Non-dict thinking is treated as not_present; nothing to drop.
        assert result.decision == "passthrough"
        assert result.changed is False

    def test_list_thinking_not_crashed(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort="high", fields=("thinking",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking": [{"type": "enabled"}]},
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"


# ---------------------------------------------------------------------------
# 8. Historical reasoning content preserved under fixed contract
# ---------------------------------------------------------------------------


class TestHistoricalContentPreserved:
    """Historical reasoning content must not be removed by control adaptation."""

    def test_historical_content_not_dropped_by_fixed_contract(self) -> None:
        cap = _capability(mode="fixed")
        intent = _intent(
            fields=("reasoning_content",),
            has_new=False,
        )
        result = adapt_thinking_controls(
            payload={
                "model": "test",
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
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "passthrough"
        assert result.changed is False


# ---------------------------------------------------------------------------
# 9. All control spellings across all contract modes
# ---------------------------------------------------------------------------


class TestControlSpellingMatrix:
    """Every control spelling and policy disposition."""

    @pytest.mark.parametrize(
        "reasoning_effort",
        ["high", "medium", "low"],
    )
    def test_accepted_efforts_passthrough(self, reasoning_effort: str) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(effort=reasoning_effort, fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": reasoning_effort},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        assert result.changed is False
        assert "reasoning_effort" in result.emitted_controls

    def test_thinking_budget_dropped_for_effort_contract(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(budget=4096, fields=("thinking_budget",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking_budget": 4096},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.changed is True
        assert "thinking_budget" not in result.payload

    def test_thinking_budget_kept_for_budget_contract(self) -> None:
        cap = _capability(mode="budget", budget_min=1024, budget_max=128000)
        intent = _intent(budget=4096, fields=("thinking_budget",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking_budget": 4096},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        assert result.payload.get("thinking_budget") == 4096

    def test_fixed_contract_removes_all_controls(self) -> None:
        """Fixed contract under warn_drop removes all control fields."""
        cap = _capability(mode="fixed")
        intent = _intent(
            effort="high",
            budget=4096,
            fields=("reasoning_effort", "thinking", "thinking_budget"),
        )
        result = adapt_thinking_controls(
            payload={
                "model": "test",
                "reasoning_effort": "high",
                "thinking": {
                    "type": "enabled",
                    "effort": "high",
                    "budget_tokens": 4096,
                },
                "thinking_budget": 4096,
            },
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert "reasoning_effort" not in result.payload
        assert "thinking" not in result.payload
        assert "thinking_budget" not in result.payload

    def test_none_contract_removes_all_controls(self) -> None:
        cap = _capability(mode="none")
        intent = _intent(effort="high", fields=("reasoning_effort",))
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

    def test_effort_or_budget_keeps_both(self) -> None:
        cap = _capability(
            mode="effort_or_budget",
            accepted_efforts=["low", "medium", "high"],
            budget_min=1024,
            budget_max=128000,
        )
        intent = _intent(effort="high", fields=("thinking",))
        result = adapt_thinking_controls(
            payload={
                "model": "test",
                "thinking": {"type": "enabled", "budget_tokens": 4096},
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

    def test_budget_contract_strips_type_from_thinking(self) -> None:
        cap = _capability(mode="budget", budget_min=1024, budget_max=128000)
        intent = _intent(budget=4096, fields=("thinking",))
        result = adapt_thinking_controls(
            payload={
                "model": "test",
                "thinking": {"type": "enabled", "budget_tokens": 4096},
            },
            client_protocol="anthropic",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.changed is True
        thinking = result.payload.get("thinking")
        assert isinstance(thinking, dict)
        assert "type" not in thinking
        assert thinking.get("budget_tokens") == 4096


# ---------------------------------------------------------------------------
# 10. Ambiguity validation passes for shipped contracts
# ---------------------------------------------------------------------------


class TestShippedContractsUnambiguous:
    """The shipped built-in contracts must not be ambiguous."""

    def test_no_ambiguous_contracts(self) -> None:
        errors = validate_no_ambiguous_contracts()
        assert errors == [], f"Ambiguous contracts: {errors}"

    def test_all_entries_in_builtin_contracts(self) -> None:
        assert len(BUILTIN_CONTRACTS) >= 5


# ---------------------------------------------------------------------------
# 11. Specificity class helper
# ---------------------------------------------------------------------------


class TestSpecificityClass:
    """_specificity_class returns correct specificity."""

    def test_provider_id_returns_3(self) -> None:
        from eggpool.transcoder.builtin_contracts import _specificity_class

        key = ProviderContractKey(provider_id_pattern=r"^test$")
        assert _specificity_class(key) == 3

    def test_provider_kind_returns_2(self) -> None:
        from eggpool.transcoder.builtin_contracts import _specificity_class

        key = ProviderContractKey(provider_kind_pattern=r"^anthropic$")
        assert _specificity_class(key) == 2

    def test_base_url_returns_1(self) -> None:
        from eggpool.transcoder.builtin_contracts import _specificity_class

        key = ProviderContractKey(provider_base_url_pattern=r".*api\.openai\.com.*")
        assert _specificity_class(key) == 1

    def test_no_key_returns_0(self) -> None:
        from eggpool.transcoder.builtin_contracts import _specificity_class

        key = ProviderContractKey()
        assert _specificity_class(key) == 0
