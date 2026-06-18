"""Dashboard HTTP routes.

The dashboard exposes a read-only server-rendered HTML interface.
All free-text fields are HTML-escaped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi.responses import HTMLResponse

from go_aggregator.dashboard.render import (
    get_theme,
    render_accounts,
    render_bandwidth,
    render_events,
    render_models,
    render_overview,
    render_timeseries,
)
from go_aggregator.errors import ConfigError
from go_aggregator.stats import TimeRange, resolve_period

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import Response


DEFAULT_REFRESH_S = 15


def _get_dashboard_config(request: Request) -> Any:
    """Look up the dashboard config from app state, raising ConfigError if disabled."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise ConfigError("config not loaded")
    if not config.dashboard.enabled:
        raise ConfigError("dashboard disabled")
    return config.dashboard


def _get_theme_data(request: Request) -> tuple[str, list[str]]:
    """Load theme CSS and heatmap colors from the app config."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        return "", ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]
    theme_name = config.dashboard.theme
    themes_dir = config.dashboard.themes_dir
    theme = get_theme(theme_name, themes_dir)
    return theme.to_css_variables(), theme.heatmap_colors()


def _resolve(request: Request, period: str | None) -> TimeRange:
    """Resolve a period string into a TimeRange."""
    start, end, label = resolve_period(period)
    return TimeRange(start=start, end=end, label=label)


async def handle_overview(request: Request, period: str | None = "24h") -> Response:
    """Render the overview page."""
    dashboard_config = _get_dashboard_config(request)
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    overview = await stats.get_dashboard_overview(time_range)
    accounts = await stats.get_account_stats(time_range)

    heatmap_start_dt = datetime.now(UTC) - timedelta(days=90)
    heatmap_end_dt = datetime.now(UTC)
    bandwidth_daily = await stats.get_bandwidth_timeseries(
        TimeRange(
            start=heatmap_start_dt,
            end=heatmap_end_dt,
            label="90d",
        )
    )

    refresh_s = dashboard_config.refresh_interval_s
    theme_css, heatmap_colors = _get_theme_data(request)
    html = render_overview(
        overview=overview,
        accounts=accounts,
        period=time_range.label,
        refresh_interval_s=refresh_s,
        bandwidth_daily=bandwidth_daily,
        theme_css=theme_css,
        heatmap_colors=heatmap_colors,
    )
    return HTMLResponse(
        content=html,
        headers={"refresh": str(refresh_s)},
    )


async def handle_accounts(request: Request, period: str | None = "24h") -> Response:
    """Render the accounts page."""
    _get_dashboard_config(request)
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    accounts = await stats.get_account_stats(time_range)
    theme_css, _ = _get_theme_data(request)
    return HTMLResponse(
        content=render_accounts(accounts, period=time_range.label, theme_css=theme_css)
    )


async def handle_models(
    request: Request,
    period: str | None = "24h",
    account: str | None = None,
) -> Response:
    """Render the models page."""
    _get_dashboard_config(request)
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    models = await stats.get_model_stats(time_range, account_name=account or None)
    theme_css, _ = _get_theme_data(request)
    return HTMLResponse(
        content=render_models(
            models if models is not None else [],
            account_filter=account or "",
            period=time_range.label,
            theme_css=theme_css,
        )
    )


async def handle_events(
    request: Request,
    period: str | None = "24h",
    type_filter: str | None = None,
) -> Response:
    """Render the events page."""
    _get_dashboard_config(request)
    stats = request.app.state.stats
    events = await stats.get_recent_events(limit=100, event_type=type_filter or None)
    theme_css, _ = _get_theme_data(request)
    return HTMLResponse(
        content=render_events(
            events,
            event_type=type_filter or "",
            period="recent",
            theme_css=theme_css,
        )
    )


async def handle_timeseries(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
    account: str | None = None,
    model: str | None = None,
) -> Response:
    """Render the timeseries page."""
    _get_dashboard_config(request)
    time_range = _resolve(request, period)
    if bucket not in ("hour", "day"):
        bucket = "hour"
    stats = request.app.state.stats
    series = await stats.get_timeseries(
        time_range,
        bucket=bucket,
        account_name=account or None,
        model_id=model or None,
    )
    theme_css, _ = _get_theme_data(request)
    return HTMLResponse(
        content=render_timeseries(
            series if series is not None else [],
            bucket=bucket,
            period=time_range.label,
            theme_css=theme_css,
        )
    )


async def handle_bandwidth(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
    account: str | None = None,
) -> Response:
    """Render the bandwidth page."""
    _get_dashboard_config(request)
    time_range = _resolve(request, period)
    if bucket not in ("hour", "day"):
        bucket = "hour"
    stats = request.app.state.stats
    summary = await stats.get_summary(time_range)
    daily = await stats.get_bandwidth_timeseries(
        time_range, account_name=account or None
    )
    timeseries = await stats.get_timeseries(
        time_range, bucket=bucket, account_name=account or None
    )
    theme_css, heatmap_colors = _get_theme_data(request)
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
        )
    )


def register_dashboard_routes(app: Any, require_auth: bool = False) -> None:
    """Attach the dashboard HTML routes to a FastAPI app.

    When ``require_auth`` is True the routes are gated by the
    standard ``require_auth`` dependency, enforcing API key
    authentication on every dashboard page.
    """
    from fastapi import Depends

    from go_aggregator.auth import require_auth as _require_auth

    dependencies = [Depends(_require_auth)] if require_auth else None
    app.add_api_route(
        path="/",
        endpoint=handle_overview,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/accounts",
        endpoint=handle_accounts,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/models",
        endpoint=handle_models,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/events",
        endpoint=handle_events,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/timeseries",
        endpoint=handle_timeseries,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/bandwidth",
        endpoint=handle_bandwidth,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=dependencies,
    )


__all__ = [
    "handle_accounts",
    "handle_bandwidth",
    "handle_events",
    "handle_models",
    "handle_overview",
    "handle_timeseries",
    "register_dashboard_routes",
]
