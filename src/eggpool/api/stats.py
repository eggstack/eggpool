"""Read-only JSON statistics API endpoints.

Endpoints:
- GET /api/stats/summary
- GET /api/stats/accounts
- GET /api/stats/models
- GET /api/stats/timeseries
- GET /api/stats/errors
- GET /api/stats/latency
- GET /api/stats/pings
- GET /api/stats/ips
- GET /api/events
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from eggpool.stats import TimeRange, resolve_period

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import Response


def _resolve(request: Request, period: str | None) -> TimeRange:
    """Resolve a period string into a TimeRange."""
    start, end, label = resolve_period(period)
    return TimeRange(start=start, end=end, label=label)


async def handle_summary(request: Request, period: str | None = "24h") -> Response:
    """GET /api/stats/summary."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    summary = await stats.get_summary(time_range)
    return JSONResponse(content={"period": time_range.label, **summary})


async def handle_account_stats(
    request: Request, period: str | None = "24h"
) -> Response:
    """GET /api/stats/accounts."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    accounts = await stats.get_account_stats(time_range)
    return JSONResponse(content={"period": time_range.label, "accounts": accounts})


async def handle_model_stats(
    request: Request,
    period: str | None = "24h",
    account: str | None = None,
) -> Response:
    """GET /api/stats/models."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    models = await stats.get_model_stats(time_range, account_name=account or None)
    if models is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Account {account!r} not found"},
        )
    return JSONResponse(
        content={
            "period": time_range.label,
            "account_filter": account or None,
            "models": models,
        }
    )


async def handle_timeseries(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
    account: str | None = None,
    model: str | None = None,
) -> Response:
    """GET /api/stats/timeseries."""
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
    if series is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Account {account!r} not found"},
        )
    return JSONResponse(
        content={
            "period": time_range.label,
            "bucket": bucket,
            "account_filter": account or None,
            "model_filter": model or None,
            "series": series,
        }
    )


async def handle_errors(
    request: Request, period: str | None = "24h", limit: int = 20
) -> Response:
    """GET /api/stats/errors."""
    limit = max(1, min(limit, 100))
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    errors = await stats.get_error_breakdown(time_range, limit=limit)
    return JSONResponse(content={"period": time_range.label, "errors": errors})


async def handle_events(
    request: Request,
    limit: int = 50,
    type_filter: str | None = None,
) -> Response:
    """GET /api/events."""
    limit = max(1, min(limit, 100))
    stats = request.app.state.stats
    events = await stats.get_recent_events(limit=limit, event_type=type_filter or None)
    return JSONResponse(content={"limit": limit, "type": type_filter, "events": events})


async def handle_bandwidth(
    request: Request,
    period: str | None = "24h",
    account: str | None = None,
) -> Response:
    """GET /api/stats/bandwidth."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    daily = await stats.get_bandwidth_timeseries(
        time_range, account_name=account or None
    )
    return JSONResponse(
        content={
            "period": time_range.label,
            "account_filter": account or None,
            "daily": daily,
        }
    )


async def handle_latency(
    request: Request,
    period: str | None = "24h",
) -> Response:
    """GET /api/stats/latency."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    provider_ttft = await stats.get_provider_ttft_summary(time_range)
    model_ttft = await stats.get_provider_model_ttft(time_range)
    return JSONResponse(
        content={
            "period": time_range.label,
            "provider_ttft": provider_ttft,
            "model_ttft": model_ttft,
        }
    )


async def handle_pings(
    request: Request,
    period: str | None = "24h",
    provider: str | None = None,
) -> Response:
    """GET /api/stats/pings."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    ping_summary = await stats.get_ping_summary(time_range)
    recent_pings = await stats.get_ping_recent(provider_id=provider or None, limit=50)
    return JSONResponse(
        content={
            "period": time_range.label,
            "provider_filter": provider or None,
            "summary": ping_summary,
            "recent": recent_pings,
        }
    )


async def handle_ip_stats(request: Request, period: str | None = "24h") -> Response:
    """GET /api/stats/ips."""
    time_range = _resolve(request, period)
    stats = request.app.state.stats
    ip_stats = await stats.get_ip_stats(time_range)
    return JSONResponse(content={"period": time_range.label, "ips": ip_stats})


def register_stats_routes(app: Any, require_auth: bool = False) -> None:
    """Attach the JSON statistics routes to a FastAPI app.

    When ``require_auth`` is True the routes are gated by the
    standard ``require_auth`` dependency, enforcing API key
    authentication on every stats endpoint.
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    dependencies = [Depends(_require_auth)] if require_auth else None
    app.add_api_route(
        path="/api/stats/summary",
        endpoint=handle_summary,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/accounts",
        endpoint=handle_account_stats,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/models",
        endpoint=handle_model_stats,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/timeseries",
        endpoint=handle_timeseries,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/errors",
        endpoint=handle_errors,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/events",
        endpoint=handle_events,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/bandwidth",
        endpoint=handle_bandwidth,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/latency",
        endpoint=handle_latency,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/pings",
        endpoint=handle_pings,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/ips",
        endpoint=handle_ip_stats,
        methods=["GET"],
        dependencies=dependencies,
    )


__all__ = [
    "handle_account_stats",
    "handle_bandwidth",
    "handle_errors",
    "handle_events",
    "handle_ip_stats",
    "handle_latency",
    "handle_model_stats",
    "handle_pings",
    "handle_summary",
    "handle_timeseries",
    "register_stats_routes",
]
