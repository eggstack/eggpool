"""Server-side HTML rendering for the dashboard.

Uses a minimal string-based renderer to keep the dependency footprint
small. All values rendered into HTML are escaped via the `escape` module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from html import escape as _html_escape
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from eggpool.model_info.presentation import display_model_info_status

if TYPE_CHECKING:
    from eggpool.model_info.types import CanonicalModelInfo

from eggpool.dashboard.escape import (
    escape,
    escape_attr,
    format_age_seconds,
    format_bytes,
    format_int,
    format_latency,
    format_microdollars,
    format_percent,
    format_timestamp,
    format_tokens,
    format_tokens_per_second,
    sanitize_class_name,
    short_id,
    truncate,
)
from eggpool.dashboard.theme import (
    DashboardTheme,
    get_default_theme,
    list_themes,
    load_theme,
    resolve_theme_path,
)

_DEFAULT_HEATMAP_COLORS = [
    "#ebedf0",
    "#9be9a8",
    "#40c463",
    "#30a14e",
    "#216e39",
]

_EM_DASH = "\u2014"


_UPDATE_INDICATOR_TEMPLATE = (
    '<span class="update-indicator" '
    "data-update-indicator "
    'data-tooltip="A newer eggpool version is available. '
    'Click the command to copy it." '
    'aria-label="Update available">'
    "Update available"
    ' <span class="update-versions">{current} &rarr; {latest}</span>'
    ' &middot; run <code class="update-command" '
    'data-update-command data-update-command-target="update-cmd" '
    'role="button" tabindex="0" '
    'aria-label="Copy update command">'
    "{command}"
    "</code>"
    '<span class="update-copied" data-update-copied role="status" '
    'aria-live="polite"></span>'
    "</span>"
)
"""Footer indicator shown only when an update is available.

Renders an inline `<code>` carrying the update command.  The bundled
``dashboard.js`` hook installs a click-to-copy handler on any element
with ``data-update-command`` so operators can grab the command with a
single click.  The visible text is escaped via :func:`escape`; the
command itself is never user-supplied, so escaping is purely defensive.
"""


_STATUS_BADGE_TOOLTIPS: dict[str, str] = {
    "disabled": "Account disabled by operator",
    "auth_error": "Upstream rejected the credentials",
    "auth_failed": "Upstream rejected the credentials",
    "rate_limited": "Upstream returned 429 recently",
    "quota_exhausted": "Upstream reported the quota is exhausted",
    "cooldown_active": "Account is in cooldown after recent failures",
    "circuit_open": "Account is circuit-broken until cooldown expires",
    "circuit_close": "Account circuit-breaker recovered",
    "circuit_closed": "Account circuit-breaker recovered",
    "model_appeared": "Model first seen in upstream catalog",
    "model_discovered": "Model first seen in upstream catalog",
    "reservation_recovered": "Reservation recovered after restart",
    "catalog_refresh_failed": "Catalog refresh failed; using stale data",
}

# Routing exclusion taxonomy: which reasons remove an account from
# selection (suppressive — driven by upstream failures or operator
# action) versus which only deprioritize it (advisory — local scoring
# signals).  This split lets the dashboard verify the design rule that
# upstream-observed failures control exclusion while local accounting
# only influences priority.
#
# ``circuit_breaker`` is the only reason the coordinator actually writes
# to ``exclude_reasons_json`` (see
# ``eggpool.request.coordinator._score_and_select``).  ``circuit_open``
# is kept for backwards compatibility with rows written before the
# coordinator rename and to align semantically with the
# ``_STATUS_BADGE_TOOLTIPS`` mapping above.
SUPPRESSIVE_EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        "authentication_failed",
        "auth_failed",
        "quota_exhausted_backoff",
        "quota_exhausted",
        "rate_limit_backoff",
        "rate_limited",
        "model_unavailable",
        "operator_disabled",
        "account_disabled",
        "protocol_mismatch",
        "circuit_open",
        "circuit_breaker",
    }
)

ADVISORY_EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        "high_local_quota_estimate",
        "active_reservation_pressure",
        "active_inflight_penalty",
        "low_provider_priority",
        "health_penalty_below_threshold",
    }
)

# Error taxonomy buckets surfaced to operators on the Reliability page.
# Mirrors the retry_category values that the stats endpoints return,
# but groups them into operator-friendly labels.
_ERROR_CATEGORY_LABELS: dict[str, str] = {
    "quota_exceeded": "Quota exceeded",
    "temporary": "Temporary upstream",
    "transient": "Transient upstream",
    "auth_failure": "Auth failure",
    "rate_limited": "Rate limited",
    "model_unavailable": "Model unavailable",
    "bad_request": "Bad request",
    "never": "No retry",
    "fatal": "Fatal error",
    "unclassified": "Other",
}

_CARD_TOOLTIPS: dict[str, str] = {
    "Pending requests": (
        "Requests still in progress. Subtext shows the oldest pending age."
    ),
    "Active reservations": ("Active quota or spend reservations for in-flight work."),
    "Finalizer (24h)": (
        "Reliability cleanup activity over the last 24 hours, including stale "
        "request cleanup, timeout cases, and crash recovery runs."
    ),
    "Retry rate": (
        "Share of upstream attempts that required another try instead of "
        "succeeding or failing terminally on the first attempt."
    ),
    "First-attempt success": ("Share of attempts that completed without any retry."),
    "Requests": (
        "Total proxied requests in the selected period. The subtext splits "
        "them into successful and error requests."
    ),
    "Error rate": (
        "Fraction of requests in the selected period that ended in an error."
    ),
    "Total cost": (
        "Total recorded request cost in the selected period. When the upstream "
        "provider reports a cost (e.g. OpenCode Go's usage.cost field), that "
        "value takes precedence over locally computed rates; otherwise eggpool "
        "falls back to per-token rates from the catalog. Reservation-derived "
        "estimates are advisory and never inflate the totals when more "
        "trustworthy data is available."
    ),
    "Utilization imbalance": (
        "Coefficient of variation across active accounts. Higher values mean "
        "load is concentrated unevenly."
    ),
    "Total tokens": (
        "Combined input and output tokens processed in the selected period."
    ),
    "Cache tokens": (
        "Prompt-cache token activity. The metric shows cache reads; the "
        "subtext shows read share of input and cache writes."
    ),
    "Reasoning tokens": (
        "Tokens reported by upstreams as reasoning or extended-thinking output."
    ),
    "Throughput": (
        "Aggregate token throughput across requests, computed from total tokens "
        "divided by total latency."
    ),
    "Streaming": (
        "How many requests used streaming responses versus non-streaming responses."
    ),
    "Exactness": (
        "Count of requests whose cost was exact. The subtext also shows "
        "derived, estimated, and unknown-cost rows."
    ),
    "Bandwidth received": (
        "Total bytes received from clients by EggPool in the selected period."
    ),
    "Bandwidth emitted": (
        "Total bytes emitted by EggPool toward clients in the selected period."
    ),
    "Avg TTFT (streamed)": (
        "Average time to first token for streamed requests, with P50 and P99 "
        "shown below."
    ),
    "Total received": (
        "Total bytes received from clients by EggPool in the selected period."
    ),
    "Total emitted": (
        "Total bytes emitted by EggPool toward clients in the selected period."
    ),
    "Server PID": (
        "Process identity of the running supervisor (PPID and daemon mode)."
    ),
    "Uptime": "Elapsed time since the current EggPool process started.",
    "Python": "Python runtime version and platform for the running process.",
    "RSS memory": ("Resident memory currently held by the EggPool process."),
    "Open FDs": ("Open file descriptors currently held by the process."),
    "Active threads": ("Current number of active Python threads in the process."),
    "Load average": (
        "Host load average (1m primary, subtext shows normalized load or 5m/15m)."
    ),
    "Dispatch overhead": (
        "EggPool-local time spent before each upstream dispatch attempt begins."
    ),
    "Database": ("Primary SQLite database path and on-disk size."),
    "WAL": ("SQLite write-ahead log size and whether WAL mode is active."),
    "Sync": ("SQLite synchronous mode and whether the primary DB connection is live."),
    "Stats DB": ("Whether stats use a separate SQLite connection."),
    "In-flight requests": ("Requests currently active against upstream providers."),
    "Active backoffs": (
        "Persisted account backoff rows currently suppressing or delaying "
        "eligible accounts."
    ),
    "DNS cache": ("Outbound DNS cache state and current entry count."),
    "DNS hit rate": (
        "Share of DNS lookups served from the in-memory cache instead of "
        "triggering a fresh resolver call."
    ),
    "DNS misses": "DNS lookups that required a resolver call.",
    "DNS errors": ("DNS lookup failures and degraded cache hits (stale or negative)."),
    "Outbound builds": (
        "How many times the shared outbound client manager has built a client."
    ),
    "Outbound requests": (
        "Requests via the shared outbound client. Subtext shows errors."
    ),
    "Provider clients": (
        "How many per-provider HTTP clients were built in the provider client pool."
    ),
    "Total attempts": (
        "Total upstream attempts in the selected period, including retries."
    ),
    "Success attempts": (
        "Attempts that completed successfully. The subtext highlights the "
        "first-attempt success rate."
    ),
    "Retry attempts": ("Attempts that were retries rather than initial tries."),
    "Failed attempts": (
        "Attempts that ended in failure. The subtext shows average attempt latency."
    ),
    "Pending window": (
        "Explanation of the pending-request snapshot and stale threshold used "
        "by the reliability view."
    ),
    "Routing decisions": ("Total routing decisions recorded in the selected period."),
    "Avg eligible / decision": (
        "Average number of accounts that remained eligible for each routing decision."
    ),
    "Distinct selected accounts": (
        "Count of different accounts chosen across routing decisions in the "
        "selected period."
    ),
}


def _error_category_label(category: str) -> str:
    """Return a human label for a retry_category value."""
    return _ERROR_CATEGORY_LABELS.get(str(category or ""), str(category or "Other"))


def _classify_exclusion(reason: str) -> str:
    """Classify an exclusion reason as suppressive/advisory/unknown."""
    if not reason:
        return "unknown"
    if reason in SUPPRESSIVE_EXCLUSION_REASONS:
        return "suppressive"
    if reason in ADVISORY_EXCLUSION_REASONS:
        return "advisory"
    return "unknown"


def _status_badge_tooltip(name: str) -> str | None:
    """Return the human description for a status badge, or None."""
    return _STATUS_BADGE_TOOLTIPS.get(name)


# Module-level caches. Theme TOML files are immutable for the lifetime of
# the process and ``themes_dir`` is taken from config (which only changes
# via ``eggpool rehash`` / restart), so a simple dict cache avoids repeated
# disk reads on every dashboard request.
_THEME_CACHE: dict[tuple[str, str | None], DashboardTheme] = {}
_THEME_CSS_CACHE: dict[tuple[str, str | None], str] = {}
_THEMES_LIST_CACHE: dict[str | None, list[str]] = {}


def _themes_dir_key(themes_dir: str | None) -> str | None:
    """Normalize ``themes_dir`` for use as a cache key."""
    if themes_dir is None:
        return None
    return str(themes_dir)


def _format_tooltip_date(day_str: str) -> str:
    """Format an ISO ``YYYY-MM-DD`` day string as a human-friendly label.

    Returns the original ``day_str`` unchanged if it cannot be parsed so
    that the tooltip still has something meaningful to display.
    """
    try:
        parsed = datetime.strptime(day_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return day_str
    return parsed.strftime("%a, %b %-d %Y")


def get_theme_css(theme_name: str, themes_dir: str | None = None) -> str:
    """Load a theme by name and return the CSS :root block, or empty string.

    When themes_dir is set, user-provided themes take precedence over
    bundled themes with the same name. Results are cached so repeated
    requests for the same theme do not re-read the TOML file from disk.
    """
    if theme_name == "default":
        return ""
    key = (theme_name, _themes_dir_key(themes_dir))
    cached = _THEME_CSS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        theme_path = resolve_theme_path(theme_name, themes_dir)
        if theme_path is None:
            _THEME_CSS_CACHE[key] = ""
            return ""
        theme = load_theme(theme_path)
        css = theme.to_css_variables()
    except Exception:
        css = ""
    _THEME_CSS_CACHE[key] = css
    return css


def get_theme(theme_name: str, themes_dir: str | None = None) -> DashboardTheme:
    """Load a theme by name, returning the default on failure.

    When themes_dir is set, user-provided themes take precedence over
    bundled themes with the same name. Results are cached so repeated
    requests for the same theme do not re-read the TOML file from disk.
    """
    if theme_name == "default":
        return get_default_theme()
    key = (theme_name, _themes_dir_key(themes_dir))
    cached = _THEME_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        theme_path = resolve_theme_path(theme_name, themes_dir)
        if theme_path is None:  # noqa: SIM108
            theme = get_default_theme()
        else:
            theme = load_theme(theme_path)
    except Exception:  # noqa: BLE001
        theme = get_default_theme()
    _THEME_CACHE[key] = theme
    return theme


def get_available_themes(themes_dir: str | None = None) -> list[str]:
    """Return the list of available theme names, with "default" first.

    Results are cached per ``themes_dir`` because the on-disk theme set
    is stable for the lifetime of the process; ``eggpool rehash`` and
    other config-changing operations restart the server.  The cached
    entry stores a defensive copy so caller-side mutations do not
    corrupt the cache.
    """
    key = _themes_dir_key(themes_dir)
    cached = _THEMES_LIST_CACHE.get(key)
    if cached is not None:
        return list(cached)
    available = list_themes(themes_dir)
    if "default" not in available:
        available.insert(0, "default")
    _THEMES_LIST_CACHE[key] = list(available)
    return available


def _render_update_indicator(update_info: Any | None) -> str:
    """Render the footer update indicator or empty string.

    Accepts the same shape as :class:`UpdateInfo` plus ``None`` so
    routes can pass the live snapshot directly without constructing a
    dataclass instance.  Returns ``""`` whenever no update is
    advertised — the dashboard contract is "render nothing when there
    is no update" so the rest of the footer stays unchanged.
    """
    if update_info is None:
        return ""
    if not getattr(update_info, "update_available", False):
        return ""
    current = str(getattr(update_info, "current_version", "") or "")
    latest = str(getattr(update_info, "latest_version", "") or "")
    command = str(getattr(update_info, "update_command", "") or "eggpool update")
    if not latest:
        return ""
    return _UPDATE_INDICATOR_TEMPLATE.format(
        current=escape(current or "?"),
        latest=escape(latest),
        command=escape(command),
    )


def _render_layout(
    title: str,
    body: str,
    active_nav: str = "",
    period: str = "24h",
    refresh_interval_s: int = 15,
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    auto_refresh: bool = False,
    include_chart_js: bool = False,
    update_info: Any | None = None,
) -> str:
    """Wrap a page body in the standard layout.

    Chart.js is only loaded on pages that render a chart; doing so lazily
    avoids blocking initial render with a ~200 KB script on every page.
    When loaded it is appended at the end of ``<body>`` so it never
    blocks HTML parsing on the critical path.
    """
    nav = _render_nav(active_nav, period, available_themes, current_theme)
    theme_href = f"/static/theme.css?theme={_html_escape(current_theme)}"
    theme_link = f'<link rel="stylesheet" href="{theme_href}">' if current_theme else ""
    script_block = (
        _render_auto_refresh_script(refresh_interval_s) if auto_refresh else ""
    )
    # `dashboard.js` is intentionally always-on: it wires the mobile
    # burger menu, the update-command copy affordance, and the
    # timeseries controls. Its init functions guard themselves with
    # empty-result DOM queries so they are no-ops on pages that do not
    # use charts or timeseries. Only `chart.js` is gated behind
    # `include_chart_js` because it is ~200 KB and we want to keep it
    # off the critical path for non-chart pages.
    chart_script = (
        '<script defer src="/static/chart.js"></script>' if include_chart_js else ""
    )
    dashboard_script = '<script defer src="/static/dashboard.js"></script>'
    update_indicator = _render_update_indicator(update_info)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)}</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="preload" href="/static/dashboard.css" as="style">
<link rel="stylesheet" href="/static/dashboard.css">
{theme_link}
</head>
<body>
<svg class="egg-background" viewBox="0 0 256 256"
     preserveAspectRatio="xMidYMid meet"
     aria-hidden="true" focusable="false">
  <path class="shape"
        d="M128 30
           C82 30 55 88 57 145
           C59 202 89 231 128 231
           C167 231 197 202 199 145
           C201 88 174 30 128 30 Z" />
  <path class="thin"
        d="M86 132 H112 L126 111 L144 158 L159 132 H174" />
  <circle class="shape" cx="85" cy="132" r="5" />
  <circle class="shape" cx="174" cy="132" r="5" />
</svg>
<header class="topbar">
  <h1><a href="/?period={_html_escape(period)}&amp;theme={
        _html_escape(current_theme)
    }">EggPool</a></h1>
  {nav}
</header>
<main id="dashboard-content">
{body}
</main>
<footer>
  <small>Period: <span class="period-label">{_html_escape(period)}</span>
    &middot; auto-refresh {refresh_interval_s}s
    &middot; <span id="dashboard-updated">ready</span>{update_indicator}</small>
</footer>
{script_block}
{chart_script}
{dashboard_script}
</body>
</html>"""


def _render_auto_refresh_script(refresh_interval_s: int) -> str:
    """Render the small client-side refresher used by dashboard pages."""
    interval_ms = max(1, refresh_interval_s) * 1000
    return f"""<script>
(() => {{
  const intervalMs = {interval_ms};
  const content = document.getElementById("dashboard-content");
  const updated = document.getElementById("dashboard-updated");
  if (!content || !updated || !window.DOMParser) {{
    return;
  }}
  const refresh = async () => {{
    try {{
      const response = await fetch(window.location.href, {{
        cache: "no-store",
        headers: {{"x-dashboard-refresh": "1"}},
      }});
      if (!response.ok) {{
        return;
      }}
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const next = doc.getElementById("dashboard-content");
      if (next) {{
        content.innerHTML = next.innerHTML;
        updated.textContent = new Date().toLocaleTimeString();
        if (window.EggPoolDashboard) {{
          const dash = window.EggPoolDashboard;
          if (typeof dash.initGroupedTimeseriesCharts === "function") {{
            dash.initGroupedTimeseriesCharts();
          }}
          if (typeof dash.reinitTimeseriesChart === "function") {{
            dash.reinitTimeseriesChart();
          }}
        }}
      }}
    }} catch (_err) {{
      updated.textContent = "stale";
    }}
  }};
  window.setInterval(refresh, intervalMs);
}})();
</script>"""


def _render_nav(
    active_nav: str,
    period: str,
    available_themes: list[str] | None = None,
    current_theme: str = "",
) -> str:
    """Render the top navigation bar with theme selector."""
    items = [
        ("overview", "/", "Overview"),
        ("reliability", "/reliability", "Reliability"),
        ("routing", "/routing", "Routing"),
        ("accounts", "/accounts", "Accounts"),
        ("models", "/models", "Models"),
        ("latency", "/latency", "Latency"),
        ("pings", "/pings", "Pings"),
        ("bandwidth", "/bandwidth", "Bandwidth"),
        ("traces", "/traces", "Traces"),
        ("events", "/events", "Events"),
        ("timeseries", "/timeseries", "Timeseries"),
        ("runtime", "/runtime", "Runtime"),
    ]
    parts = ['<nav class="topnav">']
    # Mobile burger button + collapsible menu panel. The CSS layer hides
    # the burger and reveals `.topnav-menu` inline on viewports ≥761px;
    # on narrower viewports the burger is shown and the menu only opens
    # when JS toggles `.topnav-open` on the nav (see
    # `dashboard.js#initNavToggle`). The theme selector lives INSIDE the
    # menu so it is only reachable on narrow viewports when the menu is
    # expanded (saves vertical space in the collapsed state) and the
    # manual refresh button stays outside the menu so it is always
    # reachable.
    parts.append(
        '<button class="topnav-burger" type="button" '
        'data-tooltip="Open page menu" '
        'data-tooltip-open-label="Close page menu" '
        'aria-label="Open page menu" '
        'aria-expanded="false" aria-controls="topnav-menu">'
        '<svg class="topnav-burger-icon" viewBox="0 0 24 24" '
        'width="24" height="24" aria-hidden="true" focusable="false">'
        '<rect class="bar bar-1" x="0" y="0" width="24" height="2" rx="1"/>'
        '<rect class="bar bar-2" x="0" y="11" width="24" height="2" rx="1"/>'
        '<rect class="bar bar-3" x="0" y="22" width="24" height="2" rx="1"/>'
        "</svg>"
        "</button>"
    )
    parts.append('<div class="topnav-menu" id="topnav-menu">')
    for key, href, label in items:
        cls = "active" if key == active_nav else ""
        parts.append(
            f'<a class="{cls}" href="{href}?period={_html_escape(period)}'
            f'&amp;theme={_html_escape(current_theme)}">'
            f"{_html_escape(label)}</a>"
        )

    # Theme selector lives inside the menu so on narrow viewports it
    # only shows when the menu is expanded. On wider viewports the
    # menu is always visible (see `@media (min-width: 761px)` in
    # `dashboard.css`) so the selector stays reachable.
    themes: list[str] = available_themes or []
    if themes:
        theme_options: list[str] = []
        for name in themes:
            sel = " selected" if name == current_theme else ""
            theme_options.append(
                f'<option value="{_html_escape(name)}"{sel}>'
                f"{_html_escape(name)}</option>"
            )
        options_html = "".join(theme_options)
        parts.append(
            '<form method="get" class="theme-selector" '
            'data-tooltip="Switch dashboard theme" '
            'data-tooltip-pos="bottom" '
            'aria-label="Switch dashboard theme">'
            '<select name="theme" onchange="this.form.submit()">'
            f"{options_html}"
            "</select>"
            f'<input type="hidden" name="period" value="{_html_escape(period)}">'
            "</form>"
        )
    parts.append("</div>")

    # Manual refresh button
    parts.append(
        '<button type="button" class="topnav-refresh" '
        'data-tooltip="Reload this page" '
        'aria-label="Reload this page" '
        'onclick="window.location.reload()">'
        "↻</button>"
    )

    parts.append("</nav>")
    return "".join(parts)


