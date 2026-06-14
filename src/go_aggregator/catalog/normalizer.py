"""Normalize upstream model responses to our domain model."""

from __future__ import annotations

from typing import Any


def normalize_openai_models(
    raw_response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize an OpenAI-compatible /models response.

    Returns a list of normalized model dicts ready for persistence.
    """
    models = []
    for item in raw_response.get("data", []):
        model_id = item.get("id", "")
        if not model_id:
            continue

        capabilities: dict[str, object] = {}
        if "context_window" in item:
            capabilities["context_window"] = item["context_window"]
        if "modalities" in item:
            capabilities["modalities"] = item["modalities"]

        source_exclude = ("id", "name", "title", "object")
        models.append(
            {
                "model_id": model_id,
                "display_name": item.get("name") or item.get("title"),
                "protocol": "openai",
                "capabilities": capabilities,
                "source_metadata": {
                    k: v for k, v in item.items() if k not in source_exclude
                },
            }
        )

    return models


def normalize_anthropic_models(
    raw_response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize an Anthropic-compatible /models response.

    Returns a list of normalized model dicts ready for persistence.
    """
    models = []
    for item in raw_response.get("data", []):
        model_id = item.get("id", "")
        if not model_id:
            continue

        capabilities: dict[str, object] = {}
        if "context_window" in item:
            capabilities["context_window"] = item["context_window"]
        if "max_output_tokens" in item:
            capabilities["max_output_tokens"] = item["max_output_tokens"]

        source_exclude = ("id", "display_name", "type")
        models.append(
            {
                "model_id": model_id,
                "display_name": item.get("display_name"),
                "protocol": "anthropic",
                "capabilities": capabilities,
                "source_metadata": {
                    k: v for k, v in item.items() if k not in source_exclude
                },
            }
        )

    return models


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

    # Auto-detect: Anthropic responses have "type": "list"
    if raw_response.get("type") == "list":
        return normalize_anthropic_models(raw_response)

    # Default to OpenAI format
    return normalize_openai_models(raw_response)
