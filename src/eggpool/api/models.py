"""Serialization helpers for /v1/models responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eggpool.catalog.capabilities import (
    dict_to_model_capabilities,
    serialize_model_capabilities,
)
from eggpool.model_info.presentation import (
    MODEL_INFO_STATUS_DISPLAY as _MODEL_INFO_STATUS_DISPLAY,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

MODEL_INFO_STATUS_DISPLAY = _MODEL_INFO_STATUS_DISPLAY


def serialize_openai_model(
    model: Mapping[str, Any],
    *,
    routing_priority: int | None = None,
    routing_priority_max: int | None = None,
    providers: list[str] | None = None,
    model_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize a catalog model entry to OpenAI-compatible model dict.

    Includes the namespaced ``eggpool`` extension with base model ID,
    provider ID, routing priority (when supplied), effective limits
    when available, and optional compact model-info summary.

    For collapsed entries (no per-provider ``provider_id``), pass the
    ``routing_priority_max`` (highest priority across contributing
    providers) and ``providers`` list so clients can see the routing
    topology.

    When ``model_info`` is supplied a compact subset is nested under
    ``eggpool["model_info"]`` — status, sparse flag, summary text,
    sources list, and last_refreshed_at.  Raw observations, full
    benchmarks, provenance maps, and conflict details are never
    included.
    """
    result: dict[str, Any] = {
        "id": model["model_id"],
        "object": "model",
        "created": int(model.get("first_seen_at", 0)),
        "owned_by": model.get("provider_id", "opencode"),
        "name": model.get("display_name") or model["model_id"],
    }

    # Add namespaced EggPool metadata
    eggpool_meta: dict[str, Any] = {}
    base_model_id = model.get("base_model_id")
    provider_id = model.get("provider_id")
    if base_model_id is not None:
        eggpool_meta["base_model_id"] = base_model_id
    if provider_id is not None:
        eggpool_meta["provider_id"] = provider_id
    if routing_priority is not None:
        eggpool_meta["routing_priority"] = routing_priority

    # Collapsed-entry metadata: contributing providers and the highest
    # routing priority across them. Both are omitted when the entry is
    # already provider-scoped (the singular `routing_priority` above
    # covers that case).
    if provider_id is None:
        if providers is not None:
            eggpool_meta["providers"] = list(providers)
        if routing_priority_max is not None:
            eggpool_meta["routing_priority_max"] = routing_priority_max

    effective = model.get("effective_limits", {})
    if effective:
        limits: dict[str, Any] = {}
        ctx = effective.get("context_tokens")
        inp = effective.get("input_tokens")
        out = effective.get("output_tokens")
        if ctx is not None:
            limits["context"] = ctx
        if inp is not None:
            limits["input"] = inp
        if out is not None:
            limits["output"] = out
        if limits:
            eggpool_meta["limits"] = limits

    if model_info is not None:
        eggpool_meta["model_info"] = {
            "status": model_info.get("status"),
            "sparse": model_info.get("sparse"),
            "summary": model_info.get("summary"),
            "sources": model_info.get("sources", []),
            "last_refreshed_at": model_info.get("last_refreshed_at"),
        }

    raw_caps = model.get("capabilities", {})
    if raw_caps:
        caps = dict_to_model_capabilities(raw_caps)
        provider_statuses = model.get("_provider_thinking_statuses")
        caps_dict = serialize_model_capabilities(
            caps,
            provider_statuses=provider_statuses,
        )
        if caps_dict:
            eggpool_meta["capabilities"] = caps_dict

    if eggpool_meta:
        result["eggpool"] = eggpool_meta

    return result
