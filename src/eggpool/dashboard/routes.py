"""Dashboard HTTP routes.

The dashboard exposes a read-only server-rendered HTML interface.
All free-text fields are HTML-escaped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, cast

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import HTMLResponse, JSONResponse

from eggpool.dashboard.render import (
    get_available_themes,
    get_theme,
    render_accounts,
    render_bandwidth,
    render_cache,
    render_events,
    render_latency,
    render_model_detail,
    render_models,
    render_overview,
    render_pings,
    render_reliability,
    render_routing,
    render_runtime,
    render_timeseries,
    render_traces,
)
from eggpool.dashboard.timeseries_buckets import (
    AUTO_BUCKET,
    resolve_bucket,
)
from eggpool.errors import ConfigError
from eggpool.model_info.presentation import (
    compact_model_info_summary,
    normalize_model_info_status_filter,
)
from eggpool.stats import TimeRange, resolve_time_range
from eggpool.stats.grouped_timeseries import clamp_grouped_limit
from eggpool.stats.queries import fetch_disabled_account_count
from eggpool.stats.segmentation import serialize_canonical_request_segmentation

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable

    from fastapi.responses import Response  # noqa: TCH004

    from eggpool.models.config import AppConfig

_ReliabilityPayload = tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]
_PingsPayload = tuple[list[dict[str, Any]], list[dict[str, Any]]]
_LatencyPayload = tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    "ModelInfoDashboardState",
]
_RoutingPayload = tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    "ModelInfoDashboardState",
]
_RoutingPayloadWithRuntime = tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    "ModelInfoDashboardState",
    dict[str, Any],
]


async def _empty_dict() -> dict[str, Any]:
    """Return an empty dict for asyncio.gather when a probe is disabled."""
    return {}


logger = logging.getLogger(__name__)
_DashboardStageResult = TypeVar("_DashboardStageResult")


def _get_stats(request: Request) -> Any:
    """Get the stats service from the active generation or app.state fallback."""
    from eggpool.app import get_active_generation  # noqa: PLC0415

    gen = get_active_generation(request)
    if gen is not None:
        return gen.stats_service
    return getattr(request.app.state, "stats", None)


def _get_model_info(request: Request) -> Any:
    """Get the model_info service from the active generation or app.state fallback."""
    from eggpool.app import get_active_generation  # noqa: PLC0415

    gen = get_active_generation(request)
    if gen is not None:
        return getattr(gen, "model_info", None)
    return getattr(request.app.state, "model_info", None)


def _get_catalog(request: Request) -> Any:
    """Get the catalog from the active generation or app.state fallback."""
    from eggpool.app import get_active_generation  # noqa: PLC0415

    gen = get_active_generation(request)
    if gen is not None:
        return gen.catalog
    return getattr(request.app.state, "catalog", None)


async def _await_dashboard_stage(
    telemetry: Any | None,
    page: str,
    stage: str,
    awaitable: Awaitable[_DashboardStageResult],
) -> _DashboardStageResult:
    """Await one dashboard operation and record its actual elapsed time."""
    started = time.perf_counter()
    try:
        return await awaitable
    finally:
        if telemetry is not None:
            telemetry.record_stage(
                page,
                stage,
                (time.perf_counter() - started) * 1000,
            )


@dataclass(frozen=True, slots=True)
class ModelInfoDashboardState:
    """Compact diagnostic bundle for the dashboard's model-info summary call."""

    summaries: dict[str, dict[str, Any]]
    available: bool
    degraded_reason: str | None = None
    error_class: str | None = None
    summary_count: int = 0
    matched_row_count: int = 0
    unmatched_row_count: int = 0
    unmatched_sample: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogRowsState:
    """Compact diagnostic bundle for the dashboard's catalog-row build.

    Carries both the produced rows and a ``degraded_reason`` /
    ``error_class`` pair so the route can surface a dashboard-level
    diagnostic when the catalog service is attached but produced no
    rows (or failed entirely).
    """

    rows: list[dict[str, Any]]
    available: bool
    degraded_reason: str | None = None
    error_class: str | None = None
    row_count: int = 0


def _known_provider_ids_from_config(config: Any | None) -> set[str] | None:
    """Collect configured provider ids as a set for suffix parsing.

    Returns ``None`` when the config is unavailable or malformed; a
    ``None`` return signals that the suffix parser should not strip
    anything (matching the ``parse_model_provider`` contract).
    """
    if config is None:
        return None
    providers_cfg_raw = getattr(config, "providers", None)
    providers_cfg = cast("dict[str, Any] | None", providers_cfg_raw)
    if not isinstance(providers_cfg, dict) or not providers_cfg:
        return None
    return {str(pid) for pid in providers_cfg}


def _normalize_dashboard_model_row(
    row: dict[str, Any],
    *,
    known_providers: set[str] | None,
) -> dict[str, Any]:
    """Normalize a model row so dashboard joins use canonical unsuffixed IDs.

    Provider-suffixed public ids (``minimax-m3/opencode-go``) get
    decomposed into ``base_model_id`` + ``provider_id``; ``model_id``
    stays the public literal because that is what operators see and
    what the detail-page URL points at.  Rows already carrying a
    ``base_model_id`` keep it when it differs from the literal
    ``model_id`` (i.e. it's an unsuffixed canonical id); otherwise the
    helper falls back to parsing ``model_id`` itself.

    Always returns a shallow copy so the caller can rely on the row
    being safe to mutate.  Writes:

    * ``base_model_id`` — canonical unsuffixed id
    * ``provider_id`` — explicit provider id or empty string
    * ``_model_info_lookup_id`` — string the renderer should use when
      looking up canonical model-info summaries
    * ``_model_id_was_suffixed`` — ``True`` when the input
      ``model_id`` carried a provider suffix we had to split
    """
    from eggpool.routing.provider import parse_model_provider  # local import

    out = dict(row)
    raw_model_id = str(out.get("model_id") or "")
    raw_base_id = str(out.get("base_model_id") or "")
    raw_provider_id = str(out.get("provider_id") or "")
    parsed_base, parsed_provider = parse_model_provider(
        raw_model_id, known_providers=known_providers
    )
    has_parsed_suffix = parsed_provider is not None and parsed_provider != ""
    if raw_base_id and (
        not raw_model_id or raw_base_id != raw_model_id or not has_parsed_suffix
    ):
        canonical_base = raw_base_id
    elif has_parsed_suffix:
        canonical_base = parsed_base
    else:
        canonical_base = raw_base_id or raw_model_id
    provider_id = raw_provider_id or parsed_provider or ""
    out["base_model_id"] = canonical_base
    out["provider_id"] = provider_id
    out["_model_info_lookup_id"] = canonical_base
    out["_model_id_was_suffixed"] = bool(has_parsed_suffix)
    return out


def _build_request_shaping_summary(
    config: AppConfig,
    *,
    routing_runtime: dict[str, Any] | None = None,
    cache_observability: dict[str, Any] | None = None,
    canonical_request_segmentation: dict[str, Any] | None = None,
    cache_stability: dict[str, Any] | None = None,
    period: str = "24h",
) -> dict[str, Any]:
    cache_observability = cache_observability or {}
    canonical_request_segmentation = canonical_request_segmentation or {}
    cache_stability = cache_stability or {}
    routing_runtime = routing_runtime or {}

    cache_status = cast("dict[str, Any]", cache_observability.get("by_status") or {})
    reported = int(cache_status.get("reported", 0) or 0)
    not_reported = int(cache_status.get("not_reported", 0) or 0)
    unknown = int(cache_status.get("unknown_format", 0) or 0)
    known_total = reported + not_reported + unknown
    cache_reported_rate = (reported / known_total) if known_total > 0 else None

    guardrails = cast("dict[str, Any]", routing_runtime.get("guardrails") or {})
    segmentation_by_status = cast(
        "dict[str, Any]",
        canonical_request_segmentation.get("by_status") or {},
    )

    segmentation_not_collected = int(
        segmentation_by_status.get("not_collected", 0) or 0
    )
    segmentation_empty_request = int(
        segmentation_by_status.get("empty_request", 0) or 0
    )
    segmentation_parse_failure = int(
        segmentation_by_status.get("parse_failure", 0) or 0
    )

    return {
        "period": period,
        "mode": {
            "routing": str(
                guardrails.get("routing_cache_compression_mode", "reporting_only")
            ),
        },
        "cache": {
            "cache_counter_reported_rate": cache_reported_rate,
            "cache_counter_reported_rows": reported,
            "cache_counter_known_rows": known_total,
            "cached_input_tokens": int(
                cache_observability.get("total_cached_input_tokens", 0) or 0
            ),
            "cache_read_tokens": int(
                cache_observability.get("total_cache_read_input_tokens", 0) or 0
            ),
            "cache_write_tokens": int(
                cache_observability.get("total_cache_creation_input_tokens", 0) or 0
            ),
            "native_cache_observed_requests": int(
                cache_stability.get("transcoded_request_count", 0) or 0
            ),
        },
        "segmentation": {
            "requests_segmented": int(segmentation_by_status.get("segmented", 0) or 0),
            "requests_not_collected": segmentation_not_collected,
            "requests_empty_request": segmentation_empty_request,
            "requests_parse_failure": segmentation_parse_failure,
            "protected_requests": int(
                canonical_request_segmentation.get("protected_requests", 0) or 0
            ),
            "compressible_candidate_requests": int(
                canonical_request_segmentation.get("compressible_candidate_requests", 0)
                or 0
            ),
        },
        "guardrails": {
            "routing_uses_cache_metrics": bool(
                guardrails.get("routing_uses_cache_metrics", False)
            ),
            "routing_uses_stable_prefix_hash": bool(
                guardrails.get("routing_uses_stable_prefix_hash", False)
            ),
        },
    }


