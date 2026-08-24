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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

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

# Literal type for thinking control contract modes — used in casts.
_ThinkingControlMode = Literal[
    "unknown", "none", "fixed", "effort", "budget", "effort_or_budget"
]
_HistoricalReasoningContent = Literal["unknown", "accepted", "required", "rejected"]

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


class ThinkingControlContract(BaseModel):
    """Explicit provider-bound contract for thinking/reasoning controls.

    Distinguishes *whether the model produces reasoning* from
    *whether this provider deployment accepts client-selectable
    effort or budget controls*.  This eliminates the previous
    ambiguity where an empty ``supported_efforts`` list could mean
    both "unknown metadata" and "known fixed behaviour."

    Control modes:

    - ``"unknown"``: metadata absent; policy decides best-effort routing.
    - ``"none"``: reasoning controls not accepted; reasoning not available.
    - ``"fixed"``: model may reason, but client cannot select effort/budget.
    - ``"effort"``: a named effort field is accepted.
    - ``"budget"``: an explicit token budget is accepted.
    - ``"effort_or_budget"``: both effort and budget are accepted.
    """

    mode: Literal[
        "unknown", "none", "fixed", "effort", "budget", "effort_or_budget"
    ] = "unknown"
    request_fields: list[str] = Field(default_factory=list)
    accepted_efforts: list[str] = Field(default_factory=list)
    effort_aliases: dict[str, str] = Field(default_factory=dict)
    effort_to_budget_tokens: dict[str, int] | None = None
    explicit_budget_min: int | None = None
    explicit_budget_max: int | None = None
    historical_reasoning_content: Literal[
        "unknown", "accepted", "required", "rejected"
    ] = "unknown"
    source: CapabilitySource = "unknown"


def infer_control_contract(capability: ThinkingCapability) -> ThinkingControlContract:
    """Infer a :class:`ThinkingControlContract` from legacy capability fields.

    Existing capability records may not carry an explicit
    ``control_contract``.  This function derives one conservatively
    from the legacy ``status``, ``supported_efforts``, and budget
    fields so downstream code can always inspect the contract without
    branching on its presence.
    """
    if capability.control_contract.mode != "unknown":
        return capability.control_contract

    status = capability.status
    efforts = capability.supported_efforts
    budget_min = capability.budget_tokens_min
    budget_max = capability.budget_tokens_max

    if status == "unsupported":
        return ThinkingControlContract(
            mode="none",
            source=capability.source,
        )

    if efforts:
        contract = ThinkingControlContract(
            mode="effort",
            accepted_efforts=list(efforts),
            source=capability.source,
        )
        if capability.effort_to_budget_tokens is not None:
            contract.effort_to_budget_tokens = dict(
                capability.effort_to_budget_tokens,
            )
        if budget_min is not None:
            contract.explicit_budget_min = budget_min
        if budget_max is not None:
            contract.explicit_budget_max = budget_max
        return contract

    if budget_min is not None or budget_max is not None:
        return ThinkingControlContract(
            mode="budget",
            explicit_budget_min=budget_min,
            explicit_budget_max=budget_max,
            source=capability.source,
        )

    if status == "supported":
        return ThinkingControlContract(
            mode="unknown",
            source=capability.source,
        )

    return ThinkingControlContract(source=capability.source)


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
    supported_efforts: list[str] = Field(default_factory=list)
    effort_to_budget_tokens: dict[str, int] | None = None
    control_contract: ThinkingControlContract = Field(
        default_factory=ThinkingControlContract,
    )
    notes: str | None = None


