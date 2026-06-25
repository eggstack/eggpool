"""Server-side HTML rendering for the dashboard.

Uses a minimal string-based renderer to keep the dependency footprint
small. All values rendered into HTML are escaped via the `escape` module.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from html import escape as _html_escape
from typing import Any, cast

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
    other config-changing operations restart the server.
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
    chart_script = (
        '<script defer src="/static/chart.js"></script>' if include_chart_js else ""
    )
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
    &middot; <span id="dashboard-updated">ready</span></small>
</footer>
{script_block}
{chart_script}
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
    for key, href, label in items:
        cls = "active" if key == active_nav else ""
        parts.append(
            f'<a class="{cls}" href="{href}?period={_html_escape(period)}'
            f'&amp;theme={_html_escape(current_theme)}">'
            f"{_html_escape(label)}</a>"
        )

    # Theme selector dropdown
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
            'aria-label="Switch dashboard theme">'
            '<select name="theme" onchange="this.form.submit()">'
            f"{options_html}"
            "</select>"
            f'<input type="hidden" name="period" value="{_html_escape(period)}">'
            "</form>"
        )

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


def _render_period_selector(current: str, current_theme: str = "") -> str:
    """Render a period selector form."""
    options = [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    parts = [
        '<form method="get" class="period-selector" '
        'data-tooltip="Select time range" '
        'aria-label="Select time range">',
        '<label>Period: <select name="period" onchange="this.form.submit()">',
    ]
    for value, label in options:
        selected = " selected" if value == current else ""
        parts.append(
            f'<option value="{_html_escape(value)}"{selected}>'
            f"{_html_escape(label)}</option>"
        )
    parts.append("</select></label>")
    if current_theme:
        parts.append(
            f'<input type="hidden" name="theme" value="{_html_escape(current_theme)}">'
        )
    parts.append("</form>")
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
            f"<td>{pid}</td>"
            f'<td class="{status}">{status}</td>'
            f"<td>{avg_lat}</td>"
            f"<td>{success_rate}%</td>"
            f"<td>{model_count}</td>"
            f"<td>{last_at}</td>"
            f"</tr>"
        )
    return (
        '<section class="panel">'
        "<h3>Provider health</h3>"
        '<table class="data">'
        "<thead><tr>"
        "<th>Provider</th>"
        "<th>Status</th>"
        "<th>Avg latency</th>"
        "<th>Success rate</th>"
        "<th>Models</th>"
        "<th>Last ping</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
        "</section>"
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
    finalizer_timeout = 0
    crash_recovery = 0
    for row in summary_rows:
        event_type = str(row.get("event_type", ""))
        event_count = int(row.get("event_count", 0) or 0)
        if event_type == "stale_request_finalizer":
            stale_finalizer_cleaned += event_count
        elif event_type == "stale_request_cancel_timeout":
            finalizer_timeout += event_count
        elif event_type == "crash_recovery":
            crash_recovery += event_count

    has_data = bool(pending_health) or bool(attempt_stats) or bool(operational_summary)
    if not has_data:
        return ""

    pending_warn = pending_count > 0 and stale_pending_count > 0
    return f"""
<section class="cards system-health">
  <div class="card{" warning" if pending_warn else ""}">
    <h3>Pending requests</h3>
    <p class="metric">{pending_count:,}</p>
    <p class="sub">oldest {pending_age} · stale {stale_pending_count}</p>
  </div>
  <div class="card">
    <h3>Active reservations</h3>
    <p class="metric">{reservation_count:,}</p>
    <p class="sub">reserved {reserved_cost}</p>
  </div>
  <div class="card{" warning" if finalizer_timeout > 0 else ""}">
    <h3>Finalizer (24h)</h3>
    <p class="metric">{format_int(stale_finalizer_cleaned)}</p>
    <p class="sub">cleaned · {finalizer_timeout} timeout · {crash_recovery} recovery</p>
  </div>
  <div class="card">
    <h3>Retry rate</h3>
    <p class="metric">{format_percent(retry_rate, digits=1)}</p>
    <p class="sub">of {format_int(total_attempts)} attempts</p>
  </div>
  <div class="card">
    <h3>First-attempt success</h3>
    <p class="metric">{format_percent(first_attempt_success_rate, digits=1)}</p>
    <p class="sub">no retry needed</p>
  </div>
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
            f"<td>{ip}</td>"
            f"<td>{req_count:,}</td>"
            f"<td>{in_tok}</td>"
            f"<td>{out_tok}</td>"
            f"<td>{total_tok}</td>"
            f"<td>{cost}</td>"
            f"<td>{avg_lat}</td>"
            f"<td>{error_count:,}</td>"
            f"<td>{unique_models}</td>"
            f"</tr>"
        )
    return (
        '<section class="panel">'
        "<h3>Request breakdown by IP</h3>"
        '<table class="data compact">'
        "<thead><tr>"
        "<th>IP Address</th>"
        "<th>Requests</th>"
        "<th>Input tokens</th>"
        "<th>Output tokens</th>"
        "<th>Total tokens</th>"
        "<th>Cost</th>"
        "<th>Avg latency</th>"
        "<th>Errors</th>"
        "<th>Models</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
        "</section>"
    )


