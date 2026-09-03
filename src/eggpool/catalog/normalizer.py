"""Normalize upstream model responses to our domain model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from eggpool.catalog.capabilities import (
    CapabilitySource,
    ModelCapabilities,
    ThinkingCapability,
    ThinkingClientControls,
    ThinkingControlContract,
    dict_to_model_capabilities,
    merge_model_capabilities,
    model_capabilities_to_dict,
    parse_reasoning_options,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_OPENAI_SOURCE_EXCLUDE = frozenset({"id", "name", "title", "object"})
_ANTHROPIC_SOURCE_EXCLUDE = frozenset({"id", "display_name", "type"})
_OPENAI_THINKING_FIELDS = ("reasoning_effort", "reasoning")
_ANTHROPIC_THINKING_FIELDS = ("thinking", "effort")


def iter_model_items(raw_response: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield valid model objects while isolating malformed upstream rows."""
    data_value: object = raw_response.get("data")
    if not isinstance(data_value, list):
        return
    for item_value in cast("list[object]", data_value):
        if not isinstance(item_value, dict):
            continue
        item = cast("dict[str, Any]", item_value)
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        yield item


def _normalize_item(
    item: dict[str, Any],
    *,
    display_name_keys: tuple[str, ...],
    capability_keys: tuple[str, ...],
    source_exclude: frozenset[str],
    protocol: str | None,
) -> dict[str, Any]:
    """Build the shared normalized representation for one model."""
    display_name = next(
        (
            value
            for key in display_name_keys
            if isinstance((value := item.get(key)), str) and value
        ),
        None,
    )
    capabilities = {key: item[key] for key in capability_keys if key in item}
    capabilities.update(extract_capabilities_from_metadata(item, protocol=protocol))
    return {
        "model_id": item["id"],
        "display_name": display_name,
        "protocol": protocol,
        "capabilities": capabilities,
        "source_metadata": {
            key: value for key, value in item.items() if key not in source_exclude
        },
    }


def _extract_supported_parameters(value: object) -> list[str]:
    """Return lower-cased supported parameter names from an upstream row."""
    if not isinstance(value, list):
        return []
    params: list[str] = []
    for raw in cast("list[object]", value):
        if isinstance(raw, str):
            params.append(raw.lower())
    return params


def _native_protocols_for_metadata(protocol: str | None) -> list[str]:
    """Best-effort native protocol list for discovered thinking controls."""
    if protocol in {"openai", "anthropic"}:
        return [protocol]
    return []


def _client_controls_for_metadata(
    *,
    protocol: str | None,
    params: list[str],
) -> dict[str, ThinkingClientControls]:
    """Return protocol-neutral client control hints for known model-list shapes."""
    controls: dict[str, ThinkingClientControls] = {}
    if protocol == "anthropic" or any(p in params for p in _ANTHROPIC_THINKING_FIELDS):
        controls["anthropic"] = ThinkingClientControls(
            request_fields=["thinking"],
            response_block_types=["thinking"],
            stream_delta_fields=["thinking_delta"],
        )
    if protocol == "openai" or any(p in params for p in _OPENAI_THINKING_FIELDS):
        controls["openai"] = ThinkingClientControls(
            request_fields=["reasoning_effort", "reasoning"],
            response_fields=["reasoning_content"],
            stream_delta_fields=["reasoning"],
        )
    return controls


