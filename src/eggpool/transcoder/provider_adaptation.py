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
class ControlFieldAdaptation:
    """Typed result of a single field-level adaptation.

    Every field-level adapter returns one of these so the aggregate
    adapter can derive a final :class:`ProviderRequestAdaptation`
    without ambiguous ``None`` semantics.
    """

    disposition: Literal[
        "unchanged",
        "mapped",
        "dropped",
        "rejected",
        "not_present",
    ]
    payload: dict[str, Any]
    requested_field: str | None = None
    emitted_field: str | None = None
    warning: AdaptationWarning | None = None


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
    if policy.unsupported_control in ("reject", "map_if_known"):
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
        had_type = "type" in thinking_dict
        had_effort = "effort" in thinking_dict
        thinking_dict.pop("budget_tokens", None)
        thinking_dict.pop("type", None)
        thinking_dict.pop("effort", None)
        if not thinking_dict:
            del new_payload["thinking"]
        else:
            new_payload["thinking"] = thinking_dict
        if had_budget:
            removed.append("thinking.budget_tokens")
        if had_type:
            removed.append("thinking.type")
        if had_effort:
            removed.append("thinking.effort")

    # Remove top-level thinking_budget.
    if "thinking_budget" in new_payload:
        del new_payload["thinking_budget"]
        removed.append("thinking_budget")

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
            result = _adapt_reasoning_effort(
                new_payload=new_payload,
                contract=contract,
                model_id=model_id,
                provider_id=provider_id,
                policy=policy,
                intent=intent,
            )
            if result.disposition == "not_present":
                pass
            elif result.disposition == "unchanged":
                emitted.append("reasoning_effort")
            elif result.disposition == "mapped":
                new_payload = result.payload
                changed = True
                emitted.append("reasoning_effort")
                if result.warning:
                    warnings.append(result.warning)
            elif result.disposition == "dropped":
                new_payload = result.payload
                changed = True
                if result.warning:
                    warnings.append(result.warning)
            elif result.disposition == "rejected":
                return _reject(
                    new_payload,
                    model_id,
                    provider_id,
                    "unsupported_effort",
                    intent,
                )

        elif field_name == "thinking":
            result = _adapt_thinking_block(
                new_payload=new_payload,
                contract=contract,
                model_id=model_id,
                provider_id=provider_id,
                intent=intent,
                policy=policy,
            )
            if result.disposition == "not_present":
                pass
            elif result.disposition == "unchanged":
                emitted.append("thinking")
            elif result.disposition in ("mapped", "dropped"):
                new_payload = result.payload
                changed = True
                if "thinking" in new_payload:
                    emitted.append("thinking")
                if result.warning:
                    warnings.append(result.warning)
            elif result.disposition == "rejected":
                return _reject(
                    new_payload,
                    model_id,
                    provider_id,
                    "unsupported_nested_thinking_control",
                    intent,
                )

        elif field_name == "thinking_budget":
            result = _adapt_thinking_budget(
                new_payload=new_payload,
                contract=contract,
                model_id=model_id,
                provider_id=provider_id,
            )
            if result.disposition == "not_present":
                pass
            elif result.disposition == "unchanged":
                emitted.append("thinking_budget")
            elif result.disposition == "dropped":
                new_payload = result.payload
                changed = True
                if result.warning:
                    warnings.append(result.warning)

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
    policy: ProviderControlPolicy,
    intent: ThinkingRequestIntent,
) -> ControlFieldAdaptation:
    """Adapt a reasoning_effort field against the contract.

    Returns a typed :class:`ControlFieldAdaptation` so the caller can
    distinguish ``unchanged`` (already valid) from ``dropped`` /
    ``rejected`` (unsupported).
    """
    effort_value: object = new_payload.get("reasoning_effort")
    if not isinstance(effort_value, str):
        return ControlFieldAdaptation(disposition="not_present", payload=new_payload)

    normalized = effort_value.lower()
    # Check alias mapping.
    alias_target = contract.effort_aliases.get(normalized)
    if alias_target is not None:
        modified = dict(new_payload)
        modified["reasoning_effort"] = alias_target
        return ControlFieldAdaptation(
            disposition="mapped",
            payload=modified,
            requested_field="reasoning_effort",
            emitted_field="reasoning_effort",
            warning=AdaptationWarning(
                kind="effort_mapped",
                field_name="reasoning_effort",
                detail=f"effort {effort_value!r} mapped to {alias_target!r}",
            ),
        )

    # Check if effort is accepted.
    accepted_normalized = {e.lower() for e in contract.accepted_efforts}
    if normalized in accepted_normalized:
        return ControlFieldAdaptation(
            disposition="unchanged",
            payload=new_payload,
            requested_field="reasoning_effort",
            emitted_field="reasoning_effort",
        )

    # Effort not accepted — reject or drop per policy.
    if policy.unsupported_control in ("reject", "map_if_known"):
        return ControlFieldAdaptation(
            disposition="rejected",
            payload=new_payload,
            requested_field="reasoning_effort",
        )

    # warn_drop: remove the field.
    modified = dict(new_payload)
    del modified["reasoning_effort"]
    return ControlFieldAdaptation(
        disposition="dropped",
        payload=modified,
        requested_field="reasoning_effort",
        warning=AdaptationWarning(
            kind="thinking_control_dropped",
            field_name="reasoning_effort",
            detail=f"effort {effort_value!r} not accepted by contract",
        ),
    )