def _render_timeseries_chart(
    period: str = "24h", initial_data: list[dict[str, Any]] | None = None
) -> str:
    """Render an interactive timeseries chart using Chart.js.

    When ``initial_data`` is provided, the chart renders immediately with
    the data already inlined; the script then re-fetches every 60s to
    pick up new buckets. When ``initial_data`` is ``None`` (or empty)
    the chart fetches its data on load.
    """
    initial_json = json.dumps(initial_data or [])
    return f"""
<section class="panel">
  <h3>Request timeseries</h3>
  <div style="height: 300px; position: relative;">
    <canvas id="timeseries-chart"></canvas>
  </div>
</section>
<script>
(() => {{
  const period = {json.dumps(period)};
  const initialData = {initial_json};
  const ctx = document.getElementById('timeseries-chart');
  if (!ctx) return;

  const labels0 = initialData.map(d => d.bucket);
  const requests0 = initialData.map(d => d.request_count || 0);
  const errors0 = initialData.map(d => d.error_count || 0);

  const chart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: labels0,
      datasets: [
        {{
          label: 'Requests',
          data: requests0,
          borderColor: 'rgb(75, 192, 192)',
          tension: 0.1
        }},
        {{
          label: 'Errors',
          data: errors0,
          borderColor: 'rgb(255, 99, 132)',
          tension: 0.1
        }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      scales: {{
        x: {{
          title: {{
            display: true,
            text: 'Time'
          }}
        }},
        y: {{
          title: {{
            display: true,
            text: 'Count'
          }},
          beginAtZero: true
        }}
      }}
    }}
  }});

  async function loadData() {{
    try {{
      const response = await fetch('/api/timeseries?period=' + period);
      if (!response.ok) return;
      const data = await response.json();

      chart.data.labels = data.map(d => d.bucket);
      chart.data.datasets[0].data = data.map(d => d.request_count || 0);
      chart.data.datasets[1].data = data.map(d => d.error_count || 0);
      chart.update();
    }} catch (err) {{
      console.error('Failed to load timeseries data:', err);
    }}
  }}

  window.setInterval(loadData, 60000);
}})();
</script>
"""


