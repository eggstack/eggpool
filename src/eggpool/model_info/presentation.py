"""Presentation helpers for model-info API and dashboard surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

MODEL_INFO_STATUS_DISPLAY: dict[str, str] = {
    "fresh": "fresh",
    "partial": "partial",
    "sparse_new": "sparse",
    "stale": "stale",
    "conflicting": "conflict",
    "unmatched": "unmatched",
    "source_unavailable": "source-unavailable",
    "manual_override": "manual",
    "withdrawn": "withdrawn",
}

MODEL_INFO_STATUS_ALIASES: dict[str, str] = {
    **{value: key for key, value in MODEL_INFO_STATUS_DISPLAY.items()},
    **{key: key for key in MODEL_INFO_STATUS_DISPLAY},
}


def display_model_info_status(value: object) -> str:
    """Return the public display label for a model-info status."""
    status = str(value) if value is not None else ""
    return MODEL_INFO_STATUS_DISPLAY.get(status, status)


def normalize_model_info_status_filter(value: str) -> str:
    """Normalize a display or canonical model-info status to canonical form."""
    return MODEL_INFO_STATUS_ALIASES.get(value, value)


def iso_datetime(value: object) -> str | None:
    """Format a datetime-like value for JSON responses."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def compact_model_info_summary(
    info: Any,
    *,
    display_status: bool = True,
) -> dict[str, Any]:
    """Build the compact, raw-payload-free model-info summary shape."""
    sources: list[str] = []
    prov_raw = cast("dict[str, Any]", getattr(info, "provenance", {}))
    raw_sources = cast("list[object]", prov_raw.get("sources", []))
    for source in raw_sources:
        sources.append(str(source))

    detail_raw = cast("dict[str, Any]", getattr(info, "detail", {}))
    providers: list[str] = []
    raw_providers = cast("list[object]", detail_raw.get("providers", []))
    for provider in raw_providers:
        providers.append(str(provider))

    status_raw = getattr(info, "status", "")
    status = str(status_raw) if status_raw is not None else ""

    return {
        "model_id": getattr(info, "model_id", ""),
        "status": display_model_info_status(status) if display_status else status,
        "sparse": getattr(info, "sparse", False),
        "summary": getattr(info, "summary", "") or "",
        "sources": sources,
        "providers": providers,
        "last_seen_at": iso_datetime(getattr(info, "last_seen_at", None)),
        "last_refreshed_at": iso_datetime(getattr(info, "last_refreshed_at", None)),
        "next_refresh_at": iso_datetime(getattr(info, "next_refresh_at", None)),
        "has_conflicts": bool(getattr(info, "conflicts", {})),
    }
