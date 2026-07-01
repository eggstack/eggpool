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
from eggpool.errors import ConfigError
from eggpool.stats import TimeRange, resolve_time_range
from eggpool.stats.grouped_timeseries import clamp_grouped_limit
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
    dict[str, Any],
]
_BandwidthPayload = tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]] | None,
]
_PingsPayload = tuple[list[dict[str, Any]], list[dict[str, Any]]]


async def _get_model_info_summary_map(
    model_info_service: Any,
) -> dict[str, dict[str, Any]]:
    """Fetch compact model-info summaries keyed by model_id.

    Returns an empty dict when the service is unavailable so dashboard
    rendering never blocks on model-info.
    """
    if model_info_service is None:
        return {}
    try:
        raw_map = await model_info_service.get_summary_map()
        result: dict[str, dict[str, Any]] = {}
        for mid, info in raw_map.items():
            sources: list[str] = []
            prov_raw = cast("dict[str, Any]", getattr(info, "provenance", {}))
            raw_sources = cast("list[object]", prov_raw.get("sources", []))
            for s in raw_sources:
                sources.append(str(s))
            status_val = getattr(info, "status", "")
            status_str = str(status_val) if status_val is not None else ""
            result[mid] = {
                "status": status_str,
                "sparse": getattr(info, "sparse", False),
                "summary": getattr(info, "summary", "") or "",
                "sources": sources,
                "last_refreshed_at": (
                    info.last_refreshed_at.isoformat()
                    if info.last_refreshed_at is not None
                    else None
                ),
            }
        return result
    except Exception:
        return {}


DEFAULT_REFRESH_S = 15

# Heatmap TimeRange shows the trailing window.  Capped at 90 days so the
# grid stays bounded and at ``retain_request_stats_days`` so it never
# scans rows the retention job will purge.  Recomputed per request so
# the dashboard cache key naturally advances with wall-clock time.
_HEATMAP_MAX_DAYS = 90
_VALID_BUCKETS = frozenset({"hour", "day"})
_VALID_GROUP_BY = frozenset({"provider", "model", "provider_model", "account"})


async def _get_disabled_account_count(request: Request, show_disabled: bool) -> int:
    """Return hidden disabled-account count for pages with that toggle."""
    if show_disabled:
        return 0
    return await fetch_disabled_account_count(request.app.state.stats_db)


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
    disabled_count = await _get_disabled_account_count(request, show_disabled)

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

    from eggpool.metrics.thinking import get_counter

    thinking_stats = await get_counter().snapshot()

    refresh_s = dashboard_config.refresh_interval_s
    theme_css, heatmap_colors, current_theme, available = _get_theme_data(
        request, theme
    )
    enabled_count = sum(1 for a in accounts if a.get("account_enabled"))
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
        enabled_count=enabled_count,
        thinking_stats=thinking_stats,
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
    _get_dashboard_config(request)
    time_range = resolve_time_range(period)
    stats = request.app.state.stats
    model_info_service = getattr(request.app.state, "model_info", None)
    catalog = getattr(request.app.state, "catalog", None)
    app_config = getattr(request.app.state, "config", None)
    collapse_models = _read_collapse_models(app_config)

    # Fetch stats, model-info summaries, and the catalog snapshot
    # concurrently so the page renders in a single round-trip.
    models, model_info_summary_map, catalog_rows = cast(
        "tuple[list[dict[str, Any]] | None, dict[str, Any], list[dict[str, Any]]]",
        await asyncio.gather(
            stats.get_model_stats(
                time_range, account_name=account or None, use_cache=True
            ),
            _get_model_info_summary_map(model_info_service),
            _get_catalog_rows(catalog, account=account or None, config=app_config),
        ),
    )
    merged_rows = _merge_models_with_catalog(
        models if models is not None else [],
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
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
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
        )
    )


async def _get_catalog_rows(
    catalog: Any,
    *,
    account: str | None = None,
    config: Any | None = None,
) -> list[dict[str, Any]]:
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

    Returns an empty list when the catalog is unavailable — the page
    must still render with whatever stats rows the caller already has.

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
        return []
    # Build a provider_id → routing_priority map once when the config
    # is available so per-row lookup is a cheap dict read.
    priority_by_provider = _build_provider_priority_map(config)
    collapse_models = _read_collapse_models(config)
    if collapse_models:
        rows = _get_collapsed_catalog_rows(
            catalog,
            priority_by_provider=priority_by_provider,
            account=account,
        )
    else:
        rows = _get_provider_scoped_catalog_rows(
            catalog,
            priority_by_provider=priority_by_provider,
            account=account,
        )
    return rows  # type: ignore[no-any-return]


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
    the default behavior the dashboard has shipped since Phase D.
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
) -> list[dict[str, Any]]:
    """One row per ``(model_id, provider_id)`` pair.

    Used when ``collapse_models`` is false (the default). Iterates
    ``catalog.cache.get_provider_model_entries()`` so each suffixed
    catalog exposure becomes a distinct dashboard row.
    """
    try:
        provider_entries = catalog.cache.get_provider_model_entries()
    except Exception:
        return []
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
    return rows