def _render_model_glance(models: list[dict[str, Any]]) -> str:
    """Render a compact top-model table for the overview page."""
    if not models:
        return '<p class="empty">No model activity in this period.</p>'
    rows: list[str] = []
    for row in models[:10]:
        total_tok = format_tokens(row.get("total_tokens", 0))
        rows.append(
            f"<tr>"
            f"<td>{escape(row.get('model_id', ''))}</td>"
            f"<td>{escape(row.get('provider_id', ''))}</td>"
            f"<td>{int(row.get('request_count', 0)):,}</td>"
            f"<td>{int(row.get('error_count', 0)):,}</td>"
            f"<td>{total_tok}</td>"
            f"<td>{format_microdollars(row.get('cost_microdollars', 0))}</td>"
            f"<td>{format_latency(row.get('avg_latency_ms', 0.0))}</td>"
            f"</tr>"
        )
    return (
        '<table class="data compact">'
        "<thead><tr>"
        "<th>Model</th>"
        "<th>Provider</th>"
        "<th>Reqs</th>"
        "<th>Errs</th>"
        "<th>Total tokens</th>"
        "<th>Cost</th>"
        "<th>Latency</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
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
        rows.append(
            f"<tr>"
            f"<td>{format_timestamp(row.get('created_at', ''))}</td>"
            f"<td>{escape(row.get('account_name', ''))}</td>"
            f'<td><span class="event-tag {sanitize_class_name(event_type)}"'
            f"{badge_attrs}>"
            f"{escape(event_type)}</span></td>"
            f"<td>{truncate(row.get('details', ''), 120)}</td>"
            f"</tr>"
        )
    return (
        '<table class="data compact">'
        "<thead><tr>"
        "<th>When</th>"
        "<th>Account</th>"
        "<th>Type</th>"
        "<th>Details</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
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

    # Day cells
    hitboxes: list[str] = []
    for week in range(num_weeks):
        for day_of_week in range(7):
            cell_date = grid_start + timedelta(weeks=week, days=day_of_week)
            if cell_date < start_date or cell_date > today:
                continue
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
                    f"{pretty_date}\n{formatter(value)} tokens · {request_count_text}"
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

    svg = (
        f'<svg width="{svg_width}" height="{svg_height}" '
        f'viewBox="0 0 {svg_width} {svg_height}" '
        f'role="img" aria-label="{_html_escape(title)}">'
        f"{''.join(cells)}</svg>"
    )
    overlay = (
        f'<div class="heatmap-overlay" aria-hidden="true">{"".join(hitboxes)}</div>'
    )

    return f'<div class="heatmap">{svg}{overlay}</div>'


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
) -> str:
    """Render the overview dashboard page."""
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
    reasoning = format_tokens(summary.get("total_reasoning_tokens", 0))
    streamed = int(summary.get("streamed_requests", 0))
    non_streamed = int(summary.get("non_streamed_requests", 0))
    exact = int(summary.get("exact_count", 0))
    derived = int(summary.get("derived_count", 0))
    estimated = int(summary.get("estimated_count", 0))
    unknown_exc = int(summary.get("unknown_count", 0))
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
  <div class="card">
    <h3>Requests</h3>
    <p class="metric">{total:,}</p>
    <p class="sub">Success {success:,} · Errors {errors:,}</p>
  </div>
  <div class="card">
    <h3>Error rate</h3>
    <p class="metric">{format_percent(error_rate)}</p>
    <p class="sub">avg latency {latency}</p>
  </div>
  <div class="card">
    <h3>Total cost</h3>
    <p class="metric">{cost}</p>
    <p class="sub">in {in_tok} · out {out_tok} · total {total_tok}</p>
  </div>
  <div class="card">
    <h3>Utilization imbalance</h3>
    <p class="metric">{imb_pct}</p>
    <p class="sub">CV across active accounts</p>
  </div>
</section>

<section class="cards">
  <div class="card">
    <h3>Cache tokens</h3>
    <p class="metric">{cache_read}</p>
    <p class="sub">read · write {cache_write}</p>
  </div>
  <div class="card">
    <h3>Reasoning tokens</h3>
    <p class="metric">{reasoning}</p>
    <p class="sub">extended thinking</p>
  </div>
  <div class="card">
    <h3>Throughput</h3>
    <p class="metric">{throughput}</p>
    <p class="sub">aggregate Σtokens / Σlatency</p>
  </div>
  <div class="card">
    <h3>Streaming</h3>
    <p class="metric">{streamed:,}</p>
    <p class="sub">streamed · {non_streamed:,} non-streamed</p>
  </div>
  <div class="card">
    <h3>Exactness</h3>
    <p class="metric">{exact:,}</p>
    <p class="sub">exact · {derived:,} derived
     · {estimated:,} est · {unknown_exc:,} unk</p>
  </div>
</section>

<section class="cards">
  <div class="card">
    <h3>Bandwidth received</h3>
    <p class="metric">{bytes_in}</p>
    <p class="sub">client → proxy</p>
  </div>
  <div class="card">
    <h3>Bandwidth emitted</h3>
    <p class="metric">{bytes_out}</p>
    <p class="sub">upstream → proxy</p>
  </div>
  <div class="card">
    <h3>Avg TTFT (streamed)</h3>
    <p class="metric">{avg_ttft}</p>
    <p class="sub">P50 {p50_ttft} · P99 {p99_ttft}</p>
  </div>
</section>

<section class="panel">
  <h3>Account breakdown</h3>
  {_render_account_table(accounts)}
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
    )


