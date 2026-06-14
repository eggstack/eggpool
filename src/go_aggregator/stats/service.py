"""Statistics service.

High-level business logic for aggregating and presenting usage data.
Used by both the JSON API and the server-rendered dashboard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from go_aggregator.stats import queries
from go_aggregator.stats.queries import (
    fetch_account_id,
    fetch_active_reservations,
    fetch_error_breakdown,
    fetch_recent_events,
    fetch_summary,
    fetch_timeseries,
)

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database


PERIOD_PRESETS: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
}

# Utilization windows in seconds
_UTILIZATION_5H = 5 * 3600
_UTILIZATION_7D = 7 * 86400
_UTILIZATION_30D = 30 * 86400


def resolve_period(period: str | None) -> tuple[datetime, datetime, str]:
    """Resolve a period string into a (start, end, label) tuple.

    Accepts:
    - Preset: "1h", "24h", "7d", "30d"
    - ISO datetime range: "START..END"
    """
    if period is None or period == "":
        period = "24h"

    if ".." in period:
        start_str, end_str = period.split("..", 1)
        start = _parse_iso(start_str)
        end = _parse_iso(end_str)
        return start, end, "custom"

    if period in PERIOD_PRESETS:
        seconds = PERIOD_PRESETS[period]
        end = datetime.now(UTC)
        start = end - timedelta(seconds=seconds)
        return start, end, period

    start = _parse_iso(period)
    return start, datetime.now(UTC), "since"


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime string into a timezone-aware datetime."""
    if "T" not in value and " " not in value:
        value = f"{value} 00:00:00"
    elif "T" in value:
        value = value.replace("T", " ")
    try:
        dt = datetime.fromisoformat(value)  # noqa: DTZ007
    except ValueError:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def format_dt(dt: datetime) -> str:
    """Format a datetime as a SQL-friendly UTC string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def microdollars_to_dollars(value: float) -> float:
    """Convert microdollars to dollars."""
    return value / 1_000_000.0


@dataclass
class TimeRange:
    """A normalized time range for statistics queries."""

    start: datetime
    end: datetime
    label: str

    def start_str(self) -> str:
        return format_dt(self.start)

    def end_str(self) -> str:
        return format_dt(self.end)


class StatsService:
    """High-level statistics service.

    Wraps the raw query layer and adds derived metrics that the dashboard
    or API consumers expect (e.g., utilization imbalance, exactness ratios).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_summary(self, time_range: TimeRange) -> dict[str, Any]:
        """Get a top-line summary for the given time range."""
        return await fetch_summary(
            self._db, time_range.start_str(), time_range.end_str()
        )

    async def get_account_stats(self, time_range: TimeRange) -> list[dict[str, Any]]:
        """Get per-account aggregates including reservations and utilization."""
        rows = await queries.fetch_account_stats(
            self._db, time_range.start_str(), time_range.end_str()
        )
        reservations = await fetch_active_reservations(self._db)
        reserved_by_account: dict[str, int] = {}
        reservation_count_by_account: dict[str, int] = {}
        for r in reservations:
            name = str(r.get("account_name", ""))
            reserved_by_account[name] = reserved_by_account.get(name, 0) + int(
                r.get("reserved_microdollars", 0)
            )
            reservation_count_by_account[name] = (
                reservation_count_by_account.get(name, 0) + 1
            )

        now = datetime.now(UTC)
        for row in rows:
            name = str(row.get("account_name", ""))
            row["reserved_microdollars"] = reserved_by_account.get(name, 0)
            row["active_reservations"] = reservation_count_by_account.get(name, 0)

            # Compute utilization across three windows
            row["utilization_5h"] = await self._compute_utilization(
                name, now - timedelta(seconds=_UTILIZATION_5H), now
            )
            row["utilization_7d"] = await self._compute_utilization(
                name, now - timedelta(seconds=_UTILIZATION_7D), now
            )
            row["utilization_30d"] = await self._compute_utilization(
                name, now - timedelta(seconds=_UTILIZATION_30D), now
            )

            # Health state placeholder (could be integrated with HealthManager)
            row["health_state"] = "healthy"

        return rows

    async def _compute_utilization(
        self, account_name: str, start: datetime, end: datetime
    ) -> float:
        """Compute cost-based utilization for an account in a time window."""
        start_s = format_dt(start)
        end_s = format_dt(end)
        row = await self._db.fetch_one(
            "SELECT COALESCE(SUM(cost_microdollars), 0) as cost "
            "FROM requests r JOIN accounts a ON a.id = r.account_id "
            "WHERE a.name = ? AND r.started_at >= ? AND r.started_at < ?",
            (account_name, start_s, end_s),
        )
        if row is None:
            return 0.0
        cost = int(row["cost"])
        # Normalize: cost per hour
        hours = max((end - start).total_seconds() / 3600.0, 1.0)
        return cost / hours

    async def get_model_stats(
        self, time_range: TimeRange, account_name: str | None = None
    ) -> list[dict[str, Any]]:
        """Get per-model aggregates, optionally filtered by account."""
        account_id: int | None = None
        if account_name is not None and account_name != "":
            account_id = await fetch_account_id(self._db, account_name)
        return await queries.fetch_model_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )

    async def get_timeseries(
        self,
        time_range: TimeRange,
        bucket: str = "hour",
        account_name: str | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get time-bucketed time series data."""
        account_id: int | None = None
        if account_name is not None and account_name != "":
            account_id = await fetch_account_id(self._db, account_name)
        model_filter: str | None = model_id if model_id else None
        return await fetch_timeseries(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            bucket=bucket,
            account_id=account_id,
            model_id=model_filter,
        )

    async def get_error_breakdown(
        self, time_range: TimeRange, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get error message breakdown."""
        return await fetch_error_breakdown(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            limit=limit,
        )

    async def get_recent_events(
        self, limit: int = 50, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get recent account events."""
        return await fetch_recent_events(self._db, limit, event_type)

    async def get_utilization_imbalance(self, time_range: TimeRange) -> dict[str, Any]:
        """Compute a utilization imbalance metric across accounts.

        The metric is the coefficient of variation of normalized account
        utilization (cost / capacity_weight). Lower is better; 0 means
        perfect balance.
        """
        account_stats = await self.get_account_stats(time_range)
        active = [a for a in account_stats if int(a.get("request_count", 0)) > 0]
        if len(active) < 2:
            return {
                "imbalance_ratio": 0.0,
                "active_accounts": len(active),
                "most_used": None,
                "least_used": None,
            }

        # Normalize by account weight (default 1.0)
        normalized: list[float] = []
        for a in active:
            cost = float(a.get("cost_microdollars", 0))
            weight = float(a.get("account_weight", 1.0))
            if weight <= 0:
                weight = 1.0
            normalized.append(cost / weight)

        mean_val = sum(normalized) / len(normalized)
        if mean_val == 0:
            return {
                "imbalance_ratio": 0.0,
                "active_accounts": len(active),
                "most_used": None,
                "least_used": None,
            }

        variance = sum((v - mean_val) ** 2 for v in normalized) / len(normalized)
        std_dev = math.sqrt(variance)
        cv = std_dev / mean_val

        most = max(active, key=lambda a: float(a.get("cost_microdollars", 0)))
        least = min(active, key=lambda a: float(a.get("cost_microdollars", 0)))
        return {
            "imbalance_ratio": cv,
            "active_accounts": len(active),
            "most_used": {
                "name": str(most.get("account_name", "")),
                "cost_microdollars": int(most.get("cost_microdollars", 0)),
            },
            "least_used": {
                "name": str(least.get("account_name", "")),
                "cost_microdollars": int(least.get("cost_microdollars", 0)),
            },
        }

    async def get_dashboard_overview(self, time_range: TimeRange) -> dict[str, Any]:
        """Get the data set used to render the overview page."""
        summary = await self.get_summary(time_range)
        imbalance = await self.get_utilization_imbalance(time_range)
        return {
            "summary": summary,
            "imbalance": imbalance,
            "period_label": time_range.label,
            "start": time_range.start_str(),
            "end": time_range.end_str(),
        }
