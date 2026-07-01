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
from typing import Literal, cast

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
    *,
    provider_statuses: dict[str, CapabilityStatus] | None = None,
) -> dict[str, object]:
    """Serialize a :class:`ThinkingCapability` for the ``/v1/models`` response.

    Returns a compact dict suitable for inclusion in the model object's
    ``capabilities`` field.  Unknown/empty values are omitted to keep
    the serialized form minimal.

    When *provider_statuses* is supplied (for collapsed/aggregate
    entries), a ``providers`` dict maps each provider ID to its
    individual thinking status so clients can see per-provider truth.
    """
    result: dict[str, object] = {"status": capability.status}
    if capability.source != "unknown":
        result["source"] = capability.source
    if capability.native_protocols:
        result["native_protocols"] = list(capability.native_protocols)

    # Client control field mappings — per-protocol request/response/streaming
    # fields that a client can use to drive thinking/reasoning controls.
    if capability.client_controls:
        for proto, ctrl in sorted(capability.client_controls.items()):
            prefix = proto.lower()
            if ctrl.request_fields:
                result[f"{prefix}_request_fields"] = list(ctrl.request_fields)
            if ctrl.response_fields:
                result[f"{prefix}_response_fields"] = list(ctrl.response_fields)
            if ctrl.stream_delta_fields:
                result[f"{prefix}_stream_delta_fields"] = list(
                    ctrl.stream_delta_fields,
                )
            if ctrl.response_block_types:
                result[f"{prefix}_response_block_types"] = list(
                    ctrl.response_block_types,
                )

    if capability.budget_tokens_min is not None:
        result["budget_tokens_min"] = capability.budget_tokens_min
    if capability.budget_tokens_max is not None:
        result["budget_tokens_max"] = capability.budget_tokens_max
    if capability.effort_to_budget_tokens is not None:
        result["effort_to_budget_tokens"] = dict(capability.effort_to_budget_tokens)

    # Per-provider status breakdown for aggregate (collapsed) entries.
    if provider_statuses:
        result["providers"] = dict(provider_statuses)

    return result


def serialize_model_capabilities(
    capabilities: ModelCapabilities,
    *,
    provider_statuses: dict[str, CapabilityStatus] | None = None,
) -> dict[str, object]:
    """Serialize :class:`ModelCapabilities` for the ``/v1/models`` response.

    Returns a dict with a ``thinking`` key containing the compact
    serialized form.  Only non-default capability families are included.

    When *provider_statuses* is supplied (for collapsed/aggregate
    entries), per-provider thinking status is forwarded to the thinking
    serializer.
    """
    result: dict[str, object] = {}
    thinking = serialize_thinking_for_models(
        capabilities.thinking,
        provider_statuses=provider_statuses,
    )
    if thinking:
        result["thinking"] = thinking
    return result


# ---------------------------------------------------------------------------
# Override conversion helpers
# ---------------------------------------------------------------------------


def thinking_override_to_capability(
    override: dict[str, object] | None,
) -> ThinkingCapability:
    """Convert a config override dict into a :class:`ThinkingCapability`.

    If *override* is ``None`` or every value is ``None``, returns a
    default (no-op) ``ThinkingCapability``.
    """
    if override is None:
        return ThinkingCapability()

    status = override.get("status")
    source = override.get("source")
    native_protocols = override.get("native_protocols")
    budget_min = override.get("budget_tokens_min")
    budget_max = override.get("budget_tokens_max")
    effort = override.get("effort_to_budget_tokens")
    notes = override.get("notes")

    fields = (status, source, native_protocols, budget_min, budget_max, effort, notes)
    has_any = any(v is not None for v in fields)
    if not has_any:
        return ThinkingCapability()

    if status is None:
        status = "unknown"
    if source is None and status != "unknown":
        source = "manual_override"
    if native_protocols is None:
        native_protocols = []

    native_list: list[str] = []
    native_val = cast("list[object] | None", native_protocols)
    if isinstance(native_val, list):
        native_list = [str(p) for p in native_val]

    cap_status: CapabilityStatus = cast(
        "CapabilityStatus",
        str(status) if status is not None else "unknown",
    )
    cap_source: CapabilitySource = cast(
        "CapabilitySource",
        str(source) if source is not None else "unknown",
    )
    effort_dict: dict[str, int] | None = None
    if isinstance(effort, dict):
        effort_dict = {str(k): int(v) for k, v in effort.items()}  # type: ignore[arg-type]
    return ThinkingCapability(
        status=cap_status,
        source=cap_source,
        native_protocols=native_list,
        budget_tokens_min=budget_min if isinstance(budget_min, int) else None,
        budget_tokens_max=budget_max if isinstance(budget_max, int) else None,
        effort_to_budget_tokens=effort_dict,
        notes=str(notes) if notes is not None else None,
    )


def model_capabilities_override_to_config(
    override: dict[str, object] | None,
) -> ModelCapabilities:
    """Convert a ``ModelCapabilitiesOverrideConfig`` dict into ModelCapabilities.

    The *override* dict may contain a ``thinking`` key whose value is a
    dict compatible with :func:`thinking_override_to_capability`.
    """
    if override is None:
        return ModelCapabilities()

    thinking_raw = override.get("thinking")
    thinking: ThinkingCapability
    if isinstance(thinking_raw, dict):
        thinking = thinking_override_to_capability(
            cast("dict[str, object]", thinking_raw),
        )
    else:
        thinking = ThinkingCapability()

    return ModelCapabilities(thinking=thinking)