def _render_account_table(accounts: list[dict[str, Any]]) -> str:
    """Render the account breakdown table."""
    if not accounts:
        return '<p class="empty">No accounts configured.</p>'
    parts = [
        '<table class="data">',
        "<thead><tr>",
        "<th>Account</th>",
        "<th>Provider</th>",
        "<th>Enabled</th>",
        "<th>Health</th>",
        "<th>Requests</th>",
        "<th>Errors</th>",
        "<th>Input tokens</th>",
        "<th>Output tokens</th>",
        "<th>Total tokens</th>",
        "<th>Cost</th>",
        "<th>Avg latency</th>",
        "<th>TPS</th>",
        "<th>Reserved</th>",
        "<th>Resv.</th>",
        "<th>5h rate</th>",
        "<th>7d rate</th>",
        "<th>30d rate</th>",
        "<th>BW received</th>",
        "<th>BW emitted</th>",
        "<th>Over budget</th>",
        "<th>Upstream backoff</th>",
        "<th>Backoff until</th>",
        "<th>Failures</th>",
        "<th>Auth fail</th>",
        "<th>Disabled</th>",
        "<th>Exactness</th>",
        "<th>Est. cost</th>",
        "<th>Cache R</th>",
        "<th>Cache W</th>",
        "<th>Reasoning</th>",
        "<th>Avg cost/req</th>",
        "<th>Avg cost/1k tok</th>",
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
        estimated = int(row.get("estimated_count", 0) or 0)
        unknown_exc = int(row.get("unknown_count", 0) or 0)
        exactness = f"{exact:,}/{derived:,}/{estimated:,}/{unknown_exc:,}"
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
            avg_cost_per_1k = format_microdollars(avg_cost_per_1k_microdollars * 1000)
        parts.append(
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{provider}</td>"
            f'<td class="{"yes" if enabled else "no"}">'
            f"{'yes' if enabled else 'no'}</td>"
            f'<td class="{sanitize_class_name(health)}">{escape(health)}</td>'
            f"<td>{int(row.get('request_count', 0)):,}</td>"
            f"<td>{int(row.get('error_count', 0)):,}</td>"
            f"<td>{in_tok}</td>"
            f"<td>{out_tok}</td>"
            f"<td>{total_tok}</td>"
            f"<td>{cost}</td>"
            f"<td>{latency}</td>"
            f"<td>{tps}</td>"
            f"<td>{reserved}</td>"
            f"<td>{active_resv}</td>"
            f"<td>{util_5h}</td>"
            f"<td>{util_7d}</td>"
            f"<td>{util_30d}</td>"
            f"<td>{format_bytes(row.get('bytes_received', 0))}</td>"
            f"<td>{format_bytes(row.get('bytes_emitted', 0))}</td>"
            f'<td class="{"yes" if over_budget else "no"}">'
            f"{'yes' if over_budget else 'no'}</td>"
            f"<td>{backoff_reason}</td>"
            f"<td>{backoff_until}</td>"
            f"<td>{consecutive_failures}</td>"
            f'<td class="{"yes" if auth_failed else "no"}">'
            f"{'yes' if auth_failed else 'no'}</td>"
            f'<td class="{"yes" if operator_disabled else "no"}">'
            f"{'yes' if operator_disabled else 'no'}</td>"
            f"<td>{exactness}</td>"
            f"<td>{est_cost_pct}</td>"
            f"<td>{cache_read_str}</td>"
            f"<td>{cache_write_str}</td>"
            f"<td>{reasoning_str}</td>"
            f"<td>{avg_cost_per_req}</td>"
            f"<td>{avg_cost_per_1k}</td>"
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
) -> str:
    """Render the accounts page."""
    body = f"""
<h2>Accounts</h2>
{_render_period_selector(period, current_theme)}
<section class="panel">
  {_render_account_table(accounts)}
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
    )


def render_models(
    models: list[dict[str, Any]],
    account_filter: str = "",
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
) -> str:
    """Render the models page."""
    if not models:
        rows_html = '<p class="empty">No model data for this period.</p>'
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            "<th>Model</th>",
            "<th>Provider</th>",
            "<th>Requests</th>",
            "<th>Errors</th>",
            "<th>Input tokens</th>",
            "<th>Output tokens</th>",
            "<th>Total tokens</th>",
            "<th>Cost</th>",
            "<th>Avg latency</th>",
            "<th>Avg TTFT</th>",
            "<th>TPS</th>",
            "<th>Exactness</th>",
            "<th>Est. cost</th>",
            "<th>Cache R</th>",
            "<th>Cache W</th>",
            "<th>Reasoning</th>",
            "<th>Avg cost/req</th>",
            "<th>Avg cost/1k tok</th>",
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
            exact = int(row.get("exact_count", 0) or 0)
            derived = int(row.get("derived_count", 0) or 0)
            estimated = int(row.get("estimated_count", 0) or 0)
            unknown_exc = int(row.get("unknown_count", 0) or 0)
            exactness = f"{exact:,}/{derived:,}/{estimated:,}/{unknown_exc:,}"
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
                avg_cost_per_1k = format_microdollars(
                    avg_cost_per_1k_microdollars * 1000
                )
            parts.append(
                f"<tr>"
                f"<td>{escape(row.get('model_id', ''))}</td>"
                f"<td>{provider}</td>"
                f"<td>{int(row.get('request_count', 0)):,}</td>"
                f"<td>{int(row.get('error_count', 0)):,}</td>"
                f"<td>{in_tok}</td>"
                f"<td>{out_tok}</td>"
                f"<td>{total_tok}</td>"
                f"<td>{cost}</td>"
                f"<td>{latency}</td>"
                f"<td>{ttft}</td>"
                f"<td>{tps}</td>"
                f"<td>{exactness}</td>"
                f"<td>{est_cost_pct}</td>"
                f"<td>{cache_read_str}</td>"
                f"<td>{cache_write_str}</td>"
                f"<td>{reasoning_str}</td>"
                f"<td>{avg_cost_per_req}</td>"
                f"<td>{avg_cost_per_1k}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")
        rows_html = "".join(parts)

    filter_form = f"""
<form method="get" class="filter-form">
  <label>Account:
    <input type="text" name="account" value="{escape_attr(account_filter)}"
           placeholder="(all)">
  </label>
  <input type="hidden" name="period" value="{escape_attr(period)}">
  <input type="hidden" name="theme" value="{escape_attr(current_theme)}">
  <button type="submit">Apply</button>
</form>
"""

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
    )


def render_events(
    events: list[dict[str, Any]],
    event_type: str = "",
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
) -> str:
    """Render the events page."""
    if not events:
        rows_html = '<p class="empty">No events recorded.</p>'
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            "<th>When</th>",
            "<th>Account</th>",
            "<th>Type</th>",
            "<th>Details</th>",
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
            parts.append(
                f"<tr>"
                f"<td>{ts}</td>"
                f"<td>{name}</td>"
                f'<td><span class="event-tag {cls}"{badge_attrs}>'
                f"{etype}</span></td>"
                f"<td>{details}</td>"
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
    )


def render_timeseries(
    series: list[dict[str, Any]],
    bucket: str,
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
) -> str:
    """Render the timeseries page."""
    if not series:
        rows_html = '<p class="empty">No requests in this window.</p>'
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            "<th>Bucket</th>",
            "<th>Requests</th>",
            "<th>Errors</th>",
            "<th>Input tokens</th>",
            "<th>Output tokens</th>",
            "<th>Total tokens</th>",
            "<th>Cost</th>",
            "<th>BW received</th>",
            "<th>BW emitted</th>",
            "</tr></thead><tbody>",
        ]
        for row in series:
            cost = format_microdollars(row.get("cost_microdollars", 0))
            in_tok = format_tokens(row.get("input_tokens", 0))
            out_tok = format_tokens(row.get("output_tokens", 0))
            total_tok = format_tokens(row.get("total_tokens", 0))
            parts.append(
                f"<tr>"
                f"<td>{escape(row.get('bucket', ''))}</td>"
                f"<td>{int(row.get('request_count', 0)):,}</td>"
                f"<td>{int(row.get('error_count', 0)):,}</td>"
                f"<td>{in_tok}</td>"
                f"<td>{out_tok}</td>"
                f"<td>{total_tok}</td>"
                f"<td>{cost}</td>"
                f"<td>{format_bytes(row.get('bytes_received', 0))}</td>"
                f"<td>{format_bytes(row.get('bytes_emitted', 0))}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")
        rows_html = "".join(parts)

    body = f"""
<h2>Timeseries ({escape(bucket)} buckets)</h2>
{_render_period_selector(period, current_theme)}
<section class="panel">
  {rows_html}
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
        "<th>Bucket</th>",
        "<th>Requests</th>",
        "<th>BW received</th>",
        "<th>BW emitted</th>",
        "</tr></thead><tbody>",
    ]
    for row in series:
        parts.append(
            f"<tr>"
            f"<td>{escape(row.get('bucket', row.get('day', '')))}</td>"
            f"<td>{int(row.get('request_count', 0)):,}</td>"
            f"<td>{format_bytes(row.get('bytes_received', 0))}</td>"
            f"<td>{format_bytes(row.get('bytes_emitted', 0))}</td>"
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
  <div class="card">
    <h3>Total received</h3>
    <p class="metric">{bytes_in}</p>
    <p class="sub">client → proxy</p>
  </div>
  <div class="card">
    <h3>Total emitted</h3>
    <p class="metric">{bytes_out}</p>
    <p class="sub">upstream → proxy</p>
  </div>
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
    )


