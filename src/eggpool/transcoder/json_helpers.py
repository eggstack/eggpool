"""Shared helpers for loosely typed protocol JSON payloads."""

from __future__ import annotations

import base64
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


def extract_tool_blocks(blocks: Any) -> list[dict[str, Any]]:
    """Return ``tool_use`` / ``tool_result`` blocks from a content array.

    Walks the iter_objects helper and returns every dict whose ``type``
    field is one of the tool-related shapes.  The caller is responsible
    for any further filtering (e.g. dropping image parts inside a
    ``tool_result`` is left to phase 6.2).
    """
    result: list[dict[str, Any]] = []
    for block in iter_objects(blocks):
        block_type = block.get("type")
        if block_type in ("tool_use", "tool_result"):
            result.append(block)
    return result


def has_non_text_blocks(blocks: Any) -> bool:
    """True when a block list contains at least one non-text object."""
    return any(block.get("type") != "text" for block in iter_objects(blocks))


def token_count_from(mapping: JsonObject | None, field: str) -> int:
    """Read a provider token count defensively from a JSON object."""
    if mapping is None:
        return 0
    return coerce_token_count(mapping.get(field, 0))


def decode_base64_payload(encoded: str) -> bytes | None:
    """Strictly decode a base64 payload, returning ``None`` for invalid data."""
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None


def split_base64_data_uri(data_uri: str) -> tuple[str, str] | None:
    """Return ``(media_type, encoded_payload)`` for a base64 data URI."""
    if ";base64," not in data_uri:
        return None
    media_prefix, encoded = data_uri.split(";base64,", 1)
    if not media_prefix.startswith("data:"):
        return None
    media_type = media_prefix[5:]
    if not media_type:
        return None
    return media_type, encoded