def _render_period_selector(
    current: str,
    current_theme: str = "",
    extra_class: str = "",
) -> str:
    """Render a period selector form."""
    options = [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    items: list[str] = []
    for value, label in options:
        selected = ' selected="selected"' if value == current else ""
        items.append(
            f'<option value="{escape(value)}"{selected}>{escape(label)}</option>'
        )
    selector = "".join(items)
    theme_hidden = (
        f'<input type="hidden" name="theme" value="{escape_attr(current_theme)}">'
        if current_theme
        else ""
    )
    class_attr = "period-selector"
    if extra_class:
        class_attr = f"{class_attr} {escape_attr(extra_class)}"
    return (
        f'<form method="get" class="{class_attr}" '
        f'data-period-selector aria-label="Period selector">'
        f'<label for="period">Period: '
        f'<select id="period" name="period">'
        f"{selector}"
        f"</select>"
        f"</label>"
        f"{theme_hidden}"
        f"</form>"
    )


def _render_account_filters(
    period: str,
    current_theme: str,
    show_disabled: bool,
) -> str:
    """Render a single GET-form filter bar for the Accounts page.

    Bundles the period selector and the ``show_disabled`` toggle into
    one form so either change preserves the other via hidden inputs.
    Disabled rows are hidden by default so the page matches the
    operator's mental model after ``eggpool logout``; the toggle is
    opt-in and lives server-side in the URL (``?show_disabled=1``) so
    it survives refresh, is bookmarkable, and works without JS.
    """
    period_options = [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    period_items: list[str] = []
    for value, label in period_options:
        selected = ' selected="selected"' if value == period else ""
        period_items.append(
            f'<option value="{escape(value)}"{selected}>{escape(label)}</option>'
        )
    period_selector = "".join(period_items)

    show_options = [
        ("0", "Hide disabled accounts"),
        ("1", "Show disabled accounts"),
    ]
    show_items: list[str] = []
    show_value = "1" if show_disabled else "0"
    for value, label in show_options:
        selected = ' selected="selected"' if value == show_value else ""
        show_items.append(
            f'<option value="{escape(value)}"{selected}>{escape(label)}</option>'
        )
    show_selector = "".join(show_items)

    theme_hidden = (
        f'<input type="hidden" name="theme" value="{escape_attr(current_theme)}">'
        if current_theme
        else ""
    )
    return (
        f'<form method="get" class="period-selector account-filters" '
        f'data-period-selector aria-label="Account filters">'
        f'<label for="period">Period: </label>'
        f'<select id="period" name="period" data-auto-submit="1">'
        f"{period_selector}"
        f"</select>"
        f'<label for="show_disabled">Disabled: </label>'
        f'<select id="show_disabled" name="show_disabled" '
        f'data-auto-submit="1">'
        f"{show_selector}"
        f"</select>"
        f"{theme_hidden}"
        f"</form>"
    )


def _render_accounts_empty_state(
    show_disabled: bool,
    disabled_count: int,
) -> str:
    """Render the empty-state hint for the Accounts page.

    When the operator has filtered disabled rows out and there are no
    enabled rows left, offer a one-click opt-in to the historical view
    instead of the generic "No accounts configured." message. The link
    is plain anchor navigation, so it works without JS.
    """
    n_disabled = _coerce_int(disabled_count)
    if show_disabled or n_disabled <= 0:
        return '<p class="empty">No accounts configured.</p>'
    plural = "s" if n_disabled != 1 else ""
    return (
        '<p class="empty">'
        "No enabled accounts. "
        f"{escape(n_disabled)} disabled account{plural} hidden — "
        '<a href="?show_disabled=1">show them</a>.'
        "</p>"
    )


_HIGH_SPEND_ESTIMATED_DOLLARS = 10.0


def _th(label: str, *, priority: int = 1) -> str:
    """Render a ``<th>`` tagged with a responsive ``data-priority``.

    The CSS layer (see ``dashboard.css`` ``@media (max-width: …)``)
    hides ``data-priority="3"`` columns below 760px and
    ``data-priority="2"`` columns below 480px so wide tables fit phone
    viewports without forcing horizontal scroll on the most common
    column sets. ``data-priority="1"`` is always shown.

    Helper exists so every table renderer emits identical markup and
    the responsive contract is enforced at the source rather than
    relying on each renderer to remember the convention.
    """
    safe_label = _html_escape(label)
    return f'<th data-priority="{priority}">{safe_label}</th>'


def _td_priority(content: str, priority: int, *, class_: str | None = None) -> str:
    """Render a ``<td>`` with the same responsive priority as its ``<th>``.

    The CSS responsive rules key on the priority attribute; the matching
    ``<td>`` must carry it too or the row misaligns when a column is
    hidden. Renderers that produce rows cell-by-cell must pair every
    ``_th(..., priority=N)`` with ``_td_priority(..., priority=N)``.

    ``class_`` is the optional CSS class (e.g. ``"yes"``/``"no"`` for
    boolean glyphs). Pass an empty string when none is wanted; pass
    ``None`` (default) to omit the attribute entirely.
    """
    if class_:
        return f'<td data-priority="{priority}" class="{class_}">{content}</td>'
    return f'<td data-priority="{priority}">{content}</td>'


def _render_metric_card(
    *,
    title: str,
    metric: str | None = None,
    sub: str | None = None,
    tooltip: str | None = None,
    warning: bool = False,
    extra_subs: tuple[str, ...] = (),
) -> str:
    """Render a dashboard metric card with the shared tooltip contract."""
    tooltip_text = tooltip or _CARD_TOOLTIPS.get(title, title)
    tooltip_attr = _html_escape(tooltip_text, quote=True)
    card_class = "card warning" if warning else "card"
    parts = [
        f'<div class="{card_class}" data-tooltip="{tooltip_attr}" '
        f'data-tooltip-pos="bottom" '
        f'aria-label="{tooltip_attr}">',
        f"<h3>{_html_escape(title)}</h3>",
    ]
    if metric is not None:
        parts.append(f'<p class="metric">{metric}</p>')
    if sub is not None:
        parts.append(f'<p class="sub">{sub}</p>')
    for line in extra_subs:
        parts.append(f'<p class="sub">{line}</p>')
    parts.append("</div>")
    return "".join(parts)


def _render_pricing_exactness_badge(
    *,
    exact: int,
    derived: int,
    partial: int,
    estimated: int,
    unknown_exc: int,
    provider_reported: int = 0,
) -> str:
    """Render a compact exactness badge.

    The badge is colored by the worst-case exactness observed: green when
    every row is ``exact``, ``derived``, or ``provider_reported``, yellow
    when any ``partial`` row exists, red when ``estimated`` or ``unknown``
    dominates. Used on the Accounts / Models tables so operators can spot
    rows whose cost numbers are advisory at a glance. ``provider_reported``
    is the most-trusted category because it reflects the upstream's own
    billing record.
    """
    total = exact + derived + partial + estimated + unknown_exc + provider_reported
    if total == 0:
        return '<span class="exactness-badge empty">—</span>'
    if estimated == total or unknown_exc == total:
        css_class = "exactness-badge est-major"
    elif estimated + unknown_exc > 0 or partial > 0:
        css_class = "exactness-badge partial-mix"
    else:
        css_class = "exactness-badge derived"
    summary = (
        f"u:{provider_reported},e:{exact},d:{derived},"
        f"p:{partial},~:{estimated},?:{unknown_exc}"
    )
    return (
        f'<span class="{css_class}" data-tooltip="{escape(summary)}" '
        f'aria-label="{escape(summary)}">{summary}</span>'
    )


# Status-to-CSS-class mapping for model-info pills
_MODEL_INFO_PILL_CLASSES: dict[str, str] = {
    "fresh": "pill-fresh",
    "partial": "pill-partial",
    "sparse_new": "pill-sparse",
    "sparse": "pill-sparse",
    "stale": "pill-stale",
    "conflicting": "pill-conflict",
    "conflict": "pill-conflict",
    "unmatched": "pill-unmatched",
    "source_unavailable": "pill-source-unavailable",
    "source-unavailable": "pill-source-unavailable",
    "manual_override": "pill-partial",
    "manual": "pill-partial",
    "withdrawn": "pill-stale",
}


def _render_model_info_pill(info: dict[str, Any] | None) -> str:
    """Render a compact model-info status pill.

    Returns an empty string when no model-info is available so the
    column stays clean.  All user-visible text is HTML-escaped and
    passed as a ``title`` tooltip attribute.
    """
    if not info:
        return (
            '<span class="pill pill-unknown" data-tooltip="No model info available" '
            'aria-label="No model info available">—</span>'
        )

    status = info.get("status", "unknown")
    sparse = info.get("sparse", False)
    summary = info.get("summary", "")
    sources = info.get("sources", [])
    last_refreshed = info.get("last_refreshed_at")

    css_class = _MODEL_INFO_PILL_CLASSES.get(status, "pill-unknown")

    # Build tooltip text (plain text, no HTML)
    tooltip_parts: list[str] = []
    if summary:
        tooltip_parts.append(summary)
    if sources:
        tooltip_parts.append(f"Sources: {', '.join(sources)}")
    if last_refreshed:
        tooltip_parts.append(f"Last checked: {last_refreshed}")
    tooltip = ". ".join(tooltip_parts) if tooltip_parts else status

    # Display label: short status
    display = display_model_info_status(status)
    if sparse and status not in ("sparse", "sparse_new"):
        display += " (sparse)"

    return (
        f'<span class="pill {css_class}" data-tooltip="{escape(tooltip)}" '
        f'aria-label="{escape(tooltip)}">'
        f"{escape(display)}</span>"
    )


def _render_availability_pill(row: dict[str, Any]) -> str:
    """Render a compact availability pill for a catalog row.

    Reads ``catalog_status`` / ``available`` from the merged row and
    surfaces a ``pill-available`` / ``pill-unavailable` /
    ``pill-configured` indicator. When neither flag is present (e.g.
    legacy traffic-only row with no catalog backing), the pill reads
    "—" so the column stays clean.
    """
    status = row.get("catalog_status")
    if status is None:
        # Fall back to the ``available`` boolean if set.
        if row.get("available") is True:
            status = "available"
        elif row.get("available") is False:
            status = "unavailable"
        else:
            return (
                '<span class="pill pill-unknown" '
                'data-tooltip="No catalog data" '
                'aria-label="No catalog data">—</span>'
            )
    status_str = str(status)
    css_map: dict[str, str] = {
        "available": "pill-available",
        "unavailable": "pill-unavailable",
        "configured": "pill-configured",
    }
    tooltip_map: dict[str, str] = {
        "available": "Catalog entry with resolved protocol; can be routed.",
        "unavailable": "Catalog entry without resolved protocol; cannot be routed.",
        "configured": "Catalog entry discovered; routing status pending.",
    }
    css_class = css_map.get(status_str, "pill-unknown")
    tooltip = tooltip_map.get(status_str, status_str)
    return (
        f'<span class="pill {css_class}" data-tooltip="{escape(tooltip)}" '
        f'aria-label="{escape(tooltip)}">'
        f"{escape(status_str)}</span>"
    )


def _render_pricing_warnings(
    accounts: list[dict[str, Any]],
) -> str:
    """Render a banner warning about high-spend estimated rows.

    Returns an empty string when no row exceeds
    ``_HIGH_SPEND_ESTIMATED_DOLLARS`` in estimated cost. The banner
    links to the affected accounts so operators can drill in.
    """
    high_spend_rows: list[tuple[str, float, float]] = []
    for row in accounts:
        cost_micro = int(row.get("cost_microdollars", 0) or 0)
        est_fraction_raw = row.get("estimated_cost_fraction")
        if est_fraction_raw is None:
            continue
        est_cost_micro = int(round(cost_micro * float(est_fraction_raw)))
        est_cost_dollars = est_cost_micro / 1_000_000.0
        if est_cost_dollars >= _HIGH_SPEND_ESTIMATED_DOLLARS:
            high_spend_rows.append(
                (
                    str(row.get("account_name", "?")),
                    est_cost_dollars,
                    float(est_fraction_raw),
                )
            )
    if not high_spend_rows:
        return ""
    parts = [
        '<div class="panel warn pricing-warning">',
        "<strong>Pricing warning:</strong> ",
        "the following accounts have substantial cost on estimated "
        "(non-exact) pricing in the selected period:",
        "<ul>",
    ]
    for name, dollars, fraction in high_spend_rows:
        parts.append(
            f"<li><code>{escape(name)}</code>: "
            f"~${dollars:,.2f} estimated "
            f"({fraction * 100:.0f}% of total)</li>"
        )
    parts.append("</ul></div>")
    return "".join(parts)


def _render_provider_health(ping_summary: list[dict[str, Any]]) -> str:
    """Render the provider health section for the overview page."""
    if not ping_summary:
        return ""
    rows: list[str] = []
    for row in ping_summary:
        pid = escape(str(row.get("provider_id", "")))
        avg_lat = format_latency(row.get("avg_latency_ms", 0))
        success_rate = row.get("success_rate", 0)
        last_at = str(row.get("last_ping_at", ""))
        model_count = int(row.get("last_model_count", 0))
        status = "healthy" if float(success_rate or 0) >= 90 else "degraded"
        rows.append(
            f"<tr>"
            f"{_td_priority(pid, 1)}"
            f"{_td_priority(status, 1, class_=status)}"
            f"{_td_priority(avg_lat, 2)}"
            f"{_td_priority(f'{success_rate}%', 2)}"
            f"{_td_priority(str(model_count), 3)}"
            f"{_td_priority(last_at, 3)}"
            f"</tr>"
        )
    return (
        '<section class="panel">'
        "<h3>Provider health</h3>"
        '<table class="data">'
        "<thead><tr>"
        + _th("Provider")
        + _th("Status")
        + _th("Avg latency", priority=2)
        + _th("Success rate", priority=2)
        + _th("Models", priority=3)
        + _th("Last ping", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
        + "</section>"
    )


def _render_system_health(
    pending_health: dict[str, Any] | None,
    attempt_stats: dict[str, Any] | None,
    operational_summary: list[dict[str, Any]] | None,
) -> str:
    """Render the System Health row for the overview page.

    Shows: pending request count + oldest pending age, active reservation
    count + reserved cost, stale finalizer cleaned count over 24h,
    finalizer timeout count over 24h, retry rate over the selected
    period, and first-attempt success rate over the selected period.

    Empty pending_health / attempt_stats / operational_summary produce a
    zero-valued rendering so the layout is stable across empty states.
    Returns the empty string when none of the inputs provide data, so
    pages without health data don't render a meaningless row.
    """
    pending = pending_health or {}
    attempts = attempt_stats or {}
    summary_rows = operational_summary or []

    pending_count = int(pending.get("pending_count", 0))
    pending_age = format_age_seconds(pending.get("oldest_pending_age_seconds"))
    reservation_count = int(pending.get("active_reservation_count", 0))
    reserved_cost = format_microdollars(pending.get("active_reserved_microdollars", 0))
    stale_pending_count = int(pending.get("stale_pending_count", 0))

    retry_rate = float(attempts.get("retry_rate", 0.0) or 0.0)
    total_attempts = int(attempts.get("total_attempts", 0) or 0)
    success_attempts = int(attempts.get("success_attempts", 0) or 0)
    first_attempt_success_rate = (
        success_attempts / total_attempts if total_attempts > 0 else 0.0
    )

    stale_finalizer_cleaned = 0
    crash_recovery = 0
    for row in summary_rows:
        event_type = str(row.get("event_type", ""))
        event_count = int(row.get("event_count", 0) or 0)
        if event_type == "stale_request_finalizer":
            stale_finalizer_cleaned += event_count
        elif event_type == "crash_recovery":
            crash_recovery += event_count

    has_data = bool(pending_health) or bool(attempt_stats) or bool(operational_summary)
    if not has_data:
        return ""

    pending_warn = pending_count > 0 and stale_pending_count > 0
    cards = "".join(
        [
            _render_metric_card(
                title="Pending requests",
                metric=f"{pending_count:,}",
                sub=f"oldest {pending_age} · stale {stale_pending_count}",
                warning=pending_warn,
            ),
            _render_metric_card(
                title="Active reservations",
                metric=f"{reservation_count:,}",
                sub=f"reserved {reserved_cost}",
            ),
            _render_metric_card(
                title="Finalizer (24h)",
                metric=format_int(stale_finalizer_cleaned),
                sub=f"cleaned · {crash_recovery} recovery",
            ),
            _render_metric_card(
                title="Retry rate",
                metric=format_percent(retry_rate, digits=1),
                sub=f"of {format_int(total_attempts)} attempts",
            ),
            _render_metric_card(
                title="First-attempt success",
                metric=format_percent(first_attempt_success_rate, digits=1),
                sub="no retry needed",
            ),
        ]
    )
    return f"""
<section class="cards system-health">
  {cards}
</section>
"""


def _render_ip_stats(ip_stats: list[dict[str, Any]]) -> str:
    """Render the per-IP statistics section for the overview page."""
    if not ip_stats:
        return ""
    rows: list[str] = []
    for row in ip_stats[:10]:  # Show top 10 IPs
        ip = escape(str(row.get("client_ip", "unknown")))
        req_count = int(row.get("request_count", 0))
        in_tok = format_tokens(row.get("input_tokens", 0))
        out_tok = format_tokens(row.get("output_tokens", 0))
        total_tok = format_tokens(row.get("total_tokens", 0))
        cost = format_microdollars(row.get("cost_microdollars", 0))
        avg_lat = format_latency(row.get("avg_latency_ms", 0.0))
        error_count = int(row.get("error_count", 0))
        unique_models = int(row.get("unique_models", 0))
        rows.append(
            f"<tr>"
            f"{_td_priority(ip, 1)}"
            f"{_td_priority(f'{req_count:,}', 1)}"
            f"{_td_priority(cost, 1)}"
            f"{_td_priority(avg_lat, 2)}"
            f"{_td_priority(f'{error_count:,}', 2)}"
            f"{_td_priority(in_tok, 3)}"
            f"{_td_priority(out_tok, 3)}"
            f"{_td_priority(total_tok, 3)}"
            f"{_td_priority(str(unique_models), 3)}"
            f"</tr>"
        )
    return (
        '<section class="panel">'
        "<h3>Request breakdown by IP</h3>"
        '<table class="data compact">'
        "<thead><tr>"
        # Priority 1 — always shown
        + _th("IP Address")
        + _th("Requests")
        + _th("Cost")
        # Priority 2 — shown on tablet+
        + _th("Avg latency", priority=2)
        + _th("Errors", priority=2)
        # Priority 3 — desktop only
        + _th("Input tokens", priority=3)
        + _th("Output tokens", priority=3)
        + _th("Total tokens", priority=3)
        + _th("Models", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
        + "</section>"
    )


def _render_timeseries_chart(
    period: str = "24h", initial_data: list[dict[str, Any]] | None = None
) -> str:
    """Render an interactive timeseries chart using Chart.js.

    The chart is initialised client-side from a sibling JSON data island
    so that Chart.js (loaded with ``defer``) is available before the
    canvas is touched. ``window.EggPoolDashboard.reinitTimeseriesChart``
    consumes the data island and falls back to ``GET /api/timeseries``
    when no inlined payload is available.
    """
    payload = list(initial_data or [])
    payload_json = json.dumps(payload)
    period_attr = escape_attr(period)
    return f"""
<section class="panel">
  <h3>Request timeseries</h3>
  <div class="chart-wrap" style="height: 300px;">
    <canvas id="timeseries-chart" data-period="{period_attr}"></canvas>
  </div>
</section>
<script type="application/json" id="timeseries-initial-data"
        data-period="{period_attr}">{payload_json}</script>
"""


def _render_model_glance(models: list[dict[str, Any]]) -> str:
    """Render a compact top-model table for the overview page."""
    if not models:
        return '<p class="empty">No model activity in this period.</p>'
    rows: list[str] = []
    for row in models[:10]:
        total_tok = format_tokens(row.get("total_tokens", 0))
        req_count = int(row.get("request_count", 0))
        err_count = int(row.get("error_count", 0))
        rows.append(
            f"<tr>"
            f"{_td_priority(escape(row.get('model_id', '')), 1)}"
            f"{_td_priority(f'{req_count:,}', 1)}"
            f"{_td_priority(format_microdollars(row.get('cost_microdollars', 0)), 1)}"
            f"{_td_priority(escape(row.get('provider_id', '')), 2)}"
            f"{_td_priority(f'{err_count:,}', 2)}"
            f"{_td_priority(format_latency(row.get('avg_latency_ms', 0.0)), 2)}"
            f"{_td_priority(total_tok, 3)}"
            f"</tr>"
        )
    return (
        '<table class="data compact">'
        + "<thead><tr>"
        # Priority 1 — always shown
        + _th("Model")
        + _th("Reqs")
        + _th("Cost")
        # Priority 2 — shown on tablet+
        + _th("Provider", priority=2)
        + _th("Errs", priority=2)
        + _th("Latency", priority=2)
        # Priority 3 — desktop only
        + _th("Total tokens", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
    )


def _render_event_glance(events: list[dict[str, Any]]) -> str:
    """Render recent events for the overview page."""
    if not events:
        return '<p class="empty">No recent events.</p>'
    rows: list[str] = []
    for row in events[:10]:
        event_type = str(row.get("event_type", ""))
        badge_tooltip = _status_badge_tooltip(event_type) or ""
        badge_attrs = (
            f' data-tooltip="{_html_escape(badge_tooltip)}"'
            f' aria-label="{_html_escape(badge_tooltip)}"'
            if badge_tooltip
            else ""
        )
        badge_html = (
            f'<span class="event-tag {sanitize_class_name(event_type)}"'
            f"{badge_attrs}>{escape(event_type)}</span>"
        )
        rows.append(
            f"<tr>"
            f"{_td_priority(format_timestamp(row.get('created_at', '')), 1)}"
            f"{_td_priority(escape(row.get('account_name', '')), 1)}"
            f"{_td_priority(badge_html, 1)}"
            f"{_td_priority(truncate(row.get('details', ''), 120), 2)}"
            f"</tr>"
        )
    return (
        '<table class="data compact">'
        + "<thead><tr>"
        + _th("When")
        + _th("Account")
        + _th("Type")
        + _th("Details", priority=2)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
    )


def _render_bandwidth_heatmap(
    daily_data: list[dict[str, Any]],
    title: str = "Bandwidth activity (last 90 days)",
    heatmap_colors: list[str] | None = None,
    value_field: str = "bytes",
) -> str:
    """Render a GitHub-style contribution heatmap as inline SVG.

    ``value_field`` selects which per-day value to plot.  ``"bytes"`` is
    the default (sums ``bytes_emitted`` + ``bytes_received`` per day) and
    keeps the dedicated ``/bandwidth`` page working unchanged.  Pass
    ``"total_tokens"`` to render the overview's token-activity heatmap.
    """
    if not daily_data:
        return '<p class="empty">No activity data available.</p>'

    # Build day -> aggregated-value lookup and day -> request_count lookup.
    day_requests: dict[str, int] = {}
    if value_field == "bytes":
        day_values: dict[str, int] = {}
        for row in daily_data:
            day_str = str(row.get("day", ""))
            val = int(row.get("bytes_emitted", 0)) + int(row.get("bytes_received", 0))
            day_values[day_str] = val
            day_requests[day_str] = int(row.get("request_count", 0))
        formatter: Any = format_bytes
        in_out: dict[str, tuple[int, int]] = {
            str(row.get("day", "")): (
                int(row.get("bytes_received", 0)),
                int(row.get("bytes_emitted", 0)),
            )
            for row in daily_data
        }
    elif value_field == "total_tokens":
        day_values = {
            str(row.get("day", "")): int(row.get("total_tokens", 0))
            for row in daily_data
        }
        formatter = format_tokens
        in_out = {}
        for row in daily_data:
            day_str = str(row.get("day", ""))
            day_requests[day_str] = int(row.get("request_count", 0))
    else:
        raise ValueError(f"unsupported heatmap value_field: {value_field!r}")

    # Date range: last 90 days ending today
    today = date.today()
    start_date = today - timedelta(days=89)

    # Pad start to Sunday (GitHub convention: weeks start on Sunday)
    weekday = start_date.weekday()  # Monday=0, Sunday=6
    padding_days = (weekday + 1) % 7  # days to go back to Sunday
    grid_start = start_date - timedelta(days=padding_days)

    # Calculate number of weeks
    num_weeks = ((today - grid_start).days // 7) + 1

    # Find max value for color scaling
    max_val = max(day_values.values()) if day_values else 1
    if max_val == 0:
        max_val = 1

    # Color scale (theme-aware or default GitHub Primer green)
    colors = heatmap_colors or _DEFAULT_HEATMAP_COLORS

    def _get_color(value: int) -> str:
        if value == 0:
            return colors[0]
        ratio = value / max_val
        if ratio < 0.25:
            return colors[1]
        if ratio < 0.5:
            return colors[2]
        if ratio < 0.75:
            return colors[3]
        return colors[4]

    cell_size = 13
    cell_gap = 3
    step = cell_size + cell_gap
    left_margin = 36
    top_margin = 20
    svg_width = left_margin + num_weeks * step + 10
    svg_height = top_margin + 7 * step + 10

    # Day-of-week labels (Mon, Wed, Fri)
    day_labels = {1: "Mon", 3: "Wed", 5: "Fri"}

    # Month labels
    month_labels: dict[int, str] = {}
    for i in range(12):
        dt = date(2000, i + 1, 1)
        month_labels[i + 1] = dt.strftime("%b")

    cells: list[str] = []

    # Day-of-week labels
    for day_num, label_text in day_labels.items():
        y = top_margin + day_num * step + cell_size // 2
        cells.append(
            f'<text x="0" y="{y}" class="heatmap-label" '
            f'text-anchor="start" dominant-baseline="central">'
            f"{label_text}</text>"
        )

    # Month labels
    month_positions: dict[int, int] = {}
    current_month = -1
    for week in range(num_weeks):
        week_start = grid_start + timedelta(weeks=week)
        if week_start.month != current_month:
            current_month = week_start.month
            month_positions[current_month] = week

    for month_num, week_pos in month_positions.items():
        x = left_margin + week_pos * step
        cells.append(
            f'<text x="{x}" y="10" class="heatmap-label" '
            f'text-anchor="start">{month_labels.get(month_num, "")}</text>'
        )

    # Day cells and overlay hitboxes.  Hitboxes are emitted for every
    # grid slot (week x day_of_week) in column-major order so the
    # CSS grid's column-first auto-flow places each hitbox at the SVG
    # coordinates of the day it represents.  Out-of-range days
    # (before start_date or after today) get empty hitboxes that take
    # up the right grid slot but show no tooltip on hover; without
    # this, every visible hitbox shifts to the wrong row/column and
    # the tooltip for the day being hovered no longer matches the
    # highlighted day.
    hitboxes: list[str] = []
    for week in range(num_weeks):
        for day_of_week in range(7):
            cell_date = grid_start + timedelta(weeks=week, days=day_of_week)
            in_visible_range = start_date <= cell_date <= today
            if in_visible_range:
                day_str = cell_date.isoformat()
                value = day_values.get(day_str, 0)
                color = _get_color(value)
                x = left_margin + week * step
                y = top_margin + day_of_week * step
                tooltip = f"{day_str}: {formatter(value)}"
                request_count = day_requests.get(day_str, 0)
                request_count_text = (
                    f"{request_count:,} request{'s' if request_count != 1 else ''}"
                )
                pretty_date = _format_tooltip_date(day_str)
                if value_field == "bytes":
                    in_bytes, out_bytes = in_out.get(day_str, (0, 0))
                    tooltip_text = (
                        f"{pretty_date}\n"
                        f"{format_bytes(in_bytes)} in · "
                        f"{format_bytes(out_bytes)} out · "
                        f"{request_count_text}"
                    )
                else:
                    tooltip_text = (
                        f"{pretty_date}\n{formatter(value)} tokens · "
                        f"{request_count_text}"
                    )
                tooltip_attr = _html_escape(tooltip_text, quote=True)
                cells.append(
                    f'<rect x="{x}" y="{y}" width="{cell_size}" '
                    f'height="{cell_size}" rx="2" fill="{color}" '
                    f'class="heatmap-cell" pointer-events="none">'
                    f"<title>{_html_escape(tooltip)}</title></rect>"
                )
                hitboxes.append(
                    f'<div class="heatmap-hitbox" '
                    f'data-tooltip="{tooltip_attr}" '
                    f'aria-label="{tooltip_attr}"></div>'
                )
            else:
                hitboxes.append('<div class="heatmap-hitbox"></div>')

    svg = (
        f'<svg width="{svg_width}" height="{svg_height}" '
        f'viewBox="0 0 {svg_width} {svg_height}" '
        f'role="img" aria-label="{_html_escape(title)}">'
        f"{''.join(cells)}</svg>"
    )
    overlay = (
        f'<div class="heatmap-overlay" '
        f'style="--heatmap-weeks: {num_weeks}" '
        f'aria-hidden="true">{"".join(hitboxes)}</div>'
    )

    return f'<div class="heatmap">{svg}{overlay}</div>'


def _render_thinking_stats(thinking_stats: dict[str, Any] | None) -> str:
    """Render the Thinking/Reasoning metrics card for the overview page.

    Shows total thinking requests, per-decision breakdown, and
    per-protocol breakdown when data is available.  Returns an empty
    string when the counter snapshot is empty so the layout stays
    clean during idle periods.
    """
    if not thinking_stats:
        return ""
    total = int(thinking_stats.get("total", 0) or 0)
    if total == 0:
        return ""
    label_breakdown: dict[str, dict[str, int]] = thinking_stats.get(
        "label_breakdown", {}
    )

    # Per-decision breakdown from the "requested" category
    requested_keys: dict[str, int] = label_breakdown.get("requested", {})
    requested_total = sum(requested_keys.values())

    # Per-decision breakdown from specific decision categories
    transcoded = sum(label_breakdown.get("transcoded", {}).values())
    dropped = sum(label_breakdown.get("dropped", {}).values())
    rejected = sum(label_breakdown.get("rejected", {}).values())
    unknown_cap = sum(label_breakdown.get("unknown_capability", {}).values())
    unsupported_cap = sum(label_breakdown.get("unsupported_capability", {}).values())
    budget_clamped = sum(label_breakdown.get("budget_clamped", {}).values())

    sub_parts: list[str] = []
    if requested_total > 0:
        sub_parts.append(f"{requested_total:,} requested")
    if transcoded > 0:
        sub_parts.append(f"{transcoded:,} transcoded")
    if dropped > 0:
        sub_parts.append(f"{dropped:,} dropped")
    if rejected > 0:
        sub_parts.append(f"{rejected:,} rejected")
    if unknown_cap > 0:
        sub_parts.append(f"{unknown_cap:,} unknown cap")
    if unsupported_cap > 0:
        sub_parts.append(f"{unsupported_cap:,} unsupported cap")
    if budget_clamped > 0:
        sub_parts.append(f"{budget_clamped:,} budget-clamped")
    sub_text = " · ".join(sub_parts) if sub_parts else "no decisions recorded"

    return f"""
<section class="cards">
  {
        _render_metric_card(
            title="Thinking/Reasoning",
            metric=f"{total:,}",
            sub=sub_text,
            tooltip="In-memory thinking/reasoning observability counters. "
            "Tracks per-request thinking decisions made by the transcoder "
            "and routing layers.",
        )
    }
</section>
"""


def render_overview(
    overview: dict[str, Any],
    accounts: list[dict[str, Any]],
    account_filter: str = "",
    period: str = "24h",
    refresh_interval_s: int = 60,
    bandwidth_daily: list[dict[str, Any]] | None = None,
    ping_summary: list[dict[str, Any]] | None = None,
    models: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    theme_css: str = "",
    heatmap_colors: list[str] | None = None,
    available_themes: list[str] | None = None,
    current_theme: str = "",
    ip_stats: list[dict[str, Any]] | None = None,
    timeseries: list[dict[str, Any]] | None = None,
    pending_health: dict[str, Any] | None = None,
    attempt_stats: dict[str, Any] | None = None,
    operational_summary: list[dict[str, Any]] | None = None,
    update_info: Any | None = None,
    *,
    show_disabled: bool = False,
    disabled_count: int = 0,
    enabled_count: int = 0,
    thinking_stats: dict[str, Any] | None = None,
) -> str:
    """Render the overview dashboard page.

    ``show_disabled`` toggles whether disabled (soft-deleted) accounts
    appear in the Account breakdown table. Defaults to False so the
    overview matches the operator's mental model after
    ``eggpool logout``: only active accounts are visible by default.

    ``disabled_count`` is the total disabled-row count, used only when
    ``accounts`` is empty AND ``show_disabled`` is False: the empty
    state becomes a one-click "N disabled — show them?" hint instead
    of the generic "No accounts configured." message. The same total
    also drives the panel-header chip and the "Show N disabled"
    toggle button label.

    ``enabled_count`` is the number of rows in the currently-rendered
    ``accounts`` list with ``account_enabled == 1``. It drives the
    panel-header "X enabled · Y disabled" chip and stays accurate
    whether or not ``show_disabled`` is on.

    ``thinking_stats`` is the snapshot from
    ``eggpool.metrics.thinking.get_counter().snapshot()``. When
    provided and non-empty, a Thinking/Reasoning metric card is
    rendered alongside the existing token and streaming cards.
    """
    summary = overview.get("summary", {})
    imbalance = overview.get("imbalance", {})

    cost = format_microdollars(summary.get("total_cost_microdollars", 0))
    total = int(summary.get("total_requests", 0))
    errors = int(summary.get("error_requests", 0))
    success = int(summary.get("successful_requests", 0))
    error_rate = float(summary.get("error_rate", 0.0))
    in_tok = format_tokens(summary.get("total_input_tokens", 0))
    out_tok = format_tokens(summary.get("total_output_tokens", 0))
    total_tok = format_tokens(summary.get("total_tokens", 0))
    latency = format_latency(summary.get("avg_latency_ms", 0.0))
    imb_pct = format_percent(float(imbalance.get("imbalance_ratio", 0.0)))

    cache_read = format_tokens(summary.get("total_cache_read_tokens", 0))
    cache_write = format_tokens(summary.get("total_cache_write_tokens", 0))
    total_cache_read_tokens = int(summary.get("total_cache_read_tokens", 0))
    total_input_tokens = int(summary.get("total_input_tokens", 0))
    if total_input_tokens > 0:
        cache_read_ratio = total_cache_read_tokens / total_input_tokens
    else:
        cache_read_ratio = summary.get("cache_read_ratio")
    cache_read_pct = _format_percent_unit(cache_read_ratio, digits=1)
    reasoning = format_tokens(summary.get("total_reasoning_tokens", 0))
    streamed = int(summary.get("streamed_requests", 0))
    non_streamed = int(summary.get("non_streamed_requests", 0))
    exact = int(summary.get("exact_count", 0))
    derived = int(summary.get("derived_count", 0))
    estimated = int(summary.get("estimated_count", 0))
    unknown_exc = int(summary.get("unknown_count", 0))
    provider_reported = int(summary.get("provider_reported_count", 0))
    bytes_in = format_bytes(summary.get("total_bytes_received", 0))
    bytes_out = format_bytes(summary.get("total_bytes_emitted", 0))

    avg_ttft = format_latency(summary.get("avg_ttft_ms", 0.0))
    p50_ttft = format_latency(summary.get("p50_ttft_ms", 0.0))
    p99_ttft = format_latency(summary.get("p99_ttft_ms", 0.0))
    throughput = format_tokens_per_second(summary.get("tokens_per_second", 0.0))

    most: dict[str, Any] = imbalance.get("most_used") or {}
    least: dict[str, Any] = imbalance.get("least_used") or {}

    body = f"""
<h2>Overview</h2>
{_render_period_selector(period, current_theme)}

{_render_system_health(pending_health, attempt_stats, operational_summary)}

<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Requests",
                    metric=f"{total:,}",
                    sub=f"Success {success:,} · Errors {errors:,}",
                ),
                _render_metric_card(
                    title="Error rate",
                    metric=format_percent(error_rate),
                    sub=f"avg latency {latency}",
                ),
                _render_metric_card(
                    title="Total cost",
                    metric=cost,
                    sub=(
                        f"in {in_tok} · out {out_tok} · total {total_tok}"
                        + (
                            f" · {provider_reported:,} provider-billed"
                            if provider_reported > 0
                            else ""
                        )
                    ),
                ),
                _render_metric_card(
                    title="Utilization imbalance",
                    metric=imb_pct,
                    sub="CV across active accounts",
                ),
            ]
        )
    }
</section>

<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Total tokens",
                    metric=total_tok,
                    sub=f"in {in_tok} · out {out_tok}",
                ),
                _render_metric_card(
                    title="Cache tokens",
                    metric=cache_read,
                    sub=f"{cache_read_pct} of input · write {cache_write}",
                ),
                _render_metric_card(
                    title="Reasoning tokens",
                    metric=reasoning,
                    sub="extended thinking",
                ),
                _render_metric_card(
                    title="Throughput",
                    metric=throughput,
                    sub="aggregate Σtokens / Σlatency",
                ),
                _render_metric_card(
                    title="Streaming",
                    metric=f"{streamed:,}",
                    sub=f"streamed · {non_streamed:,} non-streamed",
                ),
                _render_metric_card(
                    title="Exactness",
                    metric=f"{exact:,}",
                    sub=(
                        f"exact · {derived:,} derived · "
                        f"{provider_reported:,} upstream · "
                        f"{estimated:,} est · {unknown_exc:,} unk"
                    ),
                ),
            ]
        )
    }
</section>

<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Bandwidth received",
                    metric=bytes_in,
                    sub="client → proxy",
                ),
                _render_metric_card(
                    title="Bandwidth emitted",
                    metric=bytes_out,
                    sub="upstream → proxy",
                ),
                _render_metric_card(
                    title="Avg TTFT (streamed)",
                    metric=avg_ttft,
                    sub=f"P50 {p50_ttft} · P99 {p99_ttft}",
                ),
            ]
        )
    }
</section>

{_render_thinking_stats(thinking_stats)}

<section class="panel">
  <div class="panel-header">
    <h3>Account breakdown{_account_count_chip(enabled_count, disabled_count)}</h3>
    {
        _render_account_breakdown_filter(
            period, current_theme, show_disabled, disabled_count
        )
    }
  </div>
  {_render_account_breakdown_body(accounts, show_disabled, disabled_count)}
</section>

{_render_timeseries_chart(period, initial_data=timeseries)}

<section class="overview-grid">
  <div class="panel">
    <h3>Top models</h3>
    {_render_model_glance(models or [])}
  </div>
  <div class="panel">
    <h3>Recent events</h3>
    {_render_event_glance(events or [])}
  </div>
</section>

<section class="panel">
  <h3>Utilization range</h3>
  <p>
    Most used: <strong>{_html_escape(str(most.get("name", "—")))}</strong>
    ({format_microdollars(most.get("cost_microdollars", 0))})
    &mdash; Least used:
    <strong>{_html_escape(str(least.get("name", "—")))}</strong>
    ({format_microdollars(least.get("cost_microdollars", 0))})
  </p>
</section>

{_render_ip_stats(ip_stats or [])}

<section class="panel">
  <h3>Token activity (last 90 days)</h3>
  {
        _render_bandwidth_heatmap(
            bandwidth_daily or [],
            title="Token activity (last 90 days)",
            heatmap_colors=heatmap_colors,
            value_field="total_tokens",
        )
    }
</section>

{_render_provider_health(ping_summary or [])}
"""
    return _render_layout(
        title="Overview",
        body=body,
        active_nav="overview",
        period=period,
        refresh_interval_s=refresh_interval_s,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        auto_refresh=True,
        include_chart_js=True,
        update_info=update_info,
    )


def _build_href_with_state(
    *,
    period: str | None = None,
    theme: str | None = None,
    show_disabled: str | None = None,
) -> str:
    """Build a query string that preserves operator URL state.

    The overview's Show/Hide disabled toggle, the panel-header count
    chip "show them" link, and any future panel that wants the same
    treatment all need to reload the page with one knob flipped while
    leaving the rest of the URL alone. Centralizing the construction
    here keeps the order and escaping consistent across call sites.
    """
    parts: list[tuple[str, str]] = []
    if period:
        parts.append(("period", period))
    if theme:
        parts.append(("theme", theme))
    if show_disabled:
        parts.append(("show_disabled", show_disabled))
    if not parts:
        return ""
    return "?" + "&".join(
        f"{escape(name)}={escape_attr(value)}" for name, value in parts
    )


def _coerce_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to a non-negative ``int``.

    Routes always pass real ints from the DB aggregate, but the
    renderer is called from tests with raw strings (for XSS safety
    coverage) and from anywhere else a future caller might plug in.
    Silently fall back to ``default`` when ``value`` is not a
    numeric-ish string so the chip/toggle never blows up the page.
    """
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else 0


def _account_count_chip(enabled_count: int, disabled_count: int) -> str:
    """Render the panel-header count chip ("12 enabled · 3 disabled").

    Always renders something for layout stability (avoids the heading
    line shifting when ``disabled_count`` flips from 0 to N mid-page).
    Drops the disabled side when the operator has no disabled rows so
    the chip just reads "12 enabled".
    """
    n_disabled = _coerce_int(disabled_count)
    if n_disabled <= 0:
        return (
            f' <span class="panel-header-chip">{escape(enabled_count)} enabled</span>'
        )
    return (
        f' <span class="panel-header-chip">'
        f"{escape(enabled_count)} enabled &middot; "
        f"{escape(n_disabled)} disabled"
        f"</span>"
    )


def _render_account_breakdown_filter(
    period: str,
    current_theme: str,
    show_disabled: bool,
    disabled_count: int,
) -> str:
    """Render the Show/Hide disabled toggle for the overview's Account
    breakdown section.

    Renders a plain anchor (``<a href>``) rather than a form so the
    toggle works without JavaScript and survives middle-click /
    bookmarking.  ``aria-pressed`` reflects the current state so screen
    readers (and dashboard.js) can read it as a toggle.  When
    ``disabled_count > 0`` the label shows the filtered-out size
    (``"Show 3 disabled"``) so the operator learns the size of the
    hidden set without expanding the table; when zero it falls back to
    the static label and clicking the toggle remains a no-op visually.
    """
    n_disabled = _coerce_int(disabled_count)
    if show_disabled:
        label = "Hide disabled"
        next_value = "0"
    elif n_disabled > 0:
        label = f"Show {n_disabled} disabled"
        next_value = "1"
    else:
        label = "Show disabled"
        next_value = "1"
    href = _build_href_with_state(
        period=period or None,
        theme=current_theme or None,
        show_disabled=next_value,
    )
    return (
        f'<a class="show-disabled-toggle" '
        f'href="{escape_attr(href)}" '
        f'aria-pressed="{str(show_disabled).lower()}">'
        f'<span class="disabled-toggle-icon" aria-hidden="true">&#x25BE;</span>'
        f"{escape(label)}"
        f"</a>"
    )


def _render_account_breakdown_body(
    accounts: list[dict[str, Any]],
    show_disabled: bool,
    disabled_count: int,
) -> str:
    """Render the body of the Account breakdown section.

    When the operator has filtered disabled rows out and there are no
    enabled rows left, offer a one-click opt-in to the historical view
    instead of the generic "No accounts configured." message. The
    link preserves the current period + theme so the toggle does not
    reset the rest of the operator's view.
    """
    if not accounts:
        return _render_accounts_empty_state(show_disabled, disabled_count)
    return _render_account_table(accounts)


def _render_account_table(accounts: list[dict[str, Any]]) -> str:
    """Render the account breakdown table."""
    if not accounts:
        return '<p class="empty">No accounts configured.</p>'
    parts = [
        '<table class="data">',
        "<thead><tr>",
        # Priority 1 — always shown (the operator's quick-glance columns)
        _th("Account"),
        _th("Provider"),
        _th("Enabled"),
        _th("Requests"),
        _th("Cost"),
        # Priority 2 — shown on tablet+ (the diagnostic core)
        _th("Health", priority=2),
        _th("Errors", priority=2),
        _th("Input tokens", priority=2),
        _th("Output tokens", priority=2),
        _th("Total tokens", priority=2),
        _th("Avg latency", priority=2),
        _th("TPS", priority=2),
        _th("Exactness", priority=2),
        # Priority 3 — desktop-only (the deep diagnostic tail)
        _th("Reserved", priority=3),
        _th("Resv.", priority=3),
        _th("5h rate", priority=3),
        _th("7d rate", priority=3),
        _th("30d rate", priority=3),
        _th("BW received", priority=3),
        _th("BW emitted", priority=3),
        _th("Over budget", priority=3),
        _th("Upstream backoff", priority=3),
        _th("Backoff until", priority=3),
        _th("Failures", priority=3),
        _th("Auth fail", priority=3),
        _th("Disabled", priority=3),
        _th("Est. cost", priority=3),
        _th("Cache R", priority=3),
        _th("Cache W", priority=3),
        _th("Reasoning", priority=3),
        _th("Avg cost/req", priority=3),
        _th("Avg cost/1k tok", priority=3),
        "</tr></thead><tbody>",
    ]
    for row in accounts:
        enabled = bool(row.get("account_enabled", 0))
        name = escape(row.get("account_name", ""))
        provider = escape(row.get("provider_id", ""))
        cost = format_microdollars(row.get("cost_microdollars", 0))
        latency = format_latency(row.get("avg_latency_ms", 0.0))
        reserved = format_microdollars(row.get("reserved_microdollars", 0))
        in_tok = format_tokens(row.get("input_tokens", 0))
        out_tok = format_tokens(row.get("output_tokens", 0))
        total_tok = format_tokens(row.get("total_tokens", 0))
        tps = format_tokens_per_second(row.get("tokens_per_second", 0.0))
        health = str(row.get("health_state", "unknown"))
        active_resv = int(row.get("active_reservations", 0))
        util_5h = format_microdollars(row.get("utilization_5h", 0))
        util_7d = format_microdollars(row.get("utilization_7d", 0))
        util_30d = format_microdollars(row.get("utilization_30d", 0))
        over_budget = bool(row.get("estimated_over_local_budget", False))
        backoff_reason = escape(str(row.get("upstream_backoff_reason") or "—"))
        backoff_until_raw = row.get("backoff_until")
        backoff_until = (
            escape(_format_backoff_until(backoff_until_raw))
            if backoff_until_raw is not None
            else "—"
        )
        consecutive_failures = int(row.get("consecutive_upstream_failures", 0))
        auth_failed = bool(row.get("authentication_failed", False))
        operator_disabled = bool(row.get("operator_disabled", False))
        exact = int(row.get("exact_count", 0) or 0)
        derived = int(row.get("derived_count", 0) or 0)
        partial_count = int(row.get("partial_count", 0) or 0)
        estimated = int(row.get("estimated_count", 0) or 0)
        unknown_exc = int(row.get("unknown_count", 0) or 0)
        provider_reported = int(row.get("provider_reported_count", 0) or 0)
        exactness = _render_pricing_exactness_badge(
            exact=exact,
            derived=derived,
            partial=partial_count,
            estimated=estimated,
            unknown_exc=unknown_exc,
            provider_reported=provider_reported,
        )
        est_cost_fraction = row.get("estimated_cost_fraction")
        est_cost_pct = (
            _format_percent_unit(est_cost_fraction, digits=1)
            if est_cost_fraction is not None
            else "—"
        )
        cache_read_ratio = row.get("cache_read_ratio")
        cache_write_ratio = row.get("cache_write_ratio")
        reasoning_ratio = row.get("reasoning_output_ratio")
        cache_read_str = (
            _format_percent_unit(cache_read_ratio, digits=1)
            if cache_read_ratio is not None
            else "—"
        )
        cache_write_str = (
            _format_percent_unit(cache_write_ratio, digits=1)
            if cache_write_ratio is not None
            else "—"
        )
        reasoning_str = (
            _format_percent_unit(reasoning_ratio, digits=1)
            if reasoning_ratio is not None
            else "—"
        )
        avg_cost_per_req_microdollars = row.get("avg_cost_per_request")
        if avg_cost_per_req_microdollars is None:
            avg_cost_per_req = "—"
        else:
            avg_cost_per_req = format_microdollars(avg_cost_per_req_microdollars)
        avg_cost_per_1k_microdollars = row.get("avg_cost_per_1k_tokens")
        if avg_cost_per_1k_microdollars is None:
            avg_cost_per_1k = "—"
        else:
            avg_cost_per_1k = format_microdollars(avg_cost_per_1k_microdollars)
        enabled_cls = "yes" if enabled else "no"
        health_cls = sanitize_class_name(health)
        over_budget_cls = "yes" if over_budget else "no"
        auth_failed_cls = "yes" if auth_failed else "no"
        disabled_str = "yes" if operator_disabled else "no"
        disabled_cls = "yes" if operator_disabled else "no"
        req_count = int(row.get("request_count", 0))
        err_count = int(row.get("error_count", 0))
        parts.append(
            f"<tr>"
            f"{_td_priority(name, 1)}"
            f"{_td_priority(provider, 1)}"
            f"{_td_priority('yes' if enabled else 'no', 1, class_=enabled_cls)}"
            f"{_td_priority(f'{req_count:,}', 1)}"
            f"{_td_priority(cost, 1)}"
            f"{_td_priority(escape(health), 2, class_=health_cls)}"
            f"{_td_priority(f'{err_count:,}', 2)}"
            f"{_td_priority(in_tok, 2)}"
            f"{_td_priority(out_tok, 2)}"
            f"{_td_priority(total_tok, 2)}"
            f"{_td_priority(latency, 2)}"
            f"{_td_priority(tps, 2)}"
            f"{_td_priority(exactness, 2)}"
            f"{_td_priority(reserved, 3)}"
            f"{_td_priority(f'{active_resv}', 3)}"
            f"{_td_priority(util_5h, 3)}"
            f"{_td_priority(util_7d, 3)}"
            f"{_td_priority(util_30d, 3)}"
            f"{_td_priority(format_bytes(row.get('bytes_received', 0)), 3)}"
            f"{_td_priority(format_bytes(row.get('bytes_emitted', 0)), 3)}"
            f"{_td_priority('yes' if over_budget else 'no', 3, class_=over_budget_cls)}"
            f"{_td_priority(backoff_reason, 3)}"
            f"{_td_priority(backoff_until, 3)}"
            f"{_td_priority(f'{consecutive_failures}', 3)}"
            f"{_td_priority('yes' if auth_failed else 'no', 3, class_=auth_failed_cls)}"
            f"{_td_priority(disabled_str, 3, class_=disabled_cls)}"
            f"{_td_priority(est_cost_pct, 3)}"
            f"{_td_priority(cache_read_str, 3)}"
            f"{_td_priority(cache_write_str, 3)}"
            f"{_td_priority(reasoning_str, 3)}"
            f"{_td_priority(avg_cost_per_req, 3)}"
            f"{_td_priority(avg_cost_per_1k, 3)}"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def _format_backoff_until(value: object) -> str:
    """Format a POSIX epoch or ISO timestamp for display."""
    import datetime as _dt

    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.UTC).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
    return str(value)


def render_accounts(
    accounts: list[dict[str, Any]],
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
    *,
    show_disabled: bool = False,
    disabled_count: int = 0,
) -> str:
    """Render the accounts page.

    ``show_disabled`` controls whether soft-deleted accounts (those
    ``sync_from_config`` marked ``enabled = 0`` after ``eggpool logout``)
    are listed. Defaults to False so the page matches the operator's
    mental model after logout.

    ``disabled_count`` is the total disabled-row count, used only when
    ``accounts`` is empty AND ``show_disabled`` is False: the empty
    state becomes a one-click "N disabled — show them?" hint instead
    of the generic "No accounts configured." message.
    """
    table_html = (
        _render_accounts_empty_state(show_disabled, disabled_count)
        if not accounts
        else _render_account_table(accounts)
    )
    body = f"""
<h2>Accounts</h2>
{_render_account_filters(period, current_theme, show_disabled)}
{_render_pricing_warnings(accounts)}
<section class="panel">
  {table_html}
</section>
"""
    return _render_layout(
        title="Accounts",
        body=body,
        active_nav="accounts",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def render_models(
    models: list[dict[str, Any]],
    account_filter: str = "",
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
    model_info_map: dict[str, dict[str, Any]] | None = None,
    info_status_filter: str = "",
    availability_filter: str = "",
    used_filter: str = "",
    has_filters: bool = False,
    account_options: list[str] | None = None,
) -> str:
    """Render the models page."""
    mi_map = model_info_map or {}
    if not models:
        if has_filters:
            rows_html = '<p class="empty">No models match the selected filters.</p>'
        else:
            rows_html = (
                '<p class="empty">No models discovered from configured providers.</p>'
            )
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            # Priority 1 — always shown
            _th("Model"),
            _th("Provider"),
            _th("Avail."),
            _th("Info"),
            _th("Requests"),
            _th("Cost"),
            _th("Exactness"),
            # Priority 2 — shown on tablet+
            _th("Errors", priority=2),
            _th("Input tokens", priority=2),
            _th("Output tokens", priority=2),
            _th("Total tokens", priority=2),
            _th("Avg latency", priority=2),
            _th("Avg TTFT", priority=2),
            _th("TPS", priority=2),
            # Priority 3 — desktop-only
            _th("Priority", priority=3),
            _th("Est. cost", priority=3),
            _th("Cache R", priority=3),
            _th("Cache W", priority=3),
            _th("Reasoning", priority=3),
            _th("Avg cost/req", priority=3),
            _th("Avg cost/1k tok", priority=3),
            "</tr></thead><tbody>",
        ]
        for row in models:
            cost = format_microdollars(row.get("cost_microdollars", 0))
            latency = format_latency(row.get("avg_latency_ms", 0.0))
            ttft = format_latency(row.get("avg_ttft_ms", 0.0))
            in_tok = format_tokens(row.get("input_tokens", 0))
            out_tok = format_tokens(row.get("output_tokens", 0))
            total_tok = format_tokens(row.get("total_tokens", 0))
            tps = format_tokens_per_second(row.get("tokens_per_second", 0.0))
            provider = escape(row.get("provider_id", ""))
            avail_html = _render_availability_pill(row)
            routing_priority = row.get("routing_priority")
            priority_html = (
                str(int(routing_priority)) if isinstance(routing_priority, int) else "—"
            )
            exact = int(row.get("exact_count", 0) or 0)
            derived = int(row.get("derived_count", 0) or 0)
            partial_count = int(row.get("partial_count", 0) or 0)
            estimated = int(row.get("estimated_count", 0) or 0)
            unknown_exc = int(row.get("unknown_count", 0) or 0)
            provider_reported = int(row.get("provider_reported_count", 0) or 0)
            exactness = _render_pricing_exactness_badge(
                exact=exact,
                derived=derived,
                partial=partial_count,
                estimated=estimated,
                unknown_exc=unknown_exc,
                provider_reported=provider_reported,
            )
            est_cost_fraction = row.get("estimated_cost_fraction")
            est_cost_pct = (
                _format_percent_unit(est_cost_fraction, digits=1)
                if est_cost_fraction is not None
                else "—"
            )
            cache_read_ratio = row.get("cache_read_ratio")
            cache_write_ratio = row.get("cache_write_ratio")
            reasoning_ratio = row.get("reasoning_output_ratio")
            cache_read_str = (
                _format_percent_unit(cache_read_ratio, digits=1)
                if cache_read_ratio is not None
                else "—"
            )
            cache_write_str = (
                _format_percent_unit(cache_write_ratio, digits=1)
                if cache_write_ratio is not None
                else "—"
            )
            reasoning_str = (
                _format_percent_unit(reasoning_ratio, digits=1)
                if reasoning_ratio is not None
                else "—"
            )
            avg_cost_per_req_microdollars = row.get("avg_cost_per_request")
            if avg_cost_per_req_microdollars is None:
                avg_cost_per_req = "—"
            else:
                avg_cost_per_req = format_microdollars(avg_cost_per_req_microdollars)
            avg_cost_per_1k_microdollars = row.get("avg_cost_per_1k_tokens")
            if avg_cost_per_1k_microdollars is None:
                avg_cost_per_1k = "—"
            else:
                avg_cost_per_1k = format_microdollars(avg_cost_per_1k_microdollars)
            req_count = int(row.get("request_count", 0))
            err_count = int(row.get("error_count", 0))

            # Model-info pill. Provider-suffixed rows store the
            # canonical lookup key under ``base_model_id`` so the
            # dashboard looks up by the unsuffixed ID first and falls
            # back to the literal row model_id when the catalog
            # entry has no provider suffix.
            model_id = row.get("model_id", "")
            base_id = row.get("base_model_id", "") or model_id
            mi_info = mi_map.get(base_id) or mi_map.get(model_id)
            info_pill = _render_model_info_pill(mi_info)

            # URL path-encode the model id so provider suffixes
            # (``/``), query metacharacters (``?``), and any future
            # characters that would change the route's path/query
            # shape are emitted as percent escapes.  The route
            # ``/models/{model_id:path}`` plus the detail handler's
            # ``unquote()`` round-trip recovers the original id on
            # the server side.
            quoted_model_id = quote(str(model_id or ""), safe="")
            model_link = (
                f'<a href="/models/{quoted_model_id}'
                f'?theme={escape_attr(current_theme)}">'
                f"{escape(model_id)}</a>"
            )

            parts.append(
                f"<tr>"
                f"{_td_priority(model_link, 1)}"
                f"{_td_priority(provider, 1)}"
                f"{_td_priority(avail_html, 1)}"
                f"{_td_priority(info_pill, 1)}"
                f"{_td_priority(f'{req_count:,}', 1)}"
                f"{_td_priority(cost, 1)}"
                f"{_td_priority(exactness, 1)}"
                f"{_td_priority(f'{err_count:,}', 2)}"
                f"{_td_priority(in_tok, 2)}"
                f"{_td_priority(out_tok, 2)}"
                f"{_td_priority(total_tok, 2)}"
                f"{_td_priority(latency, 2)}"
                f"{_td_priority(ttft, 2)}"
                f"{_td_priority(tps, 2)}"
                f"{_td_priority(priority_html, 3)}"
                f"{_td_priority(est_cost_pct, 3)}"
                f"{_td_priority(cache_read_str, 3)}"
                f"{_td_priority(cache_write_str, 3)}"
                f"{_td_priority(reasoning_str, 3)}"
                f"{_td_priority(avg_cost_per_req, 3)}"
                f"{_td_priority(avg_cost_per_1k, 3)}"
                f"</tr>"
            )
        parts.append("</tbody></table>")
        rows_html = "".join(parts)

    def _sel(name: str, value: str, current: str) -> str:
        """Render a <select> option with the selected attr."""
        sel = " selected" if value == current else ""
        return f'<option value="{escape_attr(value)}"{sel}>{escape(name)}</option>'

    used_options = "".join(
        _sel(n, v, used_filter)
        for n, v in [
            ("All", ""),
            ("Used", "used"),
            ("Unused", "unused"),
        ]
    )
    info_options = "".join(
        _sel(n, v, info_status_filter)
        for n, v in [
            ("All", ""),
            ("Fresh", "fresh"),
            ("Partial", "partial"),
            ("Sparse", "sparse_new"),
            ("Stale", "stale"),
            ("Conflict", "conflicting"),
            ("Unmatched", "unmatched"),
        ]
    )
    avail_options = "".join(
        _sel(n, v, availability_filter)
        for n, v in [
            ("All", ""),
            ("Available", "available"),
            ("Unavailable", "unavailable"),
        ]
    )
    account_select_options: list[tuple[str, str]] = [("", "(any account)")]
    for name in account_options or []:
        if name:
            account_select_options.append((name, name))
    account_options_html = "".join(
        _sel(label, value, account_filter) for value, label in account_select_options
    )
    filter_form = (
        '<form method="get" class="filter-form">'
        "<label>Account: "
        f'<select name="account">{account_options_html}</select>'
        "</label>"
        "<label>Used: "
        f'<select name="used">{used_options}</select>'
        "</label>"
        "<label>Info: "
        f'<select name="info_status">{info_options}</select>'
        "</label>"
        "<label>Availability: "
        f'<select name="availability">{avail_options}</select>'
        "</label>"
        f'<input type="hidden" name="period" '
        f'value="{escape_attr(period)}">'
        f'<input type="hidden" name="theme" '
        f'value="{escape_attr(current_theme)}">'
        '<button type="submit">Apply</button>'
        "</form>"
    )

    body = f"""
<h2>Models</h2>
{filter_form}
{_render_period_selector(period, current_theme)}
<section class="panel">
  {rows_html}
</section>
"""
    return _render_layout(
        title="Models",
        body=body,
        active_nav="models",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def render_model_detail(
    info: CanonicalModelInfo | None,
    model_id: str,
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the model-info detail page for a single model."""
    if info is None:
        body = f"""
<h2>Model: {escape(model_id)}</h2>
<p class="empty">Model info not available.</p>
"""
        return _render_layout(
            title=f"Model: {model_id}",
            body=body,
            active_nav="models",
            theme_css=theme_css,
            available_themes=available_themes,
            current_theme=current_theme,
            update_info=update_info,
        )

    # Extract fields from the CanonicalModelInfo object
    status_raw = info.status
    status_str = str(status_raw)
    status_display = display_model_info_status(status_str)
    sparse = info.sparse
    summary = info.summary or ""
    last_seen = info.last_seen_at
    last_refreshed = info.last_refreshed_at
    next_refresh = info.next_refresh_at
    detail = _as_mapping(info.detail)
    provenance = _as_mapping(info.provenance)
    conflicts = _as_mapping(info.conflicts)

    # Status pill
    pill_info: dict[str, Any] = {
        "status": status_str,
        "sparse": sparse,
        "summary": summary,
        "sources": _as_str_list(provenance.get("sources")),
        "last_refreshed_at": (last_refreshed.isoformat() if last_refreshed else None),
    }
    status_pill = _render_model_info_pill(pill_info)

    # Sources list
    sources = _as_str_list(provenance.get("sources"))
    sources_html = (
        ", ".join(f"<code>{escape(s)}</code>" for s in sources) if sources else _EM_DASH
    )

    # Provider IDs
    providers = _as_str_list(detail.get("providers"))
    providers_html = (
        ", ".join(f"<code>{escape(p)}</code>" for p in providers)
        if providers
        else _EM_DASH
    )

    # Limits — read the normalized nested block first, then fall back to
    # pre-Phase-B flat keys (context_tokens, context_window_external,
    # max_output_tokens, max_output_tokens_external) for legacy rows
    # that haven't been backfilled yet. The startup backfill
    # (``backfill_legacy_detail_blocks``) lifts every such row on the
    # next launch; this fallback only covers the race window before
    # that runs.
    limits = _as_mapping(detail.get("limits"))
    ctx = limits.get("effective_context")
    if ctx is None:
        ctx = detail.get("context_tokens")
    ext_ctx = limits.get("external_context")
    if ext_ctx is None:
        ext_ctx = detail.get("context_window_external")
    eff_out = limits.get("effective_output")
    if eff_out is None:
        eff_out = detail.get("max_output_tokens")
    ext_out = limits.get("external_output")
    if ext_out is None:
        ext_out = detail.get("max_output_tokens_external")
    limits_parts = _render_model_limit_parts(
        {
            "Effective ctx": ctx,
            "External ctx": ext_ctx,
            "Effective out": eff_out,
            "External out": ext_out,
        }
    )
    limits_html = " &middot; ".join(limits_parts) if limits_parts else "—"

    # Modalities
    modalities = _as_str_list(detail.get("modalities"))
    modalities_html = ", ".join(escape(m) for m in modalities) if modalities else "—"

    # Tool support
    tools = detail.get("supports_tools")
    tools_html = "Yes" if tools is True else ("No" if tools is False else "—")

    # Family
    family = detail.get("family")
    family_html = escape(str(family)) if family else "—"

    # License
    license_val = detail.get("license")
    license_html = escape(str(license_val)) if license_val else "—"

    # Release date
    release_date = detail.get("release_date")
    release_html = escape(str(release_date)) if release_date else "—"

    # External IDs
    external_ids = _as_mapping(detail.get("external_ids"))
    ext_id_parts: list[str] = []
    for ext_source, ext_id in external_ids.items():
        src_esc = escape(str(ext_source))
        id_esc = escape(str(ext_id))
        ext_id_parts.append(f"{src_esc}: <code>{id_esc}</code>")
    ext_ids_html = "<br>".join(ext_id_parts) if ext_id_parts else _EM_DASH

    # Benchmarks
    benchmarks_html = _render_benchmarks_table(detail.get("benchmarks"))

    # HuggingFace metadata
    hf_html = _render_hf_metadata(detail.get("huggingface_metadata"))

    # Conflicts
    conflicts_html = _render_conflicts_section(conflicts)

    # Provenance / reconciled at
    reconciled_at = provenance.get("reconciled_at")
    reconciled_html = format_timestamp(reconciled_at) if reconciled_at else _EM_DASH

    # Display name
    display_name = detail.get("display_name")
    title_text = str(display_name) if display_name else model_id

    body = f"""
<h2>{escape(title_text)} <small>({escape(model_id)})</small></h2>
<p>{status_pill}</p>
<section class="cards">
  {
        _render_metric_card(
            title="Status",
            metric=escape(status_display),
            sub="Sparse" if sparse else None,
            tooltip="Current freshness status of model-info metadata",
        )
    }
  {
        _render_metric_card(
            title="Last seen",
            metric=format_age_seconds((datetime.now(UTC) - last_seen).total_seconds())
            if last_seen
            else _EM_DASH,
            sub="Observation time",
            tooltip="When this model was last observed by any source",
        )
    }
  {
        _render_metric_card(
            title="Last refreshed",
            metric=format_age_seconds(
                (datetime.now(UTC) - last_refreshed).total_seconds()
            )
            if last_refreshed
            else _EM_DASH,
            sub="Refresh time",
            tooltip="When model-info metadata was last refreshed",
        )
    }
  {
        _render_metric_card(
            title="Next refresh",
            metric=format_age_seconds(
                (next_refresh - datetime.now(UTC)).total_seconds()
            )
            if next_refresh
            else _EM_DASH,
            sub="Scheduled",
            tooltip="When the next scheduled refresh will occur",
        )
    }
</section>

<section class="panel">
  <h3>Summary</h3>
  <p>{escape(summary) if summary else "<em>No summary available.</em>"}</p>
</section>

<section class="panel">
  <h3>Provider / Callability</h3>
  <table class="data">
    <tbody>
      <tr><th>Providers</th><td>{providers_html}</td></tr>
      <tr><th>Sources</th><td>{sources_html}</td></tr>
      <tr><th>Limits</th><td>{limits_html}</td></tr>
      <tr><th>Modalities</th><td>{modalities_html}</td></tr>
      <tr><th>Tool support</th><td>{tools_html}</td></tr>
    </tbody>
  </table>
</section>

<section class="panel">
  <h3>Metadata</h3>
  <table class="data">
    <tbody>
      <tr><th>Family</th><td>{family_html}</td></tr>
      <tr><th>License</th><td>{license_html}</td></tr>
      <tr><th>Release date</th><td>{release_html}</td></tr>
      <tr><th>External IDs</th><td>{ext_ids_html}</td></tr>
    </tbody>
  </table>
</section>

{benchmarks_html}

{hf_html}

{conflicts_html}

<section class="panel">
  <h3>Provenance</h3>
  <table class="data">
    <tbody>
      <tr><th>Sources</th><td>{sources_html}</td></tr>
      <tr><th>Reconciled at</th><td>{reconciled_html}</td></tr>
    </tbody>
  </table>
</section>
"""
    return _render_layout(
        title=f"Model: {title_text}",
        body=body,
        active_nav="models",
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def _render_model_limit_parts(values: Mapping[str, object]) -> list[str]:
    parts: list[str] = []
    for label, raw_value in values.items():
        value = _coerce_positive_int(raw_value)
        if value is not None:
            parts.append(f"{label}: {format_tokens(value)}")
    return parts


def _render_benchmarks_table(benchmarks: object) -> str:
    """Render a benchmarks section if benchmark data is available."""
    if not isinstance(benchmarks, list):
        return ""
    benchmark_items = cast("list[object]", benchmarks)
    rows: list[str] = []
    for bm in benchmark_items:
        bm_map = _as_mapping(bm)
        if not bm_map:
            continue
        source = escape(str(bm_map.get("source", "")))
        name = escape(str(bm_map.get("benchmark", bm_map.get("name", ""))))
        score = bm_map.get("score")
        rank = bm_map.get("rank")
        percentile = bm_map.get("percentile")
        observed = bm_map.get("observed_at", "")
        caveat = bm_map.get("caveat", "")
        value_parts: list[str] = []
        if score is not None:
            value_parts.append(f"Score: {escape(str(score))}")
        if rank is not None:
            value_parts.append(f"Rank: {escape(str(rank))}")
        if percentile is not None:
            value_parts.append(f"Percentile: {escape(str(percentile))}")
        value_html = ", ".join(value_parts) if value_parts else "—"
        caveat_html = f"<br><em>{escape(str(caveat))}</em>" if caveat else ""
        rows.append(
            f"<tr><td>{source}</td><td>{name}</td>"
            f"<td>{value_html}{caveat_html}</td>"
            f"<td>{escape(str(observed)) if observed else '—'}</td></tr>"
        )
    if not rows:
        return ""
    return (
        '<section class="panel">'
        "<h3>Benchmarks</h3>"
        '<table class="data"><thead><tr>'
        "<th>Source</th><th>Benchmark</th><th>Value</th><th>Observed</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )


def _render_hf_metadata(hf_metadata: object) -> str:
    """Render Hugging Face metadata section if available."""
    hf_metadata = _as_mapping(hf_metadata)
    if not hf_metadata:
        return ""
    parts: list[str] = []
    for key in ("downloads", "likes", "pipeline_tag", "library_name", "license"):
        val = hf_metadata.get(key)
        if val is not None:
            parts.append(f"<tr><th>{escape(key)}</th><td>{escape(str(val))}</td></tr>")
    tags = hf_metadata.get("tags")
    tags_list = _as_str_list(tags)
    if tags_list:
        tags_html = ", ".join(escape(tag) for tag in tags_list[:10])
        parts.append(f"<tr><th>Tags</th><td>{tags_html}</td></tr>")
    if not parts:
        return ""
    return (
        '<section class="panel">'
        "<h3>Hugging Face</h3>"
        '<table class="data"><tbody>' + "".join(parts) + "</tbody></table></section>"
    )


def _render_conflicts_section(conflicts: Mapping[str, object]) -> str:
    """Render a conflicts section if any conflicts exist."""
    if not conflicts:
        return ""
    rows: list[str] = []
    for field, val in conflicts.items():
        val_map = _as_mapping(val)
        if val_map:
            sources_vals = _as_mapping(val_map.get("sources"))
            selected = val_map.get("selected", "")
            reason = val_map.get("reason", "")
            sources_str = (
                ", ".join(
                    f"{escape(str(k))}: {escape(str(v))}"
                    for k, v in sources_vals.items()
                )
                if sources_vals
                else escape(str(sources_vals))
            )
            rows.append(
                f"<tr><td>{escape(str(field))}</td>"
                f"<td>{sources_str}</td>"
                f"<td>{escape(str(selected))}</td>"
                f"<td>{escape(str(reason))}</td></tr>"
            )
        else:
            rows.append(
                f"<tr><td>{escape(str(field))}</td>"
                f"<td colspan='3'>{escape(str(val))}</td></tr>"
            )
    if not rows:
        return ""
    return (
        '<section class="panel">'
        "<h3>Conflicts</h3>"
        '<table class="data"><thead><tr>'
        "<th>Field</th><th>Source values</th><th>Selected</th><th>Reason</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )


def render_events(
    events: list[dict[str, Any]],
    event_type: str = "",
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the events page."""
    if not events:
        rows_html = '<p class="empty">No events recorded.</p>'
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            _th("When"),
            _th("Account"),
            _th("Type"),
            _th("Details", priority=2),
            "</tr></thead><tbody>",
        ]
        for row in events:
            ts = format_timestamp(row.get("created_at", ""))
            name = escape(row.get("account_name", ""))
            event_type = str(row.get("event_type", ""))
            etype = escape(event_type)
            details = truncate(row.get("details", ""), 200)
            cls = sanitize_class_name(event_type)
            badge_tooltip = _status_badge_tooltip(event_type) or ""
            badge_attrs = (
                f' data-tooltip="{_html_escape(badge_tooltip)}"'
                f' aria-label="{_html_escape(badge_tooltip)}"'
                if badge_tooltip
                else ""
            )
            badge_html = f'<span class="event-tag {cls}"{badge_attrs}>{etype}</span>'
            parts.append(
                f"<tr>"
                f"{_td_priority(ts, 1)}"
                f"{_td_priority(name, 1)}"
                f"{_td_priority(badge_html, 1)}"
                f"{_td_priority(details, 2)}"
                f"</tr>"
            )
        parts.append("</tbody></table>")
        rows_html = "".join(parts)

    filter_form = f"""
<form method="get" class="filter-form">
  <label>Type:
    <input type="text" name="type" value="{escape_attr(event_type)}"
           placeholder="(all)">
  </label>
  <input type="hidden" name="period" value="{escape_attr(period)}">
  <input type="hidden" name="theme" value="{escape_attr(current_theme)}">
  <button type="submit">Apply</button>
</form>
"""

    body = f"""
<h2>Events</h2>
{filter_form}
{_render_period_selector(period, current_theme)}
<section class="panel">
  {rows_html}
</section>
"""
    return _render_layout(
        title="Events",
        body=body,
        active_nav="events",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def _render_aggregate_timeseries_table(series: list[dict[str, Any]]) -> str:
    """Render the legacy aggregate per-bucket timeseries table."""
    if not series:
        return '<p class="empty">No requests in this window.</p>'
    parts = [
        '<table class="data">',
        "<thead><tr>",
        # Priority 1 — always shown
        _th("Bucket"),
        _th("Requests"),
        _th("Cost"),
        # Priority 2 — shown on tablet+
        _th("Errors", priority=2),
        _th("Total tokens", priority=2),
        # Priority 3 — desktop only
        _th("Input tokens", priority=3),
        _th("Output tokens", priority=3),
        _th("BW received", priority=3),
        _th("BW emitted", priority=3),
        "</tr></thead><tbody>",
    ]
    for row in series:
        cost = format_microdollars(row.get("cost_microdollars", 0))
        in_tok = format_tokens(row.get("input_tokens", 0))
        out_tok = format_tokens(row.get("output_tokens", 0))
        total_tok = format_tokens(row.get("total_tokens", 0))
        req_count = int(row.get("request_count", 0))
        err_count = int(row.get("error_count", 0))
        parts.append(
            f"<tr>"
            f"{_td_priority(escape(row.get('bucket', '')), 1)}"
            f"{_td_priority(f'{req_count:,}', 1)}"
            f"{_td_priority(cost, 1)}"
            f"{_td_priority(f'{err_count:,}', 2)}"
            f"{_td_priority(total_tok, 2)}"
            f"{_td_priority(in_tok, 3)}"
            f"{_td_priority(out_tok, 3)}"
            f"{_td_priority(format_bytes(row.get('bytes_received', 0)), 3)}"
            f"{_td_priority(format_bytes(row.get('bytes_emitted', 0)), 3)}"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_grouped_timeseries_chart(
    grouped: dict[str, Any],
    *,
    period: str,
    bucket: str,
    group_by: str,
    metric: str,
    limit: int,
    account_filter: str = "",
    model_filter: str = "",
    compact: bool = False,
) -> str:
    """Render the grouped timeseries chart panel with sibling JSON data island.

    The chart is rendered server-side as a ``<canvas>`` plus a sibling
    ``<script type="application/json">`` data island.  Initialisation is
    intentionally not done inline so that Chart.js loads (deferred) before
    the canvas is touched, and so that ``_render_auto_refresh_script``
    can call ``initGroupedTimeseriesCharts`` after the dashboard content
    is replaced without re-executing inline scripts.

    The canvas is always emitted (even when the initial payload is empty)
    so the dashboard.js handler can update the chart in place when the
    filter form's selects change.  An empty-state paragraph is shown or
    hidden by JS based on the live payload.
    """
    points = list(grouped.get("points") or [])
    buckets = list(grouped.get("buckets") or [])
    has_data = bool(points) and bool(buckets)
    chart_id = (
        "grouped-timeseries-chart-compact" if compact else "grouped-timeseries-chart"
    )
    container_class = "chart-container-compact" if compact else "chart-container"
    period_attr = escape_attr(period)
    bucket_attr = escape_attr(bucket)
    group_by_attr = escape_attr(group_by)
    metric_attr = escape_attr(metric)
    limit_attr = escape_attr(str(limit))
    account_attr = escape_attr(account_filter)
    model_attr = escape_attr(model_filter)
    payload_json = json.dumps(grouped)
    empty_state_style = ' style="display: none;"' if has_data else ""
    canvas_style = "" if has_data else ' style="display: none;"'
    return f"""
<section class="panel timeseries-chart-panel">
  <h3>Usage breakdown</h3>
  <div class="{container_class}"{canvas_style}>
    <canvas class="grouped-timeseries-chart"
            data-chart-id="{chart_id}"
            data-period="{period_attr}"
            data-bucket="{bucket_attr}"
            data-group-by="{group_by_attr}"
            data-metric="{metric_attr}"
            data-limit="{limit_attr}"
            data-account="{account_attr}"
            data-model="{model_attr}"></canvas>
  </div>
  <p class="empty grouped-timeseries-empty"{empty_state_style}>
    No requests in this window.
  </p>
  <script type="application/json" class="grouped-timeseries-data"
          data-chart-id="{chart_id}">{payload_json}</script>
</section>
"""


def _render_grouped_timeseries_table(grouped: dict[str, Any]) -> str:
    """Render the grouped detail table below the chart."""
    points = list(grouped.get("points") or [])
    buckets = list(grouped.get("buckets") or [])
    if not points or not buckets:
        return '<p class="empty">No requests in this window.</p>'
    include_account = str(grouped.get("group_by") or "") == "account" or any(
        row.get("account_name") for row in points
    )
    header_cells = [
        _th("Bucket"),
        _th("Series"),
        _th("Provider"),
        _th("Model"),
    ]
    if include_account:
        header_cells.append(_th("Account", priority=2))
    header_cells.extend(
        [
            _th("Requests"),
            _th("Cost", priority=2),
            _th("Errors", priority=2),
            _th("Total tokens", priority=2),
            _th("Avg latency", priority=2),
            # Priority 3 — desktop only
            _th("Input tokens", priority=3),
            _th("Output tokens", priority=3),
            _th("Cache read", priority=3),
            _th("Cache write", priority=3),
            _th("Reasoning", priority=3),
            _th("BW received", priority=3),
            _th("BW emitted", priority=3),
            _th("Avg TTFT", priority=3),
        ]
    )
    parts = [
        '<table class="data">',
        "<thead><tr>",
        *header_cells,
        "</tr></thead><tbody>",
    ]
    for row in points:
        cells = [
            _td_priority(escape(row.get("bucket", "")), 1),
            _td_priority(escape(row.get("label", "")), 1),
            _td_priority(escape(row.get("provider_id") or ""), 1),
            _td_priority(escape(row.get("model_id") or ""), 1),
        ]
        if include_account:
            cells.append(_td_priority(escape(row.get("account_name") or ""), 2))
        cells.extend(
            [
                _td_priority(format_int(row.get("request_count", 0)), 1),
                _td_priority(format_microdollars(row.get("cost_microdollars", 0)), 2),
                _td_priority(format_int(row.get("error_count", 0)), 2),
                _td_priority(format_tokens(row.get("total_tokens", 0)), 2),
                _td_priority(format_latency(row.get("avg_latency_ms", 0.0)), 2),
                _td_priority(format_tokens(row.get("input_tokens", 0)), 3),
                _td_priority(format_tokens(row.get("output_tokens", 0)), 3),
                _td_priority(format_tokens(row.get("cache_read_tokens", 0)), 3),
                _td_priority(format_tokens(row.get("cache_write_tokens", 0)), 3),
                _td_priority(format_tokens(row.get("reasoning_tokens", 0)), 3),
                _td_priority(format_bytes(row.get("bytes_received", 0)), 3),
                _td_priority(format_bytes(row.get("bytes_emitted", 0)), 3),
                _td_priority(format_latency(row.get("avg_ttft_ms", 0.0)), 3),
            ]
        )
        parts.append(f"<tr>{''.join(cells)}</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_timeseries_controls(
    *,
    bucket: str,
    group_by: str,
    metric: str,
    limit: int,
    account_filter: str,
    model_filter: str,
    account_options: list[str] | None = None,
    model_options: list[str] | None = None,
    period: str,
    current_theme: str,
) -> str:
    """Render the timeseries filter form with bucket, group, metric, etc.

    ``account_options`` and ``model_options`` populate dropdowns so
    operators don't have to type account names or model IDs by hand.
    The form auto-submits on change so chart updates feel instant; the
    Apply button is preserved as a keyboard / no-JS fallback.
    """

    def _select(name: str, current: str, options: list[tuple[str, str]]) -> str:
        items: list[str] = []
        for value, label in options:
            sel = " selected" if value == current else ""
            items.append(
                f'<option value="{escape_attr(value)}"{sel}>{escape(label)}</option>'
            )
        return f'<select name="{name}">{"".join(items)}</select>'

    bucket_options: list[tuple[str, str]] = [
        ("hour", "Hour"),
        ("day", "Day"),
    ]
    group_options: list[tuple[str, str]] = [
        ("provider_model", "Provider / model"),
        ("provider", "Provider"),
        ("model", "Model"),
        ("account", "Account"),
    ]
    metric_options: list[tuple[str, str]] = [
        ("tokens", "Tokens"),
        ("requests", "Requests"),
        ("cost", "Cost"),
        ("errors", "Errors"),
        ("bytes", "Bandwidth"),
        ("latency", "Avg latency"),
        ("ttft", "Avg TTFT"),
    ]
    limit_options: list[tuple[str, str]] = [
        (str(n), f"Top {n}") for n in (6, 8, 12, 16, 20, 25)
    ]
    selected_limit = str(limit if limit in {int(v) for v, _ in limit_options} else 12)

    account_choice = account_filter or ""
    account_select_options: list[tuple[str, str]] = [("", "(any account)")]
    for name in account_options or []:
        if name:
            account_select_options.append((name, name))
    model_choice = model_filter or ""
    model_select_options: list[tuple[str, str]] = [("", "(any model)")]
    for model_id in model_options or []:
        if model_id:
            model_select_options.append((model_id, model_id))

    return f"""
<form method="get" class="filter-form timeseries-controls"
      data-timeseries-controls
      aria-label="Timeseries filters">
  <label>Bucket: {_select("bucket", bucket, bucket_options)}</label>
  <label>Group by: {_select("group_by", group_by, group_options)}</label>
  <label>Metric: {_select("metric", metric, metric_options)}</label>
  <label>Limit: {_select("limit", selected_limit, limit_options)}</label>
  <label>Account: {_select("account", account_choice, account_select_options)}</label>
  <label>Model: {_select("model", model_choice, model_select_options)}</label>
  <input type="hidden" name="period" value="{escape_attr(period)}">
  <input type="hidden" name="theme" value="{escape_attr(current_theme)}">
  <button type="submit">Apply</button>
</form>
"""


def render_timeseries(
    series: list[dict[str, Any]],
    bucket: str,
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    *,
    grouped: dict[str, Any] | None = None,
    group_by: str = "provider_model",
    metric: str = "tokens",
    limit: int = 12,
    account_filter: str = "",
    model_filter: str = "",
    account_options: list[str] | None = None,
    model_options: list[str] | None = None,
    update_info: Any | None = None,
) -> str:
    """Render the timeseries page.

    The page renders the period selector at the top, then a filter form
    for bucket/group/metric/limit/account/model, then the grouped chart
    panel and the grouped detail table.  The legacy aggregate per-bucket
    table remains below as a secondary reference for operators who want
    the older single-bucket totals view.
    """
    controls = _render_timeseries_controls(
        bucket=bucket,
        group_by=group_by,
        metric=metric,
        limit=limit,
        account_filter=account_filter,
        model_filter=model_filter,
        account_options=account_options,
        model_options=model_options,
        period=period,
        current_theme=current_theme,
    )
    chart_panel = _render_grouped_timeseries_chart(
        grouped or {},
        period=period,
        bucket=bucket,
        group_by=group_by,
        metric=metric,
        limit=limit,
        account_filter=account_filter,
        model_filter=model_filter,
    )
    detail_table = _render_grouped_timeseries_table(grouped or {})
    aggregate_table = _render_aggregate_timeseries_table(series)

    body = f"""
<h2>Timeseries ({escape(bucket)} buckets, group by {escape(group_by)})</h2>
{_render_period_selector(period, current_theme, "timeseries-period-selector")}
{controls}

{chart_panel}

<section class="panel">
  <h3>Usage breakdown</h3>
  {detail_table}
</section>

<section class="panel">
  <h3>Aggregate per bucket</h3>
  {aggregate_table}
</section>
"""
    return _render_layout(
        title="Timeseries",
        body=body,
        active_nav="timeseries",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        include_chart_js=True,
        update_info=update_info,
    )


def _render_bandwidth_timeseries_table(
    series: list[dict[str, Any]],
) -> str:
    """Render a timeseries table with bandwidth columns."""
    if not series:
        return '<p class="empty">No bandwidth data in this window.</p>'
    parts = [
        '<table class="data">',
        "<thead><tr>",
        _th("Bucket"),
        _th("Requests"),
        _th("BW received"),
        _th("BW emitted"),
        "</tr></thead><tbody>",
    ]
    for row in series:
        req_count = int(row.get("request_count", 0))
        parts.append(
            f"<tr>"
            f"{_td_priority(escape(row.get('bucket', row.get('day', ''))), 1)}"
            f"{_td_priority(f'{req_count:,}', 1)}"
            f"{_td_priority(format_bytes(row.get('bytes_received', 0)), 1)}"
            f"{_td_priority(format_bytes(row.get('bytes_emitted', 0)), 1)}"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def render_bandwidth(
    summary: dict[str, Any],
    daily: list[dict[str, Any]],
    timeseries: list[dict[str, Any]],
    bucket: str = "hour",
    period: str = "24h",
    account_filter: str = "",
    theme_css: str = "",
    heatmap_colors: list[str] | None = None,
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the bandwidth page."""
    bytes_in = format_bytes(summary.get("total_bytes_received", 0))
    bytes_out = format_bytes(summary.get("total_bytes_emitted", 0))

    filter_form = f"""
<form method="get" class="filter-form">
  <label>Account:
    <input type="text" name="account" value="{escape_attr(account_filter)}"
           placeholder="(all)">
  </label>
  <input type="hidden" name="period" value="{escape_attr(period)}">
  <input type="hidden" name="bucket" value="{escape_attr(bucket)}">
  <input type="hidden" name="theme" value="{escape_attr(current_theme)}">
  <button type="submit">Apply</button>
</form>
"""

    body = f"""
<h2>Bandwidth</h2>
{filter_form}
{_render_period_selector(period, current_theme)}

<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Total received",
                    metric=bytes_in,
                    sub="client → proxy",
                ),
                _render_metric_card(
                    title="Total emitted",
                    metric=bytes_out,
                    sub="upstream → proxy",
                ),
            ]
        )
    }
</section>

<section class="panel">
  <h3>Bandwidth activity (last 90 days)</h3>
  {_render_bandwidth_heatmap(daily, heatmap_colors=heatmap_colors)}
</section>

<section class="panel">
  <h3>Bandwidth timeseries ({escape(bucket)} buckets)</h3>
  {_render_bandwidth_timeseries_table(timeseries)}
</section>
"""
    return _render_layout(
        title="Bandwidth",
        body=body,
        active_nav="bandwidth",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def render_pings(
    ping_summary: list[dict[str, Any]],
    recent_pings: list[dict[str, Any]],
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the provider pings health page."""
    # Provider health summary cards
    if ping_summary:
        cards: list[str] = []
        for row in ping_summary:
            pid = escape(str(row.get("provider_id", "")))
            avg_lat = format_latency(row.get("avg_latency_ms", 0))
            success_rate = row.get("success_rate", 0)
            model_count = int(row.get("last_model_count", 0))
            status = "healthy" if float(success_rate or 0) >= 90 else "degraded"
            cards.append(
                _render_metric_card(
                    title=str(row.get("provider_id", "")),
                    metric=avg_lat,
                    sub=(
                        f'<span class="{status}">{status}</span>'
                        f" · {success_rate}% success"
                        f" · {model_count} models"
                    ),
                    tooltip=(
                        "Provider ping latency summary. The metric is average "
                        "ping latency; the subtext shows health status, "
                        "success rate, and last seen model count."
                    ),
                )
            )
        provider_cards = f'<section class="cards">{"".join(cards)}</section>'
    elif recent_pings:
        provider_cards = ""
    else:
        provider_cards = (
            '<p class="empty">No ping data yet. '
            "Data appears after the first catalog refresh.</p>"
        )

    # Recent pings table
    if recent_pings:
        ping_parts = [
            '<table class="data">',
            "<thead><tr>",
            # Priority 1 — always shown
            _th("Provider"),
            _th("Time"),
            _th("Latency"),
            _th("Status"),
            # Priority 2 — shown on tablet+
            _th("Account", priority=2),
            _th("Models", priority=2),
            # Priority 3 — desktop only
            _th("Error", priority=3),
            "</tr></thead><tbody>",
        ]
        for row in recent_pings:
            pid = escape(str(row.get("provider_id", "")))
            acct = escape(str(row.get("account_name", "")))
            ts = escape(str(row.get("probed_at", "")))
            lat = format_latency(row.get("latency_ms", 0))
            status_code = row.get("status_code")
            status_str = str(status_code) if status_code else "—"
            model_count = int(row.get("model_count", 0))
            error = escape(str(row.get("error") or ""))
            ping_parts.append(
                f"<tr>"
                f"{_td_priority(pid, 1)}"
                f"{_td_priority(ts, 1)}"
                f"{_td_priority(lat, 1)}"
                f"{_td_priority(status_str, 1)}"
                f"{_td_priority(acct, 2)}"
                f"{_td_priority(str(model_count), 2)}"
                f"{_td_priority(error, 3)}"
                f"</tr>"
            )
        ping_parts.append("</tbody></table>")
        recent_table = (
            '<section class="panel">'
            "<h3>Recent pings</h3>"
            f"{''.join(ping_parts)}</section>"
        )
    else:
        recent_table = (
            '<section class="panel">'
            "<h3>Recent pings</h3>"
            '<p class="empty">No pings recorded yet.</p>'
            "</section>"
        )

    body = f"""
<h2>Provider Pings</h2>
{_render_period_selector(period, current_theme)}

{provider_cards}

{recent_table}
"""
    return _render_layout(
        title="Provider Pings",
        body=body,
        active_nav="pings",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def render_runtime(
    snapshot: dict[str, Any],
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
    transcoding_stats: dict[str, Any] | None = None,
    period: str = "24h",
) -> str:
    """Render the runtime metrics page."""
    server = _as_dict(snapshot.get("server"))
    memory = _as_dict(snapshot.get("memory"))
    processes = _as_dict(snapshot.get("processes"))
    background_tasks: list[dict[str, Any]] = snapshot.get("background_tasks") or []
    db = _as_dict(snapshot.get("db"))
    routing = _as_dict(snapshot.get("routing_runtime"))
    probe_errors: list[str] = snapshot.get("probe_errors") or []
    outbound = _as_dict(snapshot.get("outbound_client"))
    provider_pool = _as_dict(snapshot.get("provider_client_pool"))
    dns_cache = _as_dict(snapshot.get("dns_cache"))
    load = _as_dict(snapshot.get("load"))
    dispatch = _as_dict(snapshot.get("dispatch_overhead"))

    # Transcoding stats
    tc_total: int = int(transcoding_stats.get("total", 0)) if transcoding_stats else 0
    tc_native: int = (
        int(transcoding_stats.get("native_count", 0)) if transcoding_stats else 0
    )
    tc_transcoded: int = (
        int(transcoding_stats.get("transcoded_count", 0)) if transcoding_stats else 0
    )
    tc_per_direction: dict[tuple[str, str], int] = (
        dict(transcoding_stats.get("per_direction", {})) if transcoding_stats else {}
    )

    # Server section
    pid = server.get("pid", "—")
    uptime_s = server.get("uptime_seconds")
    uptime = format_age_seconds(uptime_s)
    python_ver = escape(str(server.get("python_version", "—")))
    platform_str = escape(str(server.get("platform", "—")))
    ppid = server.get("ppid", "—")
    is_daemon = server.get("is_daemon_hint", False)
    daemon_label = "yes" if is_daemon else "no"

    # Process count
    process_count = processes.get("eggpool_process_count")
    expected_count = processes.get("expected_worker_process_count", 2)
    process_warning = processes.get("process_count_warning", False)

    # Memory
    rss_bytes = memory.get("rss_bytes")
    rss = format_bytes(rss_bytes) if rss_bytes is not None else "—"
    open_fds = format_int(memory.get("open_fd_count"))
    thread_count = format_int(memory.get("thread_count"))

    # Load average
    load_available = bool(load.get("available", False))
    load_1m = load.get("load_1m")
    load_5m = load.get("load_5m")
    load_15m = load.get("load_15m")
    norm_1m = load.get("normalized_1m")
    cpu_count = load.get("cpu_count")

    if (
        load_available
        and load_1m is not None
        and load_5m is not None
        and load_15m is not None
    ):
        load_metric = f"{float(load_1m):.2f}"
        if norm_1m is not None:
            load_sub = f"{float(norm_1m):.2f}/core · {format_int(cpu_count)} CPUs"
        else:
            load_sub = f"5m {float(load_5m):.2f} · 15m {float(load_15m):.2f}"
    else:
        load_metric = "—"
        load_sub = "load average unavailable"

    # Dispatch overhead
    avg_dispatch_ms = dispatch.get("avg_ms")
    p95_dispatch_ms = dispatch.get("p95_ms")
    p99_dispatch_ms = dispatch.get("p99_ms")
    max_dispatch_ms = dispatch.get("max_ms")
    sample_count = dispatch.get("sample_count", 0)
    window_size = dispatch.get("window_size", 100)

    def _format_small_ms(value: Any) -> str:
        if value is None:
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if number < 1:
            return f"{number:.2f} ms"
        if number < 10:
            return f"{number:.1f} ms"
        return f"{number:.0f} ms"

    if avg_dispatch_ms is None:
        dispatch_metric = "—"
        dispatch_sub = (
            f"last {format_int(sample_count)} / {format_int(window_size)} attempts"
        )
    else:
        dispatch_metric = _format_small_ms(avg_dispatch_ms)
        dispatch_sub = (
            f"p95 {_format_small_ms(p95_dispatch_ms)} · "
            f"p99 {_format_small_ms(p99_dispatch_ms)} · "
            f"max {_format_small_ms(max_dispatch_ms)} · "
            f"n={format_int(sample_count)}"
        )

    # Database
    db_path = escape(str(db.get("path") or ":memory:"))
    db_file_size = format_bytes(db.get("file_size_bytes"))
    db_wal_size = format_bytes(db.get("wal_size_bytes"))
    db_wal_enabled = db.get("wal_enabled", False)
    db_wal_live = db.get("wal_mode_live", "")
    db_sync = escape(str(db.get("synchronous_live") or "—"))
    db_primary_connected = db.get("primary_connected")
    db_separate_stats = db.get("stats_connection_separate", False)
    db_worker_threads = format_int(db.get("configured_worker_threads"))

    # Routing / in-flight
    pending_count = routing.get("pending_count")
    oldest_pending_age = format_age_seconds(routing.get("oldest_pending_age_seconds"))
    active_reservations = routing.get("active_reservations_count")
    reserved_microdollars = format_microdollars(routing.get("reserved_microdollars", 0))
    active_requests = routing.get("active_requests_total")
    active_backoff_count = routing.get("active_backoff_count")
    health_states: dict[str, str] = routing.get("health_states_by_account") or {}

    # Process count warning card
    process_count_display = (
        format_int(process_count) if process_count is not None else "—"
    )
    expected_display = format_int(expected_count) if expected_count is not None else "—"

    # Server info cards
    server_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Server PID",
                    metric=str(pid),
                    sub=f"PPID {ppid} · daemon {daemon_label}",
                ),
                _render_metric_card(
                    title="Uptime",
                    metric=uptime,
                    sub="uptime since start",
                ),
                _render_metric_card(
                    title="Python",
                    metric=python_ver,
                    sub=platform_str,
                ),
            ]
        )
    }
</section>
"""

    # Process & memory cards
    memory_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="RSS memory",
                    metric=rss,
                    sub="resident set size",
                ),
                _render_metric_card(
                    title="Open FDs",
                    metric=open_fds,
                    sub="file descriptors",
                ),
                _render_metric_card(
                    title="Active threads",
                    metric=thread_count,
                    sub="threading.active_count()",
                ),
                _render_metric_card(
                    title="Load average",
                    metric=load_metric,
                    sub=load_sub,
                ),
                _render_metric_card(
                    title="Dispatch overhead",
                    metric=dispatch_metric,
                    sub=dispatch_sub,
                ),
            ]
        )
    }
</section>
"""

    process_warning_section = ""
    if process_warning:
        process_warning_section = f"""
<section class="panel warning">
  <h3>Process count warning</h3>
  <p>Observed {process_count_display} EggPool processes;
     expected {expected_display}.</p>
</section>
"""

    # Background tasks table
    if background_tasks:
        task_rows: list[str] = []
        for task in background_tasks:
            name = escape(str(task.get("name", "")))
            running = bool(task.get("running", False))
            done = bool(task.get("done", False))
            cancelled = bool(task.get("cancelled", False))
            restarts = int(task.get("restart_count", 0) or 0)
            max_restarts = task.get("max_restarts")
            status = "running" if running else ("cancelled" if cancelled else "stopped")
            status_cls = "yes" if running else ("no" if cancelled else "")
            max_str = format_int(max_restarts) if max_restarts is not None else "—"
            task_rows.append(
                f"<tr>"
                f"{_td_priority(escape(name), 1)}"
                f"{_td_priority(status, 1, class_=status_cls)}"
                f"{_td_priority(str(restarts), 2)}"
                f"{_td_priority(max_str, 2)}"
                f"{_td_priority('yes' if done else 'no', 3)}"
                f"</tr>"
            )
        tasks_table = (
            '<table class="data compact">'
            + "<thead><tr>"
            # Priority 1 — always shown
            + _th("Task")
            + _th("Status")
            # Priority 2 — shown on tablet+
            + _th("Restarts", priority=2)
            + _th("Max restarts", priority=2)
            # Priority 3 — desktop only
            + _th("Done", priority=3)
            + "</tr></thead><tbody>"
            + f"{''.join(task_rows)}"
            + "</tbody></table>"
        )
    else:
        tasks_table = '<p class="empty">No background tasks registered.</p>'

    # Database info cards
    db_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Database",
                    metric=db_path,
                    sub=f"file size {db_file_size}",
                ),
                _render_metric_card(
                    title="WAL",
                    metric=db_wal_size,
                    sub=f"enabled {escape(str(db_wal_enabled))} · mode {db_wal_live}",
                ),
                _render_metric_card(
                    title="Sync",
                    metric=db_sync,
                    sub=f"connected {escape(str(db_primary_connected))}",
                ),
                _render_metric_card(
                    title="Stats DB",
                    metric="separate" if db_separate_stats else "shared",
                    sub=f"{db_worker_threads} configured SQLite worker threads",
                ),
            ]
        )
    }
</section>
"""

    # In-flight / routing cards
    pending_count_str = format_int(pending_count) if pending_count is not None else "—"
    active_res_str = (
        format_int(active_reservations) if active_reservations is not None else "—"
    )
    active_req_str = format_int(active_requests) if active_requests is not None else "—"
    backoff_str = (
        format_int(active_backoff_count) if active_backoff_count is not None else "—"
    )
    routing_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Pending requests",
                    metric=pending_count_str,
                    sub=f"oldest {oldest_pending_age}",
                ),
                _render_metric_card(
                    title="Active reservations",
                    metric=active_res_str,
                    sub=f"reserved {reserved_microdollars}",
                ),
                _render_metric_card(
                    title="In-flight requests",
                    metric=active_req_str,
                    sub="active upstream",
                ),
                _render_metric_card(
                    title="Active backoffs",
                    metric=backoff_str,
                    sub="account backoff rows",
                ),
            ]
        )
    }
</section>
"""

    # Health states table
    if health_states:
        health_rows: list[str] = []
        for acct, state in sorted(health_states.items()):
            health_rows.append(
                f"<tr>"
                f"{_td_priority(escape(acct), 1)}"
                f"{_td_priority(escape(state), 1, class_=sanitize_class_name(state))}"
                f"</tr>"
            )
        health_table = (
            '<table class="data compact">'
            + "<thead><tr>"
            + _th("Account")
            + _th("Health state")
            + "</tr></thead><tbody>"
            + f"{''.join(health_rows)}"
            + "</tbody></table>"
        )
    else:
        health_table = '<p class="empty">No health state data.</p>'

    # Network diagnostics section
    dns_enabled = dns_cache.get("enabled", False)
    dns_entries = dns_cache.get("size", 0)
    dns_max_entries = dns_cache.get("max_entries")
    if dns_max_entries is not None:
        dns_entries_label = (
            f"entries {format_int(dns_entries)} / {format_int(dns_max_entries)}"
        )
    else:
        dns_entries_label = f"entries {format_int(dns_entries)}"
    dns_hits = dns_cache.get("hits", 0)
    dns_misses = dns_cache.get("misses", 0)
    dns_negative = dns_cache.get("negative_hits", 0)
    dns_stale = dns_cache.get("stale_hits", 0)
    # New precise metrics
    dns_suppression_rate = dns_cache.get("dns_suppression_rate", 0.0)
    dns_suppression_pct = (
        f"{dns_suppression_rate * 100:.1f}%" if dns_suppression_rate else "—"
    )
    dns_resolver_calls = dns_cache.get("resolver_calls_total", 0)
    dns_resolver_errors = dns_cache.get("resolver_errors_total", 0)
    ob_builds = format_int(outbound.get("build_count", 0))
    ob_requests = format_int(outbound.get("request_count", 0))
    ob_errors = format_int(outbound.get("error_count", 0))
    provider_builds = format_int(provider_pool.get("build_count", 0))

    network_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="DNS cache",
                    metric="enabled" if dns_enabled else "disabled",
                    sub=dns_entries_label,
                ),
                _render_metric_card(
                    title="DNS suppression",
                    metric=dns_suppression_pct,
                    sub=(f"{format_int(dns_resolver_calls)} resolver calls"),
                ),
                _render_metric_card(
                    title="DNS hits",
                    metric=format_int(dns_hits),
                    sub=f"{format_int(dns_misses)} owner misses",
                ),
                _render_metric_card(
                    title="DNS errors",
                    metric=format_int(dns_resolver_errors),
                    sub=(
                        f"stale {format_int(dns_stale)} · "
                        f"neg {format_int(dns_negative)}"
                    ),
                ),
                _render_metric_card(
                    title="Outbound builds",
                    metric=ob_builds,
                    sub="client lifecycle",
                ),
                _render_metric_card(
                    title="Outbound requests",
                    metric=ob_requests,
                    sub=f"errors {ob_errors}",
                ),
                _render_metric_card(
                    title="Provider clients",
                    metric=provider_builds,
                    sub="per-provider builds",
                ),
            ]
        )
    }
</section>
"""

    # Probe errors
    probe_section = ""
    if probe_errors:
        error_items = "".join(f"<li>{escape(err)}</li>" for err in probe_errors)
        probe_section = f"""
<section class="panel warning">
  <h3>Probe errors</h3>
  <ul>{error_items}</ul>
</section>
"""

    # Transcoding card
    tc_card = ""
    if transcoding_stats is not None:
        tc_direction_rows = ""
        for (client, upstream), count in sorted(
            tc_per_direction.items(), key=lambda x: x[1], reverse=True
        ):
            direction = f"{escape(client)} → {escape(upstream)}"
            tc_direction_rows += (
                f"<tr><td>{direction}</td><td class='num'>{format_int(count)}</td></tr>"
            )
        tc_direction_table = ""
        if tc_direction_rows:
            tc_direction_table = (
                '<table class="data compact"><thead><tr>'
                + _th("Direction")
                + _th("Count", priority=2)
                + "</tr></thead><tbody>"
                + tc_direction_rows
                + "</tbody></table>"
            )
        tc_loss_rows = ""
        tc_loss_warning_items = cast(
            "list[dict[str, object]]",
            transcoding_stats.get("top_loss_warnings") or [],
        )
        for entry in tc_loss_warning_items:
            direction_pair: object = entry.get("direction")
            count_val: object = entry.get("count")
            if (
                isinstance(direction_pair, (list, tuple))
                and len(cast("list[object] | tuple[object, ...]", direction_pair)) == 2
            ):
                client_proto = escape(str(cast("list[object]", direction_pair)[0]))
                upstream_proto = escape(str(cast("list[object]", direction_pair)[1]))
                direction = f"{client_proto} → {upstream_proto}"
            else:
                direction = "—"
            count_int: int | None
            if isinstance(count_val, int):
                count_int = count_val
            elif isinstance(count_val, float):
                count_int = int(count_val)
            else:
                count_int = 0
            tc_loss_rows += (
                f"<tr><td>{direction}</td>"
                f"<td class='num'>{format_int(count_int)}</td></tr>"
            )
        tc_loss_panel = ""
        if tc_loss_rows:
            tc_loss_panel = (
                "<h4>Top loss warnings</h4>"
                '<table class="data compact"><thead><tr>'
                + _th("Direction")
                + _th("Count", priority=2)
                + "</tr></thead><tbody>"
                + tc_loss_rows
                + "</tbody></table>"
            )
        else:
            tc_loss_panel = (
                "<p class='empty-state'>No loss warnings recorded in this period.</p>"
            )
        tc_card = f"""
<section class="panel">
  <h3>Transcoding ({escape(period)})</h3>
  <section class="cards">
    {
            _render_metric_card(
                title="Total requests",
                metric=format_int(tc_total),
                sub="in period",
            )
        }
    {
            _render_metric_card(
                title="Native",
                metric=format_int(tc_native),
                sub="no transcoding",
            )
        }
    {
            _render_metric_card(
                title="Transcoded",
                metric=format_int(tc_transcoded),
                sub="cross-protocol",
            )
        }
  </section>
  {tc_direction_table}
  {tc_loss_panel}
</section>
"""

    body = f"""
<h2>Runtime</h2>
<p class="sub">Process-level diagnostics for the running EggPool instance.</p>

{server_cards}

{memory_cards}

{process_warning_section}

<section class="panel">
  <h3>Background tasks</h3>
  {tasks_table}
</section>

{db_cards}

{routing_cards}

{network_cards}

{tc_card}

<section class="panel">
  <h3>Health states</h3>
  {health_table}
</section>

{probe_section}
"""
    return _render_layout(
        title="Runtime",
        body=body,
        active_nav="runtime",
        period="runtime",
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        auto_refresh=True,
        update_info=update_info,
    )


def _render_chart_canvas(
    canvas_id: str,
    chart_type: str,
    labels_json: str,
    datasets_json: str,
    options_json: str = "{}",
    *,
    include_chart_js: bool = True,
    height_px: int = 280,
) -> str:
    """Render a Chart.js canvas with a sibling JSON data island.

    The chart is seeded from an inlined ``<script type="application/json">``
    payload (``class="static-chart-data"``) so the deferred
    ``dashboard.js`` can initialise it after Chart.js has loaded. Emitting
    an inline ``new Chart(...)`` script would race the deferred
    ``/static/chart.js`` tag appended at the end of ``<body>`` and leave
    the canvas empty (``Chart is not defined``).

    ``include_chart_js`` mirrors the page-level helper flag; the caller
    still decides whether the page's layout pulls in the Chart.js library
    itself. ``canvas_id_json`` is kept as a local so the data island is
    self-describing even when the helper is reused across pages.
    """
    del include_chart_js
    canvas_id_json = json.dumps(canvas_id)
    payload = json.dumps(
        {
            "type": chart_type,
            "labels": json.loads(labels_json),
            "datasets": json.loads(datasets_json),
            "options": json.loads(options_json),
        }
    )
    return f"""
<div class="chart-wrap" style="height: {height_px}px;">
  <canvas id="{canvas_id}"></canvas>
</div>
<script type="application/json" class="static-chart-data"
        data-chart-id={canvas_id_json}>{payload}</script>
"""


def _format_int(value: Any) -> str:
    """Format an integer with thousands separators (zero-tolerant helper)."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _format_percent_unit(value: Any, *, fraction: bool = True, digits: int = 1) -> str:
    """Format a value that may be a fraction (0..1) or already a percent."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not fraction:
        return f"{number:.{digits}f}%"
    return f"{number * 100:.{digits}f}%"


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce ``value`` to a dict, returning ``{}`` when not a mapping."""
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, Any]", value)


def _as_mapping(value: object) -> dict[str, Any]:
    """Coerce ``value`` to a dict for untrusted dashboard sidecar payloads."""
    if not isinstance(value, Mapping):
        return {}
    typed = cast("Mapping[object, Any]", value)
    return {str(key): item for key, item in typed.items()}


def _as_str_list(value: object) -> list[str]:
    """Return a string list for list-like dashboard sidecar fields."""
    items: list[object] | tuple[object, ...] | set[object] | frozenset[object]
    if isinstance(value, list):
        items = cast("list[object]", value)
    elif isinstance(value, tuple):
        items = cast("tuple[object, ...]", value)
    elif isinstance(value, set):
        items = cast("set[object]", value)
    elif isinstance(value, frozenset):
        items = cast("frozenset[object]", value)
    else:
        return []
    return [str(item) for item in items if item is not None]


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed > 0 else None


def render_reliability(
    *,
    period: str,
    attempt_stats: dict[str, Any] | None,
    retry_distribution: list[dict[str, Any]],
    pending_health: dict[str, Any] | None,
    operational_summary: list[dict[str, Any]],
    recent_operational_events: list[dict[str, Any]],
    timeseries: list[dict[str, Any]],
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the Reliability page.

    Shows attempt stats (total, success, retry, failure), the
    attempts-by-provider chart, the pending / finalizer health card,
    the retry-category distribution, and the recent operational
    events table.
    """
    attempts = attempt_stats or {}
    total_attempts = int(attempts.get("total_attempts", 0) or 0)
    success_attempts = int(attempts.get("success_attempts", 0) or 0)
    retry_attempts = int(attempts.get("retry_attempts", 0) or 0)
    failed_attempts = int(attempts.get("failed_attempts", 0) or 0)
    retry_rate = float(attempts.get("retry_rate", 0.0) or 0.0)
    avg_attempt_latency = float(attempts.get("avg_attempt_latency_ms", 0.0) or 0.0)
    first_attempt_success_rate = (
        success_attempts / total_attempts if total_attempts > 0 else 0.0
    )
    first_attempt_pct = _format_percent_unit(first_attempt_success_rate, digits=1)

    summary_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Total attempts",
                    metric=_format_int(total_attempts),
                    sub=period,
                ),
                _render_metric_card(
                    title="Success attempts",
                    metric=_format_int(success_attempts),
                    sub=f"first-attempt success rate {first_attempt_pct}",
                ),
                _render_metric_card(
                    title="Retry attempts",
                    metric=_format_int(retry_attempts),
                    sub=f"retry rate {_format_percent_unit(retry_rate, digits=1)}",
                ),
                _render_metric_card(
                    title="Failed attempts",
                    metric=_format_int(failed_attempts),
                    sub=f"avg attempt latency {avg_attempt_latency:.1f} ms",
                ),
            ]
        )
    }