def render_pings(
    ping_summary: list[dict[str, Any]],
    recent_pings: list[dict[str, Any]],
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
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
                f'<div class="card">'
                f"<h3>{pid}</h3>"
                f'<p class="metric">{avg_lat}</p>'
                f'<p class="sub">'
                f'<span class="{status}">{status}</span>'
                f" · {success_rate}% success"
                f" · {model_count} models"
                f"</p>"
                f"</div>"
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
            "<th>Provider</th>",
            "<th>Account</th>",
            "<th>Time</th>",
            "<th>Latency</th>",
            "<th>Status</th>",
            "<th>Models</th>",
            "<th>Error</th>",
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
                f"<td>{pid}</td>"
                f"<td>{acct}</td>"
                f"<td>{ts}</td>"
                f"<td>{lat}</td>"
                f"<td>{status_str}</td>"
                f"<td>{model_count}</td>"
                f"<td>{error}</td>"
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
    )


def render_runtime(
    snapshot: dict[str, Any],
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
) -> str:
    """Render the runtime metrics page."""
    server = _as_dict(snapshot.get("server"))
    memory = _as_dict(snapshot.get("memory"))
    processes = _as_dict(snapshot.get("processes"))
    background_tasks: list[dict[str, Any]] = snapshot.get("background_tasks") or []
    db = _as_dict(snapshot.get("db"))
    routing = _as_dict(snapshot.get("routing_runtime"))
    probe_errors: list[str] = snapshot.get("probe_errors") or []

    # Server section
    pid = server.get("pid", "—")
    uptime_s = server.get("uptime_seconds")
    uptime = format_age_seconds(uptime_s)
    threads = server.get("configured_server_threads", "—")
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

    # Database
    db_path = escape(str(db.get("path") or ":memory:"))
    db_file_size = format_bytes(db.get("file_size_bytes"))
    db_wal_size = format_bytes(db.get("wal_size_bytes"))
    db_wal_enabled = db.get("wal_enabled", False)
    db_wal_live = db.get("wal_mode_live", "")
    db_sync = escape(str(db.get("synchronous_live") or "—"))
    db_primary_connected = db.get("primary_connected")
    db_separate_stats = db.get("stats_connection_separate", False)

    # Routing / in-flight
    pending_count = routing.get("pending_count")
    oldest_pending_age = format_age_seconds(routing.get("oldest_pending_age_seconds"))
    active_reservations = routing.get("active_reservations_count")
    reserved_microdollars = format_microdollars(routing.get("reserved_microdollars", 0))
    active_requests = routing.get("active_requests_total")
    active_backoff_count = routing.get("active_backoff_count")
    health_states: dict[str, str] = routing.get("health_states_by_account") or {}

    # Process count warning card
    process_warn_class = "warning" if process_warning else ""
    process_count_display = (
        format_int(process_count) if process_count is not None else "—"
    )
    expected_display = format_int(expected_count) if expected_count is not None else "—"

    # Server info cards
    server_cards = f"""
<section class="cards">
  <div class="card">
    <h3>Server PID</h3>
    <p class="metric">{pid}</p>
    <p class="sub">PPID {ppid} · daemon {daemon_label}</p>
  </div>
  <div class="card">
    <h3>Uptime</h3>
    <p class="metric">{uptime}</p>
    <p class="sub">uptime since start</p>
  </div>
  <div class="card">
    <h3>Threads</h3>
    <p class="metric">{threads}</p>
    <p class="sub">configured server threads</p>
  </div>
  <div class="card">
    <h3>Python</h3>
    <p class="metric">{python_ver}</p>
    <p class="sub">{platform_str}</p>
  </div>
</section>
"""

    # Process & memory cards
    memory_cards = f"""
<section class="cards">
  <div class="card{process_warn_class}">
    <h3>Processes</h3>
    <p class="metric">{process_count_display}</p>
    <p class="sub">expected {expected_display}</p>
  </div>
  <div class="card">
    <h3>RSS memory</h3>
    <p class="metric">{rss}</p>
    <p class="sub">resident set size</p>
  </div>
  <div class="card">
    <h3>Open FDs</h3>
    <p class="metric">{open_fds}</p>
    <p class="sub">file descriptors</p>
  </div>
  <div class="card">
    <h3>Threads (active)</h3>
    <p class="metric">{thread_count}</p>
    <p class="sub">threading.active_count()</p>
  </div>
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
                f"<td>{name}</td>"
                f'<td class="{status_cls}">{status}</td>'
                f"<td>{restarts}</td>"
                f"<td>{max_str}</td>"
                f"<td>{'yes' if done else 'no'}</td>"
                f"</tr>"
            )
        tasks_table = (
            '<table class="data compact">'
            "<thead><tr>"
            "<th>Task</th>"
            "<th>Status</th>"
            "<th>Restarts</th>"
            "<th>Max restarts</th>"
            "<th>Done</th>"
            "</tr></thead><tbody>"
            f"{''.join(task_rows)}"
            "</tbody></table>"
        )
    else:
        tasks_table = '<p class="empty">No background tasks registered.</p>'

    # Database info cards
    db_cards = f"""
