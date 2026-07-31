"""Provider request adaptation tests."""

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
    """Build a ThinkingCapability with a control_contract for testing."""
    contract = ThinkingControlContract(
        mode=mode,  # type: ignore[arg-type]
        accepted_efforts=accepted_efforts or [],
        effort_aliases=effort_aliases or {},
        effort_to_budget_tokens=effort_to_budget,
        explicit_budget_min=budget_min,
        explicit_budget_max=budget_max,
        source="manual_override",
    )
    return ThinkingCapability(
        status=status,  # type: ignore[arg-type]
        control_contract=contract,
    )


def _intent(
    *,
    effort: str | None = None,
    budget: int | None = None,
    fields: tuple[str, ...] = (),
    has_history: bool = False,
    has_new: bool = True,
) -> ThinkingRequestIntent:
    return ThinkingRequestIntent(
        requested_effort=effort,
        requested_budget_tokens=budget,
        request_fields=fields,
        has_historical_reasoning_content=has_history,
        client_requests_new_reasoning=has_new,
    )


class TestPassthrough:
    """Tests for passthrough decisions."""

    def test_no_thinking_controls(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "high"])
        intent = _intent(has_new=False, fields=())
        result = adapt_thinking_controls(
            payload={"model": "test"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        assert result.changed is False

    def test_historical_only_no_new_reasoning(self) -> None:
        cap = _capability(mode="none")
        intent = _intent(
            fields=("reasoning_content",),
            has_history=True,
            has_new=False,
        )
        result = adapt_thinking_controls(
            payload={"model": "test"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        assert result.changed is False


class TestRejectDecisions:
    """Tests for rejection decisions (CapabilityError raised)."""

    def test_reject_unknown_contract(self) -> None:
        cap = _capability(mode="unknown")
        intent = _intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError) as exc_info:
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(),
            )
        assert "does not accept thinking controls" in str(exc_info.value)
        assert exc_info.value.model_id == "test-model"

    def test_reject_none_contract_strict(self) -> None:
        cap = _capability(mode="none")
        intent = _intent(effort="high", fields=("reasoning_effort",))
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

    def test_map_if_known_none_contract_rejects(self) -> None:
        cap = _capability(mode="none")
        intent = _intent(budget=4096, fields=("thinking_budget",))
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "thinking_budget": 4096},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="map_if_known"),
            )

    def test_reject_fixed_contract_strict(self) -> None:
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
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )


class TestDropDecisions:
    """Tests for warn_drop decisions."""

    def test_drop_none_contract(self) -> None:
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
        assert result.changed is True
        assert "reasoning_effort" not in result.payload
        assert "reasoning_effort" in result.requested_controls

    def test_drop_fixed_contract_removes_effort(self) -> None:
        cap = _capability(mode="fixed")
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

    def test_drop_fixed_contract_removes_thinking_budget(self) -> None:
        cap = _capability(mode="fixed")
        intent = _intent(budget=4096, fields=("thinking",))
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
        # thinking block should have budget_tokens removed
        thinking = result.payload.get("thinking")
        if isinstance(thinking, dict):
            assert "budget_tokens" not in thinking

    def test_drop_preserves_reasoning_history(self) -> None:
        """Historical reasoning_content in messages is not dropped."""
        cap = _capability(mode="none")
        intent = _intent(
            fields=("reasoning_content",),
            has_history=True,
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
        # Historical content should pass through (no new controls to drop).
        assert result.decision == "passthrough"


class TestAllowWithWarning:
    """Tests for unknown_contract allow_with_warning policy."""

    def test_unknown_contract_forwarded(self) -> None:
        cap = _capability(mode="unknown")
        intent = _intent(effort="high", fields=("reasoning_effort",))
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


class TestEffortMapping:
    """Tests for effort mapping via contract aliases."""

    def test_known_effort_passthrough(self) -> None:
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
        )
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
        assert result.decision in ("passthrough", "mapped")
        assert "reasoning_effort" in result.emitted_controls

    def test_alias_mapping(self) -> None:
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
        assert result.changed is True
        assert result.payload.get("reasoning_effort") == "medium"
        assert result.decision == "mapped"


class TestThinkingBlockAdaptation:
    """Tests for Anthropic-style thinking block adaptation."""

    def test_effort_contract_removes_budget_from_thinking(self) -> None:
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
        )
        intent = _intent(effort="high", fields=("thinking",))
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
        assert result.changed is True
        thinking = result.payload.get("thinking")
        assert isinstance(thinking, dict)
        assert "budget_tokens" not in thinking
        assert thinking.get("effort") == "high"

    def test_budget_contract_preserves_budget(self) -> None:
        cap = _capability(
            mode="budget",
            budget_min=1024,
            budget_max=128000,
        )
        intent = _intent(budget=4096, fields=("thinking",))
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


class TestThinkingBudgetFieldAdaptation:
    """Tests for top-level thinking_budget field adaptation."""

    def test_reject_thinking_budget_for_effort_only_by_default(self) -> None:
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "medium", "high"],
        )
        intent = _intent(budget=4096, fields=("thinking_budget",))
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

    def test_keep_thinking_budget_for_budget_contract(self) -> None:
        cap = _capability(
            mode="budget",
            budget_min=1024,
            budget_max=128000,
        )
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

    def test_warn_drop_thinking_budget_for_effort_only(self) -> None:
        cap = _capability(mode="effort", accepted_efforts=["low", "high"])
        intent = _intent(budget=4096, fields=("thinking_budget",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking_budget": 4096},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert "thinking_budget" not in result.payload

    def test_map_known_budget_to_effort(self) -> None:
        cap = _capability(
            mode="effort",
            accepted_efforts=["low", "high"],
            effort_to_budget={"low": 1024, "high": 4096},
        )
        intent = _intent(budget=4096, fields=("thinking_budget",))
        result = adapt_thinking_controls(
            payload={"model": "test", "thinking_budget": 4096},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="map_if_known"),
        )
        assert result.decision == "mapped"
        assert result.payload["reasoning_effort"] == "high"
        assert "thinking_budget" not in result.payload