</section>
"""

    attempts_chart = _render_attempts_by_provider_chart(attempt_stats, period)
    pending_table = _render_pending_health_table(pending_health)
    operational_table = _render_operational_events_table(
        recent_operational_events, operational_summary
    )

    body = f"""
<h2>Reliability</h2>
{_render_period_selector(period, current_theme)}

{summary_cards}

<section class="panel">
  <h3>Attempts by provider (aggregated)</h3>
  {attempts_chart}
</section>

{pending_table}

<section class="panel">
  <h3>Retry distribution</h3>
  {_render_retry_distribution_table(retry_distribution)}
</section>

{operational_table}
"""
    return _render_layout(
        title="Reliability",
        body=body,
        active_nav="reliability",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        include_chart_js=True,
        update_info=update_info,
    )


def _render_attempts_by_provider_chart(
    attempt_stats: dict[str, Any] | None,
    period: str,
) -> str:
    """Render the bar chart of attempts by provider.

    The attempt_stats dict doesn't carry per-provider model data
    (only aggregate), so we render a single grouped bar showing the
    aggregate success / retry / failed attempt counts. This still
    answers "what fraction of attempts succeed vs retry vs fail?"
    for the selected period.
    """
    del period
    attempts = attempt_stats or {}
    success = int(attempts.get("success_attempts", 0) or 0)
    retry = int(attempts.get("retry_attempts", 0) or 0)
    failed = int(attempts.get("failed_attempts", 0) or 0)
    labels = json.dumps(["Success", "Retry", "Failed"])
    datasets = json.dumps(
        [
            {
                "label": "Attempts",
                "data": [success, retry, failed],
                "backgroundColor": [
                    "rgba(75, 192, 120, 0.7)",
                    "rgba(255, 159, 64, 0.7)",
                    "rgba(255, 99, 132, 0.7)",
                ],
            }
        ]
    )
    options = json.dumps(
        {
            "responsive": True,
            "maintainAspectRatio": False,
            "plugins": {"legend": {"display": False}},
            "scales": {
                "y": {"beginAtZero": True, "title": {"display": True, "text": "Count"}}
            },
        }
    )
    return _render_chart_canvas(
        "reliability-attempts-by-provider",
        "bar",
        labels,
        datasets,
        options,
    )


def _render_pending_health_table(pending_health: dict[str, Any] | None) -> str:
    """Render the pending / reservation health card."""
    snapshot = pending_health or {}
    pending_count = int(snapshot.get("pending_count", 0) or 0)
    oldest_pending_age = format_age_seconds(snapshot.get("oldest_pending_age_seconds"))
    stale_pending = int(snapshot.get("stale_pending_count", 0) or 0)
    active_reservation_count = int(snapshot.get("active_reservation_count", 0) or 0)
    active_reserved = format_microdollars(
        snapshot.get("active_reserved_microdollars", 0)
    )
    oldest_reservation_age = format_age_seconds(
        snapshot.get("oldest_reservation_age_seconds")
    )
    pending_warn = pending_count > 0 and stale_pending > 0
    return f"""
