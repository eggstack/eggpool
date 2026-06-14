"""Statistics package: query layer and high-level service."""

from __future__ import annotations

from go_aggregator.stats.queries import (
    fetch_account_id,
    fetch_account_stats,
    fetch_active_reservations,
    fetch_error_breakdown,
    fetch_model_stats,
    fetch_recent_events,
    fetch_summary,
    fetch_timeseries,
)
from go_aggregator.stats.service import (
    PERIOD_PRESETS,
    StatsService,
    TimeRange,
    resolve_period,
)

__all__ = [
    "PERIOD_PRESETS",
    "StatsService",
    "TimeRange",
    "fetch_account_id",
    "fetch_account_stats",
    "fetch_active_reservations",
    "fetch_error_breakdown",
    "fetch_model_stats",
    "fetch_recent_events",
    "fetch_summary",
    "fetch_timeseries",
    "resolve_period",
]
