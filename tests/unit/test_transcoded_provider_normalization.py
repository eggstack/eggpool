"""Transcoded provider normalization tests.

Verifies that provider-bound thinking control adaptation runs correctly
on transcoded request paths (where client protocol differs from upstream
protocol).  The adaptation must always run for transcoded paths regardless
of contract status.
"""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
)
from eggpool.errors import CapabilityError
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
# Transcoded path — unknown contract always runs adaptation
# ---------------------------------------------------------------------------


class TestTranscodedUnknownContract:
    """Transcoded path + unknown contract → policy decides (not skipped)."""

    def test_unknown_contract_rejects_on_transcoded_path(self) -> None:
        """Transcoded path + unknown contract + strict → CapabilityError."""
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

    def test_unknown_contract_allow_with_warning_transcoded(self) -> None:
        """Transcoded path + unknown + allow_with_warning → passthrough + warning."""
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


# ---------------------------------------------------------------------------
# Transcoded path — fixed contract
# ---------------------------------------------------------------------------


class TestTranscodedFixedContract:
    """Transcoded path + fixed contract → reject or drop."""

    def test_fixed_contract_rejects_effort(self) -> None:
        """Fixed contract + reject policy → CapabilityError."""
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

    def test_fixed_contract_drops_effort(self) -> None:
        """Fixed contract + warn_drop → effort removed."""
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
        assert "reasoning_effort" in result.requested_controls

    def test_fixed_contract_drops_thinking_budget(self) -> None:
        """Fixed contract + warn_drop → thinking.budget_tokens removed, type removed."""
        cap = _capability(mode="fixed")
        intent = _intent(budget=4096, fields=("thinking",), protocol="anthropic")
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
        thinking = result.payload.get("thinking")
        if isinstance(thinking, dict):
            assert "budget_tokens" not in thinking
            assert "type" not in thinking


# ---------------------------------------------------------------------------
# Transcoded path — effort contract with mapping
# ---------------------------------------------------------------------------


class TestTranscodedEffortContract:
    """Transcoded path + effort contract → alias mapping and validation."""

    def test_alias_mapping_med_to_medium(self) -> None:
        """'med' → 'medium' via effort_aliases."""
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
            effort_aliases={"med": "medium"},
        )
        intent = _intent(effort="med", fields=("reasoning_effort",), protocol="openai")
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "med"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.changed is True
        assert result.payload.get("reasoning_effort") == "medium"
        assert result.decision == "mapped"

    def test_known_effort_passthrough(self) -> None:
        """Accepted effort → passthrough (no change needed)."""
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
        )
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
        assert "reasoning_effort" in result.emitted_controls

    def test_effort_contract_strips_budget_from_thinking(self) -> None:
        """Effort contract → thinking.budget_tokens removed."""
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
        )
        intent = _intent(effort="high", fields=("thinking",), protocol="anthropic")
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
        assert "budget_tokens" not in thinking


# ---------------------------------------------------------------------------
# Transcoded path — budget contract
# ---------------------------------------------------------------------------


class TestTranscodedBudgetContract:
    """Transcoded path + budget contract → budget preserved, effort stripped."""

    def test_budget_preserved(self) -> None:
        """Budget contract + budget_tokens → passthrough."""
        cap = _capability(mode="budget", budget_min=1024, budget_max=128000)
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

    def test_top_level_thinking_budget_rejected_for_effort_contract(self) -> None:
        """Effort contract → unsupported top-level budget is rejected."""
        cap = _capability(mode="effort", accepted_efforts=["low", "medium", "high"])
        intent = _intent(budget=4096, fields=("thinking_budget",), protocol="openai")
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "thinking_budget": 4096},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(),
            )


# ---------------------------------------------------------------------------
# Transcoded path — historical reasoning content preserved
# ---------------------------------------------------------------------------


class TestTranscodedHistoricalContent:
    """Historical reasoning_content always passes through on transcoded paths."""

    def test_historical_content_with_fixed_contract(self) -> None:
        """Fixed contract + historical reasoning_content → passthrough."""
        cap = _capability(mode="fixed")
        intent = _intent(
            fields=("reasoning_content",),
            has_history=True,
            has_new=False,
            protocol="openai",
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

    def test_historical_content_with_none_contract(self) -> None:
        """None contract + historical reasoning_content → passthrough."""
        cap = _capability(mode="none")
        intent = _intent(
            fields=("reasoning_content",),
            has_history=True,
            has_new=False,
            protocol="openai",
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


# ---------------------------------------------------------------------------
# Transcoded path — none contract
# ---------------------------------------------------------------------------


class TestTranscodedNoneContract:
    """Transcoded path + none contract → reject or drop all controls."""

    def test_none_contract_rejects(self) -> None:
        """None contract + reject → CapabilityError."""
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

    def test_none_contract_drops_all_controls(self) -> None:
        """None contract + warn_drop → all thinking control fields removed."""
        cap = _capability(mode="none")
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