<section class="cards system-health">
  {
        "".join(
            [
                _render_metric_card(
                    title="Pending requests",
                    metric=f"{pending_count:,}",
                    sub=f"oldest {oldest_pending_age} · stale {stale_pending}",
                    warning=pending_warn,
                ),
                _render_metric_card(
                    title="Active reservations",
                    metric=f"{active_reservation_count:,}",
                    sub=f"reserved {active_reserved} · oldest {oldest_reservation_age}",
                ),
                _render_metric_card(
                    title="Pending window",
                    sub="stale &gt; 15 minutes are flagged for cleanup",
                    extra_subs=("snapshot is instantaneous; reload to refresh",),
                ),
            ]
        )
    }
</section>
"""


def _render_retry_distribution_table(
    distribution: list[dict[str, Any]],
) -> str:
    """Render the retry-category breakdown table."""
    if not distribution:
        return '<p class="empty">No attempt data for this period.</p>'
    rows: list[str] = []
    for row in distribution:
        category = str(row.get("retry_category", "unclassified"))
        attempt_count = int(row.get("attempt_count", 0) or 0)
        retry_outcome_count = int(row.get("retry_outcome_count", 0) or 0)
        success_count = int(row.get("success_count", 0) or 0)
        failure_count = int(row.get("failure_count", 0) or 0)
        avg_lat = float(row.get("avg_attempt_latency_ms", 0.0) or 0.0)
        rows.append(
            f"<tr>"
            f"{_td_priority(escape(_error_category_label(category)), 1)}"
            f"{_td_priority(f'{attempt_count:,}', 1)}"
            f"{_td_priority(f'{retry_outcome_count:,}', 2)}"
            f"{_td_priority(f'{success_count:,}', 2)}"
            f"{_td_priority(f'{failure_count:,}', 2)}"
            f"{_td_priority(f'{avg_lat:.1f} ms', 3)}"
            f"</tr>"
        )
    return (
        '<table class="data">'
        + "<thead><tr>"
        # Priority 1 — always shown
        + _th("Category")
        + _th("Attempts")
        # Priority 2 — shown on tablet+
        + _th("Retry outcomes", priority=2)
        + _th("Successes", priority=2)
        + _th("Failures", priority=2)
        # Priority 3 — desktop only
        + _th("Avg attempt latency", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
    )


def _render_operational_events_table(
    events: list[dict[str, Any]],
    summary: list[dict[str, Any]],
) -> str:
    """Render the recent operational events table.

    Combines a small per-event-type summary with the most recent raw
    events. The ``details_json`` blob is escaped and shown verbatim so
    operators can correlate ``crash_recovery`` and
    ``reservation_reconcile`` payloads without leaving the page.
    """
    summary_rows: list[str] = []
    for row in summary or []:
        event_type = str(row.get("event_type", ""))
        event_count = int(row.get("event_count", 0) or 0)
        last_at = str(row.get("last_occurred_at", "") or "")
        interrupted = int(row.get("total_interrupted_requests", 0) or 0)
        released = int(row.get("total_released_reservations", 0) or 0)
        summary_rows.append(
            f"<tr>"
            f"{_td_priority(escape(event_type), 1)}"
            f"{_td_priority(f'{event_count:,}', 1)}"
            f"{_td_priority(escape(last_at), 2)}"
            f"{_td_priority(f'{interrupted:,}', 2)}"
            f"{_td_priority(f'{released:,}', 3)}"
            f"</tr>"
        )
    summary_table = (
        '<table class="data compact">'
        + "<thead><tr>"
        # Priority 1 — always shown
        + _th("Event type")
        + _th("Count")
        # Priority 2 — shown on tablet+
        + _th("Last seen", priority=2)
        + _th("Interrupted", priority=2)
        # Priority 3 — desktop only
        + _th("Released", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(summary_rows)}"
        + "</tbody></table>"
    )
    if not summary_rows:
        summary_table = '<p class="empty">No operational events in this window.</p>'

    if not events:
        recent_table = '<p class="empty">No recent operational events.</p>'
    else:
        recent_rows: list[str] = []
        for row in events[:25]:
            event_type = str(row.get("event_type", ""))
            details_raw = row.get("details_json", "") or ""
            if isinstance(details_raw, bytes):
                details_text = details_raw.decode("utf-8", errors="replace")
            else:
                details_text = str(details_raw)
            truncated_details = truncate(details_text, 200)
            recent_rows.append(
                f"<tr>"
                f"{_td_priority(escape(str(row.get('occurred_at', ''))), 1)}"
                f"{_td_priority(escape(event_type), 1)}"
                f"{_td_priority(truncated_details, 2)}"
                f"</tr>"
            )
        recent_table = (
            '<table class="data compact">'
            + "<thead><tr>"
            + _th("When")
            + _th("Type")
            + _th("Details", priority=2)
            + "</tr></thead><tbody>"
            + f"{''.join(recent_rows)}"
            + "</tbody></table>"
        )

    return f"""
