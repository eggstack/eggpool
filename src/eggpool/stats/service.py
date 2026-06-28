"""Statistics service.

High-level business logic for aggregating and presenting usage data.
Used by both the JSON API and the server-rendered dashboard.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from eggpool.stats import queries
from eggpool.stats.grouped_timeseries import (
    empty_grouped_timeseries,
    postprocess_grouped_timeseries,
)
from eggpool.stats.queries import (
    fetch_account_id,
    fetch_active_reservations,
    fetch_attempt_stats,
    fetch_bandwidth_timeseries,
    fetch_error_breakdown,
    fetch_grouped_timeseries,
    fetch_ip_stats,
    fetch_latency_phase_breakdown,
    fetch_operational_event_summary,
    fetch_provider_model_ttft,
    fetch_provider_ttft_summary,
    fetch_recent_events,
    fetch_recent_operational_events,
    fetch_recent_requests,
    fetch_request_trace,
    fetch_retry_distribution,
    fetch_routing_decisions_for_request,
    fetch_routing_distribution,
    fetch_routing_exclusion_breakdown,
    fetch_routing_selection_breakdown,
    fetch_summary,
    fetch_timeseries,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.db.repositories import AccountBackoffRepository, PingRepository
    from eggpool.db.rollup_repository import UsageRollupRepository
    from eggpool.health.health_manager import HealthManager


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
_DASHBOARD_CACHE_TTL_S = 30.0
_DASHBOARD_CACHE_MAX_ENTRIES = 32


def resolve_period(period: str | None) -> tuple[datetime, datetime, str]:
    """Resolve a period string into a (start, end, label) tuple.

    Accepts:
    - Preset: "1h", "24h", "7d", "30d"
    - ISO datetime range: "START..END"
    """
    now = datetime.now(UTC)
    if period is None or period == "":
        return _preset_period("24h", now)

    if ".." in period:
        start_str, end_str = period.split("..", 1)
        start = _parse_iso(start_str)
        end = _parse_iso(end_str)
        if start is None or end is None or start >= end:
            return _preset_period("24h", now)
        return start, end, "custom"

    if period in PERIOD_PRESETS:
        return _preset_period(period, now)

    start = _parse_iso(period)
    if start is None:
        return _preset_period("24h", now)
    return start, now, "since"


def _preset_period(period: str, end: datetime) -> tuple[datetime, datetime, str]:
    """Build a preset range from one consistent wall-clock sample."""
    return end - timedelta(seconds=PERIOD_PRESETS[period]), end, period


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 datetime string into a timezone-aware datetime."""
    if "T" not in value and " " not in value:
        value = f"{value} 00:00:00"
    elif "T" in value:
        value = value.replace("T", " ")
    try:
        dt = datetime.fromisoformat(value)  # noqa: DTZ007
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def format_dt(dt: datetime) -> str:
    """Format a datetime as a SQL-friendly UTC string.

    Timezone-aware datetimes are converted to UTC before formatting.
    Naive datetimes are treated as UTC.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
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


def resolve_time_range(period: str | None) -> TimeRange:
    """Resolve a period directly into the service's shared range type."""
    start, end, label = resolve_period(period)
    return TimeRange(start=start, end=end, label=label)


