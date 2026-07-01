"""Protocol-neutral capability schema for model metadata.

This module defines a structured representation for model capabilities
(currently focused on thinking/reasoning) that is decoupled from any
specific transcoder implementation.  It lives in the catalog package so
it can be imported by catalog, routing, serialization, and config code
without circular dependencies.

Capability semantics:

- **Status**: whether a model/provider actually supports the capability.
- **Source**: where the status was observed (catalog, model-info, override, etc.).
- **Native protocols**: which upstream protocols expose the controls natively.
- **Client controls**: per-protocol field mappings for request/response/streaming.
- **Budget constraints**: optional min/max token bounds for thinking budgets.
- **Merge**: deterministic merge order across provider, global, and override layers.
- **Aggregate**: collapsed model entries derive a single status from all
  backing providers.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

CapabilityStatus = Literal[
    "supported", "unsupported", "unknown", "mixed", "conflicting"
]
CapabilitySource = Literal[
    "provider_catalog",
    "model_info",
    "manual_override",
    "heuristic",
    "aggregate",
    "unknown",
]

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ThinkingClientControls(BaseModel):
    """Per-protocol field mappings for thinking/reasoning controls.

    Describes which request, response, and streaming fields a client can
    send or receive through EggPool for a given upstream protocol.
    """

    request_fields: list[str] = Field(default_factory=list)
    response_fields: list[str] = Field(default_factory=list)
    stream_delta_fields: list[str] = Field(default_factory=list)
    response_block_types: list[str] = Field(default_factory=list)


class ThinkingCapability(BaseModel):
    """Structured thinking/reasoning capability for a model.

    A status of ``"unknown"`` (the default) means no data has been
    observed — it is explicitly *not* ``"unsupported"``.  This avoids
    false negatives when capability data has not yet been populated.
    """

    status: CapabilityStatus = "unknown"
    source: CapabilitySource = "unknown"
    native_protocols: list[str] = Field(default_factory=list)
    client_controls: dict[str, ThinkingClientControls] = Field(
        default_factory=dict,
    )
    budget_tokens_min: int | None = None
    budget_tokens_max: int | None = None
    effort_to_budget_tokens: dict[str, int] | None = None
    notes: str | None = None


class ModelCapabilities(BaseModel):
    """Top-level capability container for a model.

    Initially only ``thinking`` is modelled; the container is designed
    to grow future capability families (vision, tools, structured
    outputs, prompt caching, logprobs) without breaking callers.
    """

    thinking: ThinkingCapability = Field(default_factory=ThinkingCapability)


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

_MERGE_PRECEDENCE: list[CapabilityStatus] = [
    "supported",
    "unsupported",
    "mixed",
    "conflicting",
    "unknown",
]


def _status_priority(status: CapabilityStatus) -> int:
    """Lower index = higher priority for merge precedence."""
    try:
        return _MERGE_PRECEDENCE.index(status)
    except ValueError:
        return len(_MERGE_PRECEDENCE)


def merge_thinking_capabilities(
    base: ThinkingCapability,
    override: ThinkingCapability,
) -> ThinkingCapability:
    """Merge two :class:`ThinkingCapability` values with override semantics.

    Merge order (lowest to highest priority):

    1. Built-in safe defaults (``base``).
    2. Provider catalog / model-info data (``override``).

    When ``override`` carries non-default values they win.  When both
    sides carry non-default values the higher-priority status wins
    (``supported`` > ``unsupported`` > ``mixed`` > ``conflicting`` >
    ``unknown``).  If statuses are equal the override's metadata is
    preferred.
    """
    # If override is fully default, keep base unchanged.
    if override.status == "unknown" and override.source == "unknown":
        return base.model_copy(deep=True)

    # Status merge: higher-priority wins; on tie prefer override.
    base_prio = _status_priority(base.status)
    override_prio = _status_priority(override.status)
    if override.status != "unknown" and (
        base.status == "unknown" or override_prio <= base_prio
    ):
        merged_status = override.status
        merged_source = override.source
    elif base.status != "unknown" and override.status == "unknown":
        merged_status = base.status
        merged_source = base.source
    else:
        merged_status = base.status
        merged_source = base.source

    # Native protocols: union of both.
    native_protos = sorted(
        set(base.native_protocols) | set(override.native_protocols),
    )

    # Client controls: override wins per-protocol, base fills gaps.
    controls: dict[str, ThinkingClientControls] = {}
    all_protos: set[str] = set(base.client_controls) | set(override.client_controls)
    for proto in all_protos:
        if proto in override.client_controls and override.client_controls[proto]:
            controls[proto] = override.client_controls[proto]
        elif proto in base.client_controls:
            controls[proto] = base.client_controls[proto]

    # Budget tokens: override wins when non-None.
    budget_min = (
        override.budget_tokens_min
        if override.budget_tokens_min is not None
        else base.budget_tokens_min
    )
    budget_max = (
        override.budget_tokens_max
        if override.budget_tokens_max is not None
        else base.budget_tokens_max
    )
    effort = (
        override.effort_to_budget_tokens
        if override.effort_to_budget_tokens is not None
        else base.effort_to_budget_tokens
    )
    notes = override.notes if override.notes is not None else base.notes

    return ThinkingCapability(
        status=merged_status,
        source=merged_source,
        native_protocols=[p for p in native_protos if p in ("openai", "anthropic")],
        client_controls=controls,
        budget_tokens_min=budget_min,
        budget_tokens_max=budget_max,
        effort_to_budget_tokens=effort,
        notes=notes,
    )


def merge_model_capabilities(
    base: ModelCapabilities,
    override: ModelCapabilities,
) -> ModelCapabilities:
    """Merge two :class:`ModelCapabilities` values.

    Delegates to per-field merge helpers.  Currently only ``thinking``
    is implemented; new capability families follow the same pattern.
    """
    return ModelCapabilities(
        thinking=merge_thinking_capabilities(base.thinking, override.thinking),
    )


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


def aggregate_thinking_status(
    statuses: list[CapabilityStatus],
) -> CapabilityStatus:
    """Derive a single :class:`CapabilityStatus` from multiple providers.

    Rules (in order):

    - ``"supported"`` only if **every** entry is ``"supported"``.
    - ``"unsupported"`` only if **every** entry is ``"unsupported"``.
    - ``"unknown"`` if all entries are ``"unknown"``.
    - ``"conflicting"`` if any entry is ``"conflicting"``.
    - Otherwise ``"mixed"``.
    """
    if not statuses:
        return "unknown"
    unique = set(statuses)
    if unique == {"supported"}:
        return "supported"
    if unique == {"unsupported"}:
        return "unsupported"
    if unique == {"unknown"}:
        return "unknown"
    if "conflicting" in unique:
        return "conflicting"
    return "mixed"


def aggregate_thinking_capabilities(
    capabilities: list[ThinkingCapability],
) -> ThinkingCapability:
    """Aggregate thinking capabilities across multiple backing providers.

    The result carries:

    - An aggregate ``status`` derived from all individual statuses.
    - The union of ``native_protocols`` across all providers.
    - ``source`` set to ``"aggregate"``.
    - ``client_controls`` merged from all providers (last-wins per protocol).
    - Conservative budget bounds (min = max of mins, max = min of maxes).
    """
    if not capabilities:
        return ThinkingCapability()

    statuses: list[CapabilityStatus] = [c.status for c in capabilities]
    agg_status = aggregate_thinking_status(statuses)

    # Union of native protocols.
    native: set[str] = set()
    for c in capabilities:
        native |= set(c.native_protocols)

    # Merge client controls: last provider wins per protocol.
    controls: dict[str, ThinkingClientControls] = {}
    for c in capabilities:
        for proto, ctrl in c.client_controls.items():
            controls[proto] = ctrl

    # Conservative budget bounds.
    mins = [
        c.budget_tokens_min for c in capabilities if c.budget_tokens_min is not None
    ]
    maxes = [
        c.budget_tokens_max for c in capabilities if c.budget_tokens_max is not None
    ]
    budget_min = max(mins) if mins else None
    budget_max = min(maxes) if maxes else None
    # Invariant: min <= max.  If violated, fall back to None.
    if budget_min is not None and budget_max is not None and budget_min > budget_max:
        budget_min = None
        budget_max = None

    # Merge effort_to_budget_tokens: last-wins.
    effort: dict[str, int] | None = None
    for c in capabilities:
        if c.effort_to_budget_tokens is not None:
            effort = dict(c.effort_to_budget_tokens)

    return ThinkingCapability(
        status=agg_status,
        source="aggregate",
        native_protocols=[p for p in sorted(native) if p in ("openai", "anthropic")],
        client_controls=controls,
        budget_tokens_min=budget_min,
        budget_tokens_max=budget_max,
        effort_to_budget_tokens=effort,
    )


def aggregate_model_capabilities(
    capabilities_list: list[ModelCapabilities],
) -> ModelCapabilities:
    """Aggregate model capabilities across multiple backing providers."""
    if not capabilities_list:
        return ModelCapabilities()
    return ModelCapabilities(
        thinking=aggregate_thinking_capabilities(
            [c.thinking for c in capabilities_list],
        ),
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def serialize_thinking_for_models(
    capability: ThinkingCapability,
) -> dict[str, object]:
    """Serialize a :class:`ThinkingCapability` for the ``/v1/models`` response.

    Returns a compact dict suitable for inclusion in the model object's
    ``capabilities`` field.  Unknown/empty values are omitted to keep
    the serialized form minimal.
    """
    result: dict[str, object] = {"status": capability.status}
    if capability.source != "unknown":
        result["source"] = capability.source
    if capability.native_protocols:
        result["native_protocols"] = list(capability.native_protocols)
    if capability.budget_tokens_min is not None:
        result["budget_tokens_min"] = capability.budget_tokens_min
    if capability.budget_tokens_max is not None:
        result["budget_tokens_max"] = capability.budget_tokens_max
    if capability.effort_to_budget_tokens is not None:
        result["effort_to_budget_tokens"] = dict(capability.effort_to_budget_tokens)
    return result


def serialize_model_capabilities(
    capabilities: ModelCapabilities,
) -> dict[str, object]:
    """Serialize :class:`ModelCapabilities` for the ``/v1/models`` response.

    Returns a dict with a ``thinking`` key containing the compact
    serialized form.  Only non-default capability families are included.
    """
    result: dict[str, object] = {}
    thinking = serialize_thinking_for_models(capabilities.thinking)
    if thinking:
        result["thinking"] = thinking
    return result


# ---------------------------------------------------------------------------
# Request-level helpers
# ---------------------------------------------------------------------------


def client_requests_thinking(
    request_body: dict[str, object],
    capability: ThinkingCapability,
) -> bool:
    """Determine whether a client request requires thinking support.

    Heuristic: checks for ``thinking`` or ``reasoning`` keys in the
    request body, or ``reasoning_effort`` / ``thinking_budget`` fields.
    Returns ``False`` when the capability is ``"unknown"`` or
    ``"unsupported"`` (no point routing to a model that cannot serve it).
    """
    if capability.status in ("unsupported", "unknown", "conflicting"):
        return False

    return (
        "thinking" in request_body
        or "reasoning" in request_body
        or "reasoning_effort" in request_body
        or "thinking_budget" in request_body
    )


def has_thinking_support(capability: ThinkingCapability) -> bool:
    """Return whether a model is known to support thinking.

    ``True`` only when status is ``"supported"`` or ``"mixed"`` (mixed
    means at least one backing provider supports it).
    """
    return capability.status in ("supported", "mixed")