def apply_capability_overrides(
    model_id: str,
    base: ModelCapabilities,
    global_overrides: dict[str, dict[str, object]],
    provider_overrides: dict[str, dict[str, object]],
    provider_id: str | None = None,
) -> ModelCapabilities:
    """Apply a 3-layer override chain to *base* capabilities.

    Precedence (lowest → highest):

    1. *base* (discovered / provider catalog data)
    2. ``global_overrides[model_id]``
    3. ``provider_overrides[model_id]`` (only when *provider_id* matches)
    """
    result = base

    global_ov = global_overrides.get(model_id)
    if global_ov is not None:
        override_cap = model_capabilities_override_to_config(global_ov)
        result = merge_model_capabilities(result, override_cap)

    if provider_id is not None:
        provider_ov = provider_overrides.get(model_id)
        if provider_ov is not None:
            override_cap = model_capabilities_override_to_config(provider_ov)
            result = merge_model_capabilities(result, override_cap)

    return result


# ---------------------------------------------------------------------------
# Dict ↔ typed-model conversion
# ---------------------------------------------------------------------------


def dict_to_model_capabilities(data: dict[str, object]) -> ModelCapabilities:
    """Convert a plain dict (from the catalog cache) into :class:`ModelCapabilities`.

    Only thinking-related fields are extracted.  Unknown keys are
    ignored so the function degrades gracefully with future schema
    extensions.
    """
    thinking_raw = data.get("thinking")
    if not isinstance(thinking_raw, dict):
        return ModelCapabilities()

    tr = cast("dict[str, object]", thinking_raw)
    tc_status = str(tr.get("status", "unknown"))
    tc_source = str(tr.get("source", "unknown"))
    native_raw = tr.get("native_protocols")
    native_protos: list[str] = []
    native_val = cast("list[object] | None", native_raw)
    if isinstance(native_val, list):
        native_protos = [str(p) for p in native_val]
    bmin_raw = tr.get("budget_tokens_min")
    bmax_raw = tr.get("budget_tokens_max")
    effort_raw = tr.get("effort_to_budget_tokens")
    notes_raw = tr.get("notes")
    effort_dict: dict[str, int] | None = None
    if isinstance(effort_raw, dict):
        effort_dict = {str(k): int(v) for k, v in effort_raw.items()}  # type: ignore[arg-type]

    # Parse per-protocol client controls.
    client_controls_raw = tr.get("client_controls")
    client_controls: dict[str, ThinkingClientControls] = {}
    if isinstance(client_controls_raw, dict):
        cc_dict = cast("dict[str, object]", client_controls_raw)
        for proto, ctrl_raw in cc_dict.items():
            if isinstance(ctrl_raw, dict):
                ctrl_dict = cast("dict[str, object]", ctrl_raw)
                request_fields_raw = cast(
                    "list[object] | None",
                    ctrl_dict.get("request_fields"),
                )
                response_fields_raw = cast(
                    "list[object] | None",
                    ctrl_dict.get("response_fields"),
                )
                stream_delta_raw = cast(
                    "list[object] | None",
                    ctrl_dict.get("stream_delta_fields"),
                )
                block_types_raw = cast(
                    "list[object] | None",
                    ctrl_dict.get("response_block_types"),
                )
                client_controls[str(proto)] = ThinkingClientControls(
                    request_fields=(
                        [str(f) for f in request_fields_raw]
                        if isinstance(request_fields_raw, list)
                        else []
                    ),
                    response_fields=(
                        [str(f) for f in response_fields_raw]
                        if isinstance(response_fields_raw, list)
                        else []
                    ),
                    stream_delta_fields=(
                        [str(f) for f in stream_delta_raw]
                        if isinstance(stream_delta_raw, list)
                        else []
                    ),
                    response_block_types=(
                        [str(f) for f in block_types_raw]
                        if isinstance(block_types_raw, list)
                        else []
                    ),
                )

    return ModelCapabilities(
        thinking=ThinkingCapability(
            status=cast("CapabilityStatus", tc_status),
            source=cast("CapabilitySource", tc_source),
            native_protocols=native_protos,
            client_controls=client_controls,
            budget_tokens_min=bmin_raw if isinstance(bmin_raw, int) else None,
            budget_tokens_max=bmax_raw if isinstance(bmax_raw, int) else None,
            effort_to_budget_tokens=effort_dict,
            notes=str(notes_raw) if notes_raw is not None else None,
        ),
    )


def model_capabilities_to_dict(capabilities: ModelCapabilities) -> dict[str, object]:
    """Convert :class:`ModelCapabilities` back to a plain dict for storage.

    The output is suitable for the catalog cache ``capabilities`` field.
    ``None`` / empty values are filtered out.
    """
    result: dict[str, object] = {}
    tc = capabilities.thinking

    if tc.status in ("supported", "mixed"):
        result["supports_tools"] = True

    thinking_dict: dict[str, object] = {}
    if tc.status != "unknown":
        thinking_dict["status"] = tc.status
    if tc.source != "unknown":
        thinking_dict["source"] = tc.source
    if tc.native_protocols:
        thinking_dict["native_protocols"] = list(tc.native_protocols)
    if tc.client_controls:
        thinking_dict["client_controls"] = {
            proto: ctrl.model_dump(exclude_none=True)
            for proto, ctrl in tc.client_controls.items()
        }
    if tc.budget_tokens_min is not None:
        thinking_dict["budget_tokens_min"] = tc.budget_tokens_min
    if tc.budget_tokens_max is not None:
        thinking_dict["budget_tokens_max"] = tc.budget_tokens_max
    if tc.effort_to_budget_tokens is not None:
        thinking_dict["effort_to_budget_tokens"] = dict(tc.effort_to_budget_tokens)
    if tc.notes is not None:
        thinking_dict["notes"] = tc.notes

    if thinking_dict:
        result["thinking"] = thinking_dict

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
