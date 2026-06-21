"""Server-side HTML rendering for the dashboard.

Uses a minimal string-based renderer to keep the dependency footprint
small. All values rendered into HTML are escaped via the `escape` module.
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape as _html_escape
from typing import Any

from eggpool.dashboard.escape import (
    escape,
    escape_attr,
    format_bytes,
    format_latency,
    format_microdollars,
    format_percent,
    format_timestamp,
    format_tokens,
    sanitize_class_name,
    truncate,
)
from eggpool.dashboard.theme import (
    DashboardTheme,
    get_default_theme,
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


def get_theme_css(theme_name: str, themes_dir: str | None = None) -> str:
    """Load a theme by name and return the CSS :root block, or empty string.

    When themes_dir is set, user-provided themes take precedence over
    bundled themes with the same name.
    """
    if theme_name == "default":
        return ""
    try:
        theme_path = resolve_theme_path(theme_name, themes_dir)
        if theme_path is None:
            return ""
        theme = load_theme(theme_path)
        return theme.to_css_variables()
    except Exception:
        return ""


def get_theme(theme_name: str, themes_dir: str | None = None) -> DashboardTheme:
    """Load a theme by name, returning the default on failure.

    When themes_dir is set, user-provided themes take precedence over
    bundled themes with the same name.
    """
    if theme_name == "default":
        return get_default_theme()
    try:
        theme_path = resolve_theme_path(theme_name, themes_dir)
        if theme_path is None:
            return get_default_theme()
        return load_theme(theme_path)
    except Exception:
        return get_default_theme()


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
) -> str:
    """Wrap a page body in the standard layout."""
    nav = _render_nav(active_nav, period, available_themes, current_theme)
    theme_href = f"/static/theme.css?theme={_html_escape(current_theme)}"
    theme_link = f'<link rel="stylesheet" href="{theme_href}">' if current_theme else ""
    script_block = (
        _render_auto_refresh_script(refresh_interval_s) if auto_refresh else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)}</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="stylesheet" href="/static/dashboard.css">
<script src="/static/chart.js"></script>
{theme_link}
</head>
<body>
<svg class="egg-background" viewBox="0 0 256 256"
     preserveAspectRatio="xMidYMid slice"
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
        ("accounts", "/accounts", "Accounts"),
        ("models", "/models", "Models"),
        ("latency", "/latency", "Latency"),
        ("pings", "/pings", "Pings"),
        ("bandwidth", "/bandwidth", "Bandwidth"),
        ("events", "/events", "Events"),
        ("timeseries", "/timeseries", "Timeseries"),
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
            '<form method="get" class="theme-selector">'
            '<select name="theme" onchange="this.form.submit()">'
            f"{options_html}"
            "</select>"
            f'<input type="hidden" name="period" value="{_html_escape(period)}">'
            "</form>"
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
        '<form method="get" class="period-selector">',
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
        "<th>Cost</th>"
        "<th>Avg latency</th>"
        "<th>Errors</th>"
        "<th>Models</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
        "</section>"
    )


def _render_timeseries_chart(period: str = "24h") -> str:
    """Render an interactive timeseries chart using Chart.js."""
    return f"""
<section class="panel">
  <h3>Request timeseries</h3>
  <div style="height: 300px; position: relative;">
    <canvas id="timeseries-chart"></canvas>
  </div>
