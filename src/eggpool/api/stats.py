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
- GET /api/stats/attempts
- GET /api/stats/retries
- GET /api/stats/routing
- GET /api/stats/routing-selections
- GET /api/stats/routing-exclusions
- GET /api/stats/operational
- GET /api/stats/pending-health
- GET /api/stats/pricing-provenance
- GET /api/stats/recent/{request_id}  (always auth-gated)
- GET /api/stats/recent-requests  (always auth-gated)
- GET /api/stats/update  (always auth-gated, in api/update.py)
- GET /api/stats/thinking
- GET /api/events
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse

from eggpool.stats import resolve_time_range

if TYPE_CHECKING:
    from fastapi.responses import Response


async def handle_summary(request: Request, period: str | None = "24h") -> Response:
    """GET /api/stats/summary."""
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    summary = await stats.get_summary(time_range)
    return JSONResponse(content={"period": time_range.label, **summary})


async def handle_account_stats(
    request: Request,
    period: str | None = "24h",
    include_disabled: bool = True,
) -> Response:
    """GET /api/stats/accounts.

    ``include_disabled`` defaults to True so existing API consumers
    continue to see soft-deleted (``enabled = 0``) accounts — their
    historical request/cost rows must still be attributable to the
    account that produced them. Pass ``?include_disabled=0`` to hide
    them, mirroring the dashboard's "Show disabled accounts" toggle.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    accounts = await stats.get_account_stats(
        time_range, include_disabled=include_disabled
    )
    return JSONResponse(
        content={
            "period": time_range.label,
            "include_disabled": include_disabled,
            "accounts": accounts,
        }
    )


async def handle_model_stats(
    request: Request,
    period: str | None = "24h",
    account: str | None = None,
) -> Response:
    """GET /api/stats/models."""
    time_range = resolve_time_range(period)
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
    time_range = resolve_time_range(period)
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
    time_range = resolve_time_range(period)
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
    time_range = resolve_time_range(period)
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
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    provider_ttft = await stats.get_provider_ttft_summary(time_range)
    model_ttft = await stats.get_provider_model_ttft(time_range)
    phases = await stats.get_latency_phase_breakdown(time_range)
    return JSONResponse(
        content={
            "period": time_range.label,
            "provider_ttft": provider_ttft,
            "model_ttft": model_ttft,
            "phases": phases,
        }
    )


async def handle_pings(
    request: Request,
    period: str | None = "24h",
    provider: str | None = None,
) -> Response:
    """GET /api/stats/pings."""
    time_range = resolve_time_range(period)
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
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    ip_stats = await stats.get_ip_stats(time_range)
    return JSONResponse(content={"period": time_range.label, "ips": ip_stats})


async def handle_attempt_stats(
    request: Request,
    period: str | None = "24h",
    account: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> Response:
    """GET /api/stats/attempts.

    Per-attempt aggregates: total attempts, retry attempts, success
    attempts, latency percentiles, byte totals, retry rate.  Filters
    on account, model, and provider are accepted as query params.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    attempts = await stats.get_attempt_stats(
        time_range,
        account_name=account or None,
        model_id=model or None,
        provider_id=provider or None,
    )
    return JSONResponse(
        content={
            "period": time_range.label,
            "account_filter": account or None,
            "model_filter": model or None,
            "provider_filter": provider or None,
            **attempts,
        }
    )


async def handle_retry_distribution(
    request: Request,
    period: str | None = "24h",
) -> Response:
    """GET /api/stats/retries.

    Distribution of attempts by ``retry_category`` (quota_exceeded,
    temporary, transient, auth_failure, etc.).  Useful for "what
    class of error is most common?" dashboards.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    rows = await stats.get_retry_distribution(time_range)
    return JSONResponse(content={"period": time_range.label, "distribution": rows})


async def handle_request_trace(request: Request, request_id: int) -> Response:
    """GET /api/stats/recent/{request_id}.

    Returns the parent request row plus its full attempt chain.
    Always auth-gated: per-request traces expose model, prompt
    volume, and error detail that operators consider sensitive.
    """
    stats = request.app.state.stats
    trace = await stats.get_request_trace(request_id)
    if trace is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Request {request_id} not found"},
        )
    decisions = await stats.get_routing_decisions_for_request(request_id)
    trace["routing_decisions"] = decisions
    if trace and "request" in trace:
        thinking_json = trace["request"].get("thinking_trace_json")
        if thinking_json:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                trace["request"]["thinking_trace"] = json.loads(thinking_json)
    return JSONResponse(content=trace)


async def handle_routing_distribution(
    request: Request, period: str | None = "24h"
) -> Response:
    """GET /api/stats/routing.

    Per-(model, provider) routing decision aggregates: count, average
    eligible/scored counts, average selected score, and the number
    of distinct accounts that were selected.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    rows = await stats.get_routing_distribution(time_range)
    return JSONResponse(content={"period": time_range.label, "distribution": rows})


