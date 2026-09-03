"""OpenCode Go MiniMax-M3 thinking-control contract.

The opencode-go upstream accepts ``reasoning_effort`` (OpenAI Chat
Completions) and the Anthropic Messages ``thinking`` block for low /
medium / high effort, matching the native MiniMax contract. These tests
exercise the contract resolution and adapter behavior to lock that in.
"""

from __future__ import annotations

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
)
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


def _opencode_go_capability() -> tuple[ThinkingCapability, ThinkingControlContract]:
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


def test_muse_spark_responses_contract_includes_xhigh() -> None:
    for model_id in (
        "muse-spark-1.2-contributor",
        "muse-spark-1.3-contributor",
    ):
        contract = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="opencode-go",
            model_id=model_id,
            protocol="openai",
        )

        assert contract.mode == "effort_or_budget"
        assert contract.accepted_efforts == [
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ]
        assert contract.effort_to_budget_tokens is not None
        assert contract.effort_to_budget_tokens["xhigh"] == 24576

        url_compat = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="custom-opencode-go",
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id=model_id,
            protocol="openai",
        )
        assert url_compat.accepted_efforts == contract.accepted_efforts


class TestOpenCodeGoAdaptationBehavior:
    def test_contract_is_effort_or_budget_mode(self) -> None:
        _, contract = _opencode_go_capability()
        assert contract.mode == "effort_or_budget"
        assert "low" in contract.accepted_efforts
        assert "medium" in contract.accepted_efforts
        assert "high" in contract.accepted_efforts

    def test_effort_passthrough_under_default_policy(self) -> None:
        adapted, _ = _opencode_go_capability()
        intent = _intent(effort="high", fields=("reasoning_effort",))
        result = adapt_thinking_controls(
            payload={"model": "MiniMax-M3", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="MiniMax-M3",
            provider_id="opencode-go",
            capability=adapted,
            intent=intent,
            policy=ProviderControlPolicy(),
        )
        assert result.decision == "passthrough"
        assert result.payload["reasoning_effort"] == "high"

    def test_opencode_go_and_native_minimax_share_effort_contract(self) -> None:
        """OpenCode Go's MiniMax-M3 and the native MiniMax Anthropic
        endpoint expose the same effort vocabulary, so the two surfaces
        agree on accepted values and budget mapping."""
        opencode_go = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        native = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert opencode_go.accepted_efforts == native.accepted_efforts
        assert opencode_go.effort_to_budget_tokens == native.effort_to_budget_tokens

    def test_url_compat_contract_matches_id_contract(self) -> None:
        """Providers configured with the opencode.ai URL but a non-canonical
        provider ID resolve to the same effort-or-budget contract as the
        ID-based rule."""
        url_compat = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="custom-id",
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        id_match = resolve_control_contract(
            capability=ThinkingCapability(status="supported"),
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert url_compat.mode == id_match.mode == "effort_or_budget"
        assert url_compat.accepted_efforts == id_match.accepted_efforts