def _adapt_thinking_block(
    *,
    new_payload: dict[str, Any],
    contract: ThinkingControlContract,
    model_id: str,
    provider_id: str,
    intent: ThinkingRequestIntent,
    policy: ProviderControlPolicy,
) -> ControlFieldAdaptation:
    """Adapt an Anthropic-style thinking block against the contract.

    The nested fields use the same reject/drop/map policy as top-level
    controls.  ``type`` is structural for effort-capable contracts; it is
    unsupported for fixed/none and budget-only contracts.
    """
    thinking_obj: object = new_payload.get("thinking")
    if not isinstance(thinking_obj, dict):
        return ControlFieldAdaptation(disposition="not_present", payload=new_payload)

    thinking_dict: dict[str, object] = dict(thinking_obj)  # type: ignore[arg-type]
    removed_fields: list[str] = []
    mapped_fields: list[str] = []
    warnings: list[AdaptationWarning] = []

    def reject(field: str, detail: str) -> None:
        raise CapabilityError(
            model_id=model_id,
            capability="thinking",
            requested_fields=[field],
            message=f"Provider {provider_id} does not accept {field} ({detail})",
        )

    def unsupported(field: str, detail: str) -> None:
        if policy.unsupported_control in ("reject", "map_if_known"):
            reject(field, detail)
        thinking_dict.pop(field.removeprefix("thinking."), None)
        removed_fields.append(field)

    if "type" in thinking_dict and contract.mode not in (
        "effort",
        "effort_or_budget",
    ):
        unsupported("thinking.type", "thinking type is not selectable")

    if "effort" in thinking_dict:
        value = thinking_dict["effort"]
        if not isinstance(value, str):
            unsupported("thinking.effort", "effort must be a string")
        else:
            normalized = value.lower()
            alias = contract.effort_aliases.get(normalized)
            accepted = {effort.lower(): effort for effort in contract.accepted_efforts}
            if contract.mode not in ("effort", "effort_or_budget"):
                unsupported("thinking.effort", "effort is not accepted")
            elif alias is not None:
                thinking_dict["effort"] = alias
                mapped_fields.append("thinking.effort")
            elif normalized in accepted:
                thinking_dict["effort"] = accepted[normalized]
                if value != accepted[normalized]:
                    mapped_fields.append("thinking.effort")
            else:
                unsupported("thinking.effort", f"unknown effort {value!r}")

    if "budget_tokens" in thinking_dict:
        value = thinking_dict["budget_tokens"]
        valid_budget = isinstance(value, int) and not isinstance(value, bool)
        numeric_budget = (
            value if isinstance(value, int) and not isinstance(value, bool) else None
        )
        if numeric_budget is not None and contract.explicit_budget_min is not None:
            valid_budget = numeric_budget >= contract.explicit_budget_min
        if numeric_budget is not None and contract.explicit_budget_max is not None:
            valid_budget = numeric_budget <= contract.explicit_budget_max
        if contract.mode not in ("budget", "effort_or_budget"):
            unsupported("thinking.budget_tokens", "budget is not accepted")
        elif not valid_budget:
            unsupported("thinking.budget_tokens", "budget is outside provider bounds")

    if not removed_fields and not mapped_fields:
        return ControlFieldAdaptation(disposition="unchanged", payload=new_payload)

    modified = dict(new_payload)
    if thinking_dict:
        modified["thinking"] = thinking_dict
    else:
        del modified["thinking"]
    if removed_fields:
        warnings.append(
            AdaptationWarning(
                kind="thinking_control_dropped",
                field_name="thinking",
                detail=f"removed fields: {removed_fields}",
            )
        )
    return ControlFieldAdaptation(
        disposition="dropped" if removed_fields else "mapped",
        payload=modified,
        requested_field="thinking",
        emitted_field="thinking" if thinking_dict else None,
        warning=warnings[0] if warnings else None,
    )


def _adapt_thinking_budget(
    *,
    new_payload: dict[str, Any],
    contract: ThinkingControlContract,
    model_id: str,
    provider_id: str,
) -> ControlFieldAdaptation:
    """Adapt a top-level thinking_budget field against the contract."""
    if "thinking_budget" not in new_payload:
        return ControlFieldAdaptation(disposition="not_present", payload=new_payload)

    if contract.mode in ("budget", "effort_or_budget"):
        return ControlFieldAdaptation(
            disposition="unchanged",
            payload=new_payload,
            requested_field="thinking_budget",
            emitted_field="thinking_budget",
        )

    # Budget not accepted — remove it.
    modified = dict(new_payload)
    del modified["thinking_budget"]
    return ControlFieldAdaptation(
        disposition="dropped",
        payload=modified,
        requested_field="thinking_budget",
        warning=AdaptationWarning(
            kind="thinking_control_dropped",
            field_name="thinking_budget",
            detail=f"contract mode {contract.mode!r} does not accept budget",
        ),
    )


def _thinking_control_field_names() -> list[str]:
    """Return the top-level field names that carry thinking controls."""
    return [
        "reasoning_effort",
        "thinking",
        "thinking_budget",
        "reasoning",
    ]