<section class="panel">
  <h3>Operational events (summary)</h3>
  {summary_table}
</section>

<section class="panel">
  <h3>Operational events (recent)</h3>
  {recent_table}
</section>
"""


def render_routing(
    *,
    period: str,
    routing_distribution: list[dict[str, Any]],
    routing_selection_breakdown: list[dict[str, Any]],
    routing_exclusion_breakdown: list[dict[str, Any]],
    routing_skew_summary: dict[str, Any] | None = None,
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the Routing page.

    Visualises how the router distributes requests across
    (model, provider) combinations, which accounts get selected, and
    why accounts are excluded. The exclusion table is grouped by the
    suppressive/advisory taxonomy so operators can verify that local
    scoring only influences priority while upstream failures control
    exclusion.
    """
    total_decisions = sum(
        int(row.get("decision_count", 0) or 0) for row in routing_distribution or []
    )
    avg_eligible = (
        sum(
            float(row.get("avg_eligible_count", 0.0) or 0.0)
            for row in routing_distribution or []
        )
        / len(routing_distribution)
        if routing_distribution
        else 0.0
    )
    distinct_accounts = sum(
        int(row.get("distinct_selected_accounts", 0) or 0)
        for row in routing_distribution or []
    )

    skew = routing_skew_summary or {}
    skew_ratio = float(skew.get("skew_ratio", 0.0) or 0.0)
    most_selected = escape(str(skew.get("most_selected_account") or "—"))
    least_selected = escape(str(skew.get("least_selected_account") or "—"))
    skew_total = int(skew.get("total_selections", 0) or 0)
    skew_accounts = int(skew.get("distinct_accounts", 0) or 0)
    skew_warning = skew_ratio > 3.0 and skew_total > 10

    skew_cards = ""
    if skew_total > 0:
        skew_cards = f"""
{
            _render_metric_card(
                title="Selection skew",
                metric=f"{skew_ratio:.1f}x",
                sub=f"max/min ratio ({most_selected} / {least_selected})",
                warning=skew_warning,
                extra_subs=(
                    f"{skew_total:,} selections across {skew_accounts} accounts",
                ),
            )
        }"""

    summary_cards = f"""
<section class="cards">
  {
        "".join(
            [
                _render_metric_card(
                    title="Routing decisions",
                    metric=_format_int(total_decisions),
                    sub="in selected period",
                ),
                _render_metric_card(
                    title="Avg eligible / decision",
                    metric=f"{avg_eligible:.2f}",
                    sub="candidate accounts per decision",
                ),
                _render_metric_card(
                    title="Distinct selected accounts",
                    metric=_format_int(distinct_accounts),
                    sub="across all (model, provider) groups",
                ),
            ]
        )
    }
  {skew_cards}
</section>
"""

    exclusion_chart = _render_exclusion_taxonomy_chart(routing_exclusion_breakdown)
    distribution_table = _render_routing_distribution_table(routing_distribution)
    selection_table = _render_selection_breakdown_table(routing_selection_breakdown)
    exclusion_table = _render_exclusion_table(routing_exclusion_breakdown)

    body = f"""
<h2>Routing</h2>
{_render_period_selector(period, current_theme)}

{summary_cards}

<section class="panel">
  <h3>Exclusion taxonomy</h3>
  {exclusion_chart}
</section>

<section class="panel">
  <h3>Routing distribution</h3>
  {distribution_table}
</section>

<section class="panel">
  <h3>Account selection breakdown</h3>
  {selection_table}
</section>

<section class="panel">
  <h3>Account exclusions</h3>
  {exclusion_table}
</section>
"""
    return _render_layout(
        title="Routing",
        body=body,
        active_nav="routing",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        include_chart_js=True,
        update_info=update_info,
    )


