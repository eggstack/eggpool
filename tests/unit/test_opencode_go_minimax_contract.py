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


def _intent(
    *,
    effort: str | None = None,
    fields: tuple[str, ...] = (),
    has_new: bool = True,
) -> ThinkingRequestIntent:
    return ThinkingRequestIntent(
        requested_effort=effort,
        request_fields=fields,
        has_historical_reasoning_content=False,
        client_requests_new_reasoning=has_new,
        client_protocol="openai",
    )


def _opencode_go_capability() -> tuple[ThinkingCapability, object]:
    cap = ThinkingCapability(status="supported")
    contract = resolve_control_contract(
        capability=cap,
        provider_id="opencode-go",
        model_id="MiniMax-M3",
        protocol="anthropic",
    )
    adapted = cap.model_copy(deep=True)
    adapted.control_contract = contract
    return adapted, contract


class TestOpenCodeGoAdaptationBehavior:
    def test_effort_rejected_under_strict_policy(self) -> None:
        adapted, _ = _opencode_go_capability()
        intent = _intent(effort="high", fields=("reasoning_effort",))
        with pytest.raises(CapabilityError) as exc_info:
            adapt_thinking_controls(
                payload={"model": "MiniMax-M3", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                capability=adapted,
                intent=intent,
                policy=ProviderControlPolicy(unsupported_control="reject"),
            )
        assert "opencode-go" in str(exc_info.value)

    def test_effort_dropped_under_warn_drop(self) -> None:
        adapted, _ = _opencode_go_capability()
        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "MiniMax-M3", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted,
            intent=intent,
            policy=ProviderControlPolicy(unsupported_control="warn_drop"),
        )
        assert result.decision == "dropped"
        assert "reasoning_effort" not in result.payload