class PromptCacheCapability(BaseModel):
    """Provider/model contract for explicit prompt-cache boundaries.

    Protocol compatibility is deliberately not enough to populate this
    contract.  ``dialect`` identifies whether the selected provider/model
    implements the first-party protocol fields or a verified compatible
    provider extension.  TTLs and the boundary limit are facts about that
    selected contract, not protocol-wide assumptions.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: Literal["first_party", "compatible_extension"]
    supported_ttls: list[str] = Field(default_factory=list, max_length=4)
    default_ttl: str | None = None
    max_breakpoints: int = Field(default=4, ge=1, le=4)

    def ttl_label(self) -> str:
        """Return bounded semantic TTL metadata for loss warnings."""
        values = [value for value in self.supported_ttls if _is_ttl_label(value)]
        if not values:
            return "provider-defined"
        if self.default_ttl is not None and _is_ttl_label(self.default_ttl):
            if self.default_ttl in values:
                values = [
                    self.default_ttl,
                    *[v for v in values if v != self.default_ttl],
                ]
            if len(values) == 1:
                return values[0]
            return f"{values[0]} default; {' or '.join(values)} supported"
        return " or ".join(values)


def _is_ttl_label(value: object) -> bool:
    """Accept only small duration/cache-policy labels in diagnostics."""
    if not isinstance(value, str) or not value or len(value) > 16:
        return False
    if value in {"in_memory", "ephemeral"}:
        return True
    if not value[:-1].isdigit():
        return False
    return value[-1] in "smhd"


class TranscodingCapabilities(BaseModel):
    """Explicit native controls available on a provider/model target.

    Empty mappings are intentionally conservative: protocol compatibility
    does not imply that a compatible provider implements these newer
    controls.  Cache entries are provider/model contracts keyed by target
    protocol, not protocol-family defaults.
    """

    native_structured_outputs: list[str] = Field(default_factory=list)
    strict_tools: list[str] = Field(default_factory=list)
    parallel_tool_control: list[str] = Field(default_factory=list)
    reasoning_efforts: dict[str, list[str]] = Field(default_factory=dict)
    prompt_cache_breakpoints: dict[str, PromptCacheCapability] = Field(
        default_factory=dict,
    )

    def supports(self, feature: str, protocol: str) -> bool:
        """Return whether *protocol* explicitly supports *feature*."""
        values: object = getattr(self, feature, ())
        if isinstance(values, list):
            return protocol in values
        if feature == "prompt_cache_breakpoints" and isinstance(values, dict):
            return protocol in values
        return False

    def prompt_cache_capability(
        self,
        protocol: str,
    ) -> PromptCacheCapability | None:
        """Return the verified cache contract for a target protocol."""
        return self.prompt_cache_breakpoints.get(protocol)

    def supports_reasoning_effort(self, protocol: str, effort: str) -> bool:
        """Return whether a target explicitly accepts this effort value."""
        return effort in self.reasoning_efforts.get(protocol, ())


# ---------------------------------------------------------------------------
# Multimodal capability model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaCapability:
    """Granular media support for a provider/model/protocol.

    Each field indicates a supported source form.  ``False`` means
    unknown (conservative: never authorize a field).
    """

    base64: bool = False
    url: bool = False
    max_source_bytes: int | None = None
    """Maximum decoded source bytes when provider docs define them."""


@dataclass(frozen=True)
class MultimodalCapabilities:
    """Per-model multimodal capabilities.

    These are provider/model/protocol scoped.  Unknown remains unknown
    and must never authorize a field.
    """

    image_input: MediaCapability = field(default_factory=MediaCapability)
    document_input: MediaCapability = field(default_factory=MediaCapability)
    audio_input: MediaCapability = field(default_factory=MediaCapability)
    non_text_tool_result: bool = False
    """Whether the provider supports media inside tool results."""
    max_serialized_request_bytes: int | None = None
    """Maximum serialized upstream request bytes when known."""


class ModelCapabilities(BaseModel):
    """Top-level capability container for a model.

    Initially only ``thinking`` is modelled; the container is designed
    to grow future capability families (vision, tools, structured
    outputs, prompt caching, logprobs) without breaking callers.
    """

    thinking: ThinkingCapability = Field(default_factory=ThinkingCapability)
    transcoding: TranscodingCapabilities = Field(
        default_factory=TranscodingCapabilities,
    )
    multimodal: MultimodalCapabilities = Field(
        default_factory=MultimodalCapabilities,
    )


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
    supported_efforts = (
        override.supported_efforts
        if override.supported_efforts
        else base.supported_efforts
    )
    notes = override.notes if override.notes is not None else base.notes

    # Control contract: override wins when non-default.
    contract = base.control_contract
    if override.control_contract.mode != "unknown":
        contract = override.control_contract

    return ThinkingCapability(
        status=merged_status,
        source=merged_source,
        native_protocols=[p for p in native_protos if p in ("openai", "anthropic")],
        client_controls=controls,
        budget_tokens_min=budget_min,
        budget_tokens_max=budget_max,
        supported_efforts=supported_efforts,
        effort_to_budget_tokens=effort,
        control_contract=contract,
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
        transcoding=(
            override.transcoding
            if override.transcoding != TranscodingCapabilities()
            else base.transcoding.model_copy(deep=True)
        ),
        multimodal=(
            override.multimodal
            if override.multimodal != MultimodalCapabilities()
            else base.multimodal
        ),
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

    # Merge effort metadata conservatively: retain the lowest budget for
    # each effort so the aggregate never promises more than one provider
    # can support. Advertised effort labels remain a union.
    effort: dict[str, int] | None = None
    supported_efforts: list[str] = []
    for c in capabilities:
        if c.effort_to_budget_tokens is not None:
            if effort is None:
                effort = {}
            for effort_name, budget in c.effort_to_budget_tokens.items():
                previous = effort.get(effort_name)
                if previous is None or budget < previous:
                    effort[effort_name] = budget
        for item in c.supported_efforts:
            if item not in supported_efforts:
                supported_efforts.append(item)

    return ThinkingCapability(
        status=agg_status,
        source="aggregate",
        native_protocols=[p for p in sorted(native) if p in ("openai", "anthropic")],
        client_controls=controls,
        budget_tokens_min=budget_min,
        budget_tokens_max=budget_max,
        supported_efforts=supported_efforts,
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
    if capability.supported_efforts:
        result["supported_efforts"] = list(capability.supported_efforts)
    if capability.effort_to_budget_tokens is not None:
        result["effort_to_budget_tokens"] = dict(capability.effort_to_budget_tokens)

    # Provider-bound control contract — always emit when mode is not
    # "unknown" so clients and operators can see the explicit contract.
    contract = infer_control_contract(capability)
    if contract.mode != "unknown":
        contract_dict: dict[str, object] = {"mode": contract.mode}
        if contract.request_fields:
            contract_dict["request_fields"] = list(contract.request_fields)
        if contract.accepted_efforts:
            contract_dict["accepted_efforts"] = list(contract.accepted_efforts)
        if contract.effort_aliases:
            contract_dict["effort_aliases"] = dict(contract.effort_aliases)
        if contract.effort_to_budget_tokens is not None:
            contract_dict["effort_to_budget_tokens"] = dict(
                contract.effort_to_budget_tokens,
            )
        if contract.explicit_budget_min is not None:
            contract_dict["explicit_budget_min"] = contract.explicit_budget_min
        if contract.explicit_budget_max is not None:
            contract_dict["explicit_budget_max"] = contract.explicit_budget_max
        if contract.historical_reasoning_content != "unknown":
            contract_dict["historical_reasoning_content"] = (
                contract.historical_reasoning_content
            )
        if contract.source != "unknown":
            contract_dict["source"] = contract.source
        result["control_contract"] = contract_dict

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


def _parse_transcoding_capabilities(raw: object) -> TranscodingCapabilities:
    """Parse cached/configured transcoding data conservatively.

    Releases before the dialect contract stored cache targets as a bare
    protocol list. Treat that stale shape as unknown instead of allowing it
    to enable native fields or breaking catalog hydration.
    """
    if not isinstance(raw, dict):
        return TranscodingCapabilities()
    data = dict(cast("Mapping[str, object]", raw))
    if isinstance(data.get("prompt_cache_breakpoints"), list):
        data["prompt_cache_breakpoints"] = {}
    return TranscodingCapabilities.model_validate(data)


def _parse_media_capability(raw: object) -> MediaCapability:
    """Parse a cached media capability dict conservatively."""
    if not isinstance(raw, dict):
        return MediaCapability()
    data = cast("Mapping[str, object]", raw)
    max_src = data.get("max_source_bytes")
    return MediaCapability(
        base64=bool(data.get("base64", False)),
        url=bool(data.get("url", False)),
        max_source_bytes=int(max_src) if isinstance(max_src, int) else None,
    )


def _parse_multimodal_capabilities(raw: object) -> MultimodalCapabilities:
    """Parse cached multimodal capability data conservatively."""
    if not isinstance(raw, dict):
        return MultimodalCapabilities()
    data = cast("Mapping[str, object]", raw)
    max_req = data.get("max_serialized_request_bytes")
    return MultimodalCapabilities(
        image_input=_parse_media_capability(data.get("image_input")),
        document_input=_parse_media_capability(data.get("document_input")),
        audio_input=_parse_media_capability(data.get("audio_input")),
        non_text_tool_result=bool(data.get("non_text_tool_result", False)),
        max_serialized_request_bytes=(
            int(max_req) if isinstance(max_req, int) else None
        ),
    )


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
    supported_efforts = override.get("supported_efforts")
    effort = override.get("effort_to_budget_tokens")
    notes = override.get("notes")

    fields = (
        status,
        source,
        native_protocols,
        budget_min,
        budget_max,
        supported_efforts,
        effort,
        notes,
    )
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
        raw_effort = cast("dict[object, object]", effort)
        parsed_effort: dict[str, int] = {}
        for key, value in raw_effort.items():
            if isinstance(value, int):
                parsed_effort[str(key)] = value
            elif isinstance(value, str):
                try:
                    parsed_effort[str(key)] = int(value)
                except ValueError:
                    continue
        effort_dict = parsed_effort
    supported_effort_list: list[str] = []
    if isinstance(supported_efforts, list):
        raw_supported_efforts = cast("list[object]", supported_efforts)
        supported_effort_list = [str(v) for v in raw_supported_efforts]

    # Parse optional control_contract from override dict.
    contract_raw = override.get("control_contract")
    control_contract = ThinkingControlContract()
    if isinstance(contract_raw, dict):
        cc = cast("dict[str, object]", contract_raw)
        cc_mode = str(cc.get("mode", "unknown"))
        cc_source_raw = cc.get("source")
        cc_source: CapabilitySource = (
            cast("CapabilitySource", str(cc_source_raw))
            if cc_source_raw is not None
            else cap_source
        )
        cc_fields_raw = cast("list[object] | None", cc.get("request_fields"))
        cc_fields = (
            [str(f) for f in cc_fields_raw] if isinstance(cc_fields_raw, list) else []
        )
        cc_efforts_raw = cast("list[object] | None", cc.get("accepted_efforts"))
        cc_efforts = (
            [str(f) for f in cc_efforts_raw] if isinstance(cc_efforts_raw, list) else []
        )
        cc_aliases_raw = cc.get("effort_aliases")
        cc_aliases: dict[str, str] = {}
        if isinstance(cc_aliases_raw, dict):
            raw_aliases = cast("dict[str, object]", cc_aliases_raw)
            cc_aliases = {str(k): str(v) for k, v in raw_aliases.items()}
        cc_eb_raw = cc.get("effort_to_budget_tokens")
        cc_eb: dict[str, int] | None = None
        if isinstance(cc_eb_raw, dict):
            raw_eb = cast("dict[str, object]", cc_eb_raw)
            cc_eb = {str(k): int(v) for k, v in raw_eb.items()}  # type: ignore[arg-type]
        cc_bmin = cc.get("explicit_budget_min")
        cc_bmax = cc.get("explicit_budget_max")
        cc_hist = cc.get("historical_reasoning_content", "unknown")
        control_contract = ThinkingControlContract(
            mode=cast("_ThinkingControlMode", cc_mode),
            request_fields=cc_fields,
            accepted_efforts=cc_efforts,
            effort_aliases=cc_aliases,
            effort_to_budget_tokens=cc_eb,
            explicit_budget_min=(cc_bmin if isinstance(cc_bmin, int) else None),
            explicit_budget_max=(cc_bmax if isinstance(cc_bmax, int) else None),
            historical_reasoning_content=cast(
                "_HistoricalReasoningContent",
                str(cc_hist),
            ),
            source=cc_source,
        )

    return ThinkingCapability(
        status=cap_status,
        source=cap_source,
        native_protocols=native_list,
        budget_tokens_min=budget_min if isinstance(budget_min, int) else None,
        budget_tokens_max=budget_max if isinstance(budget_max, int) else None,
        supported_efforts=supported_effort_list,
        effort_to_budget_tokens=effort_dict,
        control_contract=control_contract,
        notes=str(notes) if notes is not None else None,
    )


def _parse_media_capability_override(raw: object) -> MediaCapability:
    """Parse a media capability override dict into a :class:`MediaCapability`.

    ``None`` or missing fields are left as their conservative defaults
    (``False``).  Only ``True`` values enable a source form.
    """
    if not isinstance(raw, dict):
        return MediaCapability()
    data = cast("Mapping[str, object]", raw)
    max_src = data.get("max_source_bytes")
    return MediaCapability(
        base64=bool(data.get("base64", False)),
        url=bool(data.get("url", False)),
        max_source_bytes=int(max_src) if isinstance(max_src, int) else None,
    )


def _parse_multimodal_capability_override(
    raw: object,
) -> MultimodalCapabilities:
    """Parse a multimodal capability override dict into MultimodalCapabilities."""
    if not isinstance(raw, dict):
        return MultimodalCapabilities()
    data = cast("Mapping[str, object]", raw)
    max_req = data.get("max_serialized_request_bytes")
    return MultimodalCapabilities(
        image_input=_parse_media_capability_override(data.get("image_input")),
        document_input=_parse_media_capability_override(data.get("document_input")),
        audio_input=_parse_media_capability_override(data.get("audio_input")),
        non_text_tool_result=bool(data.get("non_text_tool_result", False)),
        max_serialized_request_bytes=(
            int(max_req) if isinstance(max_req, int) else None
        ),
    )


def model_capabilities_override_to_config(
    override: dict[str, object] | None,
) -> ModelCapabilities:
    """Convert a ``ModelCapabilitiesOverrideConfig`` dict into ModelCapabilities.

    The *override* dict may contain ``thinking``, ``transcoding``, and
    ``multimodal`` keys whose values are dicts compatible with their
    respective parsers.
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

    transcode_raw = override.get("transcoding")
    transcoding = _parse_transcoding_capabilities(transcode_raw)
    multimodal = _parse_multimodal_capability_override(override.get("multimodal"))
    return ModelCapabilities(
        thinking=thinking, transcoding=transcoding, multimodal=multimodal
    )


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
    transcoding = _parse_transcoding_capabilities(data.get("transcoding"))
    multimodal = _parse_multimodal_capabilities(data.get("multimodal"))
    thinking_raw = data.get("thinking")
    if not isinstance(thinking_raw, dict):
        return ModelCapabilities(transcoding=transcoding, multimodal=multimodal)

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
    supported_efforts_raw = tr.get("supported_efforts")
    effort_raw = tr.get("effort_to_budget_tokens")
    notes_raw = tr.get("notes")
    effort_dict: dict[str, int] | None = None
    if isinstance(effort_raw, dict):
        effort_dict = {str(k): int(v) for k, v in effort_raw.items()}  # type: ignore[arg-type]
    supported_efforts: list[str] = []
    supported_efforts_val = cast("list[object] | None", supported_efforts_raw)
    if isinstance(supported_efforts_val, list):
        supported_efforts = [str(v) for v in supported_efforts_val]

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

    # Parse provider-bound control contract.
    contract_raw = tr.get("control_contract")
    control_contract = ThinkingControlContract()
    if isinstance(contract_raw, dict):
        cc_dict = cast("dict[str, object]", contract_raw)
        cc_mode = str(cc_dict.get("mode", "unknown"))
        cc_source_raw = cc_dict.get("source")
        cc_source: CapabilitySource = (
            cast("CapabilitySource", str(cc_source_raw))
            if cc_source_raw is not None
            else "unknown"
        )
        cc_request_fields_raw = cast(
            "list[object] | None",
            cc_dict.get("request_fields"),
        )
        cc_request_fields = (
            [str(f) for f in cc_request_fields_raw]
            if isinstance(cc_request_fields_raw, list)
            else []
        )
        cc_accepted_efforts_raw = cast(
            "list[object] | None",
            cc_dict.get("accepted_efforts"),
        )
        cc_accepted_efforts = (
            [str(f) for f in cc_accepted_efforts_raw]
            if isinstance(cc_accepted_efforts_raw, list)
            else []
        )
        cc_effort_aliases_raw = cc_dict.get("effort_aliases")
        cc_effort_aliases: dict[str, str] = {}
        if isinstance(cc_effort_aliases_raw, dict):
            raw_aliases_2 = cast("dict[str, object]", cc_effort_aliases_raw)
            cc_effort_aliases = {str(k): str(v) for k, v in raw_aliases_2.items()}
        cc_effort_budget_raw = cc_dict.get("effort_to_budget_tokens")
        cc_effort_budget: dict[str, int] | None = None
        if isinstance(cc_effort_budget_raw, dict):
            raw_eb_2 = cast("dict[str, object]", cc_effort_budget_raw)
            cc_effort_budget = {
                str(k): int(v)  # type: ignore[arg-type]
                for k, v in raw_eb_2.items()
            }
        cc_budget_min_raw = cc_dict.get("explicit_budget_min")
        cc_budget_max_raw = cc_dict.get("explicit_budget_max")
        cc_historical_raw = cc_dict.get("historical_reasoning_content", "unknown")
        control_contract = ThinkingControlContract(
            mode=cast("_ThinkingControlMode", cc_mode),
            request_fields=cc_request_fields,
            accepted_efforts=cc_accepted_efforts,
            effort_aliases=cc_effort_aliases,
            effort_to_budget_tokens=cc_effort_budget,
            explicit_budget_min=(
                cc_budget_min_raw if isinstance(cc_budget_min_raw, int) else None
            ),
            explicit_budget_max=(
                cc_budget_max_raw if isinstance(cc_budget_max_raw, int) else None
            ),
            historical_reasoning_content=cast(
                "_HistoricalReasoningContent",
                str(cc_historical_raw),
            ),
            source=cc_source,
        )

    return ModelCapabilities(
        transcoding=transcoding,
        thinking=ThinkingCapability(
            status=cast("CapabilityStatus", tc_status),
            source=cast("CapabilitySource", tc_source),
            native_protocols=native_protos,
            client_controls=client_controls,
            budget_tokens_min=bmin_raw if isinstance(bmin_raw, int) else None,
            budget_tokens_max=bmax_raw if isinstance(bmax_raw, int) else None,
            supported_efforts=supported_efforts,
            effort_to_budget_tokens=effort_dict,
            control_contract=control_contract,
            notes=str(notes_raw) if notes_raw is not None else None,
        ),
        multimodal=multimodal,
    )


