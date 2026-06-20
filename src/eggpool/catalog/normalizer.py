"""Normalize upstream model responses to our domain model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

_OPENAI_SOURCE_EXCLUDE = frozenset({"id", "name", "title", "object"})
_ANTHROPIC_SOURCE_EXCLUDE = frozenset({"id", "display_name", "type"})


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
    return {
        "model_id": item["id"],
        "display_name": display_name,
        "protocol": protocol,
        "capabilities": {key: item[key] for key in capability_keys if key in item},
        "source_metadata": {
            key: value for key, value in item.items() if key not in source_exclude
        },
    }


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
