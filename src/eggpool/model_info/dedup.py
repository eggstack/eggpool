"""Deduplication helpers for canonical row writes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.model_info.types import CanonicalModelInfo


def canonical_needs_update(
    existing: CanonicalModelInfo | None,
    new: CanonicalModelInfo,
) -> bool:
    """Check if a canonical row actually needs updating.

    Compares status, summary, sparse flag, detail, provenance, conflicts,
    and next_refresh_at.  Returns True when the existing row is None or
    differs from the proposed payload, False when a write would be a no-op.
    """
    if existing is None:
        return True

    if existing.status != new.status:
        return True
    if existing.summary != new.summary:
        return True
    if existing.sparse != new.sparse:
        return True
    if existing.next_refresh_at != new.next_refresh_at:
        return True

    if _json_dumps(existing.detail) != _json_dumps(new.detail):
        return True
    if _json_dumps(existing.provenance) != _json_dumps(new.provenance):
        return True
    return _json_dumps(existing.conflicts) != _json_dumps(new.conflicts)


def _json_dumps(obj: dict[str, object]) -> str:
    """Deterministic JSON serialisation for comparison."""
    return json.dumps(obj, sort_keys=True, default=str)
