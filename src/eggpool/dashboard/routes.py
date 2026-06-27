"""Dashboard HTTP routes.

The dashboard exposes a read-only server-rendered HTML interface.
All free-text fields are HTML-escaped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import HTMLResponse, JSONResponse

from eggpool.dashboard.render import (
    get_available_themes,
    get_theme,
    render_accounts,
    render_bandwidth,
    render_events,
    render_latency,
    render_models,
    render_overview,
    render_pings,
    render_reliability,
    render_routing,
    render_runtime,
    render_timeseries,
    render_traces,
)
from eggpool.errors import ConfigError
from eggpool.stats import TimeRange, resolve_time_range
from eggpool.stats.queries import fetch_disabled_account_count

if TYPE_CHECKING:
    from fastapi.responses import Response  # noqa: TCH004

_ReliabilityPayload = tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]
_RoutingPayload = tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]
_BandwidthPayload = tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]] | None,
]
_PingsPayload = tuple[list[dict[str, Any]], list[dict[str, Any]]]


DEFAULT_REFRESH_S = 15

# Heatmap TimeRange shows the trailing window.  Capped at 90 days so the
# grid stays bounded and at ``retain_request_stats_days`` so it never
# scans rows the retention job will purge.  Recomputed per request so
# the dashboard cache key naturally advances with wall-clock time.
_HEATMAP_MAX_DAYS = 90
_VALID_BUCKETS = frozenset({"hour", "day"})
_VALID_GROUP_BY = frozenset({"provider", "model", "provider_model", "account"})


def _normalize_bucket(bucket: str) -> str:
    """Return a supported dashboard bucket, falling back to hourly."""
    return bucket if bucket in _VALID_BUCKETS else "hour"


def _normalize_group_by(group_by: str) -> str:
    """Return a supported grouped-timeseries dimension."""
    return group_by if group_by in _VALID_GROUP_BY else "provider_model"


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
    return checker.snapshot()


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
    return [acct.name for acct in config.all_accounts() if acct.name]


def _collect_model_options(request: Request) -> list[str]:
    """Collect exposed model IDs for the timeseries filter dropdown.

    Pulls the same model list the public ``/v1/models`` endpoint serves
    so the dropdown options track what the catalog currently knows
    about, including provider-suffixed IDs when ``collapse_models`` is
    false (the default).  Falls back to an empty list when no catalog
    is attached yet — e.g. early in startup before the first refresh.
    """
    catalog = getattr(request.app.state, "catalog", None)
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
    dashboard_config = _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    heatmap_range = _heatmap_time_range(dashboard_config.retain_request_stats_days)

    # Always fetch the disabled count so the Account breakdown empty
    # state can offer a one-click opt-in even when no rows are
    # currently visible. Cheap one-row aggregate; safe on every render.
    disabled_count = 0
    if not show_disabled:
        disabled_count = await fetch_disabled_account_count(request.app.state.stats_db)

    # Fan out the independent stat reads concurrently.  The single
    # shared connection lock serializes per-query execution, so without
    # this the page load is the sum of ten sequential round trips; with
    # it the load is bounded by the slowest query instead.
    (
        accounts,
        models,
        events,
        bandwidth_daily,
        ping_summary,
        ip_stats,
        timeseries,
        attempt_stats,
        operational_summary,
        pending_health,
    ) = await asyncio.gather(
        stats.get_account_stats(
            time_range, include_disabled=show_disabled, use_cache=True
        ),
        stats.get_model_stats(time_range, use_cache=True),
        stats.get_recent_events(limit=10),
        stats.get_bandwidth_timeseries(heatmap_range, use_cache=True),
        stats.get_ping_summary(time_range, use_cache=True),
        stats.get_ip_stats(time_range, use_cache=True),
        stats.get_timeseries(time_range, bucket="hour", use_cache=True),
        stats.get_attempt_stats(time_range),
        stats.get_operational_event_summary(time_range),
        stats.get_pending_health_snapshot(),
    )

    # ``get_dashboard_overview`` is derived from ``accounts`` and the
    # per-period summary; both are cache hits after the gather above.
    overview = await stats.get_dashboard_overview(
        time_range, account_stats=accounts, use_cache=True
    )

    refresh_s = dashboard_config.refresh_interval_s
    theme_css, heatmap_colors, current_theme, available = _get_theme_data(
        request, theme
    )
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
        timeseries=timeseries or [],
        pending_health=pending_health,
        attempt_stats=attempt_stats,
        operational_summary=operational_summary,
        update_info=_get_update_info(request),
        show_disabled=show_disabled,
        disabled_count=disabled_count,
    )
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
    stats = request.app.state.stats

    # Always fetch the disabled count so the empty state can offer the
    # one-click opt-in even when no rows are currently visible.
    disabled_count = 0
    if not show_disabled:
        disabled_count = await fetch_disabled_account_count(request.app.state.stats_db)

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
) -> Response:
    """Render the models page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    models = await stats.get_model_stats(
        time_range, account_name=account or None, use_cache=True
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_models(
            models if models is not None else [],
            account_filter=account or "",
            period=time_range.label,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


async def handle_latency(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the latency breakdown page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    provider_ttft, model_ttft, phases = cast(
        "tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]",
        await asyncio.gather(
            stats.get_provider_ttft_summary(time_range),
            stats.get_provider_model_ttft(time_range),
            stats.get_latency_phase_breakdown(time_range),
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
        )
    )


async def handle_reliability(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the Reliability page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    (
        attempt_stats,
        retry_distribution,
        pending_health,
        operational_summary,
        recent_operational_events,
        timeseries,
    ) = cast(
        _ReliabilityPayload,  # noqa: TC006 — pyright needs the TypeAlias to propagate through gather()
        await asyncio.gather(
            stats.get_attempt_stats(time_range),
            stats.get_retry_distribution(time_range),
            stats.get_pending_health_snapshot(),
            stats.get_operational_event_summary(time_range),
            stats.get_recent_operational_events(limit=25),
            stats.get_timeseries(time_range, bucket="hour", use_cache=True),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_reliability(
            period=time_range.label,
            attempt_stats=attempt_stats,
            retry_distribution=retry_distribution or [],
            pending_health=pending_health,
            operational_summary=operational_summary or [],
            recent_operational_events=recent_operational_events or [],
            timeseries=timeseries or [],
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


async def handle_routing(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the Routing page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    (
        routing_distribution,
        routing_selection_breakdown,
        routing_exclusion_breakdown,
    ) = cast(
        _RoutingPayload,  # noqa: TC006 — pyright needs the TypeAlias to propagate through gather()
        await asyncio.gather(
            stats.get_routing_distribution(time_range),
            stats.get_routing_selection_breakdown(time_range),
            stats.get_routing_exclusion_breakdown(time_range),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_routing(
            period=time_range.label,
            routing_distribution=routing_distribution or [],
            routing_selection_breakdown=routing_selection_breakdown or [],
            routing_exclusion_breakdown=routing_exclusion_breakdown or [],
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
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
    stats = request.app.state.stats
    recent_requests = await stats.get_recent_requests(limit=bounded_limit)
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
        )
    )


async def handle_pings(
    request: Request, period: str | None = "24h", theme: str | None = None
) -> Response:
    """Render the provider pings health page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    ping_summary, recent_pings = cast(
        "_PingsPayload",
        await asyncio.gather(
            stats.get_ping_summary(time_range),
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
    stats = request.app.state.stats
    events = await stats.get_recent_events(limit=100, event_type=type_filter or None)
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_events(
            events,
            event_type=type_filter or "",
            period="recent",
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


async def handle_timeseries(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
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
    bucket = _normalize_bucket(bucket)
    group_by = _normalize_group_by(group_by)
    bounded_limit = _clamp_int(limit, minimum=1, maximum=25)
    stats = request.app.state.stats
    series, grouped = cast(
        "tuple[list[dict[str, Any]] | None, dict[str, Any]]",
        await asyncio.gather(
            stats.get_timeseries(
                time_range,
                bucket=bucket,
                account_name=account or None,
                model_id=model or None,
                use_cache=True,
            ),
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
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    account_options = _collect_account_options(request)
    model_options = _collect_model_options(request)
    return HTMLResponse(
        content=render_timeseries(
            series if series is not None else [],
            bucket=bucket,
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
        )
    )


async def handle_bandwidth(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
    account: str | None = None,
    theme: str | None = None,
) -> Response:
    """Render the bandwidth page."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    bucket = _normalize_bucket(bucket)
    stats = request.app.state.stats
    summary, daily, timeseries = cast(
        _BandwidthPayload,  # noqa: TC006 — pyright needs the TypeAlias to propagate through gather()
        await asyncio.gather(
            stats.get_summary(time_range, account_name=account or None, use_cache=True),
            stats.get_bandwidth_timeseries(time_range, account_name=account or None),
            stats.get_timeseries(
                time_range,
                bucket=bucket,
                account_name=account or None,
                use_cache=True,
            ),
        ),
    )
    theme_css, heatmap_colors, current_theme, available = _get_theme_data(
        request, theme
    )
    return HTMLResponse(
        content=render_bandwidth(
            summary=summary,
            daily=daily,
            timeseries=timeseries if timeseries is not None else [],
            bucket=bucket,
            period=time_range.label,
            account_filter=account or "",
            theme_css=theme_css,
            heatmap_colors=heatmap_colors,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


async def handle_timeseries_json(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
    account: str | None = None,
    model: str | None = None,
) -> Response:
    """Return timeseries data as JSON for Chart.js."""
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    bucket = _normalize_bucket(bucket)
    stats = request.app.state.stats
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
    bucket: str = "hour",
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
    is normalized to ``"hour"`` or ``"day"``.
    """
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    bucket = _normalize_bucket(bucket)
    group_by = _normalize_group_by(group_by)
    bounded_limit = _clamp_int(limit, minimum=1, maximum=25)
    stats = request.app.state.stats
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


async def handle_runtime(request: Request, theme: str | None = None) -> Response:
    """Render the runtime metrics page."""
    _get_dashboard_config(request)
    runtime_metrics = request.app.state.runtime_metrics
    snapshot = await runtime_metrics.snapshot()
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_runtime(
            snapshot,
            theme_css=theme_css,
            available_themes=available,
            current_theme=current_theme,
            update_info=_get_update_info(request),
        )
    )


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
        ("/latency", handle_latency, HTMLResponse),
        ("/events", handle_events, HTMLResponse),
        ("/timeseries", handle_timeseries, HTMLResponse),
        ("/bandwidth", handle_bandwidth, HTMLResponse),
        ("/pings", handle_pings, HTMLResponse),
        ("/reliability", handle_reliability, HTMLResponse),
        ("/routing", handle_routing, HTMLResponse),
        ("/traces", handle_traces, HTMLResponse),
        ("/runtime", handle_runtime, HTMLResponse),
        ("/api/timeseries", handle_timeseries_json, JSONResponse),
        ("/api/timeseries/grouped", handle_grouped_timeseries_json, JSONResponse),
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
    "handle_events",
    "handle_grouped_timeseries_json",
    "handle_latency",
    "handle_models",
    "handle_overview",
    "handle_pings",
    "handle_reliability",
    "handle_routing",
    "handle_runtime",
    "handle_timeseries",
    "handle_timeseries_json",
    "handle_traces",
    "register_dashboard_routes",
]