def _thinking_capability_from_metadata(
    item: dict[str, Any],
    *,
    protocol: str | None,
    source: CapabilitySource = "provider_catalog",
) -> ThinkingCapability:
    """Extract structured thinking metadata from a model-list row.

    ``source`` identifies who published the row; parsing is shared by live
    provider catalogs and verified external model-info enrichment.
    """
    raw_caps = item.get("capabilities")
    base = ModelCapabilities()
    if isinstance(raw_caps, dict):
        caps_dict = cast("dict[str, object]", raw_caps)
        thinking = caps_dict.get("thinking")
        if isinstance(thinking, dict):
            base = dict_to_model_capabilities({"thinking": thinking})

    reasoning = item.get("reasoning")
    params = _extract_supported_parameters(item.get("supported_parameters"))
    options = parse_reasoning_options(
        item.get("reasoning_options"),
        complete="reasoning_options" in item,
    )

    status: str | None = None
    if isinstance(reasoning, bool):
        status = "supported" if reasoning else "unsupported"
    elif (
        options.toggle == "supported"
        or options.effort == "supported"
        or options.budget == "supported"
        or any(("reasoning" in param or "thinking" in param) for param in params)
    ):
        status = "supported"

    if status is None:
        return base.thinking

    parameter_effort = any(param in {"reasoning_effort", "effort"} for param in params)
    contract = ThinkingControlContract(
        toggle=options.toggle,
        effort=(
            "supported"
            if options.effort == "supported" or parameter_effort
            else options.effort
        ),
        budget=options.budget,
        accepted_efforts=options.accepted_efforts,
        effort_aliases=options.effort_aliases,
        explicit_budget_min=options.explicit_budget_min,
        explicit_budget_max=options.explicit_budget_max,
        source=source,
    )
    if status == "unsupported":
        contract = ThinkingControlContract(
            toggle="unsupported",
            effort="unsupported",
            budget="unsupported",
            source=source,
        )
    override = ModelCapabilities(
        thinking=ThinkingCapability(
            status=cast("Any", status),
            source=source,
            native_protocols=_native_protocols_for_metadata(protocol),
            client_controls=_client_controls_for_metadata(
                protocol=protocol,
                params=params,
            ),
            supported_efforts=list(options.accepted_efforts),
            effort_to_budget_tokens=None,
            budget_tokens_min=options.explicit_budget_min,
            budget_tokens_max=options.explicit_budget_max,
            control_contract=contract,
            notes=(
                "Upstream model metadata reports reasoning/thinking controls."
                if status == "supported"
                else "Upstream model metadata reports no reasoning support."
            ),
        )
    )
    return merge_model_capabilities(base, override).thinking


def extract_capabilities_from_metadata(
    item: dict[str, Any],
    *,
    protocol: str | None = None,
    source: CapabilitySource = "provider_catalog",
) -> dict[str, object]:
    """Extract EggPool capability metadata from a model metadata row."""
    capability = _thinking_capability_from_metadata(
        item,
        protocol=protocol,
        source=source,
    )
    return model_capabilities_to_dict(ModelCapabilities(thinking=capability))


def normalize_openai_models(
    raw_response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize an OpenAI-compatible /models response.

    Returns a list of normalized model dicts ready for persistence.
    """
    models: list[dict[str, Any]] = []
    for item in iter_model_items(raw_response):
        models.append(
            _normalize_item(
                item,
                display_name_keys=("name", "title"),
                capability_keys=("context_window", "modalities"),
                source_exclude=_OPENAI_SOURCE_EXCLUDE,
                protocol=None,
            )
        )

    return models


def normalize_anthropic_models(
    raw_response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize an Anthropic-compatible /models response.

    Returns a list of normalized model dicts ready for persistence.
    """
    models: list[dict[str, Any]] = []
    for item in iter_model_items(raw_response):
        models.append(
            _normalize_item(
                item,
                display_name_keys=("display_name",),
                capability_keys=("context_window", "max_output_tokens"),
                source_exclude=_ANTHROPIC_SOURCE_EXCLUDE,
                protocol="anthropic",
            )
        )

    return models


_ANTHROPIC_PAGINATION_KEYS = frozenset({"first_id", "has_more", "last_id"})


def _is_anthropic_shaped(raw_response: dict[str, Any]) -> bool:
    """Heuristically detect Anthropic-compatible model list responses.

    Anthropic's native ``/v1/models`` returns ``{"type": "list", ...}``
    but some compatible providers (e.g. MiniMax) may omit the ``type``
    marker.  We fall back to additional structural signals so that the
    response is still normalised with ``protocol = "anthropic"``.
    """
    if raw_response.get("type") == "list":
        return True

    if _ANTHROPIC_PAGINATION_KEYS & raw_response.keys():
        return True

    data_value = raw_response.get("data")
    if isinstance(data_value, list) and data_value:
        items = cast("list[dict[str, Any]]", data_value)
        first_item = items[0]
        if "display_name" in first_item and "object" not in first_item:
            return True

    return False


def normalize_models(
    raw_response: dict[str, Any],
    protocol: str | None = None,
) -> list[dict[str, Any]]:
    """Auto-detect protocol and normalize model list.

    If protocol is not specified, attempts to detect from response shape.
    """
    if protocol == "anthropic":
        return normalize_anthropic_models(raw_response)
    if protocol == "openai":
        return normalize_openai_models(raw_response)

    if _is_anthropic_shaped(raw_response):
        return normalize_anthropic_models(raw_response)

    # Default to OpenAI format
    return normalize_openai_models(raw_response)
