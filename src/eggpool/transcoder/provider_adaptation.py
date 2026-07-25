"""Provider-bound thinking control adaptation.

This module implements the post-selection normalization stage that
validates and adapts thinking/reasoning controls against the selected
provider's capability contract.  It runs *after* provider/account
selection and *before* upstream request construction, for both native
and transcoded request paths.

The adaptation function is pure with respect to runtime health,
database state, routing, and logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
    infer_control_contract,
)
from eggpool.errors import CapabilityError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adaptation result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptationWarning:
    """A non-fatal warning produced during provider adaptation."""

    kind: str
    detail: str = ""
    field_name: str = ""


@dataclass(frozen=True, slots=True)
class ProviderRequestAdaptation:
    """Typed pure result of provider-bound thinking control adaptation.

    The adaptation function returns this value instead of mutating
    shared state.  Callers inspect ``decision`` to determine the
    appropriate observability and trace actions.
    """

    payload: dict[str, Any]
    changed: bool
    decision: Literal["passthrough", "mapped", "dropped", "rejected"]
    requested_controls: tuple[str, ...] = ()
    emitted_controls: tuple[str, ...] = ()
    warnings: tuple[AdaptationWarning, ...] = ()
    retry_signature: str | None = None


# ---------------------------------------------------------------------------
# Adaptation policy
# ---------------------------------------------------------------------------

AdaptationPolicyUnsupported = Literal["reject", "warn_drop", "map_if_known"]
AdaptationPolicyUnknown = Literal["reject", "allow_with_warning"]


@dataclass(frozen=True, slots=True)
class ProviderControlPolicy:
    """Configuration for provider-bound thinking control adaptation.

    Mirrors the ``[transcoder.provider_control_policy]`` config section.
    """

    unsupported_control: AdaptationPolicyUnsupported = "reject"
    unknown_contract: AdaptationPolicyUnknown = "reject"
    allow_compatibility_retry: bool = False


# ---------------------------------------------------------------------------
# Core adaptation function
# ---------------------------------------------------------------------------


def adapt_thinking_controls(
    *,
    payload: dict[str, Any],
    client_protocol: str,
    model_id: str,
    provider_id: str,
    capability: ThinkingCapability,
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy,
) -> ProviderRequestAdaptation:
    """Validate and adapt thinking controls against the provider contract.

    This function is **pure** — it does not touch runtime health,
    database state, routing, or logging.  It receives the decoded
    provider-bound payload and returns an adaptation result.

    Parameters:
        payload: the decoded request body (may have been transcoded).
        client_protocol: the original client protocol (``"openai"`` or
            ``"anthropic"``).
        model_id: the resolved model id.
        provider_id: the resolved provider id.
        capability: the selected provider's ``ThinkingCapability``.
        intent: the original client thinking intent (Workstream D).
        policy: the operator-configured adaptation policy.

    Returns:
        A :class:`ProviderRequestAdaptation` describing the adaptation
        decision and the (possibly modified) payload.
    """
    contract = infer_control_contract(capability)

    # If the client did not request any thinking controls, pass through.
    # Historical reasoning content (reasoning_content in messages) is
    # not a control — it must always pass through.
    if not intent.client_requests_new_reasoning:
        return ProviderRequestAdaptation(
            payload=payload,
            changed=False,
            decision="passthrough",
        )

    # If the contract is unknown, apply the unknown_contract policy.
    if contract.mode == "unknown":
        if policy.unknown_contract == "allow_with_warning":
            return ProviderRequestAdaptation(
                payload=payload,
                changed=False,
                decision="passthrough",
                warnings=(
                    AdaptationWarning(
                        kind="unknown_contract_forwarded",
                        detail=f"contract unknown for {provider_id}/{model_id}",
                    ),
                ),
            )
        return _reject(
            payload,
            model_id,
            provider_id,
            "unknown_contract",
            intent,
        )

    # If the contract says no thinking controls are accepted.
    if contract.mode == "none":
        return _handle_none_contract(
            payload=payload,
            model_id=model_id,
            provider_id=provider_id,
            contract=contract,
            intent=intent,
            policy=policy,
        )

    # If the contract says reasoning is fixed (no client controls).
    if contract.mode == "fixed":
        return _handle_fixed_contract(
            payload=payload,
            model_id=model_id,
            provider_id=provider_id,
            contract=contract,
            intent=intent,
            policy=policy,
        )

    # For effort/budget/effort_or_budget modes, validate and adapt.
    return _handle_effort_budget_contract(
        payload=payload,
        client_protocol=client_protocol,
        model_id=model_id,
        provider_id=provider_id,
        contract=contract,
        intent=intent,
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _reject(
    payload: dict[str, Any],
    model_id: str,
    provider_id: str,
    reason: str,
    intent: ThinkingRequestIntent,
) -> ProviderRequestAdaptation:
    """Produce a rejected adaptation result."""
    raise CapabilityError(
        model_id=model_id,
        capability="thinking",
        requested_fields=list(intent.request_fields),
        message=(
            f"Provider {provider_id} does not accept thinking controls "
            f"for {model_id} ({reason})"
        ),
    )


def _handle_none_contract(
    *,
    payload: dict[str, Any],
    model_id: str,
    provider_id: str,
    contract: ThinkingControlContract,
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy,
) -> ProviderRequestAdaptation:
    """Handle a contract that accepts no thinking controls."""
    if policy.unsupported_control == "reject":
        return _reject(payload, model_id, provider_id, "none_contract", intent)

    # warn_drop: remove thinking controls, preserve reasoning history.
    new_payload = dict(payload)
    removed: list[str] = []
    for field_name in _thinking_control_field_names():
        if field_name in new_payload:
            del new_payload[field_name]
            removed.append(field_name)

    if not removed:
        return ProviderRequestAdaptation(
            payload=payload,
            changed=False,
            decision="passthrough",
        )

    return ProviderRequestAdaptation(
        payload=new_payload,
        changed=True,
        decision="dropped",
        requested_controls=tuple(removed),
        warnings=(
            AdaptationWarning(
                kind="thinking_control_dropped",
                detail=f"none contract: removed {removed}",
            ),
        ),
    )


def _handle_fixed_contract(
    *,
    payload: dict[str, Any],
    model_id: str,
    provider_id: str,
    contract: ThinkingControlContract,
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy,
) -> ProviderRequestAdaptation:
    """Handle a contract where reasoning is fixed (no client controls)."""
    if policy.unsupported_control == "reject":
        return _reject(
            payload,
            model_id,
            provider_id,
            "fixed_contract",
            intent,
        )

    # warn_drop: remove effort/budget controls, keep reasoning content.
    new_payload = dict(payload)
    removed: list[str] = []

    # Remove reasoning_effort (OpenAI style).
    if "reasoning_effort" in new_payload:
        del new_payload["reasoning_effort"]
        removed.append("reasoning_effort")

    # Remove thinking block (Anthropic style) but preserve content.
    thinking_obj: object = new_payload.get("thinking")
    if isinstance(thinking_obj, dict):
        thinking_dict: dict[str, object] = dict(thinking_obj)  # type: ignore[arg-type]
        had_budget = "budget_tokens" in thinking_dict
        thinking_dict.pop("budget_tokens", None)
        thinking_dict.pop("type", None)
        if not thinking_dict:
            del new_payload["thinking"]
        else:
            new_payload["thinking"] = thinking_dict
        if had_budget:
            removed.append("thinking.budget_tokens")

    if not removed:
        return ProviderRequestAdaptation(
            payload=payload,
            changed=False,
            decision="passthrough",
        )

    return ProviderRequestAdaptation(
        payload=new_payload,
        changed=True,
        decision="dropped",
        requested_controls=tuple(removed),
        warnings=(
            AdaptationWarning(
                kind="thinking_control_dropped",
                detail=f"fixed contract: removed {removed}",
            ),
        ),
    )


def _handle_effort_budget_contract(
    *,
    payload: dict[str, Any],
    client_protocol: str,
    model_id: str,
    provider_id: str,
    contract: ThinkingControlContract,
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy,
) -> ProviderRequestAdaptation:
    """Handle effort/budget/effort_or_budget contracts."""
    requested = list(intent.request_fields)
    emitted: list[str] = []
    warnings: list[AdaptationWarning] = []
    new_payload = dict(payload)
    changed = False

    for field_name in requested:
        if field_name == "reasoning_effort":
            adapted = _adapt_reasoning_effort(
                new_payload=new_payload,
                contract=contract,
                model_id=model_id,
                provider_id=provider_id,
            )
            if adapted is not None:
                new_payload = adapted
                changed = True
                emitted.append("reasoning_effort")
            else:
                warnings.append(
                    AdaptationWarning(
                        kind="effort_mapped",
                        field_name="reasoning_effort",
                        detail=(
                            f"effort {intent.requested_effort!r} mapped via contract"
                        ),
                    ),
                )
                emitted.append("reasoning_effort")

        elif field_name == "thinking":
            adapted = _adapt_thinking_block(
                new_payload=new_payload,
                contract=contract,
                model_id=model_id,
                provider_id=provider_id,
                intent=intent,
                policy=policy,
            )
            if adapted is not None:
                new_payload = adapted
                changed = True
                emitted.append("thinking")
            else:
                emitted.append("thinking")

        elif field_name == "thinking_budget":
            adapted = _adapt_thinking_budget(
                new_payload=new_payload,
                contract=contract,
                model_id=model_id,
                provider_id=provider_id,
            )
            if adapted is not None:
                new_payload = adapted
                changed = True
                emitted.append("thinking_budget")

    decision: Literal["passthrough", "mapped", "dropped", "rejected"] = "passthrough"
    if changed:
        if any(w.kind == "thinking_control_dropped" for w in warnings):
            decision = "dropped"
        else:
            decision = "mapped"

    return ProviderRequestAdaptation(
        payload=new_payload,
        changed=changed,
        decision=decision,
        requested_controls=tuple(requested),
        emitted_controls=tuple(emitted),
        warnings=tuple(warnings),
    )


def _adapt_reasoning_effort(
    *,
    new_payload: dict[str, Any],
    contract: ThinkingControlContract,
    model_id: str,
    provider_id: str,
) -> dict[str, Any] | None:
    """Adapt a reasoning_effort field against the contract.

    Returns the modified payload or ``None`` if no change was needed.
    """
    effort_value: object = new_payload.get("reasoning_effort")
    if not isinstance(effort_value, str):
        return None

    normalized = effort_value.lower()
    # Check alias mapping.
    alias_target = contract.effort_aliases.get(normalized)
    if alias_target is not None:
        new_payload = dict(new_payload)
        new_payload["reasoning_effort"] = alias_target
        return new_payload

    # Check if effort is accepted.
    accepted_normalized = {e.lower() for e in contract.accepted_efforts}
    if normalized in accepted_normalized:
        return None  # already valid

    # Effort not accepted — depends on policy.
    return None


def _adapt_thinking_block(
    *,
    new_payload: dict[str, Any],
    contract: ThinkingControlContract,
    model_id: str,
    provider_id: str,
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy,
) -> dict[str, Any] | None:
    """Adapt an Anthropic-style thinking block against the contract."""
    thinking_obj: object = new_payload.get("thinking")
    if not isinstance(thinking_obj, dict):
        return None

    thinking_dict: dict[str, object] = dict(thinking_obj)  # type: ignore[arg-type]
    changed = False

    # If contract doesn't accept budget, remove it.
    budget_not_accepted = contract.mode not in ("budget", "effort_or_budget")
    if budget_not_accepted and "budget_tokens" in thinking_dict:
        del thinking_dict["budget_tokens"]
        changed = True

    # If contract doesn't accept effort/type, remove them.
    if contract.mode not in ("effort", "effort_or_budget"):
        for key in ("type", "effort"):
            if key in thinking_dict:
                del thinking_dict[key]
                changed = True

    if not changed:
        return None

    new_payload = dict(new_payload)
    if thinking_dict:
        new_payload["thinking"] = thinking_dict
    else:
        del new_payload["thinking"]
    return new_payload


def _adapt_thinking_budget(
    *,
    new_payload: dict[str, Any],
    contract: ThinkingControlContract,
    model_id: str,
    provider_id: str,
) -> dict[str, Any] | None:
    """Adapt a top-level thinking_budget field against the contract."""
    if (
        contract.mode not in ("budget", "effort_or_budget")
        and "thinking_budget" in new_payload
    ):
        new_payload = dict(new_payload)
        del new_payload["thinking_budget"]
        return new_payload
    return None


def _thinking_control_field_names() -> list[str]:
    """Return the top-level field names that carry thinking controls."""
    return [
        "reasoning_effort",
        "thinking",
        "thinking_budget",
        "reasoning",
    ]