def model_capabilities_to_dict(capabilities: ModelCapabilities) -> dict[str, object]:
    """Convert :class:`ModelCapabilities` back to a plain dict for storage.

    The output is suitable for the catalog cache ``capabilities`` field.
    ``None`` / empty values are filtered out. ``supports_tools`` is
    **not** written here — tool support is tracked in the
    catalog/model layer (e.g. via ``supports_tools`` on the per-model
    row) and is independent of the thinking capability.  Emitting it
    inside this dict would conflate unrelated capability metadata.
    """
    result: dict[str, object] = {}
    if capabilities.transcoding != TranscodingCapabilities():
        result["transcoding"] = capabilities.transcoding.model_dump(exclude_none=True)
    if capabilities.multimodal != MultimodalCapabilities():
        mm = capabilities.multimodal
        mm_dict: dict[str, object] = {}

        def _media_to_dict(mc: MediaCapability) -> dict[str, object]:
            d: dict[str, object] = {}
            if mc.base64:
                d["base64"] = True
            if mc.url:
                d["url"] = True
            if mc.max_source_bytes is not None:
                d["max_source_bytes"] = mc.max_source_bytes
            return d

        img = _media_to_dict(mm.image_input)
        if img:
            mm_dict["image_input"] = img
        doc = _media_to_dict(mm.document_input)
        if doc:
            mm_dict["document_input"] = doc
        aud = _media_to_dict(mm.audio_input)
        if aud:
            mm_dict["audio_input"] = aud
        if mm.non_text_tool_result:
            mm_dict["non_text_tool_result"] = True
        if mm.max_serialized_request_bytes is not None:
            mm_dict["max_serialized_request_bytes"] = mm.max_serialized_request_bytes
        if mm_dict:
            result["multimodal"] = mm_dict

    tc = capabilities.thinking

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
    if tc.supported_efforts:
        thinking_dict["supported_efforts"] = list(tc.supported_efforts)
    if tc.effort_to_budget_tokens is not None:
        thinking_dict["effort_to_budget_tokens"] = dict(tc.effort_to_budget_tokens)
    if tc.control_contract.mode != "unknown":
        cc = tc.control_contract
        contract_dict: dict[str, object] = {"mode": cc.mode}
        if cc.request_fields:
            contract_dict["request_fields"] = list(cc.request_fields)
        if cc.accepted_efforts:
            contract_dict["accepted_efforts"] = list(cc.accepted_efforts)
        if cc.effort_aliases:
            contract_dict["effort_aliases"] = dict(cc.effort_aliases)
        if cc.effort_to_budget_tokens is not None:
            contract_dict["effort_to_budget_tokens"] = dict(cc.effort_to_budget_tokens)
        if cc.explicit_budget_min is not None:
            contract_dict["explicit_budget_min"] = cc.explicit_budget_min
        if cc.explicit_budget_max is not None:
            contract_dict["explicit_budget_max"] = cc.explicit_budget_max
        if cc.historical_reasoning_content != "unknown":
            contract_dict["historical_reasoning_content"] = (
                cc.historical_reasoning_content
            )
        if cc.source != "unknown":
            contract_dict["source"] = cc.source
        thinking_dict["control_contract"] = contract_dict
    if tc.notes is not None:
        thinking_dict["notes"] = tc.notes

    if thinking_dict:
        result["thinking"] = thinking_dict

    return result