def _get_collapsed_catalog_rows(
    catalog: Any,
    *,
    priority_by_provider: dict[str, int],
    account: str | None,
) -> list[dict[str, Any]]:
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
    """
    try:
        entries = catalog.get_models_for_exposure()
    except Exception:
        return []
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
    return rows


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
        normalized = _normalize_info_status_filter(info_status)

        def _matches(row: dict[str, Any]) -> bool:
            base_id = str(row.get("base_model_id") or "")
            literal = str(row.get("model_id") or "")
            mi_entry = mi_map.get(base_id) or mi_map.get(literal)
            if mi_entry is None:
                return False
            return mi_entry.get("status") == normalized

        result = [r for r in result if _matches(r)]
    if availability == "available":
        result = [r for r in result if r.get("_in_catalog")]
    elif availability == "unavailable":
        result = [r for r in result if not r.get("_in_catalog")]
    return result


# Display-to-canonical status aliases.  The dashboard's filter UI
# historically accepts the canonical names (``sparse_new``,
# ``conflicting``, ``source_unavailable``, ``manual_override``) but
# compact summaries expose the display labels (``sparse``,
# ``conflict``, ``source-unavailable``, ``manual``).  Both forms are
# honored so that ``?info_status=sparse`` and
# ``?info_status=sparse_new`` filter the same set of rows.
_STATUS_ALIASES: dict[str, str] = {
    "sparse": "sparse_new",
    "sparse_new": "sparse_new",
    "conflict": "conflicting",
    "conflicting": "conflicting",
    "source-unavailable": "source_unavailable",
    "source_unavailable": "source_unavailable",
    "manual": "manual_override",
    "manual_override": "manual_override",
    "withdrawn": "withdrawn",
}


def _normalize_info_status_filter(value: str) -> str:
    """Normalize an info-status filter value to its canonical form."""
    return _STATUS_ALIASES.get(value, value)


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
    for row in stats_rows:
        key = _model_row_key(row, collapse_models=collapse_models)
        mid, pid = key
        if not mid:
            continue
        seen_keys.add(key)
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
    for row in catalog_rows:
        key = _model_row_key(row, collapse_models=collapse_models)
        if not key[0] or key in seen_keys:
            continue
        seen_keys.add(key)
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
    model_info_service = getattr(request.app.state, "model_info", None)
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
    if model_info_service is not None:
        try:
            info = await model_info_service.get_summary(lookup_id)
            if info is None:
                info = await model_info_service.ensure_canonical(lookup_id)
        except Exception:
            info = None
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
        content=render_model_detail(
            info=info,
            model_id=decoded_id,
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
        routing_skew_summary,
    ) = cast(
        _RoutingPayload,  # noqa: TC006 — pyright needs the TypeAlias to propagate through gather()
        await asyncio.gather(
            stats.get_routing_distribution(time_range),
            stats.get_routing_selection_breakdown(time_range),
            stats.get_routing_exclusion_breakdown(time_range),
            stats.get_routing_skew_summary(time_range),
        ),
    )
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
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
    bounded_limit = clamp_grouped_limit(limit)
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
    bounded_limit = clamp_grouped_limit(limit)
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


async def handle_runtime(
    request: Request,
    period: str | None = "24h",
    theme: str | None = None,
) -> Response:
    """Render the runtime metrics page."""
    _get_dashboard_config(request)
    runtime_metrics = request.app.state.runtime_metrics
    db = request.app.state.db
    from eggpool.stats import StatsService

    stats_service = StatsService(db)
    transcoding_stats = await stats_service.get_transcoding_stats(period)
    snapshot = await runtime_metrics.snapshot()
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(
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


async def handle_transcoding_stats_json(request: Request) -> Response:
    """Return transcoding statistics as JSON."""
    _get_dashboard_config(request)
    db = request.app.state.db
    from starlette.responses import JSONResponse as _JSONResponse

    from eggpool.stats import StatsService

    period = request.query_params.get("period", "24h")
    stats_service = StatsService(db)
    data = await stats_service.get_transcoding_stats(period)
    return _JSONResponse(content=data)


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
        ("/api/timeseries", handle_timeseries_json, JSONResponse),
        ("/api/timeseries/grouped", handle_grouped_timeseries_json, JSONResponse),
        ("/api/stats/transcoding", handle_transcoding_stats_json, JSONResponse),
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
    "handle_model_detail",
    "handle_models",
    "handle_overview",
    "handle_pings",
    "handle_reliability",
    "handle_routing",
    "handle_runtime",
    "handle_timeseries",
    "handle_timeseries_json",
    "handle_transcoding_stats_json",
    "handle_traces",
    "register_dashboard_routes",
]
