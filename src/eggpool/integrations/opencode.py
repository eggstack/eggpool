"""OpenCode configuration generation from EggPool catalog data."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Sequence


def _extract_thinking_metadata(
    caps: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Extract OpenCode-compatible thinking metadata.

    Returns ``(supported, efforts)``. Unsupported, unknown, mixed, and
    conflicting statuses are omitted so generated configs never claim
    support without confirmed upstream backing.
    """
    thinking = caps.get("thinking")
    if not isinstance(thinking, dict):
        return False, []
    thinking_dict = cast("dict[str, object]", thinking)
    status = thinking_dict.get("status")
    if not isinstance(status, str):
        return False, []
    if status != "supported":
        return False, []
    efforts_raw = thinking_dict.get("supported_efforts")
    efforts: list[str] = []
    if isinstance(efforts_raw, list):
        for item in cast("list[object]", efforts_raw):
            if isinstance(item, str) and item not in efforts:
                efforts.append(item)
    return True, efforts


def build_opencode_provider_config(
    *,
    base_url: str,
    api_key: str,
    models: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Build an OpenCode provider configuration dict.

    Parameters
    ----------
    base_url:
        The EggPool server base URL (e.g. ``http://host:port/v1``).
    api_key:
        The EggPool API key.
    models:
        Sequence of model dicts from the catalog cache. Each must have
        at least ``model_id`` and may have ``effective_limits``,
        ``display_name``, ``base_model_id``, ``provider_id``, and
        ``capabilities``.  When ``provider_id`` is set and
        ``display_name`` differs from ``model_id``, the rendered
        ``name`` is suffixed with ``/provider_id`` so OpenCode's model
        picker disambiguates providers serving the same upstream model.

        Models whose ``capabilities.thinking.status`` is ``"supported"``
        receive OpenCode-compatible ``reasoning`` metadata. When
        ``supported_efforts`` is known, per-effort variants are generated
        using the OpenAI-compatible ``reasoningEffort`` option.
    """
    model_map: dict[str, Any] = {}
    for m in models:
        model_id = m["model_id"]
        entry: dict[str, Any] = {}
        display = m.get("display_name") or model_id
        if not display:
            continue
        provider_id = m.get("provider_id")
        if provider_id:
            if display.endswith(f"/{provider_id}"):
                if display != model_id:
                    entry["name"] = display
            else:
                entry["name"] = f"{display}/{provider_id}"
        elif display != model_id:
            entry["name"] = display

        effective = m.get("effective_limits", {})
        limit: dict[str, int] = {}
        ctx = effective.get("context_tokens") if effective else None
        inp = effective.get("input_tokens") if effective else None
        out = effective.get("output_tokens") if effective else None
        if ctx is not None and ctx > 0:
            limit["context"] = ctx
        if inp is not None and inp > 0:
            limit["input"] = inp
        if out is not None and out > 0:
            limit["output"] = out
        if limit:
            entry["limit"] = limit

        caps = m.get("capabilities", {})
        thinking_supported, thinking_efforts = _extract_thinking_metadata(caps)
        if thinking_supported:
            entry["reasoning"] = True
        if thinking_efforts:
            entry["variants"] = {
                effort: {"reasoningEffort": effort} for effort in thinking_efforts
            }

        if entry:
            model_map[model_id] = entry
        else:
            model_map[model_id] = {}

    # Sort model IDs for deterministic output
    sorted_models = dict(sorted(model_map.items()))

    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "eggpool": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "EggPool",
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key,
                },
                "models": sorted_models,
            }
        },
    }


def build_opencode_config_json(
    *,
    base_url: str,
    api_key: str,
    models: Sequence[dict[str, Any]],
    indent: int = 2,
) -> str:
    """Build and serialize OpenCode configuration as JSON string."""
    config = build_opencode_provider_config(
        base_url=base_url,
        api_key=api_key,
        models=models,
    )
    return json.dumps(config, indent=indent, ensure_ascii=False)