async def _get_model_info_summary_state(
    model_info_service: Any,
    *,
    model_ids: Iterable[str] | None = None,
) -> ModelInfoDashboardState:
    """Fetch compact model-info summaries with diagnostic state.

    Returns a dataclass carrying the summaries plus a degraded-state
    signal.  The dashboard renderer uses ``degraded_reason`` to decide
    whether to show a degraded-state notice above the table.
    """
    if model_info_service is None:
        logger.warning(
            "Model-info dashboard summary unavailable: "
            "app.state.model_info is not attached"
        )
        return ModelInfoDashboardState(
            summaries={},
            available=False,
            degraded_reason="service_unattached",
            summary_count=0,
        )
    try:
        raw_map = await model_info_service.get_summary_map(model_ids)
    except Exception as exc:
        logger.exception(
            "Failed to fetch model_info summary map for dashboard models page"
        )
        return ModelInfoDashboardState(
            summaries={},
            available=True,
            degraded_reason="fetch_error",
            error_class=type(exc).__name__,
            summary_count=0,
        )
    compact = {
        mid: compact_model_info_summary(info, display_status=False)
        for mid, info in raw_map.items()
    }
    logger.debug(
        "Model-info dashboard summary map fetched: %d canonical rows",
        len(compact),
    )
    return ModelInfoDashboardState(
        summaries=compact,
        available=True,
        summary_count=len(compact),
    )


