"""Shared helpers for loosely typed protocol JSON payloads."""

from __future__ import annotations

from typing import Any, cast

from eggpool.catalog.pricing import coerce_token_count

JsonObject = dict[str, Any]


def as_object(value: Any) -> JsonObject | None:
    """Return ``value`` as a JSON object, or ``None`` for any other shape."""
    if isinstance(value, dict):
        return cast("JsonObject", value)
    return None


def iter_objects(value: Any) -> tuple[JsonObject, ...]:
    """Yield JSON-object items from a list-like JSON value."""
    if not isinstance(value, list):
        return ()
    items = cast("list[object]", value)
    return tuple(cast("JsonObject", item) for item in items if isinstance(item, dict))


def extract_text_blocks(blocks: Any) -> list[str]:
    """Extract text from ``[{type: text, text: ...}, ...]`` content blocks."""
    result: list[str] = []
    for block in iter_objects(blocks):
        if block.get("type") == "text":
            result.append(str(block.get("text", "")))
    return result


def has_non_text_blocks(blocks: Any) -> bool:
    """True when a block list contains at least one non-text object."""
    return any(block.get("type") != "text" for block in iter_objects(blocks))


def token_count_from(mapping: JsonObject | None, field: str) -> int:
    """Read a provider token count defensively from a JSON object."""
    if mapping is None:
        return 0
    return coerce_token_count(mapping.get(field, 0))