<section class="cards">
  <div class="card">
    <h3>Database</h3>
    <p class="metric">{db_path}</p>
    <p class="sub">file size {db_file_size}</p>
  </div>
  <div class="card">
    <h3>WAL</h3>
    <p class="metric">{db_wal_size}</p>
    <p class="sub">enabled {escape(str(db_wal_enabled))} · mode {db_wal_live}</p>
  </div>
  <div class="card">
    <h3>Sync</h3>
    <p class="metric">{db_sync}</p>
    <p class="sub">connected {escape(str(db_primary_connected))}</p>
  </div>
  <div class="card">
    <h3>Stats DB</h3>
    <p class="metric">{"separate" if db_separate_stats else "shared"}</p>
    <p class="sub">stats connection</p>
  </div>
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
  <div class="card">
    <h3>Pending requests</h3>
    <p class="metric">{pending_count_str}</p>
    <p class="sub">oldest {oldest_pending_age}</p>
  </div>
  <div class="card">
    <h3>Active reservations</h3>
    <p class="metric">{active_res_str}</p>
    <p class="sub">reserved {reserved_microdollars}</p>
  </div>
  <div class="card">
    <h3>In-flight requests</h3>
    <p class="metric">{active_req_str}</p>
    <p class="sub">active upstream</p>
  </div>
  <div class="card">
    <h3>Active backoffs</h3>
    <p class="metric">{backoff_str}</p>
    <p class="sub">account backoff rows</p>
  </div>
