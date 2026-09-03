"""OpenCode Go reasoning controls are driven by provider metadata."""

from __future__ import annotations

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingRequestIntent,
    dict_to_model_capabilities,
)
from eggpool.catalog.normalizer import extract_capabilities_from_metadata
from eggpool.transcoder.builtin_contracts import resolve_control_contract
from eggpool.transcoder.provider_adaptation import (
    ProviderControlPolicy,
    adapt_thinking_controls,
)


def _capability(model_id: str, options: list[dict[str, object]]) -> ThinkingCapability:
    metadata = {
        "id": model_id,
        "reasoning": True,
        "reasoning_options": options,
    }
    return dict_to_model_capabilities(
        extract_capabilities_from_metadata(
            metadata,
            protocol="anthropic",
            source="provider_catalog",
        )
    ).thinking


def test_minimax_metadata_is_toggle_only() -> None:
    capability = _capability("minimax-m3", [{"type": "toggle"}])

    assert capability.control_contract.toggle == "supported"
    assert capability.control_contract.effort == "unsupported"
    assert capability.control_contract.budget == "unsupported"
    assert capability.supported_efforts == []


def test_muse_metadata_preserves_exact_efforts() -> None:
    capability = _capability(
        "muse-spark-1.3-contributor",
        [
            {
                "type": "effort",
                "values": ["minimal", "low", "medium", "high", "xhigh"],
            }
        ],
    )

    assert capability.control_contract.accepted_efforts == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert capability.control_contract.effort == "supported"
    assert capability.control_contract.budget == "unsupported"


def test_explicit_provider_metadata_drives_adaptation() -> None:
    capability = _capability("muse-spark-1.3-contributor", [{"type": "toggle"}])
    contract = resolve_control_contract(
        capability=capability,
        provider_id="opencode-go",
        model_id="muse-spark-1.3-contributor",
        protocol="openai",
    )
    capability.control_contract = contract

    result = adapt_thinking_controls(
        payload={"model": "muse-spark-1.3-contributor", "thinking": True},
        client_protocol="openai",
        model_id="muse-spark-1.3-contributor",
        provider_id="opencode-go",
        capability=capability,
        intent=ThinkingRequestIntent(
            client_protocol="openai",
            request_fields=("thinking",),
            client_requests_new_reasoning=True,
            has_historical_reasoning_content=False,
        ),
        policy=ProviderControlPolicy(),
    )

    assert result.decision in {"passthrough", "warn_drop", "reject"}