async def handle_routing_selection_breakdown(
    request: Request, period: str | None = "24h"
) -> Response:
    """GET /api/stats/routing-selections.

    Account-level selection counts derived from routing_decisions.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    rows = await stats.get_routing_selection_breakdown(time_range)
    return JSONResponse(content={"period": time_range.label, "selections": rows})


async def handle_routing_exclusion_breakdown(
    request: Request, period: str | None = "24h"
) -> Response:
    """GET /api/stats/routing-exclusions.

    Distribution of (account, reason) exclusions parsed from the
    ``exclude_reasons_json`` JSON array stored on each
    routing_decisions row.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    rows = await stats.get_routing_exclusion_breakdown(time_range)
    return JSONResponse(content={"period": time_range.label, "exclusions": rows})


async def handle_routing_skew_summary(
    request: Request, period: str | None = "24h"
) -> Response:
    """GET /api/stats/routing-skew.

    Routing selection skew summary: max/min selection ratio,
    most/least selected accounts, total selections.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    summary = await stats.get_routing_skew_summary(time_range)
    return JSONResponse(content={"period": time_range.label, **summary})


async def handle_routing_eligibility_explanation(
    request: Request,
    model_id: str,
    provider_id: str | None = None,
    protocol: str | None = None,
) -> Response:
    """GET /api/stats/routing/eligibility.

    Returns one row per registered account explaining why each is or
    is not eligible to serve ``model_id``. Re-evaluated on every
    call against the live registry + catalog so operators can diagnose
    routing skew without restarting the service.
    """
    router = getattr(request.app.state, "router", None)
    if router is None:
        return JSONResponse(
            content={"error": "router unavailable"},
            status_code=503,
        )
    rows = await router.explain_account_eligibility(
        model_id=model_id,
        provider_id=provider_id,
        protocol=protocol,
        transcode_eligibility=None,
    )
    return JSONResponse(
        content={
            "model_id": model_id,
            "provider_id": provider_id,
            "protocol": protocol,
            "rows": rows,
        }
    )


async def handle_operational_health(
    request: Request,
    period: str | None = "24h",
    limit: int = 50,
    type_filter: str | None = None,
) -> Response:
    """GET /api/stats/operational.

    Aggregated safety-net activity (crash recovery, stale-request
    finalizer, reservation reconciliation) plus the most recent raw
    events.  Mirrors the structure of ``/api/stats/pings`` (summary +
    recent) for consistency.
    """
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    limit = max(1, min(limit, 200))
    summary = await stats.get_operational_event_summary(time_range)
    recent = await stats.get_recent_operational_events(
        limit=limit, event_type=type_filter or None
    )
    return JSONResponse(
        content={
            "period": time_range.label,
            "type_filter": type_filter,
            "summary": summary,
            "recent": recent,
        }
    )


async def handle_pending_health(request: Request) -> Response:
    """GET /api/stats/pending-health.

    Instantaneous snapshot of pending requests and active reservations.
    Used by the dashboard System Health cards and the Reliability page
    to surface leak-style failures (pending requests surviving past
    their reservation TTL, orphaned active reservations).
    """
    stats = request.app.state.stats
    snapshot = await stats.get_pending_health_snapshot()
    return JSONResponse(content=snapshot)


async def handle_pricing_provenance(request: Request) -> Response:
    """GET /api/stats/pricing-provenance.

    Per-(model, provider) provenance breakdown for the latest price
    snapshot. The dashboard uses this to render the "cost exactness"
    badges and the high-spend estimated warnings on the Reliability and
    Accounts pages. Returns one row per ``(model_id, provider_id)``
    with the latest snapshot's source_detail, source_confidence, and
    catalog_source. Counts the number of pricing categories (input /
    output / cache_read / cache_write) that carry a non-null rate so
    the dashboard can distinguish fully-priced snapshots from partial
    ones.
    """
    from eggpool.stats.queries import fetch_pricing_provenance_stats

    stats = request.app.state.stats
    rows = await fetch_pricing_provenance_stats(stats._db)
    return JSONResponse(content={"snapshots": rows})


async def handle_thinking_stats(request: Request) -> Response:
    """GET /api/stats/thinking.

    Returns in-memory thinking/reasoning observability counters.
    Counters are low-cardinality (protocol, decision, capability_status)
    and track per-request thinking decisions across the system.
    """
    from eggpool.metrics.thinking import get_counter

    counter = get_counter()
    snapshot = await counter.snapshot()
    return JSONResponse(content=snapshot)


async def handle_recent_requests(
    request: Request,
    limit: int = 50,
    account: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
) -> Response:
    """GET /api/stats/recent-requests.

    Auth-gated bounded debugging view.  Returns request metadata only
    (no prompt, body, or auth headers).  Client IP is omitted by
    default and is only included when the operator has explicitly
    enabled IP stats on the dashboard.  Error class is returned but
    the raw upstream error_detail is never sent to this endpoint.
    """
    stats = request.app.state.stats
    account_id_value: int | None = None
    if account:
        from eggpool.stats.queries import fetch_account_id

        account_id_value = await fetch_account_id(stats._db, account)
        if account_id_value is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Account {account!r} not found"},
            )
    # Phase 6 safety default: never expose client_ip from this
    # endpoint.  An operator can opt in via the existing dashboard
    # config surface, but EggPool currently has no toggle for it, so
    # the safe default applies.
    include_client_ip = False
    rows = await stats.get_recent_requests(
        limit=limit,
        account_id=account_id_value,
        provider_id=provider or None,
        model_id=model or None,
        status=status or None,
        include_client_ip=include_client_ip,
    )
    return JSONResponse(
        content={
            "limit": max(1, min(limit, 200)),
            "account_filter": account,
            "provider_filter": provider,
            "model_filter": model,
            "status_filter": status,
            "include_client_ip": include_client_ip,
            "requests": rows,
        }
    )


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
    app.add_api_route(
        path="/api/stats/attempts",
        endpoint=handle_attempt_stats,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/retries",
        endpoint=handle_retry_distribution,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/routing",
        endpoint=handle_routing_distribution,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/routing-selections",
        endpoint=handle_routing_selection_breakdown,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/routing-exclusions",
        endpoint=handle_routing_exclusion_breakdown,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/routing/eligibility",
        endpoint=handle_routing_eligibility_explanation,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/routing-skew",
        endpoint=handle_routing_skew_summary,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/operational",
        endpoint=handle_operational_health,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/pending-health",
        endpoint=handle_pending_health,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/pricing-provenance",
        endpoint=handle_pricing_provenance,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/stats/thinking",
        endpoint=handle_thinking_stats,
        methods=["GET"],
        dependencies=dependencies,
    )

    # Per-request trace endpoint.  Per-request traces expose the
    # selected model, prompt volume, and error detail that operators
    # consider sensitive, so it is ALWAYS auth-gated even when the
    # rest of /api/stats/* is public.
    app.add_api_route(
        path="/api/stats/recent/{request_id}",
        endpoint=handle_request_trace,
        methods=["GET"],
        dependencies=[Depends(_require_auth)],
    )

    # Bounded recent-requests list.  Even though it does not return
    # bodies or error_detail, the metadata still reveals model choice,
    # token usage, and error class — sensitive enough to require auth
    # regardless of dashboard.public.
    app.add_api_route(
        path="/api/stats/recent-requests",
        endpoint=handle_recent_requests,
        methods=["GET"],
        dependencies=[Depends(_require_auth)],
    )


__all__ = [
    "handle_account_stats",
    "handle_attempt_stats",
    "handle_bandwidth",
    "handle_errors",
    "handle_events",
    "handle_ip_stats",
    "handle_latency",
    "handle_model_stats",
    "handle_operational_health",
    "handle_pending_health",
    "handle_pings",
    "handle_pricing_provenance",
    "handle_recent_requests",
    "handle_request_trace",
    "handle_retry_distribution",
    "handle_routing_distribution",
    "handle_routing_eligibility_explanation",
    "handle_routing_exclusion_breakdown",
    "handle_routing_selection_breakdown",
    "handle_routing_skew_summary",
    "handle_summary",
    "handle_thinking_stats",
    "handle_timeseries",
    "register_stats_routes",
]