</section>
"""

    # Health states table
    if health_states:
        health_rows: list[str] = []
        for acct, state in sorted(health_states.items()):
            health_rows.append(
                f"<tr>"
                f"<td>{escape(acct)}</td>"
                f'<td class="{sanitize_class_name(state)}">{escape(state)}</td>'
                f"</tr>"
            )
        health_table = (
            '<table class="data compact">'
            "<thead><tr>"
            "<th>Account</th>"
            "<th>Health state</th>"
            "</tr></thead><tbody>"
            f"{''.join(health_rows)}"
            "</tbody></table>"
        )
    else:
        health_table = '<p class="empty">No health state data.</p>'

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

    body = f"""
<h2>Runtime</h2>
<p class="sub">Process-level diagnostics for the running EggPool instance.</p>

{server_cards}

{memory_cards}

<section class="panel">
  <h3>Background tasks</h3>
  {tasks_table}
</section>

{db_cards}

{routing_cards}

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
    """Render a Chart.js canvas with an inline initialisation script.

    ``include_chart_js`` mirrors the page-level helper flag; the helper
    always emits the inline script (so the chart renders on first
    paint), but the caller still decides whether the page's layout
    pulls in the Chart.js library itself.
    """
    del include_chart_js
    canvas_id_json = json.dumps(canvas_id)
    chart_type_json = json.dumps(chart_type)
    return f"""
<div style="height: {height_px}px; position: relative;">
  <canvas id="{canvas_id}"></canvas>
</div>
<script>
(() => {{
  const ctx = document.getElementById({canvas_id_json});
  if (!ctx) return;
  new Chart(ctx, {{
    type: {chart_type_json},
    data: {{
      labels: {labels_json},
      datasets: {datasets_json},
    }},
    options: {options_json}
  }});
}})();
</script>
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
  <div class="card">
    <h3>Total attempts</h3>
    <p class="metric">{_format_int(total_attempts)}</p>
    <p class="sub">{period}</p>
  </div>
  <div class="card">
    <h3>Success attempts</h3>
    <p class="metric">{_format_int(success_attempts)}</p>
    <p class="sub">first-attempt success rate {first_attempt_pct}</p>
  </div>
  <div class="card">
    <h3>Retry attempts</h3>
    <p class="metric">{_format_int(retry_attempts)}</p>
    <p class="sub">retry rate {_format_percent_unit(retry_rate, digits=1)}</p>
  </div>
  <div class="card">
    <h3>Failed attempts</h3>
    <p class="metric">{_format_int(failed_attempts)}</p>
    <p class="sub">avg attempt latency {avg_attempt_latency:.1f} ms</p>
  </div>
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
  <div class="card{" warning" if pending_warn else ""}">
    <h3>Pending requests</h3>
    <p class="metric">{pending_count:,}</p>
    <p class="sub">oldest {oldest_pending_age} · stale {stale_pending}</p>
  </div>
  <div class="card">
    <h3>Active reservations</h3>
    <p class="metric">{active_reservation_count:,}</p>
    <p class="sub">reserved {active_reserved} · oldest {oldest_reservation_age}</p>
  </div>
  <div class="card">
    <h3>Pending window</h3>
    <p class="sub">stale &gt; 15 minutes are flagged for cleanup</p>
    <p class="sub">snapshot is instantaneous; reload to refresh</p>
  </div>
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
            f"<td>{escape(_error_category_label(category))}</td>"
            f"<td>{attempt_count:,}</td>"
            f"<td>{retry_outcome_count:,}</td>"
            f"<td>{success_count:,}</td>"
            f"<td>{failure_count:,}</td>"
            f"<td>{avg_lat:.1f} ms</td>"
            f"</tr>"
        )
    return (
        '<table class="data">'
        "<thead><tr>"
        "<th>Category</th>"
        "<th>Attempts</th>"
        "<th>Retry outcomes</th>"
        "<th>Successes</th>"
        "<th>Failures</th>"
        "<th>Avg attempt latency</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
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
            f"<td>{escape(event_type)}</td>"
            f"<td>{event_count:,}</td>"
            f"<td>{escape(last_at)}</td>"
            f"<td>{interrupted:,}</td>"
            f"<td>{released:,}</td>"
            f"</tr>"
        )
    summary_table = (
        '<table class="data compact">'
        "<thead><tr>"
        "<th>Event type</th>"
        "<th>Count</th>"
        "<th>Last seen</th>"
        "<th>Interrupted</th>"
        "<th>Released</th>"
        "</tr></thead><tbody>"
        f"{''.join(summary_rows)}"
        "</tbody></table>"
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
                f"<td>{escape(str(row.get('occurred_at', '')))}</td>"
                f"<td>{escape(event_type)}</td>"
                f"<td>{truncated_details}</td>"
                f"</tr>"
            )
        recent_table = (
            '<table class="data compact">'
            "<thead><tr>"
            "<th>When</th>"
            "<th>Type</th>"
            "<th>Details</th>"
            "</tr></thead><tbody>"
            f"{''.join(recent_rows)}"
            "</tbody></table>"
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
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
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

    summary_cards = f"""