async def _get_model_info_summary_map(  # pyright: ignore[reportUnusedFunction]
    model_info_service: Any,
    *,
    model_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch compact model-info summaries keyed by model_id.

    Thin wrapper around ``_get_model_info_summary_state`` that returns
    only the summaries dict for backwards compatibility.
    """
    state = await _get_model_info_summary_state(model_info_service, model_ids=model_ids)
    return state.summaries


DEFAULT_REFRESH_S = 15

# Heatmap TimeRange shows the trailing window.  Capped at 180 days
# (~6 months) so the grid stays bounded and at ``retain_request_stats_days``
# so it never scans rows the retention job will purge.  Recomputed per
# request so the dashboard cache key naturally advances with wall-clock time.
_HEATMAP_MAX_DAYS = 180
_VALID_BUCKETS = frozenset({"hour", "day"})
_VALID_GROUP_BY = frozenset({"provider", "model", "provider_model", "account"})


async def _get_disabled_account_count(request: Request, show_disabled: bool) -> int:
    """Return hidden disabled-account count for pages with that toggle."""
    if not show_disabled:
        return 0
    return await fetch_disabled_account_count(request.app.state.stats_db)


def _normalize_bucket(bucket: str | None, period: str | None = None) -> str:
    """Return a supported dashboard bucket, expanding ``"auto"`` via the period.

    Preserved as a thin shim around
    :func:`eggpool.dashboard.timeseries_buckets.resolve_bucket` so the
    existing call sites and their unit tests keep working.  Unknown
    buckets (including ``"auto"`` and ``None``) fall back to the
    period-aware default rather than always defaulting to ``"hour"`` —
    that lets a 30-day window render at daily granularity when the
    client omits the bucket parameter.
    """
    return resolve_bucket(bucket, period)


def _normalize_group_by(group_by: str) -> str:
    """Return a supported grouped-timeseries dimension."""
    return group_by if group_by in _VALID_GROUP_BY else "provider_model"


def _aggregate_series_from_grouped(
    grouped: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reuse grouped bucket totals for the legacy aggregate table.

    The dedicated timeseries page previously issued both a grouped query
    and a second flat query, even though the grouped contract already
    carries lossless ``bucket_totals``.  Keeping the conversion here makes
    the page's aggregate table a zero-cost view of the query it already
    needs for the chart and detail table.
    """
    totals = grouped.get("bucket_totals")
    if not isinstance(totals, list):
        return []
    result: list[dict[str, Any]] = []
    for row_obj in cast("list[object]", totals):
        if isinstance(row_obj, dict):
            row = cast("dict[str, Any]", row_obj)
            result.append(dict(row))
    return result


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    """Clamp an integer query value to an inclusive range."""
    return max(minimum, min(value, maximum))


def _heatmap_time_range(retain_days: int) -> TimeRange:
    """Return a TimeRange for the heatmap bounded by retention + max."""
    days = max(1, min(_HEATMAP_MAX_DAYS, retain_days))
    now = datetime.now(UTC)
    return TimeRange(
        start=now - timedelta(days=days),
        end=now,
        label=f"{days}d",
    )


def _get_dashboard_config(request: Request) -> Any:
    """Look up the dashboard config from app state, raising ConfigError if disabled."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise ConfigError("config not loaded")
    if not config.dashboard.enabled:
        raise ConfigError("dashboard disabled")
    return config.dashboard


def _get_update_info(request: Request) -> Any | None:
    """Return the latest :class:`UpdateInfo` snapshot or ``None``.

    Returns ``None`` when no checker is attached — the renderer
    interprets that as "do not render any indicator", matching the
    dashboard contract.
    """
    checker = getattr(request.app.state, "update_checker", None)
    if checker is None:
        return None
    try:
        return checker.snapshot()
    except Exception:
        return None


def _get_theme_data(
    request: Request, theme_override: str | None = None
) -> tuple[str, list[str], str, list[str]]:
    """Load theme CSS, heatmap colors, current theme name, and available themes.

    Returns (css_variables, heatmap_colors, current_theme_name, available_themes).
    """
    config = getattr(request.app.state, "config", None)
    default_colors = ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]
    if config is None:
        return "", default_colors, "default", []

    themes_dir = config.dashboard.themes_dir
    # Use query param override if provided, else config default
    theme_name = theme_override or config.dashboard.theme
    available = get_available_themes(themes_dir)
    if theme_name not in available:
        theme_name = config.dashboard.theme
    if theme_name not in available:
        theme_name = "default"
    theme = get_theme(theme_name, themes_dir)
    return theme.to_css_variables(), theme.heatmap_colors(), theme_name, available


def _collect_account_options(request: Request) -> list[str]:
    """Collect configured account names for the timeseries filter dropdown.

    Returns an empty list when no config is loaded so the renderer can
    still emit a valid (any-account) dropdown.  Order matches the
    provider-priority order from ``config.all_accounts()`` so the
    dropdown mirrors the routing tier order operators see elsewhere.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return []
    all_accounts = getattr(config, "all_accounts", None)
    if not callable(all_accounts):
        return []
    accounts = cast("list[Any]", cast("Any", all_accounts)())
    names: list[str] = []
    for acct in accounts:
        name = getattr(acct, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _collect_model_options(request: Request) -> list[str]:
    """Collect exposed model IDs for the timeseries filter dropdown.

    Pulls the same model list the public ``/v1/models`` endpoint serves
    so the dropdown options track what the catalog currently knows
    about, including provider-suffixed IDs when ``collapse_models`` is
    false (the default).  Falls back to an empty list when no catalog
    is attached yet — e.g. early in startup before the first refresh.
    """
    catalog = _get_catalog(request)
    if catalog is None:
        return []
    try:
        models = catalog.get_models_for_exposure()
    except Exception:
        return []
    seen: set[str] = set()
    options: list[str] = []
    for entry in models:
        model_id = str(entry.get("model_id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        options.append(model_id)
    return options


async def handle_overview(
    request: Request,
    period: str | None = "24h",
    theme: str | None = None,
    show_disabled: bool = False,
) -> Response:
    """Render the overview page.

    ``show_disabled`` toggles whether disabled (soft-deleted) accounts
    appear in the Account breakdown table. Defaults to False so the
    page matches the operator's mental model after ``eggpool logout``:
    only enabled accounts are visible by default. Pass
    ``?show_disabled=1`` to opt in to the historical view.
    """
    _start = time.perf_counter()
    dashboard_config = _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    # The renderer always displays the six-month calendar. Query that full
    # window rather than the shorter raw-request retention setting, otherwise
    # the overview renders empty-looking gaps for activity still available in
    # usage_rollups.
    heatmap_range = _heatmap_time_range(_HEATMAP_MAX_DAYS)
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    model_info_service = _get_model_info(request)

    # Always fetch the disabled count so the Account breakdown empty
    # state can offer a one-click opt-in even when no rows are
    # currently visible. Cheap one-row aggregate; safe on every render.
    disabled_count = await _await_dashboard_stage(
        telemetry,
        "overview",
        "disabled_count",
        _get_disabled_account_count(request, show_disabled),
    )

    # Fan out the independent stat reads concurrently.  The single
    # shared connection lock serializes per-query execution, so without
    # this the page load is the sum of ten sequential round trips; with
    # it the load is bounded by the slowest query instead.
    (
        accounts,
        models,
        events,
        bandwidth_daily,
        summary,
        ping_summary,
        ip_stats,
        attempt_stats,
        operational_summary,
        pending_health,
        cache_observability,
        model_info_state,
    ) = await asyncio.gather(
        _await_dashboard_stage(
            telemetry,
            "overview",
            "account_stats",
            stats.get_account_stats(
                time_range, include_disabled=show_disabled, use_cache=True
            ),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "model_stats",
            stats.get_model_stats(time_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "recent_events",
            stats.get_recent_events(limit=10),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "bandwidth_daily",
            stats.get_bandwidth_timeseries(heatmap_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "summary",
            stats.get_summary(time_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "ping_summary",
            stats.get_ping_summary(time_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "ip_stats",
            stats.get_ip_stats(time_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "attempt_stats",
            stats.get_attempt_stats(time_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "operational_summary",
            stats.get_operational_event_summary(time_range, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "pending_health",
            stats.get_pending_health_snapshot(use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "cache_observability",
            stats.get_cache_observability(time_range.label, use_cache=True),
        ),
        _await_dashboard_stage(
            telemetry,
            "overview",
            "model_info_summaries",
            _get_model_info_summary_state(model_info_service),
        ),
    )
    # ``get_dashboard_overview`` is derived from already-fetched overview
    # inputs. Passing them through avoids a second summary/cache query after
    # the parallel stats batch completes.
    overview = await _await_dashboard_stage(
        telemetry,
        "overview",
        "dashboard_overview",
        stats.get_dashboard_overview(
            time_range,
            account_stats=accounts,
            summary=summary,
            cache_observability=cache_observability,
            use_cache=True,
        ),
    )

    from eggpool.metrics.thinking import get_counter

    thinking_stats = await get_counter().snapshot()

    refresh_s = dashboard_config.refresh_interval_s
    theme_css, heatmap_colors, current_theme, available = _get_theme_data(
        request, theme
    )
    request_shaping_summary = _build_request_shaping_summary(
        request.app.state.config,
        cache_observability=cache_observability,
        period=time_range.label,
    )
    enabled_count = sum(1 for a in accounts if a.get("account_enabled"))
    _render_start = time.perf_counter()
    html = render_overview(
        overview=overview,
        accounts=accounts,
        period=time_range.label,
        refresh_interval_s=refresh_s,
        bandwidth_daily=bandwidth_daily,
        ping_summary=ping_summary,
        models=models if models is not None else [],
        events=events,
        theme_css=theme_css,
        heatmap_colors=heatmap_colors,
        available_themes=available,
        current_theme=current_theme,
        ip_stats=ip_stats,
        timeseries=[],
        pending_health=pending_health,
        attempt_stats=attempt_stats,
        operational_summary=operational_summary,
        update_info=_get_update_info(request),
        show_disabled=show_disabled,
        disabled_count=disabled_count,
        enabled_count=enabled_count,
        thinking_stats=thinking_stats,
        request_shaping_summary=request_shaping_summary,
        progressive_timeseries=True,
        cache_observability=cache_observability,
        model_info_map=model_info_state.summaries,
    )
    if telemetry is not None:
        telemetry.record_stage(
            "overview",
            "render_html",
            (time.perf_counter() - _render_start) * 1000,
        )
    _elapsed_ms = (time.perf_counter() - _start) * 1000
    if telemetry is not None:
        telemetry.record_render("overview", _elapsed_ms)
    return HTMLResponse(content=html)


async def handle_accounts(
    request: Request,
    period: str | None = "24h",
    theme: str | None = None,
    show_disabled: bool = False,
) -> Response:
    """Render the accounts page.

    ``show_disabled`` defaults to False so the page matches the
    operator's mental model after ``eggpool logout``: disabled rows
    are hidden by default. Pass ``?show_disabled=1`` to opt in to the
    historical view (soft-deleted accounts still appear with
    ``Enabled = no``).  When the operator filters disabled rows out and
    the empty result set hides disabled tombstones, the renderer shows
    a one-click "N disabled — show them?" hint instead of the generic
    "No accounts configured." empty state.
    """
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)

    # Always fetch the disabled count so the empty state can offer the
    # one-click opt-in even when no rows are currently visible.
    disabled_count = await _get_disabled_account_count(request, show_disabled)

    accounts = await stats.get_account_stats(
        time_range, include_disabled=show_disabled, use_cache=True
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_accounts(
            accounts,
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            show_disabled=show_disabled,
            disabled_count=disabled_count,
        )
    )


async def handle_models(
    request: Request,
    period: str | None = "24h",
    account: str | None = None,
    theme: str | None = None,
    info_status: str | None = None,
    availability: str | None = None,
    used: str | None = None,
) -> Response:
    """Render the models page.

    The page is catalog-complete: every model known to the catalog
    cache is listed even if it has zero requests in the requested
    time window.  Usage stats from ``stats.get_model_stats`` are
    merged onto the catalog rows so the operator sees activity
    columns alongside model-info pills for every model.
    """
    _start = time.perf_counter()
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    model_info_service = _get_model_info(request)
    catalog = _get_catalog(request)
    app_config = getattr(request.app.state, "config", None)
    collapse_models = _read_collapse_models(app_config)
    known_providers = _known_provider_ids_from_config(app_config)

    catalog_state = await _get_catalog_rows(
        catalog, account=account or None, config=app_config
    )
    # Normalize rows eagerly so the canonical lookup key
    # (``_model_info_lookup_id``) is set before any join work happens.
    catalog_rows: list[dict[str, Any]] = [
        _normalize_dashboard_model_row(row, known_providers=known_providers)
        for row in catalog_state.rows
    ]
    if (
        catalog_state.available
        and catalog_state.degraded_reason is None
        and not catalog_rows
        and catalog is not None
    ):
        # Catalog was reachable but produced no rows.  This is a
        # likely join-failure signal: surface it as a diagnostic.
        logger.warning(
            "Dashboard catalog returned no rows despite an attached catalog "
            "service — model-info join cannot match."
        )

    models, model_info_state = cast(
        "tuple[list[dict[str, Any]] | None, ModelInfoDashboardState]",
        await asyncio.gather(
            stats.get_model_stats(
                time_range, account_name=account or None, use_cache=True
            ),
            _get_model_info_summary_state(
                model_info_service,
                # Pass ``None`` so the canonical-table summary fetch
                # returns every available summary; the join side then
                # matches against the rendered dashboard rows.  The
                # canonical table is small enough (tens/hundreds of
                # rows) that this avoids under-requesting.
                model_ids=None,
            ),
        ),
    )
    normalized_stats_rows: list[dict[str, Any]] = []
    for raw_row in models or []:
        normalized_stats_rows.append(
            _normalize_dashboard_model_row(raw_row, known_providers=known_providers)
        )
    model_info_summary_map = model_info_state.summaries
    merged_rows = _merge_models_with_catalog(
        normalized_stats_rows,
        catalog_rows,
        collapse_models=collapse_models,
    )
    filtered_rows = _apply_model_filters(
        merged_rows,
        info_status=info_status,
        availability=availability,
        used=used,
        model_info_map=model_info_summary_map,
    )
    # Compute join diagnostics over the post-filter rows.  The
    # renderer uses these to surface a degraded-state notice when
    # the model-info summaries exist but none of the rendered rows
    # matched them.
    matched, unmatched_sample = _compute_model_info_join_stats(
        filtered_rows, model_info_summary_map
    )
    model_info_state = ModelInfoDashboardState(
        summaries=model_info_state.summaries,
        available=model_info_state.available,
        degraded_reason=model_info_state.degraded_reason,
        error_class=model_info_state.error_class,
        summary_count=model_info_state.summary_count,
        matched_row_count=matched,
        unmatched_row_count=max(0, len(filtered_rows) - matched),
        unmatched_sample=unmatched_sample,
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    account_options = _collect_account_options(request)
    response = HTMLResponse(
        content=render_models(
            filtered_rows,
            account_filter=account or "",
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            model_info_map=model_info_summary_map,
            info_status_filter=info_status or "",
            availability_filter=availability or "",
            used_filter=used or "",
            has_filters=any(v is not None for v in (info_status, availability, used)),
            account_options=account_options,
            model_info_state=model_info_state,
        )
    )
    _elapsed_ms = (time.perf_counter() - _start) * 1000
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    if telemetry is not None:
        telemetry.record_render("models", _elapsed_ms)
    return response


async def _get_catalog_rows(
    catalog: Any,
    *,
    account: str | None = None,
    config: Any | None = None,
) -> CatalogRowsState:
    """Build sparse rows for every catalog model so the page is
    catalog-complete.

    Row shape follows ``models.collapse_models``:

    * When ``collapse_models`` is false (default), the page lists one
      row per ``(model_id, provider_id)`` pair — i.e. provider-scoped
      suffixed rows.  This matches the shape of
      ``stats.get_model_stats`` rows so the merge is straightforward.
    * When ``collapse_models`` is true, the page lists one row per
      unsuffixed model with a ``providers`` list containing every
      contributing provider id.  This mirrors what
      ``/v1/models`` exposes in collapsed mode.

    Returns a :class:`CatalogRowsState`.  When the catalog is
    unavailable the state carries ``available=False`` and zero rows;
    the page must still render with whatever stats rows the caller
    already has.  When the catalog service is attached but row
    construction raises, the helper logs and surfaces
    ``degraded_reason="fetch_error"`` so the route can render a
    diagnostic instead of silently dropping rows.

    Each row carries:

    * ``base_model_id`` — the unsuffixed canonical key (same as
      ``model_id`` for collapsed rows; identical to ``model_id`` for
      suffixed rows when the catalog is provider-scoped).
    * ``providers`` — the list of contributing provider IDs (single
      element for provider-scoped rows; the full union for collapsed
      rows).
    * ``available`` — derived flag (``True`` when the entry has a
      resolved protocol; ``False`` when the protocol is unresolved).
    * ``catalog_status`` — short string pill (``"available"``,
      ``"unavailable"``, or ``"configured"``).
    * ``routing_priority`` — pulled from ``config.providers`` when
      ``config`` is supplied; ``None`` otherwise. Collapsed rows
      surface the max priority across contributing providers.
    * ``routing_priority_max`` — collapsed-row convenience: max
      ``routing_priority`` across the contributing providers.
    * ``protocol``, ``display_name`` — surfaced from the provider
      entry so the dashboard can render provider-specific facts.
    """
    if catalog is None:
        return CatalogRowsState(
            rows=[],
            available=False,
            degraded_reason="service_unattached",
            row_count=0,
        )
    # Build a provider_id → routing_priority map once when the config
    # is available so per-row lookup is a cheap dict read.
    priority_by_provider = _build_provider_priority_map(config)
    collapse_models = _read_collapse_models(config)
    if collapse_models:
        return _get_collapsed_catalog_rows(
            catalog,
            priority_by_provider=priority_by_provider,
            account=account,
        )
    return _get_provider_scoped_catalog_rows(
        catalog,
        priority_by_provider=priority_by_provider,
        account=account,
    )


def _sparse_row_template(
    *,
    model_id: str,
    base_model_id: str,
    provider_id: str,
    providers: list[str],
    available: bool,
    catalog_status: str,
    routing_priority: int | None,
    routing_priority_max: int | None,
    protocol: str | None,
    display_name: str | None,
) -> dict[str, Any]:
    """Build a catalog-complete sparse row with zero activity fields.

    Used by both the provider-scoped and collapsed builders so the
    row shape stays identical regardless of which catalog path was
    taken.
    """
    return {
        "model_id": model_id,
        "base_model_id": base_model_id,
        "provider_id": provider_id,
        "providers": list(providers),
        "available": available,
        "catalog_status": catalog_status,
        "routing_priority": routing_priority,
        "routing_priority_max": routing_priority_max,
        "protocol": protocol,
        "display_name": display_name,
        "request_count": 0,
        "cost_microdollars": 0,
        "avg_latency_ms": 0.0,
        "avg_ttft_ms": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tokens_per_second": 0.0,
        "error_count": 0,
        "exact_count": 0,
        "derived_count": 0,
        "partial_count": 0,
        "estimated_count": 0,
        "unknown_count": 0,
        "provider_reported_count": 0,
        "estimated_cost_fraction": None,
        "cache_read_ratio": None,
        "cache_write_ratio": None,
        "reasoning_output_ratio": None,
        "avg_cost_per_request": None,
        "avg_cost_per_1k_tokens": None,
        "_sparse": True,
    }


def _build_provider_priority_map(config: Any) -> dict[str, int]:
    """Return ``provider_id -> routing_priority`` from ``config.providers``.

    Defensive against missing or malformed config: returns an empty
    map when ``config`` is ``None`` or the ``providers`` attribute is
    unavailable.
    """
    priority_by_provider: dict[str, int] = {}
    if config is None:
        return priority_by_provider
    try:
        providers_cfg_raw = getattr(config, "providers", None)
        providers_cfg = cast("dict[str, Any] | None", providers_cfg_raw)
    except Exception:
        providers_cfg = None
    if providers_cfg is None:
        return priority_by_provider
    items: Any = []
    try:
        items = providers_cfg.items()
    except Exception:
        items = []
    for pid_key, pcfg in items:
        pri = getattr(pcfg, "routing_priority", None)
        if isinstance(pri, int):
            priority_by_provider[str(pid_key)] = pri
    return priority_by_provider


def _read_collapse_models(config: Any) -> bool:
    """Read ``config.models.collapse_models`` defensively.

    Returns ``False`` when ``config`` is unavailable, the ``models``
    attribute is missing, or the value isn't a boolean — matching
    the default behavior the dashboard has shipped since
    ``models.collapse_models`` was introduced.
    """
    if config is None:
        return False
    models_cfg = getattr(config, "models", None)
    if models_cfg is None:
        return False
    val = getattr(models_cfg, "collapse_models", None)
    return val if isinstance(val, bool) else False


def _account_provider_for_supported_model(
    cache: Any,
    *,
    model_id: str,
    account: str,
) -> str | None:
    """Return the account's provider when it supports ``model_id``.

    The catalog cache tracks model support by account and separately
    tracks each account's provider.  Account-filtered provider-scoped
    rows need both facts: model-level support alone would let sibling
    provider rows leak into an account-specific view.
    """
    try:
        supporting: frozenset[str] = cache.get_supporting_accounts(model_id)
    except Exception:
        return None
    if account not in supporting:
        return None
    try:
        provider_id = cache.get_provider_for_account(account)
    except Exception:
        return None
    return str(provider_id) if provider_id else None


def _get_provider_scoped_catalog_rows(
    catalog: Any,
    *,
    priority_by_provider: dict[str, int],
    account: str | None,
) -> CatalogRowsState:
    """One row per ``(model_id, provider_id)`` pair.

    Used when ``collapse_models`` is false (the default). Iterates
    ``catalog.cache.get_provider_model_entries()`` so each suffixed
    catalog exposure becomes a distinct dashboard row.

    Returns a :class:`CatalogRowsState`.  When ``get_provider_model_entries``
    raises, the exception is logged with its full traceback and the
    state carries ``degraded_reason="fetch_error"`` so the route can
    surface a diagnostic instead of silently emitting an empty table.
    """
    try:
        provider_entries = catalog.cache.get_provider_model_entries()
    except Exception as exc:
        logger.exception(
            "Failed to enumerate provider-scoped catalog rows: %s",
            type(exc).__name__,
        )
        return CatalogRowsState(
            rows=[],
            available=True,
            degraded_reason="fetch_error",
            error_class=type(exc).__name__,
            row_count=0,
        )
    rows: list[dict[str, Any]] = []
    for (model_id, provider_id), entry in provider_entries.items():
        if account:
            account_provider = _account_provider_for_supported_model(
                catalog.cache,
                model_id=model_id,
                account=account,
            )
            if account_provider != provider_id:
                continue
        protocol_str, display_name = _entry_protocol_and_name(entry)
        available = bool(protocol_str)
        catalog_status = "available" if available else "unavailable"
        routing_priority = priority_by_provider.get(str(provider_id))
        rows.append(
            _sparse_row_template(
                model_id=model_id,
                base_model_id=model_id,
                provider_id=provider_id,
                providers=[provider_id],
                available=available,
                catalog_status=catalog_status,
                routing_priority=routing_priority,
                routing_priority_max=routing_priority,
                protocol=protocol_str,
                display_name=display_name,
            )
        )
    return CatalogRowsState(
        rows=rows,
        available=True,
        row_count=len(rows),
    )


def _get_collapsed_catalog_rows(
    catalog: Any,
    *,
    priority_by_provider: dict[str, int],
    account: str | None,
) -> CatalogRowsState:
    """One row per unsuffixed model with contributing ``providers``.

    Used when ``collapse_models`` is true. Calls
    ``catalog.get_models_for_exposure()`` which already returns the
    collapsed view from the catalog layer.  ``provider_id`` is set
    to the first contributing provider (sorted) so the merge with
    stats rows keyed by ``(model_id, provider_id)`` still works for
    entries that report a specific provider.

    Rows where every contributing provider is unresolved
    (``protocol=None``) still appear, flagged unavailable, so the
    operator can see collapsed entries that exist in the catalog but
    cannot currently route.  When the catalog layer excludes them
    entirely, this helper naturally inherits that filter.

    Returns a :class:`CatalogRowsState`.  When
    ``catalog.get_models_for_exposure`` raises, the exception is
    logged with its full traceback and the state carries
    ``degraded_reason="fetch_error"`` so the route can surface a
    diagnostic instead of silently emitting an empty table.
    """
    try:
        entries = catalog.get_models_for_exposure()
    except Exception as exc:
        logger.exception(
            "Failed to enumerate collapsed catalog rows: %s",
            type(exc).__name__,
        )
        return CatalogRowsState(
            rows=[],
            available=True,
            degraded_reason="fetch_error",
            error_class=type(exc).__name__,
            row_count=0,
        )
    rows: list[dict[str, Any]] = []
    for entry in entries:
        entry_dict = cast("dict[str, Any] | None", entry)
        if not isinstance(entry_dict, dict):
            continue
        model_id = str(entry_dict.get("model_id", "") or "")
        if not model_id:
            continue
        account_provider: str | None = None
        if account:
            account_provider = _account_provider_for_supported_model(
                catalog.cache,
                model_id=model_id,
                account=account,
            )
            if account_provider is None:
                continue
        providers_raw: Any = entry_dict.get("providers")
        if isinstance(providers_raw, list):
            providers = [
                str(p)
                for p in cast("list[Any]", providers_raw)
                if isinstance(p, str) and p
            ]
        else:
            providers = []
        if account_provider is not None:
            if providers and account_provider not in providers:
                continue
            providers = [account_provider]
        # Pick a primary provider for stats-key matching.  Falls back
        # to the empty string when nothing contributes; the merge
        # logic uses ``catalog_by_id`` for that case.
        primary_provider = providers[0] if providers else ""
        provider_entry = None
        if account_provider is not None:
            try:
                provider_entry = catalog.cache.get_provider_model_entry(
                    model_id,
                    account_provider,
                )
            except Exception:
                provider_entry = None
        protocol_str, display_name = _entry_protocol_and_name(
            provider_entry or entry_dict
        )
        # Collapsed entry is "available" only when at least one
        # contributing provider resolves the protocol.
        available = bool(protocol_str)
        catalog_status = "available" if available else "unavailable"
        priorities: list[int] = [
            pri
            for pid in providers
            if (pri := priority_by_provider.get(pid)) is not None
        ]
        routing_priority_max = max(priorities) if priorities else None
        rows.append(
            _sparse_row_template(
                model_id=model_id,
                base_model_id=model_id,
                provider_id=primary_provider,
                providers=providers,
                available=available,
                catalog_status=catalog_status,
                routing_priority=routing_priority_max,
                routing_priority_max=routing_priority_max,
                protocol=protocol_str,
                display_name=display_name,
            )
        )
    return CatalogRowsState(
        rows=rows,
        available=True,
        row_count=len(rows),
    )


def _entry_protocol_and_name(
    entry: Any,
) -> tuple[str | None, str | None]:
    """Extract ``(protocol, display_name)`` from a catalog entry.

    Returns ``(None, None)`` for malformed entries.  ``protocol`` is
    a string only when the entry has a resolved protocol; the
    dashboard treats ``protocol=None`` as unavailable.
    """
    entry_dict = cast("dict[str, Any] | None", entry)
    if not isinstance(entry_dict, dict):
        return None, None
    protocol_raw: Any = entry_dict.get("protocol")
    protocol_str: str | None = protocol_raw if isinstance(protocol_raw, str) else None
    display_name_raw: Any = entry_dict.get("display_name")
    display_name: str | None = (
        display_name_raw if isinstance(display_name_raw, str) else None
    )
    return protocol_str, display_name


def _apply_model_filters(
    rows: list[dict[str, Any]],
    *,
    info_status: str | None = None,
    availability: str | None = None,
    used: str | None = None,
    model_info_map: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply post-merge query filters to the merged model row list.

    Filters are applied in order; each narrows the result set.
    Unknown or ``None`` filter values are ignored (no-op).

    * ``used``: ``"used"`` keeps rows with ``request_count > 0``;
      ``"unused"`` keeps rows with ``request_count == 0``.
    * ``info_status``: matches the ``status`` field in the
      ``model_info_map`` entry for each model.  Looks up by
      ``base_model_id`` first (the canonical unsuffixed key) and
      falls back to the literal ``model_id`` for legacy rows.
    * ``availability``: ``"available"`` keeps models present in the
      catalog (``_in_catalog`` flag); ``"unavailable"`` keeps the rest.
    """
    if not rows:
        return rows
    mi_map = model_info_map or {}
    result = rows
    if used == "used":
        result = [r for r in result if int(r.get("request_count", 0) or 0) > 0]
    elif used == "unused":
        result = [r for r in result if int(r.get("request_count", 0) or 0) == 0]
    if info_status is not None:
        normalized = normalize_model_info_status_filter(info_status)

        def _matches(row: dict[str, Any]) -> bool:
            lookup_id = str(row.get("_model_info_lookup_id") or "")
            base_id = str(row.get("base_model_id") or "")
            literal = str(row.get("model_id") or "")
            mi_entry = (
                mi_map.get(lookup_id) or mi_map.get(base_id) or mi_map.get(literal)
            )
            if mi_entry is None:
                return False
            entry_status = str(mi_entry.get("status") or "")
            return normalize_model_info_status_filter(entry_status) == normalized

        result = [r for r in result if _matches(r)]
    if availability == "available":
        result = [r for r in result if r.get("_in_catalog")]
    elif availability == "unavailable":
        result = [r for r in result if not r.get("_in_catalog")]
    return result


def _model_info_lookup_keys(row: dict[str, Any]) -> tuple[str, ...]:
    """Return the ordered lookup keys to try against the summary map."""
    return (
        str(row.get("_model_info_lookup_id") or ""),
        str(row.get("base_model_id") or ""),
        str(row.get("model_id") or ""),
    )


def _compute_model_info_join_stats(
    rows: list[dict[str, Any]],
    summary_map: dict[str, Any],
) -> tuple[int, tuple[dict[str, Any], ...]]:
    """Count rows that match the canonical summary map.

    Returns ``(matched_count, unmatched_sample)`` where
    ``unmatched_sample`` carries at most five rows of diagnostic
    info (``model_id``, ``base_model_id``, ``_model_info_lookup_id``,
    ``provider_id``) for the operator to inspect.
    """
    if not rows:
        return 0, ()
    matched = 0
    unmatched: list[dict[str, Any]] = []
    for row in rows:
        keys = _model_info_lookup_keys(row)
        if any(key and key in summary_map for key in keys):
            matched += 1
            continue
        unmatched.append(
            {
                "model_id": str(row.get("model_id") or ""),
                "base_model_id": str(row.get("base_model_id") or ""),
                "lookup_id": str(row.get("_model_info_lookup_id") or ""),
                "provider_id": str(row.get("provider_id") or ""),
            }
        )
    return matched, tuple(unmatched[:5])


def _model_row_key(row: dict[str, Any], *, collapse_models: bool) -> tuple[str, str]:
    """Compute the dedupe key for a merge row.

    In provider-scoped mode the key is ``(model_id, provider_id)`` so
    sibling provider exposures for the same base model remain
    distinct.  In collapsed mode the key collapses to
    ``(model_id, "")`` so one row per base model wins.
    """
    model_id = str(row.get("model_id") or "")
    if collapse_models:
        return (model_id, "")
    provider_id = str(row.get("provider_id") or "")
    return (model_id, provider_id)


def _merge_models_with_catalog(
    stats_rows: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
    *,
    collapse_models: bool = False,
) -> list[dict[str, Any]]:
    """Merge usage stats onto catalog rows.

    Stats rows win on numeric columns (they reflect real activity);
    catalog rows are preserved when stats has no entry.  The merged
    list is sorted by request count (descending) so active models
    appear first; sparse catalog rows fall to the bottom.

    Dedup behavior depends on ``collapse_models``:

    * ``collapse_models=False`` (provider-scoped): keys are
      ``(model_id, provider_id)`` so an unused sibling provider for
      the same base model is not suppressed by an active provider's
      stats row.
    * ``collapse_models=True``: keys collapse to ``(model_id, "")`` so
      one row per base model wins (the ``providers`` list on the
      catalog row carries every contributing provider).

    Diagnostic fields that originate from the catalog
    (``base_model_id``, ``providers``, ``available``,
    ``catalog_status``, ``routing_priority``, ``routing_priority_max``,
    ``protocol``, ``display_name``) are preserved on stats rows that
    share the same dedupe key, so the dashboard renders
    provider/protocol facts even for active models.  Legacy stats
    rows that lack ``provider_id`` fall back to ``catalog_by_id`` for
    diagnostic fields but do not suppress provider-scoped catalog
    rows.
    """
    catalog_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    catalog_by_id: dict[str, dict[str, Any]] = {}
    for row in catalog_rows:
        key = _model_row_key(row, collapse_models=collapse_models)
        mid, _pid = key
        if not mid:
            continue
        catalog_by_id.setdefault(mid, row)
        catalog_by_key.setdefault(key, row)
    _diagnostic_keys = (
        "base_model_id",
        "providers",
        "available",
        "catalog_status",
        "routing_priority",
        "routing_priority_max",
        "protocol",
        "display_name",
    )
    merged: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for raw_row in stats_rows:
        key = _model_row_key(raw_row, collapse_models=collapse_models)
        mid, pid = key
        if not mid:
            continue
        seen_keys.add(key)
        row = dict(raw_row)
        row.pop("_sparse", None)
        row.pop("_display_name", None)
        row.pop("_providers", None)
        # Find the matching catalog row: exact key first, then fall
        # back to the id-only map for legacy stats rows that lack
        # ``provider_id`` in provider-scoped mode.
        catalog_row = catalog_by_key.get(key)
        if catalog_row is None and not collapse_models and not pid:
            catalog_row = catalog_by_id.get(mid)
        if catalog_row is not None:
            row["_in_catalog"] = True
            for k in _diagnostic_keys:
                if k in catalog_row and k not in row:
                    row[k] = catalog_row[k]
        merged.append(row)
    for raw_row in catalog_rows:
        key = _model_row_key(raw_row, collapse_models=collapse_models)
        if not key[0] or key in seen_keys:
            continue
        seen_keys.add(key)
        row = dict(raw_row)
        row["_in_catalog"] = True
        merged.append(row)
    merged.sort(
        key=lambda r: (
            -int(r.get("request_count", 0) or 0),
            r.get("model_id", ""),
            str(r.get("provider_id") or ""),
        )
    )
    return merged


async def handle_model_detail(
    request: Request,
    model_id: str,
    theme: str | None = None,
) -> Response:
    """Render the model-info detail page for a specific model."""
    _get_dashboard_config(request)
    model_info_service = _get_model_info(request)
    from urllib.parse import unquote

    from eggpool.routing.provider import parse_model_provider

    decoded_id = unquote(model_id)
    # The {model_id:path} route accepts provider-suffixed IDs like
    # ``gpt-4o/openai``.  Strip the suffix so the lookup matches the
    # unsuffixed canonical key used by the catalog and stats layer.
    config = getattr(request.app.state, "config", None)
    known_providers: set[str] | None = None
    if config is not None:
        known_providers = set(config.providers)
    lookup_id, _provider_suffix = parse_model_provider(decoded_id, known_providers)
    info = None
    info_error: str | None = None
    observations: list[dict[str, Any]] = []
    observations_error: str | None = None
    if model_info_service is not None:
        try:
            info = await model_info_service.get_summary(lookup_id)
            if info is None:
                info = await model_info_service.ensure_canonical(lookup_id)
            # Phase 5: surface per-source observations on the model
            # page so operators can see which external sources
            # actually contributed and when they last observed this
            # canonical row.
            try:
                observations = (
                    await model_info_service.repo.list_compact_observations_for_model(
                        lookup_id
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read compact observations for %s: %s",
                    lookup_id,
                    exc,
                )
                observations = []
                observations_error = type(exc).__name__
        except Exception as exc:
            logger.exception(
                "Model-info detail lookup failed for decoded_id=%r lookup_id=%r",
                decoded_id,
                lookup_id,
            )
            info_error = type(exc).__name__
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_model_detail(
            info=info,
            model_id=decoded_id,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            model_info_error=info_error,
            observations=observations,
            observations_error=observations_error,
        )
    )


async def handle_latency(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the latency breakdown page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    model_info_service = _get_model_info(request)
    provider_ttft, model_ttft, phases, model_info_state = cast(
        "_LatencyPayload",
        await asyncio.gather(
            stats.get_provider_ttft_summary(time_range, use_cache=True),
            stats.get_provider_model_ttft(time_range, use_cache=True),
            stats.get_latency_phase_breakdown(time_range, use_cache=True),
            _get_model_info_summary_state(model_info_service),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_latency(
            provider_ttft,
            model_ttft,
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            phases=phases,
            update_info=_get_update_info(request),
            model_info_map=model_info_state.summaries,
        )
    )


async def handle_reliability(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the Reliability page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    _gather_start = time.perf_counter()
    (
        attempt_stats,
        retry_distribution,
        pending_health,
        operational_summary,
        recent_operational_events,
    ) = cast(
        "_ReliabilityPayload",
        await asyncio.gather(
            stats.get_attempt_stats(time_range, use_cache=True),
            stats.get_retry_distribution(time_range, use_cache=True),
            stats.get_pending_health_snapshot(use_cache=True),
            stats.get_operational_event_summary(time_range, use_cache=True),
            stats.get_recent_operational_events(limit=25),
        ),
    )
    _gather_ms = (time.perf_counter() - _gather_start) * 1000
    if telemetry is not None:
        for name in (
            "attempt_stats",
            "retry_distribution",
            "pending_health",
            "operational_summary",
            "recent_operational_events",
        ):
            telemetry.record_stage("reliability", name, _gather_ms)
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    _render_start = time.perf_counter()
    response = HTMLResponse(
        content=render_reliability(
            period=time_range.label,
            attempt_stats=attempt_stats,
            retry_distribution=retry_distribution or [],
            pending_health=pending_health,
            operational_summary=operational_summary or [],
            recent_operational_events=recent_operational_events or [],
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )
    if telemetry is not None:
        telemetry.record_stage(
            "reliability",
            "render_html",
            (time.perf_counter() - _render_start) * 1000,
        )
    return response


async def handle_routing(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the Routing page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    model_info_service = _get_model_info(request)
    runtime_metrics = getattr(request.app.state, "runtime_metrics", None)
    app_config = getattr(request.app.state, "config", None)
    routing_config = getattr(app_config, "routing", None)
    trace_config = getattr(routing_config, "trace", None)
    trace_mode = getattr(trace_config, "mode", "off")
    trace_sample_rate = getattr(trace_config, "sample_rate", 0.0)
    (
        routing_distribution,
        routing_selection_breakdown,
        routing_exclusion_breakdown,
        routing_skew_summary,
        model_info_state,
        runtime_snapshot,
    ) = cast(
        "_RoutingPayloadWithRuntime",
        await asyncio.gather(
            stats.get_routing_distribution(time_range, use_cache=True),
            stats.get_routing_selection_breakdown(time_range, use_cache=True),
            stats.get_routing_exclusion_breakdown(time_range, use_cache=True),
            stats.get_routing_skew_summary(time_range, use_cache=True),
            _get_model_info_summary_state(model_info_service),
            runtime_metrics.snapshot()
            if runtime_metrics is not None
            else _empty_dict(),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    trace_writer = cast(
        "dict[str, Any]", (runtime_snapshot or {}).get("routing_trace_writer") or {}
    )
    return HTMLResponse(
        content=render_routing(
            period=time_range.label,
            routing_distribution=routing_distribution or [],
            routing_selection_breakdown=routing_selection_breakdown or [],
            routing_exclusion_breakdown=routing_exclusion_breakdown or [],
            routing_skew_summary=routing_skew_summary or {},
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            model_info_map=model_info_state.summaries,
            trace_mode=trace_mode,
            trace_sample_rate=trace_sample_rate,
            trace_writer=trace_writer,
        )
    )


async def handle_traces(
    request: Request,
    period: str | None = "24h",
    limit: int = 50,
    theme: str | None = None,
) -> Response:
    """Render the recent-request trace page.

    Auth-gated, bounded at ``limit`` (10..500, default 50).  Returns
    request metadata only — never ``error_detail`` or ``client_ip``.
    """
    _get_dashboard_config(request)
    bounded_limit = _clamp_int(limit, minimum=10, maximum=500)
    stats = _get_stats(request)
    model_info_service = _get_model_info(request)
    recent_requests, model_info_state = cast(
        "tuple[list[dict[str, Any]], ModelInfoDashboardState]",
        await asyncio.gather(
            stats.get_recent_requests(limit=bounded_limit),
            _get_model_info_summary_state(model_info_service),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_traces(
            period="recent",
            limit=bounded_limit,
            recent_requests=recent_requests or [],
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            model_info_map=model_info_state.summaries,
        )
    )


async def handle_pings(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the provider pings health page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    ping_summary, recent_pings = cast(
        "_PingsPayload",
        await asyncio.gather(
            stats.get_ping_summary(time_range, use_cache=True),
            stats.get_ping_recent(limit=50),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_pings(
            ping_summary,
            recent_pings,
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


async def handle_events(
    request: Request,
    period: str | None = "24h",
    type_filter: str | None = None,
    theme: str | None = None,
) -> Response:
    """Render the events page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = _get_stats(request)
    events, available_types = cast(
        "tuple[list[dict[str, Any]], list[str]]",
        await asyncio.gather(
            stats.get_recent_events(
                limit=100,
                event_type=type_filter or None,
                time_range=time_range,
            ),
            stats.get_event_types_in_range(time_range),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_events(
            events,
            event_type=type_filter or "",
            available_types=available_types,
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


async def handle_timeseries(
    request: Request,
    period: str | None = "24h",
    bucket: str = AUTO_BUCKET,
    account: str | None = None,
    model: str | None = None,
    group_by: str = "provider_model",
    metric: str = "tokens",
    limit: int = 12,
    theme: str | None = None,
) -> Response:
    """Render the timeseries page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    raw_bucket = bucket
    bucket = _normalize_bucket(bucket, time_range.label)
    group_by = _normalize_group_by(group_by)
    bounded_limit = clamp_grouped_limit(limit)
    stats = _get_stats(request)
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    model_info_service = _get_model_info(request)
    grouped, model_info_state = await asyncio.gather(
        _await_dashboard_stage(
            telemetry,
            "timeseries",
            "timeseries_grouped",
            stats.get_grouped_timeseries(
                time_range,
                bucket=bucket,
                group_by=group_by,
                limit=bounded_limit,
                account_name=account or None,
                model_id=model or None,
                use_cache=True,
            ),
        ),
        _get_model_info_summary_state(model_info_service),
    )
    series = _aggregate_series_from_grouped(grouped)
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    account_options = _collect_account_options(request)
    model_options = _collect_model_options(request)
    _render_start = time.perf_counter()
    response = HTMLResponse(
        content=render_timeseries(
            series,
            bucket=raw_bucket,
            resolved_bucket=bucket,
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            grouped=grouped,
            group_by=group_by,
            metric=metric,
            limit=bounded_limit,
            account_filter=account or "",
            model_filter=model or "",
            account_options=account_options,
            model_options=model_options,
            update_info=_get_update_info(request),
            model_info_map=model_info_state.summaries,
        )
    )
    if telemetry is not None:
        telemetry.record_stage(
            "timeseries",
            "render_html",
            (time.perf_counter() - _render_start) * 1000,
        )
    return response


async def handle_bandwidth(
    request: Request,
    period: str | None = "24h",
    bucket: str = AUTO_BUCKET,
    account: str | None = None,
    theme: str | None = None,
) -> Response:
    """Render the bandwidth page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    bucket = _normalize_bucket(bucket, time_range.label)
    stats = _get_stats(request)
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    _gather_start = time.perf_counter()
    heatmap_range = _heatmap_time_range(_HEATMAP_MAX_DAYS)
    summary, daily = cast(
        "tuple[dict[str, Any], list[dict[str, Any]]]",
        await asyncio.gather(
            stats.get_summary(time_range, account_name=account or None, use_cache=True),
            stats.get_bandwidth_timeseries(
                heatmap_range, account_name=account or None, use_cache=True
            ),
        ),
    )
    _gather_ms = (time.perf_counter() - _gather_start) * 1000
    if telemetry is not None:
        telemetry.record_stage("bandwidth", "summary", _gather_ms)
        telemetry.record_stage("bandwidth", "bandwidth_timeseries", _gather_ms)
    theme_css, heatmap_colors, current_theme, available = _get_theme_data(
        request, theme
    )
    account_options = _collect_account_options(request)
    _render_start = time.perf_counter()
    response = HTMLResponse(
        content=render_bandwidth(
            summary=summary,
            daily=daily,
            bucket=bucket,
            period=time_range.label,
            account_filter=account or "",
            account_options=account_options,
            theme_css=theme_css,
            heatmap_colors=heatmap_colors,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )
    if telemetry is not None:
        telemetry.record_stage(
            "bandwidth",
            "render_html",
            (time.perf_counter() - _render_start) * 1000,
        )
    return response


async def handle_timeseries_json(
    request: Request,
    period: str | None = "24h",
    bucket: str = AUTO_BUCKET,
    account: str | None = None,
    model: str | None = None,
) -> Response:
    """Return timeseries data as JSON for Chart.js."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    bucket = _normalize_bucket(bucket, time_range.label)
    stats = _get_stats(request)
    series = await stats.get_timeseries(
        time_range,
        bucket=bucket,
        account_name=account or None,
        model_id=model or None,
        use_cache=True,
    )
    return JSONResponse(content=series or [])


async def handle_grouped_timeseries_json(
    request: Request,
    period: str | None = "24h",
    bucket: str = AUTO_BUCKET,
    account: str | None = None,
    model: str | None = None,
    group_by: str = "provider_model",
    metric: str = "requests",
    limit: int = 12,
) -> Response:
    """Return grouped timeseries data as JSON.

    The ``metric`` parameter is accepted for API stability but unused in
    this pass; the dashboard contract always ranks series by
    ``request_count``.  ``limit`` is clamped to ``1..25`` and ``bucket``
    accepts ``"hour"``, ``"day"``, or ``"auto"`` (period-aware default).
    """
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    bucket = _normalize_bucket(bucket, time_range.label)
    group_by = _normalize_group_by(group_by)
    bounded_limit = clamp_grouped_limit(limit)
    stats = _get_stats(request)
    payload = await stats.get_grouped_timeseries(
        time_range,
        bucket=bucket,
        group_by=group_by,
        limit=bounded_limit,
        account_name=account or None,
        model_id=model or None,
        use_cache=True,
    )
    return JSONResponse(content=payload)


async def handle_runtime(
    request: Request,
    period: str | None = "24h",
    theme: str | None = None,
) -> Response:
    """Render the runtime metrics page."""
    _start = time.perf_counter()
    _get_dashboard_config(request)
    runtime_metrics = request.app.state.runtime_metrics
    stats_service = _get_stats(request)
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    _gather_start = time.perf_counter()
    snapshot, transcoding_stats = cast(
        "tuple[dict[str, Any], dict[str, Any] | None]",
        await asyncio.gather(
            runtime_metrics.snapshot(),
            stats_service.get_transcoding_stats(period, use_cache=True),
        ),
    )
    _gather_ms = (time.perf_counter() - _gather_start) * 1000
    if telemetry is not None:
        telemetry.record_stage("runtime", "snapshot", _gather_ms)
        telemetry.record_stage("runtime", "transcoding_stats", _gather_ms)
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    _render_start = time.perf_counter()
    response = HTMLResponse(
        content=render_runtime(
            snapshot,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            transcoding_stats=transcoding_stats,
            period=period or "24h",
        )
    )
    if telemetry is not None:
        telemetry.record_stage(
            "runtime",
            "render_html",
            (time.perf_counter() - _render_start) * 1000,
        )
    _elapsed_ms = (time.perf_counter() - _start) * 1000
    if telemetry is not None:
        telemetry.record_render("runtime", _elapsed_ms)
    return response


async def handle_cache(
    request: Request,
    period: str | None = "24h",
    theme: str | None = None,
) -> Response:
    """Render the cache / request-shaping diagnostics page."""
    _start = time.perf_counter()
    _get_dashboard_config(request)
    runtime_metrics = request.app.state.runtime_metrics
    stats_service = _get_stats(request)
    telemetry = getattr(request.app.state, "dashboard_telemetry", None)
    _gather_start = time.perf_counter()
    (
        cache_observability,
        canonical_request_segmentation,
        cache_stability,
        snapshot,
    ) = cast(
        "tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]",
        await asyncio.gather(
            stats_service.get_cache_observability(period, use_cache=True),
            stats_service.get_canonical_request_segmentation(period, use_cache=True),
            stats_service.get_cache_stability(period, use_cache=True),
            runtime_metrics.snapshot(),
        ),
    )
    _gather_ms = (time.perf_counter() - _gather_start) * 1000
    if telemetry is not None:
        for name in (
            "cache_observability",
            "canonical_request_segmentation",
            "cache_stability",
            "snapshot",
        ):
            telemetry.record_stage("cache", name, _gather_ms)
    request_shaping_summary = _build_request_shaping_summary(
        request.app.state.config,
        routing_runtime=cast("dict[str, Any]", snapshot.get("routing_runtime") or {}),
        cache_observability=cache_observability,
        canonical_request_segmentation=canonical_request_segmentation,
        cache_stability=cache_stability,
        period=period or "24h",
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    _render_start = time.perf_counter()
    response = HTMLResponse(
        content=render_cache(
            period=period or "24h",
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
            routing_runtime=cast(
                "dict[str, Any]", snapshot.get("routing_runtime") or {}
            ),
            cache_observability=cache_observability,
            canonical_request_segmentation=canonical_request_segmentation,
            cache_stability=cache_stability,
            request_shaping_summary=request_shaping_summary,
        )
    )
    _render_ms = (time.perf_counter() - _render_start) * 1000
    if telemetry is not None:
        telemetry.record_stage("cache", "render_html", _render_ms)
    _elapsed_ms = (time.perf_counter() - _start) * 1000
    if telemetry is not None:
        telemetry.record_render("cache", _elapsed_ms)
    return response


async def handle_transcoding_stats_json(request: Request) -> Response:
    """Return transcoding statistics as JSON."""
    _get_dashboard_config(request)
    from eggpool.stats import serialize_transcoding_stats

    period = request.query_params.get("period", "24h")
    stats_service = _get_stats(request)
    data = await stats_service.get_transcoding_stats(period)
    return JSONResponse(content=serialize_transcoding_stats(data))


async def handle_cache_observability_json(request: Request) -> Response:
    """Return cache-counter observability aggregates as JSON.

    Dashboard surface for ``cache_counter_status`` coverage, cached-
    token totals, known-only cache hit ratio, and per-provider /
    per-account / per-model breakdowns.  Empty data returns the
    stable zero shape so dashboards never blow up on bad input.
    """
    _get_dashboard_config(request)
    period = request.query_params.get("period", "24h")
    stats_service = _get_stats(request)
    data = await stats_service.get_cache_observability(period)
    return JSONResponse(content=data)


async def handle_canonical_request_segmentation_json(request: Request) -> Response:
    """Return canonical request segmentation aggregates as JSON."""
    _get_dashboard_config(request)
    period = request.query_params.get("period", "24h")
    stats_service = _get_stats(request)
    data = await stats_service.get_canonical_request_segmentation(period)
    return JSONResponse(content=serialize_canonical_request_segmentation(data))


async def handle_request_shaping_json(request: Request) -> Response:
    """Return the operator-facing request-shaping summary as JSON."""
    _get_dashboard_config(request)
    runtime_metrics = request.app.state.runtime_metrics

    period = request.query_params.get("period", "24h")
    stats_service = _get_stats(request)
    (
        cache_observability,
        canonical_request_segmentation,
        cache_stability,
        snapshot,
    ) = cast(
        "tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]",
        await asyncio.gather(
            stats_service.get_cache_observability(period),
            stats_service.get_canonical_request_segmentation(period),
            stats_service.get_cache_stability(period),
            runtime_metrics.snapshot(),
        ),
    )
    return JSONResponse(
        content=_build_request_shaping_summary(
            request.app.state.config,
            routing_runtime=cast(
                "dict[str, Any]", snapshot.get("routing_runtime") or {}
            ),
            cache_observability=cache_observability,
            canonical_request_segmentation=canonical_request_segmentation,
            cache_stability=cache_stability,
            period=period,
        )
    )


async def handle_cache_stability_json(request: Request) -> Response:
    """Return cache-stability summary as JSON.

    Cache boundary tracking is per-request and lives on
    :class:`TranscodeContext.cache_boundary_tracker`.  The durable
    summary counts transcoded requests so operators can confirm the
    tracker is wired; per-request loss warnings are surfaced through
    the request trace endpoint, not via this aggregate.
    """
    _get_dashboard_config(request)
    period = request.query_params.get("period", "24h")
    stats_service = _get_stats(request)
    data = await stats_service.get_cache_stability(period)
    return JSONResponse(content=data)


def register_dashboard_routes(app: Any, require_auth: bool = False) -> None:
    """Attach the dashboard HTML routes to a FastAPI app.

    When ``require_auth`` is True the routes are gated by the
    standard ``require_auth`` dependency, enforcing API key
    authentication on every dashboard page.
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    dependencies = [Depends(_require_auth)] if require_auth else None
    for path, endpoint, response_class in (
        ("/", handle_overview, HTMLResponse),
        ("/accounts", handle_accounts, HTMLResponse),
        ("/models", handle_models, HTMLResponse),
        ("/models/{model_id:path}", handle_model_detail, HTMLResponse),
        ("/latency", handle_latency, HTMLResponse),
        ("/events", handle_events, HTMLResponse),
        ("/timeseries", handle_timeseries, HTMLResponse),
        ("/bandwidth", handle_bandwidth, HTMLResponse),
        ("/pings", handle_pings, HTMLResponse),
        ("/reliability", handle_reliability, HTMLResponse),
        ("/routing", handle_routing, HTMLResponse),
        ("/traces", handle_traces, HTMLResponse),
        ("/runtime", handle_runtime, HTMLResponse),
        ("/cache", handle_cache, HTMLResponse),
        ("/api/timeseries", handle_timeseries_json, JSONResponse),
        ("/api/timeseries/grouped", handle_grouped_timeseries_json, JSONResponse),
        ("/api/stats/transcoding", handle_transcoding_stats_json, JSONResponse),
        (
            "/api/stats/cache-observability",
            handle_cache_observability_json,
            JSONResponse,
        ),
        (
            "/api/stats/canonical-request-segmentation",
            handle_canonical_request_segmentation_json,
            JSONResponse,
        ),
        ("/api/stats/cache-stability", handle_cache_stability_json, JSONResponse),
        ("/api/stats/request-shaping", handle_request_shaping_json, JSONResponse),
    ):
        app.add_api_route(
            path=path,
            endpoint=endpoint,
            methods=["GET"],
            response_class=response_class,
            dependencies=dependencies,
        )


__all__ = [
    "handle_accounts",
    "handle_bandwidth",
    "handle_cache",
    "handle_cache_observability_json",
    "handle_cache_stability_json",
    "handle_canonical_request_segmentation_json",
    "handle_events",
    "handle_grouped_timeseries_json",
    "handle_latency",
    "handle_model_detail",
    "handle_models",
    "handle_overview",
    "handle_pings",
    "handle_request_shaping_json",
    "handle_reliability",
    "handle_routing",
    "handle_runtime",
    "handle_timeseries",
    "handle_timeseries_json",
    "handle_transcoding_stats_json",
    "handle_traces",
    "register_dashboard_routes",
]