# ---------------------------------------------------------------------------
# Request-level helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThinkingRequestRequirement:
    """Result of classifying whether a request needs thinking support."""

    required: bool
    client_protocol: str
    fields: list[str]
    requested_effort: str | None = None
    requested_budget_tokens: int | None = None
    reasoning_disabled: bool = False


@dataclass(frozen=True, slots=True)
class ThinkingRequestIntent:
    """Normalized immutable record of the original client's thinking intent.

    Stored in :class:`ProxyRequestContext` so provider adaptation uses
    the original client intent rather than re-reading already-translated
    fields.  This prevents an intermediate fallback budget from becoming
    falsely authoritative.
    """

    requested_effort: str | None = None
    requested_effort_original: str | None = None
    requested_budget_tokens: int | None = None
    request_fields: tuple[str, ...] = ()
    has_historical_reasoning_content: bool = False
    client_requests_new_reasoning: bool = False
    client_protocol: str = ""


_REASONING_DISABLED_EFFORTS: frozenset[str] = frozenset({"none"})


def is_reasoning_disabled_effort(effort: str | None) -> bool:
    """Return whether an effort label explicitly disables reasoning."""
    return effort is not None and effort.strip().lower() in _REASONING_DISABLED_EFFORTS


def classify_thinking_request(
    request_body: dict[str, object],
    client_protocol: str,
) -> ThinkingRequestRequirement:
    """Classify whether a request explicitly requires thinking support.

    Inspects the request body for OpenAI and Anthropic thinking indicators
    and returns a structured result that routing can use to filter
    candidates.  Unlike ``client_requests_thinking``, this function does
    **not** consult the model capability — it only inspects the client's
    intent.
    """
    fields: list[str] = []
    effort: str | None = None
    budget: int | None = None

    # OpenAI indicators
    if "reasoning_effort" in request_body:
        fields.append("reasoning_effort")
        val: object = request_body["reasoning_effort"]
        if isinstance(val, str):
            effort = val

    if "reasoning" in request_body:
        fields.append("reasoning")

    # Anthropic indicators
    if "thinking" in request_body:
        fields.append("thinking")
        thinking_val: object = request_body["thinking"]
        if isinstance(thinking_val, dict):
            thinking_dict = cast("dict[str, object]", thinking_val)
            budget_val: object = thinking_dict.get("budget_tokens")
            if isinstance(budget_val, int) and not isinstance(budget_val, bool):
                budget = int(budget_val)

    if "thinking_budget" in request_body:
        fields.append("thinking_budget")
        tb_val: object = request_body["thinking_budget"]
        if isinstance(tb_val, int) and not isinstance(tb_val, bool):
            budget = int(tb_val)

    # Assistant history indicators (reasoning_content must be preserved)
    messages: object = request_body.get("messages")
    if isinstance(messages, list):
        msg_list = cast("list[object]", messages)
        for msg in msg_list:
            if not isinstance(msg, dict):
                continue
            msg_dict = cast("dict[str, object]", msg)
            role: object = msg_dict.get("role")
            if role == "assistant":
                content: object = msg_dict.get("content")
                if isinstance(content, list):
                    block_list = cast("list[object]", content)
                    for block in block_list:
                        if isinstance(block, dict):
                            block_dict = cast("dict[str, object]", block)
                            block_type: object = block_dict.get("type")
                            if (
                                block_type == "reasoning_content"
                                and "reasoning_content" not in fields
                            ):
                                fields.append("reasoning_content")
                elif isinstance(content, str) and "reasoning_content" in fields:
                    pass  # already flagged
                # Phase E: top-level ``reasoning_content`` on assistant
                # messages is the common OpenAI-compatible shape. Only
                # treat it as a thinking signal when the value is a
                # non-empty string or list — empty/null values mean the
                # upstream model did not actually reason.
                top_rc_obj: object = msg_dict.get("reasoning_content")
                if (
                    isinstance(top_rc_obj, str)
                    and top_rc_obj.strip()
                    and "reasoning_content" not in fields
                ) or (
                    isinstance(top_rc_obj, list)
                    and top_rc_obj
                    and "reasoning_content" not in fields
                ):
                    fields.append("reasoning_content")

    return ThinkingRequestRequirement(
        required=bool(fields)
        and not (
            is_reasoning_disabled_effort(effort)
            and not any(
                field in {"reasoning", "thinking", "thinking_budget"}
                for field in fields
            )
        ),
        client_protocol=client_protocol,
        fields=fields,
        requested_effort=effort,
        requested_budget_tokens=budget,
        reasoning_disabled=is_reasoning_disabled_effort(effort),
    )


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