</section>
<script>
(() => {{
  const period = '{period}';
  const ctx = document.getElementById('timeseries-chart');
  if (!ctx) return;

  const chart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: [],
      datasets: [
        {{
          label: 'Requests',
          data: [],
          borderColor: 'rgb(75, 192, 192)',
          tension: 0.1
        }},
        {{
          label: 'Errors',
          data: [],
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

      const labels = data.map(d => d.bucket);
      const requests = data.map(d => d.request_count || 0);
      const errors = data.map(d => d.error_count || 0);

      chart.data.labels = labels;
      chart.data.datasets[0].data = requests;
      chart.data.datasets[1].data = errors;
      chart.update();
    }} catch (err) {{
      console.error('Failed to load timeseries data:', err);
    }}
  }}

  loadData();
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
        rows.append(
            f"<tr>"
            f"<td>{escape(row.get('model_id', ''))}</td>"
            f"<td>{escape(row.get('provider_id', ''))}</td>"
            f"<td>{int(row.get('request_count', 0)):,}</td>"
            f"<td>{int(row.get('error_count', 0)):,}</td>"
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
        rows.append(
            f"<tr>"
            f"<td>{format_timestamp(row.get('created_at', ''))}</td>"
            f"<td>{escape(row.get('account_name', ''))}</td>"
            f'<td><span class="event-tag {sanitize_class_name(event_type)}">'
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
) -> str:
    """Render a GitHub-style contribution heatmap as inline SVG."""
    if not daily_data:
        return '<p class="empty">No bandwidth data available.</p>'

    # Build day -> bytes_emitted lookup
    day_values: dict[str, int] = {}
    for row in daily_data:
        day_str = str(row.get("day", ""))
        val = int(row.get("bytes_emitted", 0)) + int(row.get("bytes_received", 0))
        day_values[day_str] = val

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
            tooltip = f"{day_str}: {format_bytes(value)}"
            cells.append(
                f'<rect x="{x}" y="{y}" width="{cell_size}" '
                f'height="{cell_size}" rx="2" fill="{color}" '
                f'class="heatmap-cell">'
                f"<title>{tooltip}</title></rect>"
            )

    svg = (
        f'<svg width="{svg_width}" height="{svg_height}" '
        f'viewBox="0 0 {svg_width} {svg_height}" '
        f'role="img" aria-label="{_html_escape(title)}">'
        f"{''.join(cells)}</svg>"
    )

    return f'<div class="heatmap">{svg}</div>'


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

    most: dict[str, Any] = imbalance.get("most_used") or {}
    least: dict[str, Any] = imbalance.get("least_used") or {}

    body = f"""
<h2>Overview</h2>
{_render_period_selector(period, current_theme)}

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
    <p class="sub">in {in_tok} · out {out_tok}</p>
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

{_render_timeseries_chart(period)}

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
  <h3>Bandwidth activity</h3>
  {_render_bandwidth_heatmap(bandwidth_daily or [], heatmap_colors=heatmap_colors)}
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
        "<th>Cost</th>",
        "<th>Avg latency</th>",
        "<th>Reserved</th>",
        "<th>Resv.</th>",
        "<th>5h rate</th>",
        "<th>7d rate</th>",
        "<th>30d rate</th>",
        "<th>BW received</th>",
        "<th>BW emitted</th>",
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
        health = str(row.get("health_state", "unknown"))
        active_resv = int(row.get("active_reservations", 0))
        util_5h = format_microdollars(row.get("utilization_5h", 0))
        util_7d = format_microdollars(row.get("utilization_7d", 0))
        util_30d = format_microdollars(row.get("utilization_30d", 0))
        parts.append(
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{provider}</td>"
            f'<td class="{"yes" if enabled else "no"}">'
            f"{'yes' if enabled else 'no'}</td>"
            f'<td class="{sanitize_class_name(health)}">{health}</td>'
            f"<td>{int(row.get('request_count', 0)):,}</td>"
            f"<td>{int(row.get('error_count', 0)):,}</td>"
            f"<td>{in_tok}</td>"
            f"<td>{out_tok}</td>"
            f"<td>{cost}</td>"
            f"<td>{latency}</td>"
            f"<td>{reserved}</td>"
            f"<td>{active_resv}</td>"
            f"<td>{util_5h}</td>"
            f"<td>{util_7d}</td>"
            f"<td>{util_30d}</td>"
            f"<td>{format_bytes(row.get('bytes_received', 0))}</td>"
            f"<td>{format_bytes(row.get('bytes_emitted', 0))}</td>"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


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
            "<th>Cost</th>",
            "<th>Avg latency</th>",
            "<th>Avg TTFT</th>",
            "</tr></thead><tbody>",
        ]
        for row in models:
            cost = format_microdollars(row.get("cost_microdollars", 0))
            latency = format_latency(row.get("avg_latency_ms", 0.0))
            ttft = format_latency(row.get("avg_ttft_ms", 0.0))
            in_tok = format_tokens(row.get("input_tokens", 0))
            out_tok = format_tokens(row.get("output_tokens", 0))
            provider = escape(row.get("provider_id", ""))
            parts.append(
                f"<tr>"
                f"<td>{escape(row.get('model_id', ''))}</td>"
                f"<td>{provider}</td>"
                f"<td>{int(row.get('request_count', 0)):,}</td>"
                f"<td>{int(row.get('error_count', 0)):,}</td>"
                f"<td>{in_tok}</td>"
                f"<td>{out_tok}</td>"
                f"<td>{cost}</td>"
                f"<td>{latency}</td>"
                f"<td>{ttft}</td>"
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
            etype = escape(row.get("event_type", ""))
            details = truncate(row.get("details", ""), 200)
            cls = sanitize_class_name(str(row.get("event_type", "")))
            parts.append(
                f"<tr>"
                f"<td>{ts}</td>"
                f"<td>{name}</td>"
                f'<td><span class="event-tag {cls}">{etype}</span></td>'
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
            "<th>Cost</th>",
            "<th>BW received</th>",
            "<th>BW emitted</th>",
            "</tr></thead><tbody>",
        ]
        for row in series:
            cost = format_microdollars(row.get("cost_microdollars", 0))
            in_tok = format_tokens(row.get("input_tokens", 0))
            out_tok = format_tokens(row.get("output_tokens", 0))
            parts.append(
                f"<tr>"
                f"<td>{escape(row.get('bucket', ''))}</td>"
                f"<td>{int(row.get('request_count', 0)):,}</td>"
                f"<td>{int(row.get('error_count', 0)):,}</td>"
                f"<td>{in_tok}</td>"
                f"<td>{out_tok}</td>"
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


def render_latency(
    provider_ttft: list[dict[str, Any]],
    model_ttft: list[dict[str, Any]],
    period: str = "24h",
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
) -> str:
    """Render the latency breakdown page."""
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
            "</tr></thead><tbody>",
        ]
        for row in model_ttft:
            pid = escape(str(row.get("provider_id", "")))
            mid = escape(str(row.get("model_id", "")))
            avg = format_latency(row.get("avg_ttft_ms", 0.0))
            p50 = format_latency(row.get("p50_ttft_ms", 0.0))
            p99 = format_latency(row.get("p99_ttft_ms", 0.0))
            count = int(row.get("request_count", 0))
            model_parts.append(
                f"<tr>"
                f"<td>{pid}</td>"
                f"<td>{mid}</td>"
                f"<td>{count:,}</td>"
                f"<td>{avg}</td>"
                f"<td>{p50}</td>"
                f"<td>{p99}</td>"
                f"</tr>"
            )
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

{model_table}
"""
    return _render_layout(
        title="Latency",
        body=body,
        active_nav="latency",
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


__all__ = [
    "render_accounts",
    "render_bandwidth",
    "render_events",
    "render_latency",
    "render_models",
    "render_overview",
    "render_pings",
    "render_timeseries",
]
