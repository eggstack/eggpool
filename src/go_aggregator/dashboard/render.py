"""Server-side HTML rendering for the dashboard.

Uses a minimal string-based renderer to keep the dependency footprint
small. All values rendered into HTML are escaped via the `escape` module.
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape as _html_escape
from typing import Any

from go_aggregator.dashboard.escape import (
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


def _render_layout(
    title: str,
    body: str,
    active_nav: str = "",
    period: str = "24h",
    refresh_interval_s: int = 15,
) -> str:
    """Wrap a page body in the standard layout."""
    nav = _render_nav(active_nav, period)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_html_escape(title)}</title>
<link rel="stylesheet" href="/static/dashboard.css">
</head>
<body>
<header class="topbar">
  <h1><a href="/?period={_html_escape(period)}">Go Aggregator</a></h1>
  {nav}
</header>
<main>
{body}
</main>
<footer>
  <small>Period: <span class="period-label">{_html_escape(period)}</span>
    &middot; auto-refresh {refresh_interval_s}s</small>
</footer>
</body>
</html>"""


def _render_nav(active_nav: str, period: str) -> str:
    """Render the top navigation bar."""
    items = [
        ("overview", "/", "Overview"),
        ("accounts", "/accounts", "Accounts"),
        ("models", "/models", "Models"),
        ("bandwidth", "/bandwidth", "Bandwidth"),
        ("events", "/events", "Events"),
        ("timeseries", "/timeseries", "Timeseries"),
    ]
    parts = ['<nav class="topnav">']
    for key, href, label in items:
        cls = "active" if key == active_nav else ""
        parts.append(
            f'<a class="{cls}" href="{href}?period={_html_escape(period)}">'
            f"{_html_escape(label)}</a>"
        )
    parts.append("</nav>")
    return "".join(parts)


def _render_period_selector(current: str) -> str:
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
    parts.append("</form>")
    return "".join(parts)


def _render_bandwidth_heatmap(
    daily_data: list[dict[str, Any]],
    title: str = "Bandwidth activity (last 90 days)",
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

    # Color scale (GitHub Primer green)
    colors = ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]

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

    most: dict[str, Any] = imbalance.get("most_used") or {}
    least: dict[str, Any] = imbalance.get("least_used") or {}

    body = f"""
<h2>Overview</h2>
{_render_period_selector(period)}

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
</section>

<section class="panel">
  <h3>Account breakdown</h3>
  {_render_account_table(accounts)}
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

<section class="panel">
  <h3>Bandwidth activity</h3>
  {_render_bandwidth_heatmap(bandwidth_daily or [])}
</section>
"""
    return _render_layout(
        title="Overview",
        body=body,
        active_nav="overview",
        period=period,
        refresh_interval_s=refresh_interval_s,
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
) -> str:
    """Render the accounts page."""
    body = f"""
<h2>Accounts</h2>
{_render_period_selector(period)}
<section class="panel">
  {_render_account_table(accounts)}
</section>
"""
    return _render_layout(
        title="Accounts",
        body=body,
        active_nav="accounts",
        period=period,
    )


def render_models(
    models: list[dict[str, Any]],
    account_filter: str = "",
    period: str = "24h",
) -> str:
    """Render the models page."""
    if not models:
        rows_html = '<p class="empty">No model data for this period.</p>'
    else:
        parts = [
            '<table class="data">',
            "<thead><tr>",
            "<th>Model</th>",
            "<th>Requests</th>",
            "<th>Errors</th>",
            "<th>Input tokens</th>",
            "<th>Output tokens</th>",
            "<th>Cost</th>",
            "<th>Avg latency</th>",
            "</tr></thead><tbody>",
        ]
        for row in models:
            cost = format_microdollars(row.get("cost_microdollars", 0))
            latency = format_latency(row.get("avg_latency_ms", 0.0))
            in_tok = format_tokens(row.get("input_tokens", 0))
            out_tok = format_tokens(row.get("output_tokens", 0))
            parts.append(
                f"<tr>"
                f"<td>{escape(row.get('model_id', ''))}</td>"
                f"<td>{int(row.get('request_count', 0)):,}</td>"
                f"<td>{int(row.get('error_count', 0)):,}</td>"
                f"<td>{in_tok}</td>"
                f"<td>{out_tok}</td>"
                f"<td>{cost}</td>"
                f"<td>{latency}</td>"
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
  <button type="submit">Apply</button>
</form>
"""

    body = f"""
<h2>Models</h2>
{filter_form}
{_render_period_selector(period)}
<section class="panel">
  {rows_html}
</section>
"""
    return _render_layout(
        title="Models",
        body=body,
        active_nav="models",
        period=period,
    )


def render_events(
    events: list[dict[str, Any]],
    event_type: str = "",
    period: str = "24h",
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
  <button type="submit">Apply</button>
</form>
"""

    body = f"""
<h2>Events</h2>
{filter_form}
{_render_period_selector(period)}
<section class="panel">
  {rows_html}
</section>
"""
    return _render_layout(
        title="Events",
        body=body,
        active_nav="events",
        period=period,
    )


def render_timeseries(
    series: list[dict[str, Any]],
    bucket: str,
    period: str = "24h",
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
{_render_period_selector(period)}
<section class="panel">
  {rows_html}
</section>
"""
    return _render_layout(
        title="Timeseries",
        body=body,
        active_nav="timeseries",
        period=period,
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
  <button type="submit">Apply</button>
</form>
"""

    body = f"""
<h2>Bandwidth</h2>
{filter_form}
{_render_period_selector(period)}

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
  {_render_bandwidth_heatmap(daily)}
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
    )


__all__ = [
    "render_accounts",
    "render_bandwidth",
    "render_events",
    "render_models",
    "render_overview",
    "render_timeseries",
]