class StatsService:
    """High-level statistics service.

    Wraps the raw query layer and adds derived metrics that the dashboard
    or API consumers expect (e.g., utilization imbalance, exactness ratios).
    """

    def __init__(
        self,
        db: Database,
        health_manager: HealthManager | None = None,
        ping_repo: PingRepository | None = None,
        account_backoff_repo: AccountBackoffRepository | None = None,
        rollup_repo: UsageRollupRepository | None = None,
    ) -> None:
        self._db = db
        self._health_manager = health_manager
        self._ping_repo = ping_repo
        self._account_backoff_repo = account_backoff_repo
        self._rollup_repo = rollup_repo
        self._dashboard_cache: dict[tuple[str, ...], tuple[float, object]] = {}

    def _dashboard_cache_key(
        self, namespace: str, time_range: TimeRange, *parts: str
    ) -> tuple[str, ...]:
        if time_range.label in PERIOD_PRESETS:
            period_key = str(int(time_range.end.timestamp() // _DASHBOARD_CACHE_TTL_S))
        else:
            period_key = f"{time_range.start_str()}:{time_range.end_str()}"
        return (namespace, time_range.label, period_key, *parts)

    def _get_dashboard_cache(self, key: tuple[str, ...]) -> object | None:
        cached = self._dashboard_cache.get(key)
        if cached is None:
            return None
        stored_at, value = cached
        if time.monotonic() - stored_at >= _DASHBOARD_CACHE_TTL_S:
            self._dashboard_cache.pop(key, None)
            return None
        # Dashboard cache values are read-only data frames (dicts and
        # lists of dicts) returned straight to renderers; the renderer
        # never mutates them. Return the cached reference directly to
        # avoid the cost of a deep copy on every cache hit.
        return value

    def _set_dashboard_cache(self, key: tuple[str, ...], value: object) -> None:
        if (
            key not in self._dashboard_cache
            and len(self._dashboard_cache) >= _DASHBOARD_CACHE_MAX_ENTRIES
        ):
            oldest = min(
                self._dashboard_cache,
                key=lambda item: self._dashboard_cache[item][0],
            )
            self._dashboard_cache.pop(oldest, None)
        self._dashboard_cache[key] = (time.monotonic(), value)

    async def get_summary(
        self,
        time_range: TimeRange,
        account_name: str | None = None,
        *,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """Get a top-line summary for the given time range."""
        key = self._dashboard_cache_key("summary", time_range, account_name or "")
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await self._get_summary_inner(time_range, account_name)
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def _get_summary_inner(
        self,
        time_range: TimeRange,
        account_name: str | None = None,
    ) -> dict[str, Any]:
        if self._rollup_repo is not None:
            result = await self.get_summary_from_rollups(time_range)
            if int(result.get("total_requests", 0)) > 0:
                return result
        account_id: int | None = None
        if account_name:
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                account_id = -1
        return await fetch_summary(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )

    async def get_account_stats(
        self,
        time_range: TimeRange,
        *,
        include_disabled: bool = True,
        use_cache: bool = False,
    ) -> list[dict[str, Any]]:
        """Get per-account aggregates including reservations and utilization.

        ``include_disabled`` toggles whether accounts that
        ``sync_from_config`` marked ``enabled = 0`` (typically after a
        ``eggpool logout`` round-trip) are returned. The dashboard hides
        them by default; the JSON API keeps the historical view.
        """
        cache_flag = "all" if include_disabled else "enabled"
        key = self._dashboard_cache_key("accounts", time_range, cache_flag)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        rows = await queries.fetch_account_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            include_disabled=include_disabled,
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

        for row in rows:
            name = str(row.get("account_name", ""))
            row["reserved_microdollars"] = reserved_by_account.get(name, 0)
            row["active_reservations"] = reservation_count_by_account.get(name, 0)

            # fetch_account_stats already returns rolling window costs.
            # Convert them to cost/hour rates without issuing three extra
            # queries per account.
            row["utilization_5h"] = self._cost_per_hour(
                row.get("cost_5h", 0), _UTILIZATION_5H
            )
            row["utilization_7d"] = self._cost_per_hour(
                row.get("cost_7d", 0), _UTILIZATION_7D
            )
            row["utilization_30d"] = self._cost_per_hour(
                row.get("cost_30d", 0), _UTILIZATION_30D
            )

            # Health state from HealthManager
            if self._health_manager:
                row["health_state"] = (
                    "healthy"
                    if self._health_manager.is_account_healthy(name)
                    else "unhealthy"
                )
                health = self._health_manager.get_account_health(name)
                row["consecutive_upstream_failures"] = int(
                    getattr(health, "consecutive_failures", 0)
                )
                row["operator_disabled"] = bool(
                    getattr(health, "disabled_until", None) is not None
                    and float(getattr(health, "disabled_until", 0.0)) > time.time()
                )
            else:
                row["health_state"] = "healthy"
                row["consecutive_upstream_failures"] = 0
                row["operator_disabled"] = False

            await self._enrich_with_backoff(row, row.get("account_id"))

            reserved = row.get("reserved_microdollars", 0) or 0
            row["estimated_over_local_budget"] = bool(
                row.get("capacity_5h_microdollars") is not None
                and int(reserved) > int(row.get("capacity_5h_microdollars") or 0)
            )

        if use_cache:
            self._set_dashboard_cache(key, rows)
        return rows

    async def _enrich_with_backoff(
        self,
        row: dict[str, Any],
        account_id: int | None,
    ) -> None:
        """Populate upstream-backoff fields on a single account row.

        Sets ``upstream_backoff_reason``, ``backoff_until``, and
        ``authentication_failed`` from the most recent active
        ``account_backoffs`` row for the account. Missing data yields
        ``None``/``False`` values so the renderer can always show
        explicit placeholders.
        """
        if self._account_backoff_repo is None or account_id is None:
            row["upstream_backoff_reason"] = None
            row["backoff_until"] = None
            row["authentication_failed"] = False
            return
        try:
            backoffs: list[
                dict[str, Any]
            ] = await self._account_backoff_repo.get_for_account_model(
                account_id=int(account_id), model_id=None
            )
        except Exception:
            row["upstream_backoff_reason"] = None
            row["backoff_until"] = None
            row["authentication_failed"] = False
            return
        now = time.time()
        active: list[dict[str, Any]] = []
        for b in backoffs:
            until = b.get("backoff_until_epoch")
            if until is None or float(until) > now:
                active.append(b)
        if not active:
            row["upstream_backoff_reason"] = None
            row["backoff_until"] = None
            row["authentication_failed"] = False
            return
        preferred: dict[str, Any] | None = next(
            (
                b
                for b in active
                if str(b.get("reason") or "") == "authentication_failed"
            ),
            None,
        )
        if preferred is None:
            preferred = max(
                active,
                key=lambda b: float(b.get("backoff_until_epoch") or 0.0),
            )
        row["upstream_backoff_reason"] = str(preferred.get("reason") or "")
        row["backoff_until"] = preferred.get("backoff_until_epoch")
        row["authentication_failed"] = any(
            str(b.get("reason") or "") == "authentication_failed" for b in active
        )

    @staticmethod
    def _cost_per_hour(value: object, window_seconds: int) -> float:
        """Convert a rolling-window cost to a microdollars/hour rate."""
        hours = window_seconds / 3600.0
        if hours <= 0:
            return 0.0
        if isinstance(value, int | float):
            cost = int(value)
        elif isinstance(value, str):
            try:
                cost = int(value)
            except ValueError:
                cost = 0
        else:
            cost = 0
        return cost / hours

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
        # Normalize: cost per hour. An empty window has zero duration
        # and a degenerate rate; return 0.0 instead of dividing by the
        # 1-hour floor and reporting inflated utilization.
        hours = (end - start).total_seconds() / 3600.0
        if hours <= 0:
            return 0.0
        return cost / hours

    async def get_model_stats(
        self,
        time_range: TimeRange,
        account_name: str | None = None,
        *,
        use_cache: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Get per-model aggregates, optionally filtered by account.

        Returns None when an account filter was provided but the account
        was not found in the database. Callers can use this to distinguish
        "no results" from "unknown account."
        """
        key = self._dashboard_cache_key("models", time_range, account_name or "")
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        account_id: int | None = None
        if account_name is not None and account_name != "":
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                return None
        result = await queries.fetch_model_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_timeseries(
        self,
        time_range: TimeRange,
        bucket: str = "hour",
        account_name: str | None = None,
        model_id: str | None = None,
        *,
        use_cache: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Get time-bucketed time series data.

        Returns None when an account filter was provided but the account
        was not found in the database.
        """
        key = self._dashboard_cache_key(
            "timeseries",
            time_range,
            bucket,
            account_name or "",
            model_id or "",
        )
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        account_id: int | None = None
        if account_name is not None and account_name != "":
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                return None
        if self._rollup_repo is not None and account_id is None:
            result = await self.get_timeseries_from_rollups(time_range, bucket=bucket)
            if result:
                if use_cache:
                    self._set_dashboard_cache(key, result)
                return result
        model_filter: str | None = model_id if model_id else None
        result = await fetch_timeseries(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            bucket=bucket,
            account_id=account_id,
            model_id=model_filter,
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_bandwidth_timeseries(
        self,
        time_range: TimeRange,
        account_name: str | None = None,
        *,
        use_cache: bool = False,
    ) -> list[dict[str, Any]]:
        """Get daily-bucketed bandwidth for heatmap and detail views."""
        key = self._dashboard_cache_key("bandwidth", time_range, account_name or "")
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        account_id: int | None = None
        if account_name is not None and account_name != "":
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                result: list[dict[str, Any]] = []
                if use_cache:
                    self._set_dashboard_cache(key, result)
                return result
        if self._rollup_repo is not None and account_id is None:
            result = await self.get_bandwidth_timeseries_from_rollups(time_range)
            if result:
                if use_cache:
                    self._set_dashboard_cache(key, result)
                return result
        result = await fetch_bandwidth_timeseries(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_grouped_timeseries(
        self,
        time_range: TimeRange,
        *,
        bucket: str = "hour",
        group_by: str = "provider_model",
        limit: int = 12,
        account_name: str | None = None,
        model_id: str | None = None,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """Get time-bucketed time series grouped by a chosen dimension.

        ``bucket`` is normalized to ``"hour"`` for unknown values;
        ``group_by`` is normalized to ``"provider_model"``.  ``limit`` is
        clamped to ``1..25``.  An unknown ``account_name`` returns the
        empty stable payload rather than ``None`` so the renderer can
        rely on a consistent shape.
        """
        if bucket not in ("hour", "day"):
            bucket = "hour"
        if group_by not in ("provider", "model", "provider_model", "account"):
            group_by = "provider_model"
        bounded_limit = max(1, min(int(limit), 25))

        account_id: int | None = None
        if account_name is not None and account_name != "":
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                return empty_grouped_timeseries(bucket, group_by, bounded_limit)

        cache_key = self._dashboard_cache_key(
            "grouped_timeseries",
            time_range,
            bucket,
            group_by,
            str(bounded_limit),
            account_name or "",
            model_id or "",
        )
        if use_cache and (cached := self._get_dashboard_cache(cache_key)) is not None:
            return cast("dict[str, Any]", cached)
        if self._rollup_repo is not None and account_id is None:
            result = await self.get_grouped_timeseries_from_rollups(
                time_range,
                bucket=bucket,
                group_by=group_by,
                limit=bounded_limit,
                model_id=model_id,
            )
            if result["points"]:
                if use_cache:
                    self._set_dashboard_cache(cache_key, result)
                return result
        model_filter: str | None = model_id if model_id else None
        result = await fetch_grouped_timeseries(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            bucket=bucket,
            group_by=group_by,
            limit=bounded_limit,
            account_id=account_id,
            model_id=model_filter,
        )
        if use_cache:
            self._set_dashboard_cache(cache_key, result)
        return result

    async def get_summary_from_rollups(self, time_range: TimeRange) -> dict[str, Any]:
        """Get summary from usage_rollups."""
        assert self._rollup_repo is not None
        row = await self._rollup_repo.query_summary(
            start=time_range.start_str(),
            end=time_range.end_str(),
        )
        total_requests = _int(row.get("total_requests", 0))
        if total_requests == 0:
            return {
                "total_requests": 0,
                "successful_requests": 0,
                "error_requests": 0,
                "error_rate": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "total_cost_microdollars": 0,
                "avg_latency_ms": 0.0,
                "total_cache_read_tokens": 0,
                "total_cache_write_tokens": 0,
                "total_reasoning_tokens": 0,
                "cache_read_ratio": None,
                "streamed_requests": 0,
                "non_streamed_requests": 0,
                "exact_count": 0,
                "derived_count": 0,
                "partial_count": 0,
                "estimated_count": 0,
                "unknown_count": 0,
                "total_bytes_received": 0,
                "total_bytes_emitted": 0,
                "total_providers": 0,
                "avg_ttft_ms": 0.0,
                "tokens_per_second": 0.0,
                "p50_ttft_ms": 0.0,
                "p99_ttft_ms": 0.0,
            }
        total_input_tokens = _int(row.get("total_input_tokens", 0))
        total_cache_read = _int(row.get("total_cache_read_tokens", 0))
        cache_read_ratio = (
            total_cache_read / total_input_tokens if total_input_tokens > 0 else None
        )
        error_requests = _int(row.get("error_requests", 0))
        total_output_tokens = _int(row.get("total_output_tokens", 0))
        return {
            "total_requests": total_requests,
            "successful_requests": total_requests - error_requests,
            "error_requests": error_requests,
            "error_rate": (
                error_requests / total_requests if total_requests > 0 else 0.0
            ),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "total_cost_microdollars": _int(row.get("total_cost_microdollars", 0)),
            "avg_latency_ms": _float(row.get("avg_latency_ms", 0.0)),
            "total_cache_read_tokens": total_cache_read,
            "total_cache_write_tokens": _int(row.get("total_cache_write_tokens", 0)),
            "total_reasoning_tokens": _int(row.get("total_reasoning_tokens", 0)),
            "cache_read_ratio": cache_read_ratio,
            "streamed_requests": _int(row.get("streamed_requests", 0)),
            "non_streamed_requests": _int(row.get("non_streamed_requests", 0)),
            "exact_count": 0,
            "derived_count": 0,
            "partial_count": 0,
            "estimated_count": 0,
            "unknown_count": 0,
            "total_bytes_received": _int(row.get("total_bytes_received", 0)),
            "total_bytes_emitted": _int(row.get("total_bytes_emitted", 0)),
            "total_providers": 0,
            "avg_ttft_ms": 0.0,
            "tokens_per_second": 0.0,
            "p50_ttft_ms": 0.0,
            "p99_ttft_ms": 0.0,
        }

    async def get_timeseries_from_rollups(
        self,
        time_range: TimeRange,
        *,
        bucket: str = "hour",
    ) -> list[dict[str, Any]]:
        """Get flat timeseries from usage_rollups."""
        assert self._rollup_repo is not None
        bucket_s = _bucket_size_s(bucket)
        rows = await self._rollup_repo.query_flat_timeseries(
            start=time_range.start_str(),
            end=time_range.end_str(),
            bucket_size_s=bucket_s,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            request_count = _int(row.get("request_count", 0))
            input_tok = _int(row.get("input_tokens", 0))
            output_tok = _int(row.get("output_tokens", 0))
            result.append(
                {
                    "bucket": str(row["bucket"]),
                    "request_count": request_count,
                    "input_tokens": input_tok,
                    "output_tokens": output_tok,
                    "total_tokens": input_tok + output_tok,
                    "cost_microdollars": _int(row.get("cost_microdollars", 0)),
                    "error_count": _int(row.get("error_count", 0)),
                    "bytes_received": _int(row.get("bytes_received", 0)),
                    "bytes_emitted": _int(row.get("bytes_emitted", 0)),
                    "avg_ttft_ms": _float(row.get("avg_ttft_ms", 0.0)),
                }
            )
        return result

    async def get_bandwidth_timeseries_from_rollups(
        self, time_range: TimeRange
    ) -> list[dict[str, Any]]:
        """Get daily-bucketed bandwidth from usage_rollups."""
        assert self._rollup_repo is not None
        rows = await self._rollup_repo.query_flat_timeseries(
            start=time_range.start_str(),
            end=time_range.end_str(),
            bucket_size_s=3600,
        )
        day_buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket_str = str(row["bucket"])
            day = bucket_str[:10]
            if day not in day_buckets:
                day_buckets[day] = {
                    "day": day,
                    "bytes_received": 0,
                    "bytes_emitted": 0,
                    "total_tokens": 0,
                    "request_count": 0,
                }
            entry = day_buckets[day]
            entry["bytes_received"] = _int(entry["bytes_received"]) + _int(
                row.get("bytes_received", 0)
            )
            entry["bytes_emitted"] = _int(entry["bytes_emitted"]) + _int(
                row.get("bytes_emitted", 0)
            )
            entry["total_tokens"] = (
                _int(entry["total_tokens"])
                + _int(row.get("input_tokens", 0))
                + _int(row.get("output_tokens", 0))
            )
            entry["request_count"] = _int(entry["request_count"]) + _int(
                row.get("request_count", 0)
            )
        return [day_buckets[k] for k in sorted(day_buckets)]

    async def get_grouped_timeseries_from_rollups(
        self,
        time_range: TimeRange,
        *,
        bucket: str = "hour",
        group_by: str = "provider_model",
        limit: int = 12,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Get grouped timeseries from usage_rollups."""
        assert self._rollup_repo is not None
        bucket_s = _bucket_size_s(bucket)
        rows = await self._rollup_repo.query_timeseries(
            start=time_range.start_str(),
            end=time_range.end_str(),
            bucket_size_s=bucket_s,
            group_by=group_by,
            limit=10000,
        )
        if model_id is not None:
            rows = [
                r
                for r in rows
                if str(r.get("series_key", "")).endswith(f"/{model_id}")
                or str(r.get("series_key", "")) == model_id
            ]
        if not rows:
            return empty_grouped_timeseries(bucket, group_by, limit)

        raw_rows: list[dict[str, Any]] = []
        for row in rows:
            sk = str(row["series_key"])
            if group_by == "provider_model":
                parts = sk.split("/", 1)
                provider_id = parts[0] if len(parts) > 1 else ""
                model_id_val = parts[1] if len(parts) > 1 else sk
                label = f"{provider_id} / {model_id_val}"
            elif group_by == "provider":
                label = sk
                provider_id = sk
                model_id_val = ""
            elif group_by == "model":
                label = sk
                provider_id = ""
                model_id_val = sk
            else:
                label = sk
                provider_id = ""
                model_id_val = ""
            input_tok = _int(row.get("input_tokens", 0))
            output_tok = _int(row.get("output_tokens", 0))
            raw_rows.append(
                {
                    "bucket": str(row["bucket"]),
                    "raw_series_key": sk,
                    "raw_series_label": label,
                    "provider_id": provider_id,
                    "model_id": model_id_val,
                    "account_name": "",
                    "request_count": _int(row.get("request_count", 0)),
                    "error_count": _int(row.get("error_count", 0)),
                    "input_tokens": input_tok,
                    "output_tokens": output_tok,
                    "cache_read_tokens": _int(row.get("cache_read_tokens", 0)),
                    "cache_write_tokens": _int(row.get("cache_write_tokens", 0)),
                    "reasoning_tokens": _int(row.get("reasoning_tokens", 0)),
                    "total_tokens": input_tok + output_tok,
                    "cost_microdollars": _int(row.get("cost_microdollars", 0)),
                    "bytes_received": _int(row.get("bytes_received", 0)),
                    "bytes_emitted": _int(row.get("bytes_emitted", 0)),
                    "avg_latency_ms": _float(row.get("avg_latency_ms", 0.0)),
                    "avg_ttft_ms": _float(row.get("avg_ttft_ms", 0.0)),
                }
            )
        return postprocess_grouped_timeseries(
            raw_rows,
            bucket=bucket,
            group_by=group_by,
            limit=limit,
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

    async def get_utilization_imbalance(
        self,
        time_range: TimeRange,
        account_stats: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compute a utilization imbalance metric across accounts.

        The metric is the coefficient of variation of normalized account
        utilization (cost / capacity_weight). Lower is better; 0 means
        perfect balance.
        """
        if account_stats is None:
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

    async def get_dashboard_overview(
        self,
        time_range: TimeRange,
        account_stats: list[dict[str, Any]] | None = None,
        *,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """Get the data set used to render the overview page."""
        summary = await self.get_summary(time_range, use_cache=use_cache)
        imbalance = await self.get_utilization_imbalance(
            time_range, account_stats=account_stats
        )
        return {
            "summary": summary,
            "imbalance": imbalance,
            "period_label": time_range.label,
            "start": time_range.start_str(),
            "end": time_range.end_str(),
        }

    async def get_provider_ttft_summary(
        self, time_range: TimeRange
    ) -> list[dict[str, Any]]:
        """Get per-provider TTFT aggregate (streamed requests only)."""
        return await fetch_provider_ttft_summary(
            self._db, time_range.start_str(), time_range.end_str()
        )

    async def get_provider_model_ttft(
        self, time_range: TimeRange
    ) -> list[dict[str, Any]]:
        """Get per-provider, per-model TTFT breakdown (streamed requests only)."""
        return await fetch_provider_model_ttft(
            self._db, time_range.start_str(), time_range.end_str()
        )

    async def get_ping_summary(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Get per-provider ping summary: avg/min/max latency, success rate."""
        if self._ping_repo is None:
            return []
        key = self._dashboard_cache_key("pings", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await self._ping_repo.get_provider_ping_summary(
            time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_ping_timeseries(
        self,
        provider_id: str,
        time_range: TimeRange,
        bucket: str = "hour",
    ) -> list[dict[str, Any]]:
        """Get per-bucket ping latency trend for one provider."""
        if self._ping_repo is None:
            return []
        return await self._ping_repo.get_ping_timeseries(
            provider_id,
            time_range.start_str(),
            time_range.end_str(),
            bucket=bucket,
        )

    async def get_ping_recent(
        self,
        provider_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get most recent pings, optionally filtered by provider."""
        if self._ping_repo is None:
            return []
        return await self._ping_repo.get_ping_recent(provider_id, limit)

    async def get_ip_stats(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Get per-IP statistics for a time window."""
        key = self._dashboard_cache_key("ips", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_ip_stats(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_attempt_stats(
        self,
        time_range: TimeRange,
        *,
        account_name: str | None = None,
        model_id: str | None = None,
        provider_id: str | None = None,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """Aggregate per-attempt statistics for the given window.

        Returns aggregate counts/latency/bytes plus retry rate, with
        optional filters on account, model, and provider.  The
        attempt-level view exposes retry pressure that request-level
        aggregates hide because every request can produce multiple
        attempt rows.
        """
        cache_parts: list[str] = [
            account_name or "",
            model_id or "",
            provider_id or "",
        ]
        key = self._dashboard_cache_key("attempts", time_range, *cache_parts)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        account_id: int | None = None
        if account_name:
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                account_id = -1
        result = await fetch_attempt_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
            model_id=model_id,
            provider_id=provider_id,
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_retry_distribution(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Return the distribution of attempts by retry_category."""
        key = self._dashboard_cache_key("retries", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_retry_distribution(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_request_trace(self, request_id: int) -> dict[str, Any] | None:
        """Return the parent request row plus its full attempt chain.

        Returns ``None`` when no such request exists; otherwise returns
        a dict with ``request`` and ``attempts``.  Intended for the
        auth-gated per-request trace endpoint.
        """
        return await fetch_request_trace(self._db, request_id)

    async def get_routing_decisions_for_request(
        self, request_id: int
    ) -> list[dict[str, Any]]:
        """Return all routing decisions for a single request."""
        return await fetch_routing_decisions_for_request(self._db, request_id)

    async def get_routing_distribution(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Per-model routing distribution: how often each (model, provider)
        was selected, average eligible/scored counts."""
        key = self._dashboard_cache_key("routing", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_routing_distribution(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_routing_selection_breakdown(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Account-level selection counts derived from routing_decisions."""
        key = self._dashboard_cache_key("routing_selections", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_routing_selection_breakdown(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_routing_exclusion_breakdown(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Distribution of (account, reason) exclusions."""
        key = self._dashboard_cache_key("routing_exclusions", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_routing_exclusion_breakdown(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_operational_event_summary(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Per-event-type summary of operational_events rows.

        Aggregates the JSON details blob so the dashboard can chart
        safety-net activity without re-parsing every payload.
        """
        key = self._dashboard_cache_key("operational_summary", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_operational_event_summary(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_recent_operational_events(
        self,
        limit: int = 50,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent operational_events rows."""
        return await fetch_recent_operational_events(
            self._db, limit=limit, event_type=event_type
        )

    async def get_latency_phase_breakdown(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase-decomposed latency: connect, read, coordinator overhead."""
        key = self._dashboard_cache_key("latency_phases", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await fetch_latency_phase_breakdown(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_recent_requests(
        self,
        *,
        limit: int = 50,
        account_id: int | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        status: str | None = None,
        include_client_ip: bool = False,
    ) -> list[dict[str, Any]]:
        """Recent request metadata rows for the auth-gated debug view."""
        return await fetch_recent_requests(
            self._db,
            limit=limit,
            account_id=account_id,
            provider_id=provider_id,
            model_id=model_id,
            status=status,
            include_client_ip=include_client_ip,
        )

    async def get_pending_health_snapshot(
        self, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Return an instantaneous pending-reservation health snapshot.

        Combines the ``requests`` and ``reservations`` tables to surface
        the current number of pending requests, the age of the oldest
        pending request, the active reservation count, the reserved
        microdollar total, and the age of the oldest active reservation.

        Used by the Reliability page and the Overview System Health
        row to expose leak-style failures (pending requests surviving
        past their reservation TTL, orphaned active reservations).
        """
        from eggpool.quota.audit import (
            active_reservations_summary,
            stale_pending_requests,
        )

        key = ("pending_health",)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        pending_row = await self._db.fetch_one(
            """
            SELECT
                COUNT(*) AS pending_count,
                MIN(started_at) AS oldest_pending_at
            FROM requests
            WHERE status = 'pending'
            """
        )
        if pending_row is None:
            pending_count = 0
            oldest_pending_at = None
        else:
            pending_count = int(pending_row["pending_count"] or 0)
            oldest_pending_at = pending_row["oldest_pending_at"]
        now = datetime.now(UTC)
        oldest_pending_age_seconds: float | None = None
        if oldest_pending_at and pending_count > 0:
            parsed = _parse_dt(str(oldest_pending_at))
            if parsed is not None:
                oldest_pending_age_seconds = max(0.0, (now - parsed).total_seconds())

        stale_pending = await stale_pending_requests(self._db, threshold_seconds=900)

        reservations = await active_reservations_summary(self._db)
        active_reservation_count = sum(
            int(r.get("active_reservations", 0)) for r in reservations
        )
        active_reserved_microdollars = sum(
            int(r.get("active_reserved_microdollars", 0)) for r in reservations
        )
        oldest_reservation_age_seconds: float | None = None
        oldest_at_values = [
            r.get("oldest_reservation_at")
            for r in reservations
            if r.get("oldest_reservation_at")
        ]
        if oldest_at_values:
            parsed = min(
                (_parse_dt(str(v)) for v in oldest_at_values),
                key=lambda dt: dt or now,
                default=None,
            )
            if parsed is not None:
                oldest_reservation_age_seconds = max(
                    0.0, (now - parsed).total_seconds()
                )

        result = {
            "pending_count": pending_count,
            "oldest_pending_age_seconds": oldest_pending_age_seconds,
            "stale_pending_count": int(stale_pending or 0),
            "active_reservation_count": active_reservation_count,
            "active_reserved_microdollars": active_reserved_microdollars,
            "oldest_reservation_age_seconds": oldest_reservation_age_seconds,
            "as_of": now.isoformat(),
        }
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_transcoding_stats(self, period: str | None = None) -> dict[str, Any]:
        """Get protocol transcoding statistics for a time window."""
        time_range = resolve_time_range(period)
        return await queries.fetch_transcoding_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )


_BUCKET_SIZES: dict[str, int] = {
    "hour": 3600,
    "day": 86400,
}


def _bucket_size_s(bucket: str) -> int:
    return _BUCKET_SIZES.get(bucket, 3600)


def _int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return 0


def _float(value: object) -> float:
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, str):
        return float(value)
    return 0.0


def _parse_dt(value: str) -> datetime | None:
    """Best-effort parse for SQLite-formatted datetime strings."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC)
    return None