# Canonical warning `kind` values emitted by the transcoder and budget
# resolver when reasoning/thinking controls are handled.  Distinct from
# the broader ``dropped_field`` family, which carries unrelated loss
# signals (e.g. ``tools[].function.strict``).
THINKING_WARNING_KINDS: frozenset[str] = frozenset(
    {
        "thinking_signature_dropped",
        "reasoning_content_dropped",
        "budget_clamped",
        "unknown_effort",
        "budget_rejected",
        "budget_resolution_no_input",
        "anthropic_top_level_thinking_dropped",
    }
)

# Field names whose presence marks a generic ``dropped_field`` warning
# as thinking-related.  Substring match is intentional — paths like
# ``messages[].content[].thinking`` should also be picked up.
THINKING_FIELD_SUBSTRINGS: tuple[str, ...] = (
    "thinking",
    "reasoning_effort",
    "reasoning",
    "reasoning_content",
    "thinking_budget",
    "thinking_delta",
)


def is_thinking_warning(warning: object) -> bool:
    """Return ``True`` if *warning* pertains to a thinking control.

    Classifies by ``kind`` first (preferred — robust across all
    thinking-related subsystems including the budget resolver, which
    emits warnings without a ``field`` key) and falls back to a
    ``dropped_field`` heuristic for warnings whose field names contain
    known reasoning/thinking substrings.
    """
    if not isinstance(warning, Mapping):
        return False
    mapping: Mapping[str, object] = warning  # pyright: ignore[reportUnknownVariableType]
    kind_obj: object = mapping.get("kind")  # pyright: ignore[reportUnknownMemberType]
    if isinstance(kind_obj, str) and kind_obj in THINKING_WARNING_KINDS:
        return True
    if kind_obj == "dropped_field":
        field_obj: object = mapping.get("field")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(field_obj, str):
            lowered = field_obj.lower()
            return any(sub in lowered for sub in THINKING_FIELD_SUBSTRINGS)
    return False


