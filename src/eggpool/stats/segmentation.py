"""Helpers for shaping canonical request segmentation statistics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping


def serialize_canonical_request_segmentation(
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a JSON-safe canonical segmentation payload.

    ``fetch_canonical_request_segmentation`` uses tuple keys for
    ``per_provider_status`` so Python renderers can keep provider and
    upstream-protocol pairs grouped together. JSON object keys must be
    strings, so API and dashboard JSON output pass through this helper
    before serializing.
    """
    payload = dict(stats)
    provider_status = cast(
        "Mapping[tuple[str, str], Mapping[str, Any]]",
        payload.get("per_provider_status") or {},
    )
    payload["per_provider_status"] = {
        _provider_protocol_key_to_label(key): value
        for key, value in provider_status.items()
    }
    return payload


def _provider_protocol_key_to_label(key: tuple[str, str]) -> str:
    return f"{key[0]}->{key[1]}"
