"""Helpers for shaping transcoding statistics for public surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast


def serialize_transcoding_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe transcoding stats payload.

    ``fetch_transcoding_stats`` uses tuple keys for ``per_direction`` because
    that is convenient for Python renderers. JSON object keys must be strings,
    so API and CLI JSON output pass through this helper before serializing.
    """
    payload = dict(stats)
    payload["per_direction"] = {
        _direction_key_to_label(key): _coerce_count(count)
        for key, count in _iter_direction_items(stats.get("per_direction")).items()
    }
    return payload


def _iter_direction_items(value: object) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast("Mapping[object, object]", value)


def _direction_key_to_label(key: object) -> str:
    if isinstance(key, tuple):
        tuple_parts = cast("tuple[object, ...]", key)
        if len(tuple_parts) == 2:
            return f"{tuple_parts[0]}\u2192{tuple_parts[1]}"
    if isinstance(key, list):
        list_parts = cast("list[object]", key)
        if len(list_parts) == 2:
            return f"{list_parts[0]}\u2192{list_parts[1]}"
    return str(cast("object", key))


def _coerce_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