def classify_thinking_warning_decision(warnings: Iterable[object]) -> str:
    """Classify a transcoding trace into a thinking decision label.

    Returns one of:
    - ``"rejected"``: budget policy strict rejection (``budget_rejected``)
    - ``"clamped"``: ``budget_clamped`` warning present
    - ``"dropped"``: ``reasoning_content_dropped`` / ``thinking_signature_dropped``
    - ``"transcoded"``: any other thinking-related warning
    - ``"passthrough"``: no thinking-related warnings
    """
    thinking_warnings = [w for w in warnings if is_thinking_warning(w)]
    if not thinking_warnings:
        return "passthrough"
    if any(_warning_kind(w) == "budget_rejected" for w in thinking_warnings):
        return "rejected"
    if any(_warning_kind(w) == "budget_clamped" for w in thinking_warnings):
        return "clamped"
    if any(
        _warning_kind(w)
        in (
            "reasoning_content_dropped",
            "thinking_signature_dropped",
            "anthropic_top_level_thinking_dropped",
        )
        for w in thinking_warnings
    ):
        return "dropped"
    return "transcoded"


def _warning_kind(warning: object) -> str | None:
    """Extract the ``kind`` field from a warning mapping, defensively."""
    if isinstance(warning, Mapping):
        kind_obj: object = warning.get("kind")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(kind_obj, str):
            return kind_obj
    return None


