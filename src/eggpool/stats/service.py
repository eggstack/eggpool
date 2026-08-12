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
    clamp_grouped_limit,
    empty_grouped_timeseries,
    postprocess_grouped_timeseries,
)
from eggpool.stats.queries import (
    fetch_account_id,
    fetch_active_reservations,
    fetch_attempt_stats,
    fetch_bandwidth_timeseries,
    fetch_error_breakdown,
    fetch_event_types_in_range,
    fetch_exactness_breakdown,
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
_DASHBOARD_CACHE_TTL_BY_NAMESPACE: dict[str, float] = {
    "summary": 30.0,
    "timeseries": 30.0,
    "grouped_timeseries": 60.0,
    "bandwidth": 60.0,
    "accounts": 30.0,
    "models": 30.0,
    "ips": 60.0,
    "pings": 30.0,
    "attempts": 30.0,
    "retries": 60.0,
    "routing": 60.0,
    "routing_selections": 60.0,
    "routing_exclusions": 60.0,
    "routing_skew": 60.0,
    "operational_summary": 60.0,
    "latency_phases": 60.0,
    "ttft_providers": 60.0,
    "ttft_models": 60.0,
    "transcoding_stats": 60.0,
    "cache_observability": 60.0,
    "canonical_request_segmentation": 60.0,
    "compression_observability": 60.0,
    "compression_runtime": 60.0,
    "compression_policy_stats": 60.0,
    "cache_stability": 60.0,
    "pending_health": 15.0,
    "model_options": 300.0,
    "account_options": 300.0,
}

_DASHBOARD_CACHE_PERIOD_OVERRIDES: dict[str, dict[str, float]] = {
    "24h": {
        "timeseries": 60.0,
        "grouped_timeseries": 120.0,
        "bandwidth": 120.0,
    },
    "7d": {
        "timeseries": 120.0,
        "grouped_timeseries": 240.0,
        "bandwidth": 240.0,
    },
    "30d": {
        "timeseries": 240.0,
        "grouped_timeseries": 300.0,
        "bandwidth": 300.0,
    },
}

_DASHBOARD_CACHE_MAX_ENTRIES = 32


def _dashboard_cache_ttl(namespace: str, period_label: str) -> float:
    period_overrides = _DASHBOARD_CACHE_PERIOD_OVERRIDES.get(period_label, {})
    if namespace in period_overrides:
        return period_overrides[namespace]
    return _DASHBOARD_CACHE_TTL_BY_NAMESPACE.get(namespace, 30.0)


def _is_dashboard_period_label(label: str) -> bool:
    """Return whether a label represents a rolling dashboard window.

    The overview heatmap uses generated labels such as ``180d`` rather than
    one of :data:`PERIOD_PRESETS`. Treat those labels like presets so the
    cache key advances in TTL-sized buckets instead of changing every second
    as the rolling range's end timestamp moves.
    """
    return label in PERIOD_PRESETS or (
        len(label) > 1 and label[-1] in {"h", "d"} and label[:-1].isdigit()
    )


# Maximum window (in seconds) that allows a raw requests fallback when
# rollups return empty.  Preset periods 24h/7d/30d and custom ranges
# above this threshold require rollup data or a bounded live-tail merge.
_RAW_FALLBACK_MAX_SECONDS = 2 * 3600


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
        self._dashboard_cache_hits: int = 0
        self._dashboard_cache_misses: int = 0

    def _dashboard_cache_key(
        self, namespace: str, time_range: TimeRange, *parts: str
    ) -> tuple[str, ...]:
        if _is_dashboard_period_label(time_range.label):
            ttl = _dashboard_cache_ttl(namespace, time_range.label)
            period_key = str(int(time_range.end.timestamp() // ttl))
        else:
            period_key = f"{time_range.start_str()}:{time_range.end_str()}"
        return (namespace, time_range.label, period_key, *parts)

    def _get_dashboard_cache(self, key: tuple[str, ...]) -> object | None:
        cached = self._dashboard_cache.get(key)
        if cached is None:
            self._dashboard_cache_misses += 1
            return None
        stored_at, value = cached
        namespace = key[0]
        period_label = key[1] if len(key) > 1 else ""
        ttl = _dashboard_cache_ttl(namespace, period_label)
        if time.monotonic() - stored_at >= ttl:
            self._dashboard_cache.pop(key, None)
            self._dashboard_cache_misses += 1
            return None
        self._dashboard_cache_hits += 1
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

    def cache_snapshot(self) -> dict[str, Any]:
        total = self._dashboard_cache_hits + self._dashboard_cache_misses
        return {
            "hits": self._dashboard_cache_hits,
            "misses": self._dashboard_cache_misses,
            "hit_rate": (self._dashboard_cache_hits / total if total > 0 else 0.0),
            "entries": len(self._dashboard_cache),
        }

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
        account_id: int | None = None
        if account_name:
            account_id = await fetch_account_id(self._db, account_name)
            if account_id is None:
                account_id = -1
        if self._rollup_repo is not None:
            result = await self.get_summary_from_rollups(
                time_range,
                account_id=account_id,
            )
            if int(result.get("total_requests", 0)) > 0 and await self._rollup_is_fresh(
                time_range, account_id=account_id
            ):
                return result
        return await fetch_summary(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )

    async def _rollup_is_fresh(
        self,
        time_range: TimeRange,
        *,
        account_id: int | None = None,
    ) -> bool:
        """Guard: only trust rollup-backed summaries when their latest
        activity is at least as recent as the requests table.

        The ``MetricsWriteCoalescer`` flushes periodically; if the
        latest flush is stale (worker stalled, crash before flush,
        write_mode=immediate on a quiet cluster) the rollup table
        can trail the live ``requests`` rows.  Preferring rollups in
        that state would under-report the in-flight hour.  We compare
        the most recent ``bucket_start`` returned by the rollup
        query against the most recent ``started_at`` from the
        requests table; when the requests table is fresher, the
        caller falls back to ``fetch_summary``.

        For entirely historic time windows (the end is more than one
        bucket ago) the rollup is considered authoritative regardless
        of the freshness comparison — there's no live data to be
        fresher than.
        """
        assert self._rollup_repo is not None
        end_dt = time_range.end
        now = datetime.now(UTC)
        # Normalize end_dt for the freshness comparison so callers can
        # hand in either tz-aware or naive datetimes (TimeRange accepts
        # both, since format_dt treats naive as UTC).
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
        # Historic window: end is in the past and we are at least one
        # rollup bucket past it. The rollup table is the only store.
        if end_dt <= now and (now - end_dt) >= timedelta(seconds=3600):
            return True
        rollup_latest = await self._rollup_repo.latest_bucket_start(
            end=time_range.end_str(),
            account_id=account_id,
        )
        if rollup_latest is None:
            return False
        requests_latest = await queries.fetch_latest_started_at(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )
        if requests_latest is None:
            return True
        # Lexicographic comparison is safe — both timestamps share the
        # canonical ``YYYY-MM-DD HH:MM:SS`` shape.
        return rollup_latest >= requests_latest

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

        backoffs_by_account: dict[int, list[dict[str, Any]]] | None = None
        if self._account_backoff_repo is not None:
            try:
                backoffs_by_account = await self._account_backoff_repo.get_for_accounts(
                    [int(row["account_id"]) for row in rows if row.get("account_id")]
                )
            except Exception:
                # Preserve the pre-bulk behavior for older repository doubles
                # and transient read failures: the per-row helper catches its
                # own query error and renders explicit empty backoff fields.
                backoffs_by_account = None

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

            account_id = row.get("account_id")
            await self._enrich_with_backoff(
                row,
                account_id,
                backoffs=(
                    backoffs_by_account.get(int(account_id), [])
                    if backoffs_by_account is not None and account_id is not None
                    else None
                ),
            )

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
        backoffs: list[dict[str, Any]] | None = None,
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
        if backoffs is None:
            try:
                backoffs = await self._account_backoff_repo.get_for_account(
                    account_id=int(account_id)
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
        model_filter: str | None = model_id if model_id else None
        if self._rollup_repo is not None:
            result = await self.get_timeseries_from_rollups(
                time_range,
                bucket=bucket,
                account_id=account_id,
                model_id=model_filter,
            )
            if result:
                merged = await self._maybe_merge_livet(
                    result,
                    time_range,
                    bucket=bucket,
                    account_id=account_id,
                    model_id=model_filter,
                )
                if use_cache:
                    self._set_dashboard_cache(key, merged)
                return merged
            window_seconds = int((time_range.end - time_range.start).total_seconds())
            if window_seconds <= _RAW_FALLBACK_MAX_SECONDS:
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
            if use_cache:
                self._set_dashboard_cache(key, [])
            return []
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
        if self._rollup_repo is not None:
            result = await self.get_bandwidth_timeseries_from_rollups(
                time_range,
                account_id=account_id,
            )
            if result:
                result = await self._maybe_merge_bandwidth_livet(
                    result,
                    time_range,
                    account_id=account_id,
                )
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
        bounded_limit = clamp_grouped_limit(limit)

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
        if self._rollup_repo is not None:
            result = await self.get_grouped_timeseries_from_rollups(
                time_range,
                bucket=bucket,
                group_by=group_by,
                limit=bounded_limit,
                account_id=account_id,
                model_id=model_id,
            )
            if result["points"]:
                merged = await self._maybe_merge_grouped_livet(
                    result,
                    time_range,
                    bucket=bucket,
                    group_by=group_by,
                    limit=bounded_limit,
                    account_id=account_id,
                    model_id=model_id,
                )
                if use_cache:
                    self._set_dashboard_cache(cache_key, merged)
                return merged
            window_seconds = int((time_range.end - time_range.start).total_seconds())
            if window_seconds <= _RAW_FALLBACK_MAX_SECONDS:
                model_filter: str | None = model_id if model_id else None
                raw_result = await fetch_grouped_timeseries(
                    self._db,
                    time_range.start_str(),
                    time_range.end_str(),
                    bucket=bucket,
                    group_by=group_by,
                    limit=bounded_limit,
                    account_id=account_id,
                    model_id=model_filter,
                )
                raw_result["source"] = "raw"
                raw_result["degraded_reason"] = "rollup_empty"
                if use_cache:
                    self._set_dashboard_cache(cache_key, raw_result)
                return raw_result
            empty = empty_grouped_timeseries(bucket, group_by, bounded_limit)
            empty["source"] = "empty"
            empty["degraded_reason"] = "rollup_empty"
            if use_cache:
                self._set_dashboard_cache(cache_key, empty)
            return empty
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
        result["source"] = "raw"
        result["degraded_reason"] = "none"
        if use_cache:
            self._set_dashboard_cache(cache_key, result)
        return result

    async def get_summary_from_rollups(
        self,
        time_range: TimeRange,
        *,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        """Get summary from usage_rollups.

        Token field semantics mirror :func:`eggpool.stats.queries._build_summary`:

        - ``total_tokens`` is the legacy fresh-token volume (``input + output``)
          and is preserved unchanged for backward compatibility with external
          stats API consumers.
        - ``fresh_tokens`` is the explicit fresh-token alias of ``total_tokens``.
        - ``accounted_tokens`` is the broader provider-accounting total
          (``input + output + cache_read + cache_write``) and is what the
          dashboard headline "Accounted tokens" card uses. See
          ``plans/2026-07-07-dashboard-cache-token-card-semantics-fix.md``.
        """
        assert self._rollup_repo is not None
        row = await self._rollup_repo.query_summary(
            start=time_range.start_str(),
            end=time_range.end_str(),
            account_id=account_id,
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
                "fresh_tokens": 0,
                "accounted_tokens": 0,
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
                "provider_reported_count": 0,
                "provider_reported_cost_microdollars": 0,
                "estimated_cost_sum_microdollars": 0,
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
        total_cache_write = _int(row.get("total_cache_write_tokens", 0))
        cache_read_ratio = queries.bounded_cache_ratio(
            float(total_cache_read),
            float(total_input_tokens),
            float(total_cache_write),
        )
        error_requests = _int(row.get("error_requests", 0))
        total_output_tokens = _int(row.get("total_output_tokens", 0))
        exactness = await fetch_exactness_breakdown(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
            account_id=account_id,
        )
        return {
            "total_requests": total_requests,
            "successful_requests": total_requests - error_requests,
            "error_requests": error_requests,
            "error_rate": (
                error_requests / total_requests if total_requests > 0 else 0.0
            ),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            # ``total_tokens`` is legacy fresh-token volume (= input + output)
            # and is kept unchanged for backward compatibility. ``fresh_tokens``
            # mirrors it as the explicit alias and ``accounted_tokens`` is the
            # broader provider-accounting total the dashboard headline card
            # uses. See the plan that introduced this split.
            "total_tokens": total_input_tokens + total_output_tokens,
            "fresh_tokens": total_input_tokens + total_output_tokens,
            "accounted_tokens": (
                total_input_tokens
                + total_output_tokens
                + total_cache_read
                + total_cache_write
            ),
            "total_cost_microdollars": _int(row.get("total_cost_microdollars", 0)),
            "avg_latency_ms": _float(row.get("avg_latency_ms", 0.0)),
            "total_cache_read_tokens": total_cache_read,
            "total_cache_write_tokens": total_cache_write,
            "total_reasoning_tokens": _int(row.get("total_reasoning_tokens", 0)),
            "cache_read_ratio": cache_read_ratio,
            "streamed_requests": _int(row.get("streamed_requests", 0)),
            "non_streamed_requests": _int(row.get("non_streamed_requests", 0)),
            "exact_count": exactness["exact_count"],
            "derived_count": exactness["derived_count"],
            "partial_count": exactness["partial_count"],
            "estimated_count": exactness["estimated_count"],
            "unknown_count": exactness["unknown_count"],
            "provider_reported_count": exactness["provider_reported_count"],
            "provider_reported_cost_microdollars": exactness[
                "provider_reported_cost_microdollars"
            ],
            "estimated_cost_sum_microdollars": exactness[
                "estimated_cost_sum_microdollars"
            ],
            "total_bytes_received": _int(row.get("total_bytes_received", 0)),
            "total_bytes_emitted": _int(row.get("total_bytes_emitted", 0)),
            "total_providers": 0,
            "avg_ttft_ms": _float(row.get("avg_ttft_ms", 0.0)),
            "tokens_per_second": _float(row.get("tokens_per_second", 0.0)),
            "p50_ttft_ms": 0.0,
            "p99_ttft_ms": 0.0,
        }

    async def get_timeseries_from_rollups(
        self,
        time_range: TimeRange,
        *,
        bucket: str = "hour",
        account_id: int | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get flat timeseries from usage_rollups."""
        assert self._rollup_repo is not None
        bucket_s = _bucket_size_s(bucket)
        rows = await self._rollup_repo.query_flat_timeseries(
            start=time_range.start_str(),
            end=time_range.end_str(),
            bucket_size_s=bucket_s,
            account_id=account_id,
            model_id=model_id,
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
        self,
        time_range: TimeRange,
        *,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get daily-bucketed bandwidth from usage_rollups."""
        assert self._rollup_repo is not None
        rows = await self._rollup_repo.query_flat_timeseries(
            start=time_range.start_str(),
            end=time_range.end_str(),
            bucket_size_s=86400,
            account_id=account_id,
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

    async def _maybe_merge_bandwidth_livet(
        self,
        rollup_rows: list[dict[str, Any]],
        time_range: TimeRange,
        *,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Reconcile only the current day against unflushed requests.

        The heatmap spans up to 180 days.  Reading the entire raw request
        window to reconcile one open rollup day defeats the point of the
        rollup table, especially on the overview page.  The current UTC day
        is the only bucket that can normally be incomplete while the rollup
        writer is waiting for its next flush, so bound the raw query to that
        day and retain older rollup coverage (which may outlive request
        retention).
        """
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        range_start = (
            time_range.start
            if time_range.start.tzinfo is not None
            else time_range.start.replace(tzinfo=UTC)
        )
        range_end = (
            time_range.end
            if time_range.end.tzinfo is not None
            else time_range.end.replace(tzinfo=UTC)
        )
        tail_start = max(range_start, day_start)
        tail_end = min(range_end, now)
        if tail_start >= tail_end:
            return rollup_rows

        live_rows = await fetch_bandwidth_timeseries(
            self._db,
            format_dt(tail_start),
            format_dt(tail_end),
            account_id=account_id,
        )
        if not live_rows:
            return rollup_rows

        by_day = {str(row.get("day", "")): dict(row) for row in rollup_rows}
        for row in live_rows:
            day = str(row.get("day", ""))
            rollup_row = by_day.get(day)
            if rollup_row is None or _int(row.get("request_count", 0)) >= _int(
                rollup_row.get("request_count", 0)
            ):
                by_day[day] = dict(row)
        return [by_day[day] for day in sorted(by_day)]

    async def _maybe_merge_livet(
        self,
        rollup_rows: list[dict[str, Any]],
        time_range: TimeRange,
        *,
        bucket: str,
        account_id: int | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Merge a bounded live-tail for the current open bucket if rollups
        may be missing the most recent partial bucket.

        Only queries raw data for the single current (incomplete) bucket
        that falls after the last rollup bucket boundary.
        """
        bucket_s = _bucket_size_s(bucket)
        if not rollup_rows:
            return rollup_rows
        now = datetime.now(UTC)
        current_bucket_start_dt = now.replace(
            minute=0 if bucket_s == 3600 else now.minute,
            second=0,
            microsecond=0,
        )
        if bucket_s == 86400:
            current_bucket_start_dt = current_bucket_start_dt.replace(hour=0)
        range_start = (
            time_range.start
            if time_range.start.tzinfo is not None
            else time_range.start.replace(tzinfo=UTC)
        )
        range_end = (
            time_range.end
            if time_range.end.tzinfo is not None
            else time_range.end.replace(tzinfo=UTC)
        )
        if not (range_start <= current_bucket_start_dt < range_end):
            return rollup_rows
        current_bucket_start = format_dt(max(range_start, current_bucket_start_dt))
        livet_rows = await fetch_timeseries(
            self._db,
            current_bucket_start,
            format_dt(min(range_end, now)),
            bucket=bucket,
            account_id=account_id,
            model_id=model_id,
        )
        if not livet_rows:
            return rollup_rows
        merged: dict[str, dict[str, Any]] = {}
        for row in rollup_rows:
            merged[str(row["bucket"])] = dict(row)
        for row in livet_rows:
            merged[str(row["bucket"])] = dict(row)
        return [merged[k] for k in sorted(merged)]

    async def _maybe_merge_grouped_livet(
        self,
        rollup_result: dict[str, Any],
        time_range: TimeRange,
        *,
        bucket: str,
        group_by: str,
        limit: int,
        account_id: int | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Merge a bounded live-tail into grouped rollup results when the
        current open bucket may be incomplete in rollups.
        """
        bucket_s = _bucket_size_s(bucket)
        points = rollup_result.get("points", [])
        if not points:
            return rollup_result
        now = datetime.now(UTC)
        current_bucket_start_dt = now.replace(
            minute=0 if bucket_s == 3600 else now.minute,
            second=0,
            microsecond=0,
        )
        if bucket_s == 86400:
            current_bucket_start_dt = current_bucket_start_dt.replace(hour=0)
        range_start = (
            time_range.start
            if time_range.start.tzinfo is not None
            else time_range.start.replace(tzinfo=UTC)
        )
        range_end = (
            time_range.end
            if time_range.end.tzinfo is not None
            else time_range.end.replace(tzinfo=UTC)
        )
        if not (range_start <= current_bucket_start_dt < range_end):
            return rollup_result
        current_bucket_start = format_dt(max(range_start, current_bucket_start_dt))
        model_filter: str | None = model_id if model_id else None
        livet_raw = await fetch_grouped_timeseries(
            self._db,
            current_bucket_start,
            format_dt(min(range_end, now)),
            bucket=bucket,
            group_by=group_by,
            limit=limit,
            account_id=account_id,
            model_id=model_filter,
        )
        livet_points = livet_raw.get("points", [])
        if not livet_points:
            rollup_result["source"] = "rollup"
            rollup_result["degraded_reason"] = "none"
            return rollup_result
        existing_points: dict[tuple[str, str], dict[str, Any]] = {}
        for pt in points:
            key = (str(pt["bucket"]), str(pt["series_key"]))
            existing_points[key] = pt
        for pt in livet_points:
            key = (str(pt["bucket"]), str(pt["series_key"]))
            existing_points[key] = pt
        merged_points = sorted(
            existing_points.values(),
            key=lambda p: (p["bucket"], p.get("label", "")),
        )
        rollup_result["points"] = merged_points
        rollup_result["source"] = "mixed"
        rollup_result["degraded_reason"] = "none"
        return rollup_result

    async def get_grouped_timeseries_from_rollups(
        self,
        time_range: TimeRange,
        *,
        bucket: str = "hour",
        group_by: str = "provider_model",
        limit: int = 12,
        account_id: int | None = None,
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
            account_id=account_id,
            model_id=model_id,
            limit=limit * 4 + 100,
        )
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
        self,
        limit: int = 50,
        event_type: str | None = None,
        time_range: TimeRange | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent account events, optionally bounded by ``time_range``."""
        if time_range is None:
            return await fetch_recent_events(self._db, limit, event_type)
        return await fetch_recent_events(
            self._db,
            limit,
            event_type,
            start=time_range.start_str(),
            end=time_range.end_str(),
        )

    async def get_event_types_in_range(self, time_range: TimeRange) -> list[str]:
        """Distinct ``event_type`` values present in the given window."""
        return await fetch_event_types_in_range(
            self._db, time_range.start_str(), time_range.end_str()
        )

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

        def _pick(metric_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
            most_row = max(active, key=lambda a: int(a.get(metric_key, 0) or 0))
            least_row = min(active, key=lambda a: int(a.get(metric_key, 0) or 0))
            return most_row, least_row

        most_cost, least_cost = _pick("cost_microdollars")
        most_tokens, least_tokens = _pick("total_tokens")
        most_requests, least_requests = _pick("request_count")

        return {
            "imbalance_ratio": cv,
            "active_accounts": len(active),
            "most_used": {
                "name": str(most_cost.get("account_name", "")),
                "cost_microdollars": int(most_cost.get("cost_microdollars", 0)),
                "total_tokens": int(most_tokens.get("total_tokens", 0)),
                "request_count": int(most_requests.get("request_count", 0)),
            },
            "least_used": {
                "name": str(least_cost.get("account_name", "")),
                "cost_microdollars": int(least_cost.get("cost_microdollars", 0)),
                "total_tokens": int(least_tokens.get("total_tokens", 0)),
                "request_count": int(least_requests.get("request_count", 0)),
            },
        }

    async def get_dashboard_overview(
        self,
        time_range: TimeRange,
        account_stats: list[dict[str, Any]] | None = None,
        *,
        summary: dict[str, Any] | None = None,
        cache_observability: dict[str, Any] | None = None,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """Get the data set used to render the overview page."""
        if summary is None:
            summary = await self.get_summary(time_range, use_cache=use_cache)
        imbalance = await self.get_utilization_imbalance(
            time_range, account_stats=account_stats
        )
        if cache_observability is None:
            cache_observability = await self.get_cache_observability(
                time_range.label, use_cache=use_cache
            )
        return {
            "summary": summary,
            "imbalance": imbalance,
            "cache": cache_observability,
            "period_label": time_range.label,
            "start": time_range.start_str(),
            "end": time_range.end_str(),
        }

    async def get_provider_ttft_summary(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Get per-provider TTFT aggregate (streamed requests only)."""
        key = self._dashboard_cache_key("ttft_providers", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_provider_ttft_summary(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_provider_model_ttft(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> list[dict[str, Any]]:
        """Get per-provider, per-model TTFT breakdown (streamed requests only)."""
        key = self._dashboard_cache_key("ttft_models", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("list[dict[str, Any]]", cached)
        result = await fetch_provider_model_ttft(
            self._db, time_range.start_str(), time_range.end_str()
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

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

    async def get_routing_skew_summary(
        self, time_range: TimeRange, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Routing selection skew summary: max/min ratio and most/least selected."""
        key = self._dashboard_cache_key("routing_skew", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        from eggpool.stats.queries import fetch_routing_skew_summary

        result = await fetch_routing_skew_summary(
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

    async def get_transcoding_stats(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Get protocol transcoding statistics for a time window."""
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("transcoding_stats", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_transcoding_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_cache_observability(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase 1 cache-counter observability aggregates.

        Reads ``cache_counter_status`` + supporting cache-token columns
        populated by :mod:`eggpool.proxy.normalized_usage`.  Returns:

        - ``requests_total`` / ``total_requests``
        - ``cache_counter_reported_requests`` / ``cache_counter_unknown_requests``
        - ``input_tokens_total`` / ``output_tokens_total``
        - ``total_cached_input_tokens`` / ``cache_hit_ratio_known_only``
        - ``by_status`` / ``per_protocol_status``
        - ``per_account_status`` / ``per_model_status``

        The ``cache_hit_ratio_known_only`` denominator is restricted to
        rows with ``cache_counter_status='reported'`` so dashboards never
        silently mix zero with missing.
        """
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("cache_observability", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_cache_observability(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_canonical_request_segmentation(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase 2 canonical request segmentation aggregates.

        Reads the segmentation columns populated by
        :mod:`eggpool.transcoder.segmentation` and persisted by
        :meth:`RequestRepository.finalize_if_pending`.  Returns the
        same shape as
        :func:`eggpool.stats.queries.fetch_canonical_request_segmentation`:

        - ``total_requests`` / ``by_status`` (including
          ``not_collected`` vs ``empty_request``)
        - ``per_provider_status`` / ``per_model_status``
        - ``token_totals`` / ``byte_totals`` (per-segment-kind aggregates)
        - ``compressible_candidate_requests`` / ``protected_requests``
        """
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("canonical_request_segmentation", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_canonical_request_segmentation(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_compression_observability(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase 4 observe-mode compression accounting aggregates.

        Reads the compression columns populated by
        :mod:`eggpool.transcoder.compression.analyzer` and persisted
        by :meth:`RequestRepository.finalize_if_pending`.  Returns
        the same shape as
        :func:`eggpool.stats.queries.fetch_compression_observability`:

        - ``total_requests`` / ``by_status`` / ``by_mode``
        - ``per_provider_status`` / ``per_account_status``
          / ``per_model_status``
        - ``totals`` (aggregate candidate / token / latency
          counts, plus observed_requests and warning count)
        - ``top_reason_codes`` (top 10 reason codes)
        """
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("compression_observability", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_compression_observability(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_compression_runtime(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase 7 runtime compression aggregates for operator dashboards.

        Surfaces mode counts, applied / fallback counts, latency stats,
        per-transform aggregates, warnings rollup, and cache-safety
        counters.  All values are computed from the durable
        ``requests`` columns populated by the Phase 4 / Phase 5 / Phase 6
        finalizers — never from in-memory caches or hot-path buffers.
        """
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("compression_runtime", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_compression_runtime(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_compression_policy_stats(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase 7 per-policy compression rollup.

        Aggregates the Phase 6 ``compression_policy_name`` /
        ``compression_policy_source`` audit columns (migration 0044)
        into one entry per resolved policy.  ``<global>`` is the
        sentinel for the no-override path; operator-chosen names come
        from the ``[[compression.policies]]`` entries.  All metrics are
        advisory / audit; the :class:`QuotaFairScorer` does not
        consume policy fields.
        """
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("compression_policy_stats", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_compression_policy_stats(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result

    async def get_cache_stability(
        self, period: str | None = None, *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Phase 7 cache-stability summary.

        Phase 3 transcoder cache stability is per-request and lives on
        :attr:`TranscodeContext.cache_boundary_tracker`.  The durable
        summary counts transcoded requests so operators can confirm
        the tracker is wired.  Per-request loss warnings are persisted
        via the transcoder trace and surfaced through the request
        trace endpoint, not via this aggregate.
        """
        time_range = resolve_time_range(period)
        key = self._dashboard_cache_key("cache_stability", time_range)
        if use_cache and (cached := self._get_dashboard_cache(key)) is not None:
            return cast("dict[str, Any]", cached)
        result = await queries.fetch_cache_stability_summary(
            self._db,
            time_range.start_str(),
            time_range.end_str(),
        )
        if use_cache:
            self._set_dashboard_cache(key, result)
        return result


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