<section class="cards">
  <div class="card">
    <h3>Routing decisions</h3>
    <p class="metric">{_format_int(total_decisions)}</p>
    <p class="sub">in selected period</p>
  </div>
  <div class="card">
    <h3>Avg eligible / decision</h3>
    <p class="metric">{avg_eligible:.2f}</p>
    <p class="sub">candidate accounts per decision</p>
  </div>
  <div class="card">
    <h3>Distinct selected accounts</h3>
    <p class="metric">{_format_int(distinct_accounts)}</p>
    <p class="sub">across all (model, provider) groups</p>
  </div>
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
            f"<td>{model_id}</td>"
            f"<td>{provider_id}</td>"
            f"<td>{decision_count:,}</td>"
            f"<td>{avg_eligible:.2f}</td>"
            f"<td>{avg_scored:.2f}</td>"
            f"<td>{avg_excluded:.2f}</td>"
            f"<td>{avg_selected_score:.3f}</td>"
            f"<td>{distinct_accounts}</td>"
            f"</tr>"
        )
    return (
        '<table class="data">'
        "<thead><tr>"
        "<th>Model</th>"
        "<th>Provider</th>"
        "<th>Decisions</th>"
        "<th>Avg eligible</th>"
        "<th>Avg scored</th>"
        "<th>Avg excluded</th>"
        "<th>Avg score</th>"
        "<th>Distinct accounts</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
    )


def _render_selection_breakdown_table(
    selection_breakdown: list[dict[str, Any]],
) -> str:
    """Render the account-level selection counts."""
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
        rows.append(
            f"<tr>"
            f"<td>{account_name}</td>"
            f"<td>{provider_id}</td>"
            f"<td>{selection_count:,}</td>"
            f"<td>{avg_tier:.2f}</td>"
            f"<td>{avg_score:.3f}</td>"
            f"<td>{avg_eligible:.2f}</td>"
            f"</tr>"
        )
    return (
        '<table class="data">'
        "<thead><tr>"
        "<th>Account</th>"
        "<th>Provider</th>"
        "<th>Selections</th>"
        "<th>Avg tier</th>"
        "<th>Avg score</th>"
        "<th>Avg eligible</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
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
            f'<td class="{sanitize_class_name(category)}">{escape(category)}</td>'
            f"<td>{account_name}</td>"
            f"<td>{reason}</td>"
            f"<td>{count:,}</td>"
            f"</tr>"
        )
    return (
        '<table class="data">'
        "<thead><tr>"
        "<th>Category</th>"
        "<th>Account</th>"
        "<th>Reason</th>"
        "<th>Count</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
    )


def render_traces(
    *,
    period: str,
    limit: int,
    recent_requests: list[dict[str, Any]],
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
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
            "<th>Time</th>",
            "<th>Account</th>",
            "<th>Provider</th>",
            "<th>Model</th>",
            "<th>Protocol</th>",
            "<th>Status</th>",
            "<th>Error class</th>",
            "<th>In</th>",
            "<th>Out</th>",
            "<th>Latency</th>",
            "<th>ID</th>",
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
            parts.append(
                f"<tr>"
                f"<td>{ts}</td>"
                f"<td>{account}</td>"
                f"<td>{provider}</td>"
                f"<td>{model}</td>"
                f"<td>{protocol}</td>"
                f"<td>{escape(status_str)}</td>"
                f"<td>{error_class}</td>"
                f"<td>{in_tok}</td>"
                f"<td>{out_tok}</td>"
                f"<td>{latency_str}</td>"
                f"<td>{proxy_id}</td>"
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
                f'<div class="card">'
                f"<h3>{pid}</h3>"
                f'<p class="metric">{avg}</p>'
                f'<p class="sub">P50 {p50} · P99 {p99} · {count:,} reqs</p>'
                f"</div>"
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
            "<th>Provider</th>",
            "<th>Model</th>",
            "<th>Requests</th>",
            "<th>Avg TTFT</th>",
            "<th>P50 TTFT</th>",
            "<th>P99 TTFT</th>",
        ]
        if phases:
            model_parts.append("<th>Phases ms (c/r/o)</th>")
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
                f"<td>{pid}</td>"
                f"<td>{mid}</td>"
                f"<td>{count:,}</td>"
                f"<td>{avg}</td>"
                f"<td>{p50}</td>"
                f"<td>{p99}</td>"
            )
            if phases:
                tr += f"<td>{_format_phase_cell(row)}</td>"
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
    "render_models",
    "render_overview",
    "render_pings",
    "render_reliability",
    "render_routing",
    "render_runtime",
    "render_timeseries",
    "render_traces",
]