def _render_exclusion_taxonomy_chart(
    exclusion_breakdown: list[dict[str, Any]],
) -> str:
    """Render a doughnut chart of exclusion counts by category."""
    category_totals: dict[str, int] = {
        "suppressive": 0,
        "advisory": 0,
        "unknown": 0,
    }
    for row in exclusion_breakdown or []:
        reason = str(row.get("reason", ""))
        count = int(row.get("exclusion_count", 0) or 0)
        category = _classify_exclusion(reason)
        category_totals[category] = category_totals.get(category, 0) + count

    if sum(category_totals.values()) == 0:
        return '<p class="empty">No exclusion data in this period.</p>'

    labels = json.dumps(["Suppressive", "Advisory", "Unknown"])
    datasets = json.dumps(
        [
            {
                "label": "Exclusions",
                "data": [
                    category_totals["suppressive"],
                    category_totals["advisory"],
                    category_totals["unknown"],
                ],
                "backgroundColor": [
                    "rgba(255, 99, 132, 0.7)",
                    "rgba(255, 206, 86, 0.7)",
                    "rgba(201, 203, 207, 0.7)",
                ],
            }
        ]
    )
    options = json.dumps(
        {
            "responsive": True,
            "maintainAspectRatio": False,
            "plugins": {"legend": {"position": "right"}},
        }
    )
    return _render_chart_canvas(
        "routing-exclusion-taxonomy",
        "doughnut",
        labels,
        datasets,
        options,
    )


