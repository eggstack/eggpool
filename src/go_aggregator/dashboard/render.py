"""Server-side HTML rendering for the dashboard.

Uses a minimal string-based renderer to keep the dependency footprint
small. All values rendered into HTML are escaped via the `escape` module.
"""

from __future__ import annotations

from html import escape as _html_escape
from typing import Any

from go_aggregator.dashboard.escape import (
    escape,
    escape_attr,
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


def render_overview(
    overview: dict[str, Any],
    accounts: list[dict[str, Any]],
    account_filter: str = "",
    period: str = "24h",
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
"""
    return _render_layout(
        title="Overview",
        body=body,
        active_nav="overview",
        period=period,
    )


def _render_account_table(accounts: list[dict[str, Any]]) -> str:
    """Render the account breakdown table."""
    if not accounts:
        return '<p class="empty">No accounts configured.</p>'
    parts = [
        '<table class="data">',
        "<thead><tr>",
        "<th>Account</th>",
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
        "<th>5h util</th>",
        "<th>7d util</th>",
        "<th>30d util</th>",
        "</tr></thead><tbody>",
    ]
    for row in accounts:
        enabled = bool(row.get("account_enabled", 0))
        name = escape(row.get("account_name", ""))
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


__all__ = [
    "render_accounts",
    "render_events",
    "render_models",
    "render_overview",
    "render_timeseries",
]