# ---------------------------------------------------------------------------
# Capability-aware routing eligibility
# ---------------------------------------------------------------------------

# Policy action type aliases (mirrors the Literal types in CapabilityPolicy)
RejectPolicy = Literal["reject"]
WarnDropPolicy = Literal["warn_drop"]
AllowWithWarningPolicy = Literal["allow_with_warning"]
RouteBestEffortPolicy = Literal["route_best_effort"]
FilterPolicy = Literal["filter"]


def extract_thinking_status_from_entry(
    entry: Mapping[str, object] | None,
) -> CapabilityStatus:
    """Best-effort extraction of thinking capability status from a catalog entry.

    Returns ``"unknown"`` when the entry is ``None``, the ``capabilities``
    block is missing or not a dict, or the ``thinking`` sub-block is absent.
    This is the canonical "fail-open to unknown" helper so every routing
    decision treats missing capability metadata as semantically equivalent to
    an explicit ``ThinkingCapability(status="unknown")``, allowing the
    configured ``[transcoder.capability_policy].unknown_thinking`` policy to
    apply consistently.
    """
    if entry is None:
        return "unknown"
    caps_raw_obj: object = entry.get("capabilities")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(caps_raw_obj, dict):
        return "unknown"
    caps_raw: dict[str, object] = caps_raw_obj  # pyright: ignore[reportUnknownVariableType]
    thinking_raw_obj: object = caps_raw.get("thinking")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(thinking_raw_obj, dict):
        return "unknown"
    thinking_raw: dict[str, object] = thinking_raw_obj  # pyright: ignore[reportUnknownVariableType]
    caps = dict_to_model_capabilities({"thinking": thinking_raw})
    return caps.thinking.status


