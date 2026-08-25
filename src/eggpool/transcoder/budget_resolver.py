"""Thinking budget resolution — centralised effort-to-budget translation.

This module replaces the hard-coded ``reasoning_effort`` → ``budget_tokens``
mapping that previously lived inside ``OpenAIToAnthropic.encode_request``
with a reusable resolver that understands:

- Global defaults (``[transcoder.thinking_budget_defaults]``).
- Per-provider/model effort-to-budget overrides (via ``ThinkingCapability``).
- Capability min/max clamping.
- Strict rejection policy.

Resolution order:

1. If the client supplies an explicit ``budget_tokens`` value (Anthropic
   style), validate and clamp it.
2. If the client supplies a ``reasoning_effort`` string (OpenAI style),
   look up the budget in the capability's ``effort_to_budget_tokens``
   mapping first, then fall back to the global defaults.
3. If the effort is unknown and no default applies, reject it in strict
   mode or omit the target thinking control with a bounded warning in
   lenient mode. Never invent a target budget for an effort that has no
   verified mapping.
4. Clamp to capability min/max when known.
5. Reject if ``budget_resolution_policy = "strict"`` and the resolved
   budget was clamped or the effort was unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    is_reasoning_disabled_effort,
)
from eggpool.errors import CapabilityError

logger = logging.getLogger(__name__)

# Defensive fallback for the no-input path. Callers should only invoke the
# resolver when a thinking control is present; it is never used for an
# unknown effort.
_DEFAULT_BUDGET = 4096

# Canonical effort levels recognised by the resolver.
_KNOWN_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high"})


@dataclass(frozen=True, slots=True)
class ThinkingBudgetResolution:
    """Result of :func:`resolve_thinking_budget`."""

    budget_tokens: int | None
    source: str
    clamped: bool = False
    thinking_enabled: bool = True
    warnings: list[dict[str, Any]] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )


def resolve_thinking_budget(
    *,
    model_id: str,
    provider_id: str | None,
    requested_effort: str | None = None,
    requested_budget_tokens: int | None = None,
    capability: ThinkingCapability | None = None,
    budget_defaults: dict[str, int] | None = None,
    budget_resolution_policy: str = "lenient",
) -> ThinkingBudgetResolution:
    """Resolve a thinking budget from client inputs and capability metadata.

    Parameters:
        model_id: the resolved model id (for warning messages).
        provider_id: the resolved provider id (may be ``None``).
        requested_effort: OpenAI-style ``reasoning_effort`` string.
        requested_budget_tokens: Anthropic-style explicit ``budget_tokens``.
        capability: the resolved ``ThinkingCapability`` for the model.
        budget_defaults: global effort→budget defaults from config.
        budget_resolution_policy: ``"lenient"`` (default, omit an
            unrepresentable target control with a warning) or ``"strict"``
            (reject unknown efforts and clamped budgets).

    Returns:
        A :class:`ThinkingBudgetResolution` with the resolved budget,
        provenance, clamping flag, and any structured warnings.
    """
    warnings: list[dict[str, Any]] = []
    cap = capability or ThinkingCapability()
    provider_label = provider_id or "unknown"

    # --- Step 1: explicit Anthropic-style budget_tokens ---------------
    if requested_budget_tokens is not None:
        budget, clamped, clamp_warnings = _clamp_budget(
            requested_budget_tokens, cap, model_id, provider_label
        )
        warnings.extend(clamp_warnings)

        if budget_resolution_policy == "strict" and clamped:
            warnings.append(
                {
                    "kind": "budget_rejected",
                    "reason": "strict_clamp",
                    "requested": requested_budget_tokens,
                    "resolved": budget,
                    "model_id": model_id,
                    "provider_id": provider_id,
                }
            )
            raise BudgetResolutionError(
                f"Budget {requested_budget_tokens} clamped to {budget} "
                f"for {model_id} (strict policy)",
                model_id=model_id,
                requested_budget_tokens=requested_budget_tokens,
                resolved_budget_tokens=budget,
                budget_resolution_policy=budget_resolution_policy,
                reason="strict_clamp",
                provider_id=provider_id,
            )

        return ThinkingBudgetResolution(
            budget_tokens=budget,
            source="explicit_budget",
            clamped=clamped,
            warnings=warnings,
        )

    # --- Step 2: OpenAI-style reasoning_effort -----------------------
    if requested_effort is not None:
        effort = requested_effort.lower()
        # OpenAI's verified ``none`` effort explicitly disables reasoning. It
        # is not a low budget and must never enable Anthropic thinking as a
        # side effect of protocol translation.
        if is_reasoning_disabled_effort(effort):
            return ThinkingBudgetResolution(
                budget_tokens=None,
                source="reasoning_disabled",
                thinking_enabled=False,
            )
        return _resolve_effort(
            effort=effort,
            model_id=model_id,
            provider_id=provider_id,
            capability=cap,
            budget_defaults=budget_defaults,
            budget_resolution_policy=budget_resolution_policy,
            warnings=warnings,
        )

    # --- Step 3: no thinking controls supplied -----------------------
    # This path should not normally be reached (the caller should only
    # invoke the resolver when a thinking control is present), but
    # handle it defensively.
    warnings.append(
        {
            "kind": "budget_resolution_no_input",
            "reason": "no_effort_or_budget",
            "model_id": model_id,
            "provider_id": provider_id,
        }
    )
    return ThinkingBudgetResolution(
        budget_tokens=_DEFAULT_BUDGET,
        source="fallback_default",
        warnings=warnings,
    )


def _resolve_effort(
    *,
    effort: str,
    model_id: str,
    provider_id: str | None,
    capability: ThinkingCapability,
    budget_defaults: dict[str, int] | None,
    budget_resolution_policy: str,
    warnings: list[dict[str, Any]],
) -> ThinkingBudgetResolution:
    """Resolve a budget from an effort level string."""
    provider_label = provider_id or "unknown"

    # 2a: capability's per-model/provider effort mapping
    if capability.effort_to_budget_tokens is not None:
        raw = capability.effort_to_budget_tokens.get(effort)
        if raw is not None:
            budget, clamped, clamp_warnings = _clamp_budget(
                raw, capability, model_id, provider_label
            )
            warnings.extend(clamp_warnings)

            if budget_resolution_policy == "strict" and clamped:
                warnings.append(
                    {
                        "kind": "budget_rejected",
                        "reason": "strict_clamp",
                        "requested": raw,
                        "resolved": budget,
                        "model_id": model_id,
                        "provider_id": provider_id,
                    }
                )
                raise BudgetResolutionError(
                    f"Budget {raw} clamped to {budget} for {model_id} (strict policy)",
                    model_id=model_id,
                    requested_budget_tokens=raw,
                    resolved_budget_tokens=budget,
                    budget_resolution_policy=budget_resolution_policy,
                    reason="strict_clamp",
                    provider_id=provider_id,
                )

            return ThinkingBudgetResolution(
                budget_tokens=budget,
                source="capability_effort_mapping",
                clamped=clamped,
                warnings=warnings,
            )

    # 2b: global config defaults
    if budget_defaults is not None:
        raw = budget_defaults.get(effort)
        if raw is not None:
            budget, clamped, clamp_warnings = _clamp_budget(
                raw, capability, model_id, provider_label
            )
            warnings.extend(clamp_warnings)

            if budget_resolution_policy == "strict" and clamped:
                warnings.append(
                    {
                        "kind": "budget_rejected",
                        "reason": "strict_clamp",
                        "requested": raw,
                        "resolved": budget,
                        "model_id": model_id,
                        "provider_id": provider_id,
                    }
                )
                raise BudgetResolutionError(
                    f"Budget {raw} clamped to {budget} for {model_id} (strict policy)",
                    model_id=model_id,
                    requested_budget_tokens=raw,
                    resolved_budget_tokens=budget,
                    budget_resolution_policy=budget_resolution_policy,
                    reason="strict_clamp",
                    provider_id=provider_id,
                )

            return ThinkingBudgetResolution(
                budget_tokens=budget,
                source="global_defaults",
                clamped=clamped,
                warnings=warnings,
            )

    # 2c: hard-coded fallback
    if effort in _KNOWN_EFFORTS:
        fallback_map = {"low": 1024, "medium": 4096, "high": 16384}
        raw = fallback_map[effort]
        budget, clamped, clamp_warnings = _clamp_budget(
            raw, capability, model_id, provider_label
        )
        warnings.extend(clamp_warnings)

        if budget_resolution_policy == "strict" and clamped:
            warnings.append(
                {
                    "kind": "budget_rejected",
                    "reason": "strict_clamp",
                    "requested": raw,
                    "resolved": budget,
                    "model_id": model_id,
                    "provider_id": provider_id,
                }
            )
            raise BudgetResolutionError(
                f"Budget {raw} clamped to {budget} for {model_id} (strict policy)",
                model_id=model_id,
                requested_budget_tokens=raw,
                resolved_budget_tokens=budget,
                budget_resolution_policy=budget_resolution_policy,
                reason="strict_clamp",
                provider_id=provider_id,
            )

        return ThinkingBudgetResolution(
            budget_tokens=budget,
            source="hardcoded_fallback",
            clamped=clamped,
            warnings=warnings,
        )

    # 2d: unmapped effort level. A current provider may support a value such
    # as ``xhigh`` or ``max`` without EggPool having a verified Anthropic
    # budget mapping. In lenient mode preserve the request safely by omitting
    # the target control and reporting bounded structural loss. A guessed
    # medium budget would silently change client intent.
    warnings.append(
        {
            "kind": "unknown_effort",
            "field": "reasoning_effort",
            "effort": effort,
            "reason": "target_mapping_unknown",
            "model_id": model_id,
            "provider_id": provider_id,
        }
    )

    if budget_resolution_policy == "strict":
        warnings.append(
            {
                "kind": "budget_rejected",
                "reason": "unknown_effort_strict",
                "effort": effort,
                "model_id": model_id,
                "provider_id": provider_id,
            }
        )
        raise BudgetResolutionError(
            f"Unknown effort {effort!r} for {model_id} (strict policy)",
            model_id=model_id,
            requested_effort=effort,
            budget_resolution_policy=budget_resolution_policy,
            reason="unknown_effort_strict",
            provider_id=provider_id,
        )

    return ThinkingBudgetResolution(
        budget_tokens=None,
        source="unmapped_effort_dropped",
        thinking_enabled=False,
        warnings=warnings,
    )


def _clamp_budget(
    requested: int,
    capability: ThinkingCapability,
    model_id: str,
    provider_label: str,
) -> tuple[int, bool, list[dict[str, Any]]]:
    """Clamp *requested* to capability min/max bounds.

    Returns ``(clamped_value, was_clamped, warnings)``.
    """
    warnings: list[dict[str, Any]] = []
    budget_min = capability.budget_tokens_min
    budget_max = capability.budget_tokens_max
    clamped = False

    value = requested

    if budget_min is not None and budget_max is not None and budget_min > budget_max:
        # Malformed capability shape (config validation rejects an
        # explicit min>max, but external model-info sources may not).
        # Applying both bounds would clamp twice with contradictory
        # warnings; leave the requested value untouched instead.
        warnings.append(
            {
                "kind": "budget_clamp_skipped",
                "reason": "invalid_capability_bounds",
                "requested": requested,
                "budget_tokens_min": budget_min,
                "budget_tokens_max": budget_max,
                "model_id": model_id,
                "provider_id": provider_label,
            }
        )
        return value, clamped, warnings

    if budget_min is not None and value < budget_min:
        value = budget_min
        clamped = True
        warnings.append(
            {
                "kind": "budget_clamped",
                "direction": "min",
                "requested": requested,
                "resolved": value,
                "model_id": model_id,
                "provider_id": provider_label,
            }
        )

    if budget_max is not None and value > budget_max:
        value = budget_max
        clamped = True
        warnings.append(
            {
                "kind": "budget_clamped",
                "direction": "max",
                "requested": requested,
                "resolved": value,
                "model_id": model_id,
                "provider_id": provider_label,
            }
        )

    return value, clamped, warnings


class BudgetResolutionError(CapabilityError):
    """Raised when the budget resolver rejects a request (strict mode).

    Inherits from :class:`CapabilityError` so the request layer's existing
    capability-error renderer converts it into a protocol-appropriate
    ``capability_error`` response (HTTP 400) without a special case. The
    default ``requested_fields`` list carries the resolved budget token,
    capability id, and policy reason so operators can distinguish a
    clamping rejection (``reason="strict_clamp"``) from an unknown-effort
    rejection (``reason="unknown_effort_strict"``).
    """

    def __init__(
        self,
        message: str,
        *,
        model_id: str = "",
        requested_budget_tokens: int | None = None,
        requested_effort: str | None = None,
        resolved_budget_tokens: int | None = None,
        budget_resolution_policy: str = "strict",
        reason: str = "",
        provider_id: str | None = None,
    ) -> None:
        detail_fields: list[str] = ["thinking.budget"]
        if reason:
            detail_fields.append(f"reason={reason}")
        if budget_resolution_policy:
            detail_fields.append(f"policy={budget_resolution_policy}")
        if provider_id:
            detail_fields.append(f"provider={provider_id}")
        if resolved_budget_tokens is not None:
            detail_fields.append(f"resolved={resolved_budget_tokens}")
        if requested_budget_tokens is not None:
            detail_fields.append(f"requested={requested_budget_tokens}")
        if requested_effort:
            detail_fields.append(f"effort={requested_effort}")
        super().__init__(
            model_id=model_id,
            capability="thinking",
            requested_fields=detail_fields,
            message=message,
        )
        self.reason = reason
        self.budget_resolution_policy = budget_resolution_policy
        self.provider_id = provider_id
        self.requested_budget_tokens = requested_budget_tokens
        self.resolved_budget_tokens = resolved_budget_tokens
        self.requested_effort = requested_effort