def _render_routing_distribution_table(
    distribution: list[dict[str, Any]],
) -> str:
    """Render per-(model, provider) routing distribution."""
    if not distribution:
        return '<p class="empty">No routing decisions in this period.</p>'
    rows: list[str] = []
    for row in distribution:
        model_id = escape(str(row.get("model_id", "")))
        provider_id = escape(str(row.get("provider_id", "")))
        decision_count = int(row.get("decision_count", 0) or 0)
        avg_eligible = float(row.get("avg_eligible_count", 0.0) or 0.0)
        avg_scored = float(row.get("avg_scored_count", 0.0) or 0.0)
        avg_excluded = float(row.get("avg_attempted_excluded_count", 0.0) or 0.0)
        avg_selected_score = float(row.get("avg_selected_score", 0.0) or 0.0)
        distinct_accounts = int(row.get("distinct_selected_accounts", 0) or 0)
        rows.append(
            f"<tr>"
            f"{_td_priority(model_id, 1)}"
            f"{_td_priority(provider_id, 1)}"
            f"{_td_priority(f'{decision_count:,}', 1)}"
            f"{_td_priority(f'{avg_eligible:.2f}', 2)}"
            f"{_td_priority(f'{avg_scored:.2f}', 2)}"
            f"{_td_priority(f'{avg_excluded:.2f}', 2)}"
            f"{_td_priority(f'{avg_selected_score:.3f}', 3)}"
            f"{_td_priority(str(distinct_accounts), 3)}"
            f"</tr>"
        )
    return (
        '<table class="data">'
        + "<thead><tr>"
        # Priority 1 — always shown
        + _th("Model")
        + _th("Provider")
        + _th("Decisions")
        # Priority 2 — shown on tablet+
        + _th("Avg eligible", priority=2)
        + _th("Avg scored", priority=2)
        + _th("Avg excluded", priority=2)
        # Priority 3 — desktop only
        + _th("Avg score", priority=3)
        + _th("Distinct accounts", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
    )


def _render_selection_breakdown_table(
    selection_breakdown: list[dict[str, Any]],
) -> str:
    """Render the account-level selection counts with last-selection info."""
    if not selection_breakdown:
        return '<p class="empty">No selection data in this period.</p>'
    rows: list[str] = []
    for row in selection_breakdown:
        account_name = escape(str(row.get("account_name", "unknown")))
        provider_id = escape(str(row.get("provider_id", "")))
        selection_count = int(row.get("selection_count", 0) or 0)
        avg_tier = float(row.get("avg_selected_tier", 0.0) or 0.0)
        avg_score = float(row.get("avg_selected_score", 0.0) or 0.0)
        avg_eligible = float(row.get("avg_eligible_count", 0.0) or 0.0)
        last_score_raw = row.get("last_selected_score")
        last_score = (
            f"{float(last_score_raw):.3f}" if last_score_raw is not None else "—"
        )
        last_tier_raw = row.get("last_selected_tier")
        last_tier = f"{int(last_tier_raw)}" if last_tier_raw is not None else "—"
        last_at_raw = row.get("last_selected_at")
        last_at = escape(str(last_at_raw)[:19]) if last_at_raw else "—"
        rows.append(
            f"<tr>"
            f"{_td_priority(account_name, 1)}"
            f"{_td_priority(provider_id, 1)}"
            f"{_td_priority(f'{selection_count:,}', 1)}"
            f"{_td_priority(last_score, 2)}"
            f"{_td_priority(last_tier, 2)}"
            f"{_td_priority(f'{avg_tier:.2f}', 3)}"
            f"{_td_priority(f'{avg_score:.3f}', 3)}"
            f"{_td_priority(f'{avg_eligible:.2f}', 3)}"
            f"{_td_priority(last_at, 3)}"
            f"</tr>"
        )
    return (
        '<table class="data">'
        + "<thead><tr>"
        # Priority 1 — always shown
        + _th("Account")
        + _th("Provider")
        + _th("Selections")
        # Priority 2 — shown on tablet+
        + _th("Last score", priority=2)
        + _th("Last tier", priority=2)
        # Priority 3 — desktop only
        + _th("Avg tier", priority=3)
        + _th("Avg score", priority=3)
        + _th("Avg eligible", priority=3)
        + _th("Last selected", priority=3)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
    )


def _render_exclusion_table(exclusion_breakdown: list[dict[str, Any]]) -> str:
    """Render the per-(account, reason) exclusion table grouped by category."""
    if not exclusion_breakdown:
        return '<p class="empty">No exclusion data in this period.</p>'
    rows: list[str] = []
    for row in exclusion_breakdown:
        account_name = escape(str(row.get("account_name", "unknown")))
        reason = escape(str(row.get("reason", "")))
        count = int(row.get("exclusion_count", 0) or 0)
        category = _classify_exclusion(str(row.get("reason", "")))
        rows.append(
            f"<tr>"
            f"{_td_priority(escape(category), 1, class_=sanitize_class_name(category))}"
            f"{_td_priority(account_name, 1)}"
            f"{_td_priority(reason, 2)}"
            f"{_td_priority(f'{count:,}', 1)}"
            f"</tr>"
        )
    return (
        '<table class="data">'
        + "<thead><tr>"
        + _th("Category")
        + _th("Account")
        + _th("Count")
        + _th("Reason", priority=2)
        + "</tr></thead><tbody>"
        + f"{''.join(rows)}"
        + "</tbody></table>"
    )


def render_traces(
    *,
    period: str,
    limit: int,
    recent_requests: list[dict[str, Any]],
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
) -> str:
    """Render the recent-request trace table.

    The trace view is auth-gated and never exposes ``error_detail`` or
    ``client_ip``.  It surfaces only what an operator needs to debug
    upstream or routing behaviour without leaking prompt content.
    """
    limit_label = format_int(limit)
    if not recent_requests:
        rows_html = '<p class="empty">No recent requests.</p>'
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            # Priority 1 — always shown
            _th("Time"),
            _th("Account"),
            _th("Model"),
            _th("Status"),
            _th("Latency"),
            # Priority 2 — shown on tablet+
            _th("Provider", priority=2),
            _th("Protocol", priority=2),
            _th("Error class", priority=2),
            _th("In", priority=2),
            _th("Out", priority=2),
            # Priority 3 — desktop only
            _th("Thinking", priority=3),
            _th("ID", priority=3),
            "</tr></thead><tbody>",
        ]
        for row in recent_requests:
            ts = escape(str(row.get("started_at", "")))
            account = escape(str(row.get("account_name", "")))
            provider = escape(str(row.get("provider_id", "")))
            model = escape(str(row.get("model_id", "")))
            protocol = escape(str(row.get("protocol", "")))
            status = escape(str(row.get("status", "")))
            status_code = row.get("status_code")
            status_str = f"{status} ({status_code})" if status_code else status
            error_class = escape(str(row.get("error_class") or "—"))
            in_tok = format_tokens(row.get("input_tokens", 0))
            out_tok = format_tokens(row.get("output_tokens", 0))
            latency_ms = row.get("upstream_latency_ms")
            latency_str = (
                f"{float(latency_ms):.1f} ms"
                if latency_ms is not None and float(latency_ms) > 0
                else "—"
            )
            proxy_id = short_id(str(row.get("proxy_request_id", "") or ""))
            reasoning_tokens = int(row.get("reasoning_tokens") or 0)
            thinking_chars = int(row.get("thinking_characters") or 0)
            thinking_used = reasoning_tokens > 0 or thinking_chars > 0
            thinking_cls = "yes" if thinking_used else "no"
            thinking_display = format_tokens(reasoning_tokens) if thinking_used else "—"
            parts.append(
                f"<tr>"
                f"{_td_priority(ts, 1)}"
                f"{_td_priority(account, 1)}"
                f"{_td_priority(model, 1)}"
                f"{_td_priority(escape(status_str), 1)}"
                f"{_td_priority(latency_str, 1)}"
                f"{_td_priority(provider, 2)}"
                f"{_td_priority(protocol, 2)}"
                f"{_td_priority(error_class, 2)}"
                f"{_td_priority(in_tok, 2)}"
                f"{_td_priority(out_tok, 2)}"
                f"{_td_priority(thinking_display, 3, class_=thinking_cls)}"
                f"{_td_priority(proxy_id, 3)}"
                f"</tr>"
            )
        parts.append("</tbody></table>")
        rows_html = "".join(parts)

    filter_form = f"""
<form method="get" class="filter-form">
  <label>Limit:
    <input type="number" name="limit" value="{escape_attr(limit_label)}"
           min="10" max="500">
  </label>
  <input type="hidden" name="period" value="{escape_attr(period)}">
  <input type="hidden" name="theme" value="{escape_attr(current_theme)}">
  <button type="submit">Apply</button>
</form>
"""

    body = f"""
<h2>Traces</h2>
<p class="sub">
  Auth-gated; does not include error_detail or client_ip;
  for incident debugging only.
</p>
{filter_form}
{_render_period_selector(period, current_theme)}
<section class="panel">
  {rows_html}
</section>
"""
    return _render_layout(
        title="Traces",
        body=body,
        active_nav="traces",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        update_info=update_info,
    )