def _normalize_effort_label(value: str) -> str:
    """Normalize common effort aliases for capability comparisons."""
    lowered = value.strip().lower()
    if lowered == "med":
        return "medium"
    return lowered


def candidate_supports_requested_effort(
    entry: Mapping[str, object] | None,
    requested_effort: str | None,
) -> bool:
    """Return whether a catalog entry supports the requested effort level.

    An empty ``supported_efforts`` list means the provider did not expose
    effort-level metadata, so the caller falls back to status-only routing.
    """
    if requested_effort is None:
        return True
    if entry is None:
        return True
    caps_raw_obj: object = entry.get("capabilities")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(caps_raw_obj, dict):
        return True
    caps_raw: dict[str, object] = caps_raw_obj  # pyright: ignore[reportUnknownVariableType]
    thinking_raw_obj: object = caps_raw.get("thinking")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(thinking_raw_obj, dict):
        return True
    thinking_raw: dict[str, object] = thinking_raw_obj  # pyright: ignore[reportUnknownVariableType]
    caps = dict_to_model_capabilities({"thinking": thinking_raw})
    supported = caps.thinking.supported_efforts
    if not supported:
        return True
    requested = _normalize_effort_label(requested_effort)
    supported_normalized = {_normalize_effort_label(value) for value in supported}
    return requested in supported_normalized


def check_candidate_thinking_eligibility(
    capability_status: CapabilityStatus,
    *,
    unsupported_action: str = "reject",
    unknown_action: str = "reject",
    mixed_action: str = "filter",
) -> bool:
    """Determine whether a candidate is eligible for a thinking request.

    Parameters:
        capability_status: the model/provider's thinking capability status.
        unsupported_action: policy for ``"unsupported"`` status.
        unknown_action: policy for ``"unknown"`` status.
        mixed_action: policy for ``"mixed"`` status.

    Returns ``True`` when the candidate should be considered for routing.

    ``"supported"`` candidates are always eligible.
    ``"conflicting"`` candidates are always rejected.  An operator can
    resolve a conflict by setting a manual override
    (``[model_capabilities."<model>".thinking]``) that sets ``status``
    to a non-conflicting value; the override is merged before this
    check runs, so the merged status will already reflect the resolution.
    """
    if capability_status == "supported":
        return True
    if capability_status == "conflicting":
        return False
    if capability_status == "unsupported":
        return unsupported_action != "reject"
    if capability_status == "unknown":
        return unknown_action != "reject"
    if capability_status == "mixed":
        # "mixed" in per-provider context means this specific provider
        # supports it; only reject if policy says "reject".
        return mixed_action != "reject"
    return False
