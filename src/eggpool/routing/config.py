"""Routing configuration helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig


def routing_stale_after_s(config: AppConfig) -> float | None:
    """Return the per-account catalog stale gate for routing.

    ``allow_stale_catalog`` means EggPool may continue serving from
    previously known catalog support when refreshes are uncertain. In
    that mode, routing must not silently de-pool healthy sibling
    accounts just because their per-account refresh timestamp aged out.
    """
    if config.models.allow_stale_catalog:
        return None
    return float(config.models.stale_after_s)