def render_latency(
    provider_ttft: list[dict[str, Any]],
    model_ttft: list[dict[str, Any]],
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    *,
    phases: dict[str, Any] | None = None,
    update_info: Any | None = None,
) -> str:
    """Render the latency breakdown page.

    When ``phases`` is provided, a phase-decomposition chart is
    rendered alongside the per-provider / per-model TTFT tables, and
    the per-model table gains a ``phases_ms`` column showing the
    connect / read / coordinator overhead breakdown.
    """
    # Provider summary cards
    provider_cards = ""
    if provider_ttft:
        cards: list[str] = []
        for row in provider_ttft:
            pid = escape(str(row.get("provider_id", "")))
            avg = format_latency(row.get("avg_ttft_ms", 0.0))
            p50 = format_latency(row.get("p50_ttft_ms", 0.0))
            p99 = format_latency(row.get("p99_ttft_ms", 0.0))
            count = int(row.get("request_count", 0))
            cards.append(
                _render_metric_card(
                    title=str(row.get("provider_id", "")),
                    metric=avg,
                    sub=f"P50 {p50} · P99 {p99} · {count:,} reqs",
                    tooltip=(
                        "Provider TTFT summary. The metric is average time to "
                        "first token; the subtext shows P50, P99, and request "
                        "count."
                    ),
                )
            )
        provider_cards = f'<section class="cards">{"".join(cards)}</section>'
    else:
        provider_cards = '<p class="empty">No TTFT data for this period.</p>'

    phase_section = _render_latency_phases(phases)

    # Per-provider/model breakdown table
    if model_ttft:
        model_parts = [
            '<table class="data">',
            "<thead><tr>",
            # Priority 1 — always shown
            _th("Provider"),
            _th("Model"),
            _th("Requests"),
            _th("Avg TTFT"),
            # Priority 2 — shown on tablet+
            _th("P50 TTFT", priority=2),
            _th("P99 TTFT", priority=2),
        ]
        if phases:
            model_parts.append(_th("Phases ms (c/r/o)", priority=3))
        model_parts.append("</tr></thead><tbody>")
        for row in model_ttft:
            pid = escape(str(row.get("provider_id", "")))
            mid = escape(str(row.get("model_id", "")))
            avg = format_latency(row.get("avg_ttft_ms", 0.0))
            p50 = format_latency(row.get("p50_ttft_ms", 0.0))
            p99 = format_latency(row.get("p99_ttft_ms", 0.0))
            count = int(row.get("request_count", 0))
            tr = (
                f"<tr>"
                f"{_td_priority(pid, 1)}"
                f"{_td_priority(mid, 1)}"
                f"{_td_priority(f'{count:,}', 1)}"
                f"{_td_priority(avg, 1)}"
                f"{_td_priority(p50, 2)}"
                f"{_td_priority(p99, 2)}"
            )
            if phases:
                tr += _td_priority(_format_phase_cell(row), 3)
            tr += "</tr>"
            model_parts.append(tr)
        model_parts.append("</tbody></table>")
        model_table = (
            '<section class="panel">'
            "<h3>Per-model breakdown</h3>"
            f"{''.join(model_parts)}</section>"
        )
    else:
        model_table = (
            '<section class="panel">'
            "<h3>Per-model breakdown</h3>"
            '<p class="empty">No model data for this period.</p>'
            "</section>"
        )

    body = f"""
<h2>Latency</h2>
{_render_period_selector(period, current_theme)}

{provider_cards}

{phase_section}

{model_table}
"""
    include_chart_js = bool(phase_section)
    return _render_layout(
        title="Latency",
        body=body,
        active_nav="latency",
        period=period,
        theme_css=theme_css,
        available_themes=available_themes,
        current_theme=current_theme,
        include_chart_js=include_chart_js,
        update_info=update_info,
    )


def _render_latency_phases(phases: dict[str, Any] | None) -> str:
    """Render the latency phase decomposition chart."""
    if not phases:
        return ""
    inner: dict[str, Any] = _as_dict(phases.get("phases")) if phases else {}
    if not inner:
        return ""
    connect: dict[str, Any] = _as_dict(inner.get("upstream_connect_ms"))
    read_phase: dict[str, Any] = _as_dict(inner.get("upstream_read_ms"))
    overhead: dict[str, Any] = _as_dict(inner.get("coordinator_overhead_ms"))
    sample_count = (
        int(connect.get("sample_count", 0) or 0)
        + int(read_phase.get("sample_count", 0) or 0)
        + int(overhead.get("sample_count", 0) or 0)
    )
    if sample_count <= 0:
        return ""

    labels = json.dumps(["Connect", "Read", "Coordinator overhead"])
    datasets = json.dumps(
        [
            {
                "label": "avg",
                "data": [
                    float(connect.get("avg_ms", 0.0) or 0.0),
                    float(read_phase.get("avg_ms", 0.0) or 0.0),
                    float(overhead.get("avg_ms", 0.0) or 0.0),
                ],
                "backgroundColor": "rgba(75, 192, 192, 0.7)",
            },
            {
                "label": "p50",
                "data": [
                    float(connect.get("p50_ms", 0.0) or 0.0),
                    float(read_phase.get("p50_ms", 0.0) or 0.0),
                    float(overhead.get("p50_ms", 0.0) or 0.0),
                ],
                "backgroundColor": "rgba(54, 162, 235, 0.7)",
            },
            {
                "label": "p99",
                "data": [
                    float(connect.get("p99_ms", 0.0) or 0.0),
                    float(read_phase.get("p99_ms", 0.0) or 0.0),
                    float(overhead.get("p99_ms", 0.0) or 0.0),
                ],
                "backgroundColor": "rgba(255, 99, 132, 0.7)",
            },
        ]
    )
    options = json.dumps(
        {
            "responsive": True,
            "maintainAspectRatio": False,
            "scales": {
                "y": {"beginAtZero": True, "title": {"display": True, "text": "ms"}}
            },
        }
    )
    chart = _render_chart_canvas(
        "latency-phases",
        "bar",
        labels,
        datasets,
        options,
    )
    return f"""
<section class="panel">
  <h3>Latency phases</h3>
  <p class="sub">
    connect = DNS/TCP/TLS/send; read = TTFB minus connect;
    coordinator overhead = eggpool-side routing/retry/encode.
  </p>
  {chart}
</section>
"""


def _format_phase_cell(row: dict[str, Any]) -> str:
    """Format a model_ttft row's phases into a compact c/r/o string.

    Per-model phase data is not currently aggregated in
    ``fetch_provider_model_ttft``, so this helper reports dashes when
    the row lacks ``phase_connect_ms`` etc. The ``connect/read/overhead``
    column header makes the order obvious.
    """
    connect = row.get("phase_connect_ms")
    read_phase = row.get("phase_read_ms")
    overhead = row.get("phase_overhead_ms")
    if connect is None and read_phase is None and overhead is None:
        return "—"

    def _v(value: Any) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value):.0f}"
        except (TypeError, ValueError):
            return str(value)

    return f"{_v(connect)}/{_v(read_phase)}/{_v(overhead)}"


__all__ = [
    "render_accounts",
    "render_bandwidth",
    "render_events",
    "render_latency",
    "render_model_detail",
    "render_models",
    "render_overview",
    "render_pings",
    "render_reliability",
    "render_routing",
    "render_runtime",
    "render_timeseries",
    "render_traces",
]
