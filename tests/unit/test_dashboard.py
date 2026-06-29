"""Tests for the dashboard HTML rendering and escape utilities."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

from eggpool.dashboard.escape import (
    escape,
    escape_attr,
    format_bytes,
    format_latency,
    format_microdollars,
    format_percent,
    format_tokens,
    format_tokens_per_second,
    sanitize_class_name,
    truncate,
)
from eggpool.dashboard.render import (
    _format_tooltip_date,
    _render_bandwidth_heatmap,
    _render_nav,
    _render_period_selector,
    _render_system_health,
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


class _HTMLTextExtractor(HTMLParser):
    """Extract text and check for unescaped content."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.text_parts.append(data)


class TestEscape:
    """Tests for escape utilities."""

    def test_escape_none(self) -> None:
        assert escape(None) == ""

    def test_escape_plain_text(self) -> None:
        assert escape("hello") == "hello"

    def test_escape_html_chars(self) -> None:
        assert escape("<script>") == "&lt;script&gt;"
        assert escape("a & b") == "a &amp; b"
        assert escape('"quoted"') == "&quot;quoted&quot;"

    def test_escape_attr(self) -> None:
        result = escape_attr("a&b")
        assert "&amp;" in result

    def test_format_microdollars(self) -> None:
        assert format_microdollars(1_000_000) == "$1.00"
        assert format_microdollars(0) == "$0.00"
        assert format_microdollars(None) == "$0.00"

    def test_format_microdollars_rounds_half_even(self) -> None:
        assert format_microdollars(123_456) == "$0.12"
        assert format_microdollars(1_234_567) == "$1.23"
        assert format_microdollars(99_999) == "$0.10"
        assert format_microdollars(999) == "$0.00"

    def test_format_tokens(self) -> None:
        assert format_tokens(1_000_000) == "1.00 M"
        assert format_tokens(0) == "0"

    def test_format_tokens_per_second(self) -> None:
        assert format_tokens_per_second(12.345) == "12.3 tok/s"
        assert format_tokens_per_second(1234.5) == "1234.5 tok/s"
        assert format_tokens_per_second(0) == "—"
        assert format_tokens_per_second(-1.0) == "—"
        assert format_tokens_per_second(None) == "—"

    def test_format_percent(self) -> None:
        assert format_percent(0.5) == "50.00%"
        assert format_percent(0.123) == "12.30%"

    def test_format_latency(self) -> None:
        assert format_latency(100.5) == "100.5 ms"
        assert format_latency(0) == "0.0 ms"

    def test_format_bytes(self) -> None:
        assert format_bytes(0) == "0 B"
        assert format_bytes(500) == "500 B"
        assert format_bytes(1000) == "1.0 KB"
        assert format_bytes(1_500_000) == "1.5 MB"
        assert format_bytes(2_500_000_000) == "2.5 GB"
        assert format_bytes(1_200_000_000_000) == "1.2 TB"
        assert format_bytes(None) == "0 B"
        assert format_bytes(999) == "999 B"

    def test_truncate_short(self) -> None:
        assert truncate("hello") == "hello"

    def test_truncate_long(self) -> None:
        result = truncate("a" * 100, max_length=10)
        assert result.endswith("...")
        assert len(result) == 10

    def test_truncate_escapes(self) -> None:
        result = truncate("<script>alert(1)</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_sanitize_class_name(self) -> None:
        assert sanitize_class_name("hello-world") == "hello-world"
        assert sanitize_class_name("hello world!") == "hello_world_"
        assert sanitize_class_name("foo.bar@baz") == "foo_bar_baz"
        assert sanitize_class_name("") == ""


class TestRenderOverview:
    """Tests for the overview page renderer."""

    def test_renders_basic_structure(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 10,
                    "successful_requests": 8,
                    "error_requests": 2,
                    "error_rate": 0.2,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 2000,
                    "total_cost_microdollars": 1_500_000,
                    "avg_latency_ms": 250.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.1,
                    "active_accounts": 2,
                    "most_used": {"name": "acct_a", "cost_microdollars": 1000},
                    "least_used": {"name": "acct_b", "cost_microdollars": 500},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert "<html" in html
        assert "</html>" in html
        assert "10" in html
        assert "$1.50" in html
        assert "20.00%" in html
        assert 'id="dashboard-content"' in html
        assert "setInterval" in html
        assert "/static/dashboard.css" in html

    def test_loads_chart_js(self) -> None:
        """The overview page must load Chart.js with the ``defer`` attribute."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        assert '<script defer src="/static/chart.js"></script>' in html

    def test_chart_preloads_inlined_timeseries_data(self) -> None:
        """The chart must be seeded from an inlined JSON data island so
        the deferred dashboard.js can initialise Chart.js without an
        inline ``new Chart(...)`` script.
        """
        timeseries = [
            {
                "bucket": "2024-01-01 12:00:00",
                "request_count": 3,
                "error_count": 1,
            }
        ]
        html = render_overview(
            overview={
                "summary": {"total_requests": 3},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=timeseries,
        )
        assert "Request timeseries" in html
        assert '"2024-01-01 12:00:00"' in html
        assert 'id="timeseries-initial-data"' in html
        assert 'id="timeseries-chart"' in html

    def test_escapes_account_name_in_overview(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": {"name": "<bad>", "cost_microdollars": 0},
                    "least_used": {"name": "<worse>", "cost_microdollars": 0},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert "<bad>" not in html
        assert "&lt;bad&gt;" in html

    def test_renders_egg_background_svg(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": {"name": "a", "cost_microdollars": 0},
                    "least_used": {"name": "b", "cost_microdollars": 0},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert 'class="egg-background"' in html
        assert 'viewBox="0 0 256 256"' in html
        # `meet` (not `slice`) so the top and bottom of the egg are always
        # visible regardless of viewport aspect ratio.
        assert 'preserveAspectRatio="xMidYMid meet"' in html
        assert 'aria-hidden="true"' in html
        # Egg body, heartbeat line, and two endpoint dots
        assert 'class="shape"' in html
        assert html.count('class="shape"') == 3
        assert 'class="thin"' in html
        assert html.count('class="thin"') == 1
        assert "<circle" in html
        # No legacy or unsupported classes
        assert 'class="egg-shell"' not in html
        assert 'class="egg-crack"' not in html
        # Egg SVG should be the first child of <body>, before the topbar
        body_open = html.index("<body>")
        svg_open = html.index('<svg class="egg-background"')
        topbar_open = html.index('<header class="topbar"')
        assert body_open < svg_open < topbar_open

    def test_renders_account_table(self) -> None:
        accounts = [
            {
                "account_name": "acct_a",
                "account_enabled": 1,
                "provider_id": "opencode-go",
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 200,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 100.0,
                "reserved_microdollars": 500_000,
            }
        ]
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 5,
                    "successful_requests": 4,
                    "error_requests": 1,
                    "error_rate": 0.2,
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                    "total_cost_microdollars": 1_000_000,
                    "avg_latency_ms": 100.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=accounts,
        )
        assert "acct_a" in html
        assert "$1.00" in html

    def test_overview_renders_provider_column(self) -> None:
        accounts = [
            {
                "account_name": "acct_a",
                "account_enabled": 1,
                "provider_id": "anthropic-proxy",
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 200,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 100.0,
                "reserved_microdollars": 500_000,
            }
        ]
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 5,
                    "successful_requests": 4,
                    "error_requests": 1,
                    "error_rate": 0.2,
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                    "total_cost_microdollars": 1_000_000,
                    "avg_latency_ms": 100.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=accounts,
        )
        assert "anthropic-proxy" in html
        assert "Provider" in html

    def test_renders_no_accounts_message(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert "No accounts configured" in html

    def test_renders_overview_glance_sections(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 1,
                    "successful_requests": 1,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 10,
                    "total_output_tokens": 20,
                    "total_cost_microdollars": 100,
                    "avg_latency_ms": 50.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
            },
            accounts=[],
            models=[
                {
                    "model_id": "<model>",
                    "provider_id": "opencode-go",
                    "request_count": 1,
                    "error_count": 0,
                    "cost_microdollars": 100,
                    "avg_latency_ms": 50.0,
                }
            ],
            events=[
                {
                    "created_at": "2024-01-01 12:00:00",
                    "account_name": "acct_a",
                    "event_type": "catalog_refresh_failed",
                    "details": "<detail>",
                }
            ],
        )
        assert "Top models" in html
        assert "Recent events" in html
        assert "<model>" not in html
        assert "&lt;model&gt;" in html
        assert "<detail>" not in html
        assert "&lt;detail&gt;" in html

    def test_overview_top_models_cells_match_header_order(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 1,
                    "successful_requests": 1,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_cost_microdollars": 250_000,
                    "total_tokens": 1234,
                },
                "extremes": {},
                "efficiency": {},
            },
            period="24h",
            accounts=[],
            models=[
                {
                    "model_id": "MiniMax-M3",
                    "provider_id": "minimax",
                    "request_count": 12,
                    "error_count": 1,
                    "total_tokens": 3456,
                    "cost_microdollars": 250_000,
                    "avg_latency_ms": 50.0,
                }
            ],
            events=[],
        )

        assert '<th data-priority="1">Model</th>' in html
        assert (
            '<td data-priority="1">MiniMax-M3</td>'
            '<td data-priority="1">12</td>'
            '<td data-priority="1">$0.25</td>'
            '<td data-priority="2">minimax</td>'
            '<td data-priority="2">1</td>'
            '<td data-priority="2">50.0 ms</td>'
            '<td data-priority="3">3,456</td>'
        ) in html

    def test_overview_total_tokens_card_renders(self) -> None:
        """A 'Total tokens' card surfaces Σtokens across all providers near the top."""
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 2,
                    "successful_requests": 2,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 2500,
                    "total_tokens": 3500,
                    "total_cache_read_tokens": 250,
                    "total_cache_write_tokens": 0,
                    "total_reasoning_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 50.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
            },
            accounts=[],
        )
        assert ">Total tokens<" in html
        total_tok_card_idx = html.index(">Total tokens<")
        cache_card_idx = html.index(">Cache tokens<")
        assert total_tok_card_idx < cache_card_idx
        # Total tokens = 3500 → format_tokens → "3,500" (exact formatting
        # below 1M). The metric line of the new card carries the total.
        assert ">3,500</p>" in html
        # The new card's sub-line mirrors input/output (total now sits on the
        # metric line itself).
        total_card_section = html[total_tok_card_idx:cache_card_idx]
        assert "in 1,000" in total_card_section
        assert "out 2,500" in total_card_section

    def test_overview_cache_tokens_card_shows_percent_of_input(self) -> None:
        """Cache tokens card sub-line reports cache_read / input as a percent."""
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 2,
                    "successful_requests": 2,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 500,
                    "total_tokens": 1500,
                    "total_cache_read_tokens": 250,
                    "total_cache_write_tokens": 50,
                    "total_reasoning_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 50.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
            },
            accounts=[],
        )
        cache_idx = html.index(">Cache tokens<")
        next_card_idx = html.index(">Reasoning tokens<")
        cache_section = html[cache_idx:next_card_idx]
        assert "25.0% of input" in cache_section
        # Write token sub-line still renders alongside the percent.
        assert "write 50" in cache_section

    def test_overview_cache_tokens_percent_dash_when_no_input(self) -> None:
        """When there are no input tokens the percent collapses to an em-dash."""
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_tokens": 0,
                    "total_cache_read_tokens": 0,
                    "total_cache_write_tokens": 0,
                    "total_reasoning_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": None,
                    "least_used": None,
                },
            },
            accounts=[],
        )
        cache_idx = html.index(">Cache tokens<")
        next_card_idx = html.index(">Reasoning tokens<")
        cache_section = html[cache_idx:next_card_idx]
        assert "— of input" in cache_section

    def test_escapes_period_in_timeseries_chart(self) -> None:
        """Regression test: ``period`` is interpolated into a JS literal;
        a malicious value must not escape the string literal.
        """
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": {"name": "a", "cost_microdollars": 0},
                    "least_used": {"name": "b", "cost_microdollars": 0},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
            period="';alert(1)//",
        )
        assert "');alert(1)//" not in html
        assert r"\'" in html or r"\u0027" in html or '"' in html

    def test_account_breakdown_renders_show_disabled_filter(self) -> None:
        """The Account breakdown section exposes a Show/Hide disabled toggle."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        assert "account-breakdown-filter" in html
        assert 'id="overview_show_disabled"' in html
        assert 'name="show_disabled"' in html
        # Default state: "Hide disabled accounts" is selected.
        assert (
            '<option value="0" selected="selected">Hide disabled accounts</option>'
            in html
        )

    def test_account_breakdown_filter_reflects_state(self) -> None:
        """``show_disabled=True`` selects the show option on the overview."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            show_disabled=True,
        )
        assert (
            '<option value="1" selected="selected">Show disabled accounts</option>'
            in html
        )

    def test_account_breakdown_filter_preserves_period_and_theme(self) -> None:
        """The toggle form preserves period and theme via hidden inputs."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            period="7d",
            current_theme="midnight",
        )
        assert 'class="period-selector account-breakdown-filter"' in html
        assert 'name="period" value="7d"' in html
        assert 'name="theme" value="midnight"' in html

    def test_account_breakdown_empty_state_with_disabled_count(self) -> None:
        """When only disabled rows exist, offer a one-click opt-in link."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            show_disabled=False,
            disabled_count=3,
        )
        assert "No enabled accounts." in html
        assert "3 disabled accounts hidden" in html
        assert 'href="?show_disabled=1"' in html
        assert "show them" in html

    def test_account_breakdown_empty_state_singular(self) -> None:
        """Pluralization is correct when only one disabled account exists."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            show_disabled=False,
            disabled_count=1,
        )
        assert "1 disabled account hidden" in html
        assert "3 disabled" not in html

    def test_account_breakdown_no_hint_when_show_disabled(self) -> None:
        """With ``show_disabled=True`` an empty result is a clean empty state."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            show_disabled=True,
            disabled_count=3,
        )
        assert "No accounts configured." in html
        assert "show them" not in html

    def test_account_breakdown_no_hint_when_no_disabled(self) -> None:
        """No disabled rows means no opt-in hint even when accounts is empty."""
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            show_disabled=False,
            disabled_count=0,
        )
        assert "No accounts configured." in html
        assert "show them" not in html


class TestRenderAccounts:
    """Tests for the accounts page renderer."""

    def test_renders_empty(self) -> None:
        html = render_accounts(accounts=[], period="24h")
        assert "Accounts" in html
        assert "No accounts" in html

    def test_renders_table(self) -> None:
        accounts = [
            {
                "account_name": "alpha",
                "account_enabled": 1,
                "provider_id": "opencode-go",
                "request_count": 3,
                "error_count": 0,
                "input_tokens": 100,
                "output_tokens": 200,
                "cost_microdollars": 500_000,
                "avg_latency_ms": 75.5,
                "reserved_microdollars": 0,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert "alpha" in html

    def test_renders_provider_column(self) -> None:
        accounts = [
            {
                "account_name": "alpha",
                "account_enabled": 1,
                "provider_id": "acme-ai",
                "request_count": 3,
                "error_count": 0,
                "input_tokens": 100,
                "output_tokens": 200,
                "cost_microdollars": 500_000,
                "avg_latency_ms": 75.5,
                "reserved_microdollars": 0,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert "acme-ai" in html
        assert "Provider" in html

    def test_html_injection_blocked(self) -> None:
        accounts = [
            {
                "account_name": "<script>alert(1)</script>",
                "account_enabled": 1,
                "request_count": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_microdollars": 0,
                "avg_latency_ms": 0.0,
                "reserved_microdollars": 0,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_health_state_html_injection_blocked(self) -> None:
        """Regression test (H4): ``health_state`` flows from DB rows and
        must be escaped before being rendered as HTML body content.
        """
        accounts = [
            {
                "account_name": "alpha",
                "account_enabled": 1,
                "request_count": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_microdollars": 0,
                "avg_latency_ms": 0.0,
                "reserved_microdollars": 0,
                "health_state": "<script>alert(1)</script>",
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_does_not_load_chart_js(self) -> None:
        """The accounts page must not pull in the 200 KB Chart.js script."""
        html = render_accounts(accounts=[], period="24h")
        assert "/static/chart.js" not in html

    def test_total_tokens_and_tps_columns_present(self) -> None:
        accounts = [
            {
                "account_name": "acct_a",
                "account_enabled": 1,
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 250,
                "total_tokens": 350,
                "tokens_per_second": 70.0,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 200.0,
                "reserved_microdollars": 0,
                "bytes_received": 0,
                "bytes_emitted": 0,
                "health_state": "healthy",
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert '<th data-priority="2">Total tokens</th>' in html
        assert '<th data-priority="2">TPS</th>' in html
        assert "350" in html  # total_tokens rendered
        assert "70.0 tok/s" in html

    def test_tps_dash_when_zero(self) -> None:
        accounts = [
            {
                "account_name": "cold_acct",
                "account_enabled": 1,
                "request_count": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "tokens_per_second": 0.0,
                "cost_microdollars": 0,
                "avg_latency_ms": 0.0,
                "reserved_microdollars": 0,
                "bytes_received": 0,
                "bytes_emitted": 0,
                "health_state": "healthy",
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert "0.0 tok/s" not in html
        assert "—" in html  # the dash placeholder rendered for cold accounts

    def test_backoff_columns_rendered(self) -> None:
        """Backoff-related columns appear in the accounts table."""
        accounts = [
            {
                "account_name": "rate-limited-acct",
                "account_enabled": 1,
                "provider_id": "opencode-go",
                "request_count": 10,
                "error_count": 0,
                "input_tokens": 100,
                "output_tokens": 200,
                "total_tokens": 300,
                "tokens_per_second": 30.0,
                "cost_microdollars": 500_000,
                "avg_latency_ms": 75.5,
                "reserved_microdollars": 0,
                "active_reservations": 0,
                "bytes_received": 0,
                "bytes_emitted": 0,
                "health_state": "unhealthy",
                "estimated_over_local_budget": False,
                "upstream_backoff_reason": "rate_limited",
                "backoff_until": 1735689600.0,
                "consecutive_upstream_failures": 3,
                "authentication_failed": False,
                "operator_disabled": False,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert '<th data-priority="3">Over budget</th>' in html
        assert '<th data-priority="3">Upstream backoff</th>' in html
        assert '<th data-priority="3">Backoff until</th>' in html
        assert '<th data-priority="3">Failures</th>' in html
        assert '<th data-priority="3">Auth fail</th>' in html
        assert '<th data-priority="3">Disabled</th>' in html
        assert "rate_limited" in html
        assert ">3</td>" in html
        assert "2025-01-01" in html

    def test_backoff_columns_dash_when_no_state(self) -> None:
        """Accounts with no backoff state show explicit placeholders."""
        accounts = [
            {
                "account_name": "healthy-acct",
                "account_enabled": 1,
                "provider_id": "opencode-go",
                "request_count": 5,
                "error_count": 0,
                "input_tokens": 100,
                "output_tokens": 200,
                "total_tokens": 300,
                "tokens_per_second": 60.0,
                "cost_microdollars": 500_000,
                "avg_latency_ms": 75.5,
                "reserved_microdollars": 0,
                "active_reservations": 0,
                "bytes_received": 0,
                "bytes_emitted": 0,
                "health_state": "healthy",
                "estimated_over_local_budget": False,
                "upstream_backoff_reason": None,
                "backoff_until": None,
                "consecutive_upstream_failures": 0,
                "authentication_failed": False,
                "operator_disabled": False,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert ">no</td>" in html  # over-budget flag defaults to no
        assert ">0</td>" in html  # failure count defaults to 0
        assert "—" in html  # backoff reason and timestamp placeholders

    def test_renders_show_disabled_selector(self) -> None:
        """The accounts filter bar exposes a show-disabled toggle."""
        html = render_accounts(accounts=[], period="24h")
        assert 'name="show_disabled"' in html
        assert 'name="period"' in html
        # Default state: "Hide disabled accounts" is selected.
        assert (
            '<option value="0" selected="selected">Hide disabled accounts</option>'
            in html
        )
        assert "Show disabled accounts" in html

    def test_show_disabled_selector_reflects_state(self) -> None:
        """``show_disabled=True`` selects the show option."""
        html = render_accounts(accounts=[], period="24h", show_disabled=True)
        assert (
            '<option value="1" selected="selected">Show disabled accounts</option>'
            in html
        )
        assert 'value="0">Hide disabled accounts</option>' in html

    def test_disabled_count_one_uses_singular(self) -> None:
        """Pluralization is correct when only one disabled account exists."""
        html = render_accounts(
            accounts=[],
            period="24h",
            show_disabled=False,
            disabled_count=1,
        )
        assert "1 disabled account hidden" in html
        assert "show them" in html
        # The link must point at the toggle-on URL, not the bare page.
        assert 'href="?show_disabled=1"' in html

    def test_disabled_count_many_uses_plural(self) -> None:
        """Pluralization switches for multiple disabled accounts."""
        html = render_accounts(
            accounts=[],
            period="24h",
            show_disabled=False,
            disabled_count=3,
        )
        assert "3 disabled accounts hidden" in html

    def test_no_empty_state_hint_when_show_disabled(self) -> None:
        """The opt-in hint is suppressed when the toggle is on."""
        html = render_accounts(
            accounts=[],
            period="24h",
            show_disabled=True,
            disabled_count=3,
        )
        assert "show them" not in html
        # Falls back to the original generic empty state.
        assert "No accounts configured" in html

    def test_no_empty_state_hint_when_disabled_count_zero(self) -> None:
        """The opt-in hint is suppressed when nothing is hidden."""
        html = render_accounts(
            accounts=[],
            period="24h",
            show_disabled=False,
            disabled_count=0,
        )
        assert "show them" not in html
        assert "No accounts configured" in html

    def test_show_disabled_xss_safe(self) -> None:
        """The disabled-count empty state escapes interpolated values."""
        # Render with a normal integer count to confirm there are no
        # injection vectors in the new helper. (Account names already
        # have their own XSS tests in TestRenderAccounts.) We check for
        # a bare ``<script>`` rather than the broader ``<script`` so the
        # always-on ``<script defer src="/static/dashboard.js">`` tag
        # does not trigger a false positive.
        html = render_accounts(
            accounts=[],
            period="24h",
            show_disabled=False,
            disabled_count=2,
        )
        assert "2 disabled accounts hidden" in html
        assert "<script>" not in html


class TestRenderModels:
    """Tests for the models page renderer."""

    def test_renders_empty(self) -> None:
        html = render_models(models=[], period="24h")
        assert "No model data" in html

    def test_renders_rows(self) -> None:
        models = [
            {
                "model_id": "gpt-x",
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 200,
                "total_tokens": 300,
                "tokens_per_second": 25.0,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 200.0,
                "avg_ttft_ms": 50.0,
            }
        ]
        html = render_models(models=models, period="24h")
        assert "gpt-x" in html
        assert "$1.00" in html
        assert '<th data-priority="2">Total tokens</th>' in html
        assert '<th data-priority="2">TPS</th>' in html
        assert "300" in html
        assert "25.0 tok/s" in html

    def test_account_filter_shown(self) -> None:
        html = render_models(models=[], account_filter="acct_a", period="24h")
        assert 'value="acct_a"' in html

    def test_does_not_load_chart_js(self) -> None:
        html = render_models(models=[], period="24h")
        assert "/static/chart.js" not in html


class TestRenderEvents:
    """Tests for the events page renderer."""

    def test_renders_empty(self) -> None:
        html = render_events(events=[], period="24h")
        assert "No events" in html

    def test_renders_event(self) -> None:
        events = [
            {
                "created_at": "2024-01-01 12:00:00",
                "account_name": "acct_a",
                "event_type": "cooldown_active",
                "details": '{"seconds": 60}',
            }
        ]
        html = render_events(events=events, period="24h")
        assert "cooldown_active" in html
        assert "acct_a" in html
        assert "cooldown_active" in html

    def test_event_type_filter_in_form(self) -> None:
        html = render_events(events=[], event_type="cooldown_active", period="24h")
        assert 'value="cooldown_active"' in html

    def test_does_not_load_chart_js(self) -> None:
        html = render_events(events=[], period="24h")
        assert "/static/chart.js" not in html


class TestRenderTimeseries:
    """Tests for the timeseries page renderer."""

    def test_renders_empty(self) -> None:
        html = render_timeseries(series=[], bucket="hour", period="24h")
        assert "No requests" in html

    def test_renders_buckets(self) -> None:
        series = [
            {
                "bucket": "2024-01-01 12:00:00",
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 200,
                "total_tokens": 300,
                "cost_microdollars": 500_000,
            }
        ]
        html = render_timeseries(series=series, bucket="hour", period="24h")
        assert "2024-01-01 12:00:00" in html
        assert '<th data-priority="2">Total tokens</th>' in html
        assert "300" in html

    def test_default_metric_is_tokens(self) -> None:
        """Tokens is the operator-first default so the chart reads as a usage view."""
        html = render_timeseries(series=[], bucket="hour", period="24h")
        assert 'data-metric="tokens"' in html
        assert 'value="tokens" selected' in html
        assert 'value="requests" selected' not in html

    def test_controls_form_has_no_period_dropdown(self) -> None:
        """The canonical period selector lives outside the controls form.

        Re-rendering it inside ``form.timeseries-controls`` would give
        operators two period dropdowns that drift out of sync.
        The controls form still carries a hidden period for no-JS Apply.
        """
        html = render_timeseries(series=[], bucket="hour", period="24h")
        controls_start = html.index('class="filter-form timeseries-controls"')
        controls_end = html.index("</form>", controls_start)
        controls_section = html[controls_start:controls_end]
        assert '<select id="period" name="period">' not in controls_section
        assert '<input type="hidden" name="period" value="24h">' in controls_section
        # Period dropdown still lives outside the controls form.
        assert 'id="period"' in html

    def test_account_and_model_are_dropdowns(self) -> None:
        """Operators pick accounts and models from a list, not free-text."""
        html = render_timeseries(
            series=[],
            bucket="hour",
            period="24h",
            account_options=["acct_a", "acct_b"],
            model_options=["claude-sonnet-4/opencode-go", "gpt-5/openai"],
        )
        controls_start = html.index('class="filter-form timeseries-controls"')
        controls_end = html.index("</form>", controls_start)
        controls_section = html[controls_start:controls_end]
        # No free-text inputs remain.
        assert '<input type="text"' not in controls_section
        # Both account and model are selects.
        assert 'name="account"' in controls_section
        assert 'name="model"' in controls_section
        # Option values come from the lists passed in.
        assert 'value="acct_a"' in controls_section
        assert 'value="acct_b"' in controls_section
        assert 'value="claude-sonnet-4/opencode-go"' in controls_section
        assert 'value="gpt-5/openai"' in controls_section
        # Dropdowns include an "(any …)" option so the filter can be cleared.
        assert "(any account)" in controls_section
        assert "(any model)" in controls_section

    def test_controls_form_is_tagged_for_live_wire_up(self) -> None:
        """The form carries the hook the dashboard.js handler looks for."""
        html = render_timeseries(series=[], bucket="hour", period="24h")
        assert "data-timeseries-controls" in html

    def test_period_selector_tagged_for_live_wire_up(self) -> None:
        """The canonical period selector opts into the live wire-up."""
        html = render_timeseries(series=[], bucket="hour", period="24h")
        assert "data-period-selector" in html

    def test_timeseries_period_selector_uses_control_class(self) -> None:
        """Timeseries period styling matches the other controls."""
        html = render_timeseries(series=[], bucket="hour", period="24h")
        assert 'class="period-selector timeseries-period-selector"' in html

    def test_period_selector_preserves_theme(self) -> None:
        """The period selector's form must carry the active theme as a hidden input.

        The JS handler intercepts ``change`` on the timeseries page, but
        on every other page the form is submitted normally — without a
        hidden ``theme`` field the user's theme selection is dropped on
        every period change.
        """
        html = _render_period_selector("24h", current_theme="dark")
        assert 'name="theme"' in html
        assert 'value="dark"' in html

    def test_period_selector_omits_theme_when_unset(self) -> None:
        """No hidden ``theme`` input is emitted when no theme is active."""
        html = _render_period_selector("24h")
        assert 'name="theme"' not in html

    def test_empty_chart_panel_still_emits_canvas(self) -> None:
        """Empty payload still emits the canvas so JS can update after a filter."""
        html = render_timeseries(series=[], bucket="hour", period="24h")
        assert 'class="grouped-timeseries-chart"' in html
        assert "grouped-timeseries-empty" in html


class TestHtmlParseability:
    """Verify rendered HTML parses as valid HTML."""

    def test_overview_parses(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 10,
                    "successful_requests": 8,
                    "error_requests": 2,
                    "error_rate": 0.2,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 2000,
                    "total_cost_microdollars": 1_500_000,
                    "avg_latency_ms": 250.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.1,
                    "active_accounts": 2,
                    "most_used": {"name": "acct_a", "cost_microdollars": 1000},
                    "least_used": {"name": "acct_b", "cost_microdollars": 500},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        parser = _HTMLTextExtractor()
        parser.feed(html)
        text = "".join(parser.text_parts)
        assert "10" in text
        assert "$1.50" in text


class TestRenderNav:
    """Tests for the navigation bar renderer."""

    def test_timeseries_link_present(self) -> None:
        """Timeseries link appears in the navigation."""
        html = _render_nav("overview", "24h")
        assert "/timeseries" in html
        assert "Timeseries" in html

    def test_all_expected_links_present(self) -> None:
        """All dashboard pages are linked from navigation."""
        html = _render_nav("", "24h")
        for href in (
            "/",
            "/accounts",
            "/models",
            "/bandwidth",
            "/events",
            "/timeseries",
        ):
            assert href in html

    def test_active_nav_highlighted(self) -> None:
        """The active nav item gets the 'active' CSS class."""
        html = _render_nav("timeseries", "24h")
        assert 'class="active"' in html
        assert 'href="/timeseries' in html

    def test_non_active_nav_not_highlighted(self) -> None:
        """Non-active nav items do not get the 'active' class on their link."""
        html = _render_nav("overview", "24h")
        # Only one link should have class="active" (the overview link)
        assert html.count('class="active"') == 1
        # The timeseries link should have class="" (inactive)
        assert 'class="" href="/timeseries' in html

    def test_bandwidth_link_present(self) -> None:
        """Bandwidth link appears in the navigation."""
        html = _render_nav("overview", "24h")
        assert "/bandwidth" in html
        assert "Bandwidth" in html


class TestFormatBytes:
    """Tests for the format_bytes utility."""

    def test_zero(self) -> None:
        assert format_bytes(0) == "0 B"

    def test_bytes(self) -> None:
        assert format_bytes(999) == "999 B"

    def test_kilobytes(self) -> None:
        assert format_bytes(1500) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert format_bytes(2_500_000) == "2.5 MB"

    def test_gigabytes(self) -> None:
        assert format_bytes(3_700_000_000) == "3.7 GB"

    def test_terabytes(self) -> None:
        assert format_bytes(1_200_000_000_000) == "1.2 TB"

    def test_none(self) -> None:
        assert format_bytes(None) == "0 B"

    def test_float(self) -> None:
        assert format_bytes(1500.5) == "1.5 KB"


class TestRenderBandwidthHeatmap:
    """Tests for the bandwidth heatmap SVG renderer."""

    def test_empty_data(self) -> None:
        html = _render_bandwidth_heatmap([])
        assert "No activity data" in html

    def test_renders_svg(self) -> None:
        daily = [
            {"day": "2024-06-01", "bytes_received": 1000, "bytes_emitted": 500},
            {"day": "2024-06-02", "bytes_received": 2000, "bytes_emitted": 1000},
        ]
        html = _render_bandwidth_heatmap(daily)
        assert "<svg" in html
        assert 'role="img"' in html
        assert "heatmap-cell" in html

    def test_renders_tooltip(self) -> None:
        daily = [
            {"day": "2024-06-01", "bytes_received": 1000, "bytes_emitted": 500},
        ]
        html = _render_bandwidth_heatmap(daily)
        assert "<title>" in html

    def test_zero_values(self) -> None:
        daily = [
            {"day": "2024-06-01", "bytes_received": 0, "bytes_emitted": 0},
        ]
        html = _render_bandwidth_heatmap(daily)
        assert "<svg" in html

    def test_total_tokens_value_field(self) -> None:
        from datetime import date as _date

        today = _date.today().isoformat()
        daily = [{"day": today, "total_tokens": 4242}]
        html = _render_bandwidth_heatmap(daily, value_field="total_tokens")
        assert "<svg" in html
        assert "4,242" in html  # token-formatted tooltip

    def test_total_tokens_empty_data(self) -> None:
        html = _render_bandwidth_heatmap([], value_field="total_tokens")
        assert "No activity data" in html

    def test_invalid_value_field_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="unsupported heatmap value_field"):
            _render_bandwidth_heatmap(
                [{"day": "2024-06-01", "x": 1}], value_field="nope"
            )


class TestRenderBandwidthPage:
    """Tests for the bandwidth page renderer."""

    def test_renders_basic_structure(self) -> None:
        html = render_bandwidth(
            summary={
                "total_bytes_received": 1_500_000,
                "total_bytes_emitted": 800_000,
            },
            daily=[],
            timeseries=[],
        )
        assert "<html" in html
        assert "</html>" in html
        assert "Bandwidth" in html
        assert "1.5 MB" in html
        assert "800.0 KB" in html

    def test_renders_heatmap(self) -> None:
        daily = [
            {"day": "2024-06-01", "bytes_received": 1000, "bytes_emitted": 500},
        ]
        html = render_bandwidth(
            summary={"total_bytes_received": 0, "total_bytes_emitted": 0},
            daily=daily,
            timeseries=[],
        )
        assert "<svg" in html

    def test_renders_timeseries_table(self) -> None:
        timeseries = [
            {
                "bucket": "2024-06-01 12:00:00",
                "request_count": 5,
                "bytes_received": 1000,
                "bytes_emitted": 500,
            }
        ]
        html = render_bandwidth(
            summary={"total_bytes_received": 0, "total_bytes_emitted": 0},
            daily=[],
            timeseries=timeseries,
        )
        assert "2024-06-01 12:00:00" in html
        assert "1.0 KB" in html

    def test_empty_timeseries(self) -> None:
        html = render_bandwidth(
            summary={"total_bytes_received": 0, "total_bytes_emitted": 0},
            daily=[],
            timeseries=[],
        )
        assert "No bandwidth data" in html

    def test_account_filter(self) -> None:
        html = render_bandwidth(
            summary={"total_bytes_received": 0, "total_bytes_emitted": 0},
            daily=[],
            timeseries=[],
            account_filter="acct_a",
        )
        assert 'value="acct_a"' in html

    def test_does_not_load_chart_js(self) -> None:
        html = render_bandwidth(
            summary={"total_bytes_received": 0, "total_bytes_emitted": 0},
            daily=[],
            timeseries=[],
        )
        assert "/static/chart.js" not in html

    def test_cards_have_metric_tooltips(self) -> None:
        html = render_bandwidth(
            summary={"total_bytes_received": 0, "total_bytes_emitted": 0},
            daily=[],
            timeseries=[],
        )
        assert (
            'data-tooltip="Total bytes received from clients by EggPool in the '
            'selected period."'
        ) in html


class TestRenderOverviewBandwidth:
    """Tests for bandwidth integration in overview page."""

    def test_bandwidth_cards_present(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 10,
                    "successful_requests": 8,
                    "error_requests": 2,
                    "error_rate": 0.2,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 2000,
                    "total_cost_microdollars": 1_500_000,
                    "avg_latency_ms": 250.0,
                    "total_bytes_received": 5_000_000,
                    "total_bytes_emitted": 2_500_000,
                },
                "imbalance": {
                    "imbalance_ratio": 0.1,
                    "active_accounts": 2,
                    "most_used": {"name": "acct_a", "cost_microdollars": 1000},
                    "least_used": {"name": "acct_b", "cost_microdollars": 500},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert "Bandwidth received" in html
        assert "Bandwidth emitted" in html
        assert "5.0 MB" in html
        assert "2.5 MB" in html

    def test_token_activity_heatmap_section(self) -> None:
        from datetime import date as _date

        today = _date.today().isoformat()
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
            bandwidth_daily=[
                {"day": today, "total_tokens": 1500},
            ],
        )
        assert "Token activity" in html
        assert "<svg" in html
        assert "1,500" in html  # token-formatted tooltip

    def test_account_table_has_bw_columns(self) -> None:
        accounts = [
            {
                "account_name": "acct_a",
                "account_enabled": 1,
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 200,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 100.0,
                "reserved_microdollars": 500_000,
                "bytes_received": 3_000_000,
                "bytes_emitted": 1_500_000,
            }
        ]
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 5,
                    "successful_requests": 4,
                    "error_requests": 1,
                    "error_rate": 0.2,
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                    "total_cost_microdollars": 1_000_000,
                    "avg_latency_ms": 100.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=accounts,
        )
        assert "BW received" in html
        assert "BW emitted" in html
        assert "3.0 MB" in html
        assert "1.5 MB" in html

    def test_cost_card_subtext_shows_total_tokens(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 10,
                    "successful_requests": 10,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                    "total_tokens": 300,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        assert "in 100" in html
        assert "out 200" in html
        assert "total 300" in html

    def test_throughput_card_present(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 1,
                    "total_input_tokens": 100,
                    "total_output_tokens": 100,
                    "total_tokens": 200,
                    "tokens_per_second": 40.0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        assert "Throughput" in html
        assert "40.0 tok/s" in html

    def test_throughput_card_dash_when_zero(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "tokens_per_second": 0.0,
                    "total_cost_microdollars": 0,
                },
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        assert "Throughput" in html
        # The metric slot itself should hold the dash rather than "0.0 tok/s".
        assert "0.0 tok/s" not in html

    def test_overview_cards_have_metric_tooltips(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 1,
                    "successful_requests": 1,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                    "total_tokens": 300,
                    "tokens_per_second": 10.0,
                    "total_cost_microdollars": 1000,
                    "avg_latency_ms": 50.0,
                },
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        assert 'data-tooltip="Total proxied requests in the selected period.' in html
        assert (
            'data-tooltip="Aggregate token throughput across requests, '
            'computed from total tokens divided by total latency."'
        ) in html


class TestRenderLatency:
    """Tests for the latency page renderer."""

    def test_renders_empty(self) -> None:
        html = render_latency(provider_ttft=[], model_ttft=[], period="24h")
        assert "Latency" in html
        assert "No TTFT data" in html

    def test_renders_provider_cards(self) -> None:
        provider_ttft = [
            {
                "provider_id": "opencode-go",
                "request_count": 100,
                "avg_ttft_ms": 245.0,
                "p50_ttft_ms": 180.0,
                "p99_ttft_ms": 890.0,
            }
        ]
        html = render_latency(provider_ttft=provider_ttft, model_ttft=[], period="24h")
        assert "opencode-go" in html
        assert "245.0 ms" in html
        assert "180.0 ms" in html
        assert "890.0 ms" in html
        assert "100" in html

    def test_renders_model_table(self) -> None:
        model_ttft = [
            {
                "provider_id": "opencode-go",
                "model_id": "gpt-4",
                "request_count": 50,
                "avg_ttft_ms": 200.0,
                "p50_ttft_ms": 150.0,
                "p99_ttft_ms": 700.0,
            }
        ]
        html = render_latency(provider_ttft=[], model_ttft=model_ttft, period="24h")
        assert "gpt-4" in html
        assert "Per-model breakdown" in html
        assert "200.0 ms" in html

    def test_empty_model_table(self) -> None:
        html = render_latency(provider_ttft=[], model_ttft=[], period="24h")
        assert "No model data" in html

    def test_does_not_load_chart_js(self) -> None:
        html = render_latency(provider_ttft=[], model_ttft=[], period="24h")
        assert "/static/chart.js" not in html

    def test_escapes_html_in_provider_id(self) -> None:
        provider_ttft = [
            {
                "provider_id": "<script>alert(1)</script>",
                "request_count": 1,
                "avg_ttft_ms": 100.0,
                "p50_ttft_ms": 100.0,
                "p99_ttft_ms": 100.0,
            }
        ]
        html = render_latency(provider_ttft=provider_ttft, model_ttft=[], period="24h")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_provider_cards_have_tooltips(self) -> None:
        provider_ttft = [
            {
                "provider_id": "opencode-go",
                "request_count": 10,
                "avg_ttft_ms": 100.0,
                "p50_ttft_ms": 80.0,
                "p99_ttft_ms": 300.0,
            }
        ]
        html = render_latency(provider_ttft=provider_ttft, model_ttft=[], period="24h")
        assert 'data-tooltip="Provider TTFT summary.' in html


class TestRenderPings:
    """Tests for the pings page renderer."""

    def test_renders_empty(self) -> None:
        html = render_pings(ping_summary=[], recent_pings=[], period="24h")
        assert "Provider Pings" in html
        assert "No ping data yet" in html
        assert "No pings recorded" in html

    def test_does_not_load_chart_js(self) -> None:
        html = render_pings(ping_summary=[], recent_pings=[], period="24h")
        assert "/static/chart.js" not in html

    def test_renders_provider_cards(self) -> None:
        ping_summary = [
            {
                "provider_id": "opencode-go",
                "avg_latency_ms": 142,
                "success_rate": 100.0,
                "last_model_count": 47,
            }
        ]
        html = render_pings(ping_summary=ping_summary, recent_pings=[], period="24h")
        assert "opencode-go" in html
        assert "142.0 ms" in html
        assert "healthy" in html
        assert "100.0% success" in html

    def test_provider_cards_have_tooltips(self) -> None:
        ping_summary = [
            {
                "provider_id": "opencode-go",
                "avg_latency_ms": 142,
                "success_rate": 100.0,
                "last_model_count": 47,
            }
        ]
        html = render_pings(ping_summary=ping_summary, recent_pings=[], period="24h")
        assert 'data-tooltip="Provider ping latency summary.' in html

    def test_degraded_provider(self) -> None:
        ping_summary = [
            {
                "provider_id": "bad-provider",
                "avg_latency_ms": 500,
                "success_rate": 50.0,
                "last_model_count": 0,
            }
        ]
        html = render_pings(ping_summary=ping_summary, recent_pings=[], period="24h")
        assert "degraded" in html

    def test_renders_recent_pings_table(self) -> None:
        recent_pings = [
            {
                "provider_id": "opencode-go",
                "account_name": "acct1",
                "probed_at": "2024-01-01 12:00:00",
                "latency_ms": 142,
                "status_code": 200,
                "model_count": 47,
                "error": None,
            }
        ]
        html = render_pings(ping_summary=[], recent_pings=recent_pings, period="24h")
        assert "acct1" in html
        assert "142.0 ms" in html
        assert "200" in html
        assert "Recent pings" in html
        assert "No ping data yet" not in html

    def test_no_empty_message_when_pings_exist_outside_range(self) -> None:
        """When ping_summary is empty (outside time range) but recent_pings
        has data, the misleading 'No ping data yet' message must not appear."""
        recent_pings = [
            {
                "provider_id": "opencode-go",
                "account_name": "default",
                "probed_at": "2024-01-01 12:00:00",
                "latency_ms": 100,
                "status_code": 200,
                "model_count": 10,
                "error": None,
            },
            {
                "provider_id": "anthropic",
                "account_name": "default",
                "probed_at": "2024-01-01 11:00:00",
                "latency_ms": 200,
                "status_code": 200,
                "model_count": 20,
                "error": None,
            },
        ]
        html = render_pings(ping_summary=[], recent_pings=recent_pings, period="24h")
        assert "No ping data yet" not in html
        assert "No pings recorded yet" not in html
        assert "Recent pings" in html
        assert "opencode-go" in html
        assert "anthropic" in html

    def test_escapes_html_in_ping_data(self) -> None:
        recent_pings = [
            {
                "provider_id": "<script>xss</script>",
                "account_name": "acct1",
                "probed_at": "2024-01-01 12:00:00",
                "latency_ms": 100,
                "status_code": 200,
                "model_count": 5,
                "error": None,
            }
        ]
        html = render_pings(ping_summary=[], recent_pings=recent_pings, period="24h")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderOverviewTTFT:
    """Tests for TTFT cards on the overview page."""

    def test_ttft_cards_present(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 10,
                    "successful_requests": 8,
                    "error_requests": 2,
                    "error_rate": 0.2,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 2000,
                    "total_cost_microdollars": 1_500_000,
                    "avg_latency_ms": 250.0,
                    "avg_ttft_ms": 245.0,
                    "p50_ttft_ms": 180.0,
                    "p99_ttft_ms": 890.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.1,
                    "active_accounts": 2,
                    "most_used": {"name": "acct_a", "cost_microdollars": 1000},
                    "least_used": {"name": "acct_b", "cost_microdollars": 500},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert "Avg TTFT (streamed)" in html
        assert "245.0 ms" in html
        assert "180.0 ms" in html
        assert "890.0 ms" in html

    def test_ttft_zero_when_no_data(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                    "avg_ttft_ms": 0.0,
                    "p50_ttft_ms": 0.0,
                    "p99_ttft_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
        )
        assert "Avg TTFT (streamed)" in html
        assert "0.0 ms" in html


class TestRenderOverviewProviderHealth:
    """Tests for the provider health section on the overview page."""

    def test_provider_health_section_present(self) -> None:
        ping_summary = [
            {
                "provider_id": "opencode-go",
                "avg_latency_ms": 142,
                "success_rate": 100.0,
                "last_model_count": 47,
                "last_ping_at": "2024-01-01 12:00:00",
            }
        ]
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 10,
                    "successful_requests": 8,
                    "error_requests": 2,
                    "error_rate": 0.2,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 2000,
                    "total_cost_microdollars": 1_500_000,
                    "avg_latency_ms": 250.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.1,
                    "active_accounts": 2,
                    "most_used": {"name": "acct_a", "cost_microdollars": 1000},
                    "least_used": {"name": "acct_b", "cost_microdollars": 500},
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
            ping_summary=ping_summary,
        )
        assert "Provider health" in html
        assert "opencode-go" in html
        assert "healthy" in html
        assert "142.0 ms" in html
        assert "100.0%" in html

    def test_no_provider_health_when_empty(self) -> None:
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
            ping_summary=[],
        )
        assert "Provider health" not in html

    def test_degraded_provider_health(self) -> None:
        ping_summary = [
            {
                "provider_id": "bad-provider",
                "avg_latency_ms": 500,
                "success_rate": 50.0,
                "last_model_count": 0,
                "last_ping_at": "2024-01-01 12:00:00",
            }
        ]
        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 5,
                    "successful_requests": 3,
                    "error_requests": 2,
                    "error_rate": 0.4,
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                    "total_cost_microdollars": 500_000,
                    "avg_latency_ms": 200.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 1,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2024-01-01 00:00:00",
                "end": "2024-01-02 00:00:00",
            },
            accounts=[],
            ping_summary=ping_summary,
        )
        assert "degraded" in html
        assert "bad-provider" in html


class TestDashboardStylesheet:
    """Tests for the dashboard CSS file."""

    @staticmethod
    def _load_css() -> str:
        from pathlib import Path

        css_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "eggpool"
            / "dashboard"
            / "static"
            / "dashboard.css"
        )
        return css_path.read_text(encoding="utf-8")

    def test_egg_background_is_fixed_and_above_gradient(self) -> None:
        css = self._load_css()
        # The egg must be a fixed-position watermark, not scroll with content
        assert ".egg-background" in css
        assert "position: fixed" in css
        assert "pointer-events: none" in css
        # The egg has a soft fill body, a heartbeat line, and two endpoint dots
        assert ".shape" in css
        assert ".thin" in css
        assert "fill:" in css
        assert "stroke:" in css
        assert "non-scaling-stroke" in css
        # No legacy or unsupported classes
        assert ".egg-shell" not in css
        assert ".egg-crack" not in css
        # Egg color must track the theme via CSS variables (no hardcoded hex)
        assert "var(--page-bg)" in css
        assert "var(--page-text)" in css
        assert "color-mix" in css

    def test_body_has_gradient_and_fixed_background(self) -> None:
        css = self._load_css()
        # Subtle vertical gradient instead of a flat background color
        assert "linear-gradient(" in css
        assert "background-attachment: fixed" in css

    def test_content_sits_above_egg_watermark(self) -> None:
        css = self._load_css()
        # The topbar, main, and footer must be positioned above the egg
        for selector in ("header.topbar", "main", "footer"):
            assert selector in css
        assert "z-index: 1" in css
        assert "position: relative" in css

    def test_timeseries_period_selector_matches_control_style(self) -> None:
        css = self._load_css()
        assert ".timeseries-period-selector label" in css
        assert ".timeseries-period-selector label > select" in css
        assert ".period-selector select," in css


class TestTooltipDateFormat:
    """Tests for the ``_format_tooltip_date`` helper."""

    def test_format_known_iso_date(self) -> None:
        assert _format_tooltip_date("2026-03-05") == "Thu, Mar 5 2026"

    def test_format_first_of_month(self) -> None:
        assert _format_tooltip_date("2024-01-01") == "Mon, Jan 1 2024"

    def test_format_end_of_month(self) -> None:
        assert _format_tooltip_date("2024-12-31") == "Tue, Dec 31 2024"

    def test_format_invalid_string_returned_unchanged(self) -> None:
        assert _format_tooltip_date("not-a-date") == "not-a-date"

    def test_format_empty_string_returned_unchanged(self) -> None:
        assert _format_tooltip_date("") == ""


class TestHeatmapTooltipSystem:
    """Tests for the styled CSS tooltip system on the bandwidth heatmap."""

    @staticmethod
    def _today() -> str:
        from datetime import date as _date

        return _date.today().isoformat()

    def test_renders_data_tooltip(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 7,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        assert "data-tooltip=" in html
        assert "aria-label=" in html
        assert 'class="heatmap-hitbox"' in html

    def test_tooltip_text_includes_request_count(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1500,
                "bytes_emitted": 500,
                "request_count": 23,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        assert "23 requests" in html
        assert "1.5 KB in" in html
        assert "500 B out" in html

    def test_tooltip_text_includes_token_count_for_total_tokens(self) -> None:
        daily = [{"day": self._today(), "total_tokens": 1204, "request_count": 23}]
        html = _render_bandwidth_heatmap(daily, value_field="total_tokens")
        assert "1,204 tokens" in html
        assert "23 requests" in html

    def test_tooltip_singular_request_when_count_is_one(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 1,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        assert "1 request" in html
        assert "1 requests" not in html

    def test_tooltip_text_includes_pretty_date(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 5,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        from datetime import date as _date

        pretty = _format_tooltip_date(_date.today().isoformat())
        assert pretty in html

    def test_overlay_grid_geometry(self) -> None:
        from datetime import date as _date
        from datetime import timedelta as _td

        today = _date.today()
        daily = [
            {
                "day": (today - _td(days=i)).isoformat(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 1,
            }
            for i in range(5)
        ]
        html = _render_bandwidth_heatmap(daily)
        assert html.count('class="heatmap-overlay"') == 1
        # The heatmap renders the full 90-day window even with sparse data;
        # hitboxes are emitted once per (week, day_of_week) grid slot —
        # both visible days (with data-tooltip) and out-of-range padding
        # days (empty hitbox) — so the column-major grid flow stays aligned
        # with the SVG cell coordinates.
        assert html.count('class="heatmap-hitbox"') >= 5
        assert html.count('class="heatmap-overlay"') == 1

    def test_overlay_grid_full_window_geometry(self) -> None:
        from datetime import date as _date
        from datetime import timedelta as _td

        today = _date.today()
        n_days = 90
        daily = [
            {
                "day": (today - _td(days=i)).isoformat(),
                "bytes_received": 100,
                "bytes_emitted": 50,
                "request_count": 1,
            }
            for i in range(n_days)
        ]
        html = _render_bandwidth_heatmap(daily)
        assert html.count('class="heatmap-overlay"') == 1
        # One hitbox per (week, day_of_week) grid slot — visible days
        # carry data-tooltip, out-of-range days (before start_date or
        # after today) carry no data-tooltip.  The exact count is
        # num_weeks * 7, where num_weeks depends on the weekday of
        # `today - 89 days`.
        start_date = today - _td(days=89)
        padding_days = (start_date.weekday() + 1) % 7
        grid_start = start_date - _td(days=padding_days)
        expected_grid = ((today - grid_start).days // 7 + 1) * 7
        assert html.count('class="heatmap-hitbox"') == expected_grid
        assert html.count("data-tooltip=") == n_days

    def test_overlay_uses_dynamic_week_count(self) -> None:
        """The overlay exposes the week count via ``--heatmap-weeks`` so
        the CSS grid columns can grow to 14 when the 90-day window
        rounds up across two Sunday boundaries (5 out of 7 days of the
        week).  Previously a hardcoded 13-column template left the
        current week without hitboxes."""
        from datetime import date as _date

        today = _date.today()
        html = _render_bandwidth_heatmap(
            [
                {
                    "day": today.isoformat(),
                    "bytes_received": 100,
                    "bytes_emitted": 50,
                    "request_count": 1,
                }
            ]
        )
        import re

        match = re.search(r"--heatmap-weeks:\s*(\d+)", html)
        assert match is not None, "expected --heatmap-weeks on overlay"
        weeks = int(match.group(1))
        assert weeks in (13, 14)
        # 90-day window always renders either 13 or 14 weeks.
        assert weeks >= 13

    def test_fourteen_week_heatmap_aligns_hitboxes_with_svg(self) -> None:
        """Regression: when the 90-day window crosses a Sunday boundary
        and rounds up to 14 weeks (true on Sun/Mon/Tue/Wed/Thu), the
        overlay's CSS grid must expose enough columns for every SVG
        cell to have a hitbox at the matching row/column — otherwise
        the tooltip for the most recent day(s) drifts to whatever
        grid slot the column overflow happens to land in."""
        from datetime import date as _date
        from datetime import timedelta as _td

        from eggpool.dashboard import render as _render_module

        # Force "today" to a Sunday so the 90-day window produces 14
        # weeks (5 out of 7 days of the week hit this case).
        sunday = _date(2026, 6, 28)
        assert sunday.weekday() == 6  # Sunday in Mon=0 convention

        daily = [
            {
                "day": (sunday - _td(days=i)).isoformat(),
                "bytes_received": 100,
                "bytes_emitted": 50,
                "request_count": 1,
            }
            for i in range(90)
        ]

        class _FrozenDate(_date):
            @classmethod
            def today(cls) -> _date:
                return sunday

        original_date = _render_module.date
        _render_module.date = _FrozenDate  # type: ignore[assignment]
        try:
            html = _render_bandwidth_heatmap(daily)
        finally:
            _render_module.date = original_date  # type: ignore[assignment]

        import re

        weeks_match = re.search(r"--heatmap-weeks:\s*(\d+)", html)
        assert weeks_match is not None
        assert int(weeks_match.group(1)) == 14

        # Every visible SVG cell must have a hitbox with a data-tooltip
        # in the DOM, and the total hitbox count must match the grid
        # size (14 weeks * 7 days = 98).
        rect_count = html.count('class="heatmap-cell"')
        data_tooltip_count = html.count("data-tooltip=")
        hitbox_count = html.count('class="heatmap-hitbox"')
        assert rect_count == 90
        assert data_tooltip_count == 90
        assert hitbox_count == 98  # 14 * 7

        # The Sunday at the end of the window must have its tooltip
        # reachable; the previous bug orphaned it because the overlay
        # only had 13 columns and the 14th SVG column had no hitbox.
        assert sunday.isoformat() in html

    def test_empty_hitboxes_for_out_of_range_days(self) -> None:
        """Days before start_date or after today still get a hitbox
        so the column-major grid flow doesn't shift visible hitboxes
        into the wrong (week, day_of_week) slot.  Empty hitboxes have
        no data-tooltip so they fire no tooltip on hover."""
        from datetime import date as _date
        from datetime import timedelta as _td

        from eggpool.dashboard import render as _render_module

        # Pick a Sunday-to-Sunday window so we know exactly which
        # days are inside vs. outside the visible range.
        today = _date(2026, 6, 28)  # Sunday
        start_date = today - _td(days=89)
        daily = [
            {
                "day": (today - _td(days=i)).isoformat(),
                "bytes_received": 100,
                "bytes_emitted": 50,
                "request_count": 1,
            }
            for i in range(90)
        ]

        class _FrozenDate(_date):
            @classmethod
            def today(cls) -> _date:
                return today

        original_date = _render_module.date
        _render_module.date = _FrozenDate  # type: ignore[assignment]
        try:
            html = _render_bandwidth_heatmap(daily)
        finally:
            _render_module.date = original_date  # type: ignore[assignment]

        # Out-of-range days: those before start_date AND after today
        # in the 14-week grid.
        out_of_range = 0
        grid_start = start_date - _td(days=(start_date.weekday() + 1) % 7)
        for w in range(((today - grid_start).days // 7) + 1):
            for d in range(7):
                cell = grid_start + _td(weeks=w, days=d)
                if cell < start_date or cell > today:
                    out_of_range += 1
        assert out_of_range == 8  # 2 in week 0, 6 in week 13

        # Empty hitboxes are rendered as <div class="heatmap-hitbox"></div>
        # with no attributes other than the class.
        empty_hitbox_count = html.count('<div class="heatmap-hitbox"></div>')
        assert empty_hitbox_count == out_of_range

    def test_pointer_events_none_on_heatmap_rect(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 1,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        assert 'pointer-events="none"' in html

    def test_tooltip_attribute_uses_html_escape(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 1,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        # The hitbox attributes must be present and well-formed.
        assert 'data-tooltip="' in html
        assert 'aria-label="' in html

    def test_heatmap_contains_overlay_after_svg(self) -> None:
        daily = [
            {
                "day": self._today(),
                "bytes_received": 1000,
                "bytes_emitted": 500,
                "request_count": 1,
            }
        ]
        html = _render_bandwidth_heatmap(daily)
        svg_pos = html.index("<svg")
        overlay_pos = html.index('class="heatmap-overlay"')
        assert svg_pos < overlay_pos


class TestTooltipStylesheet:
    """Tests for the tooltip CSS rules."""

    @staticmethod
    def _load_css() -> str:
        from pathlib import Path

        css_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "eggpool"
            / "dashboard"
            / "static"
            / "dashboard.css"
        )
        return css_path.read_text(encoding="utf-8")

    def test_data_tooltip_attribute_selector_present(self) -> None:
        css = self._load_css()
        assert "[data-tooltip]" in css

    def test_data_tooltip_uses_theme_css_vars(self) -> None:
        css = self._load_css()
        assert "var(--card-bg)" in css
        assert "var(--card-border)" in css
        assert "var(--page-text)" in css

    def test_data_tooltip_supports_bottom_position(self) -> None:
        css = self._load_css()
        assert 'data-tooltip-pos="bottom"' in css

    def test_data_tooltip_includes_reduced_motion(self) -> None:
        css = self._load_css()
        assert "prefers-reduced-motion" in css

    def test_heatmap_rect_pointer_events_none(self) -> None:
        css = self._load_css()
        assert ".heatmap rect" in css
        assert "pointer-events: none" in css

    def test_heatmap_container_is_relative(self) -> None:
        css = self._load_css()
        assert ".heatmap" in css
        assert "position: relative" in css

    def test_heatmap_overlay_grid_geometry(self) -> None:
        """The overlay grid uses a custom property so the column count
        grows to 14 when the 90-day window rounds up across two
        Sunday boundaries.  Hardcoding ``repeat(13, …)`` would leave
        the most recent week orphaned (5 out of 7 days of the week)."""
        css = self._load_css()
        assert ".heatmap-overlay" in css
        assert "var(--heatmap-weeks, 13)" in css
        assert "repeat(7, 13px)" in css

    def test_heatmap_overlay_uses_column_auto_flow(self) -> None:
        """Hitboxes are appended in column-major order (outer loop = week,
        inner loop = day-of-week) so the overlay grid must flow by column;
        otherwise the rows/columns get transposed and tooltips misfire."""
        css = self._load_css()
        assert ".heatmap-overlay" in css
        assert "grid-auto-flow: column" in css

    def test_tooltip_pointer_events_none(self) -> None:
        css = self._load_css()
        assert "pointer-events: none" in css


class TestRenderReliability:
    """Tests for the Reliability page renderer."""

    def test_renders_empty_state(self) -> None:
        html = render_reliability(
            period="24h",
            attempt_stats=None,
            retry_distribution=[],
            pending_health=None,
            operational_summary=[],
            recent_operational_events=[],
            timeseries=[],
        )
        assert "Reliability" in html
        assert "/static/chart.js" in html
        assert "Attempts by provider" in html
        assert "No attempt data" in html

    def test_renders_attempt_summary_cards(self) -> None:
        html = render_reliability(
            period="24h",
            attempt_stats={
                "total_attempts": 100,
                "success_attempts": 80,
                "retry_attempts": 15,
                "failed_attempts": 5,
                "retry_rate": 0.15,
                "avg_attempt_latency_ms": 250.0,
            },
            retry_distribution=[
                {
                    "retry_category": "transient",
                    "attempt_count": 20,
                    "retry_outcome_count": 10,
                    "success_count": 15,
                    "failure_count": 5,
                    "avg_attempt_latency_ms": 200.0,
                }
            ],
            pending_health={
                "pending_count": 3,
                "oldest_pending_age_seconds": 42,
                "stale_pending_count": 0,
                "active_reservation_count": 1,
                "active_reserved_microdollars": 100_000,
                "oldest_reservation_age_seconds": 12,
            },
            operational_summary=[
                {
                    "event_type": "stale_request_finalizer",
                    "event_count": 4,
                    "last_occurred_at": "2024-01-01 12:00:00",
                    "total_interrupted_requests": 4,
                    "total_released_reservations": 0,
                }
            ],
            recent_operational_events=[
                {
                    "event_type": "crash_recovery",
                    "details_json": '{"leaked_requests": 2}',
                    "occurred_at": "2024-01-01 12:00:00",
                }
            ],
            timeseries=[],
        )
        assert "Total attempts" in html
        assert "100" in html
        assert "Retry attempts" in html
        assert "Pending requests" in html
        assert "Operational events" in html
        assert "crash_recovery" in html
        assert "Transient upstream" in html
        assert 'data-tooltip="Total upstream attempts in the selected period' in html
        assert 'data-tooltip="Explanation of the pending-request snapshot' in html

    def test_pending_health_warning_when_stale(self) -> None:
        html = render_reliability(
            period="24h",
            attempt_stats=None,
            retry_distribution=[],
            pending_health={
                "pending_count": 5,
                "oldest_pending_age_seconds": 1800,
                "stale_pending_count": 3,
                "active_reservation_count": 0,
                "active_reserved_microdollars": 0,
            },
            operational_summary=[],
            recent_operational_events=[],
            timeseries=[],
        )
        assert 'class="card warning"' in html

    def test_attempts_chart_uses_data_island(self) -> None:
        """The ``Attempts by provider`` chart must be seeded from an
        inlined JSON data island, not an inline ``new Chart(...)`` script
        that would race the deferred Chart.js load and leave the canvas
        empty (``Chart is not defined``).
        """
        html = render_reliability(
            period="24h",
            attempt_stats={
                "total_attempts": 10,
                "success_attempts": 8,
                "retry_attempts": 1,
                "failed_attempts": 1,
            },
            retry_distribution=[],
            pending_health=None,
            operational_summary=[],
            recent_operational_events=[],
            timeseries=[],
        )
        assert 'id="reliability-attempts-by-provider"' in html
        assert (
            re.search(
                r'<script type="application/json"\s+class="static-chart-data"\s+'
                r'data-chart-id="reliability-attempts-by-provider">',
                html,
            )
            is not None
        )
        assert "new Chart(" not in html
        assert "Success" in html
        assert "Failed" in html


class TestRenderRouting:
    """Tests for the Routing page renderer."""

    def test_renders_empty_state(self) -> None:
        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
        )
        assert "Routing" in html
        assert "/static/chart.js" in html
        assert "No routing decisions" in html

    def test_renders_populated(self) -> None:
        html = render_routing(
            period="24h",
            routing_distribution=[
                {
                    "model_id": "gpt-x",
                    "provider_id": "opencode-go",
                    "decision_count": 50,
                    "avg_eligible_count": 2.5,
                    "avg_scored_count": 2.0,
                    "avg_attempted_excluded_count": 0.5,
                    "avg_selected_score": 0.95,
                    "distinct_selected_accounts": 3,
                }
            ],
            routing_selection_breakdown=[
                {
                    "account_name": "alpha",
                    "provider_id": "opencode-go",
                    "selection_count": 30,
                    "avg_selected_tier": 1.0,
                    "avg_selected_score": 0.97,
                    "avg_eligible_count": 2.5,
                }
            ],
            routing_exclusion_breakdown=[
                {
                    "account_name": "alpha",
                    "reason": "quota_exhausted_backoff",
                    "exclusion_count": 7,
                    "last_seen_at": "2024-01-01 12:00:00",
                },
                {
                    "account_name": "beta",
                    "reason": "active_reservation_pressure",
                    "exclusion_count": 3,
                    "last_seen_at": "2024-01-01 12:00:00",
                },
            ],
        )
        assert "Routing decisions" in html
        assert "gpt-x" in html
        assert "opencode-go" in html
        assert "alpha" in html
        assert "quota_exhausted_backoff" in html
        assert "suppressive" in html
        assert "advisory" in html
        assert (
            'data-tooltip="Total routing decisions recorded in the selected period."'
            in html
        )

    def test_exclusion_category_coloring(self) -> None:
        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[
                {
                    "account_name": "alpha",
                    "reason": "rate_limit_backoff",
                    "exclusion_count": 5,
                },
                {
                    "account_name": "beta",
                    "reason": "low_provider_priority",
                    "exclusion_count": 2,
                },
                {
                    "account_name": "gamma",
                    "reason": "unknown_reason",
                    "exclusion_count": 1,
                },
            ],
        )
        assert "suppressive" in html
        assert "advisory" in html
        assert "unknown" in html

    def test_exclusion_chart_uses_data_island(self) -> None:
        """The ``Exclusion taxonomy`` doughnut must be seeded from an
        inlined JSON data island, not an inline ``new Chart(...)`` script
        that would race the deferred Chart.js load and leave the canvas
        empty (``Chart is not defined``).
        """
        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[
                {
                    "account_name": "alpha",
                    "reason": "rate_limit_backoff",
                    "exclusion_count": 5,
                },
            ],
        )
        assert 'id="routing-exclusion-taxonomy"' in html
        assert (
            re.search(
                r'<script type="application/json"\s+class="static-chart-data"\s+'
                r'data-chart-id="routing-exclusion-taxonomy">',
                html,
            )
            is not None
        )
        assert "new Chart(" not in html

    def test_exclusion_chart_empty_state(self) -> None:
        """When no exclusions have been recorded the doughnut must be
        suppressed entirely.  Chart.js v4 renders an empty ring with the
        legend visible when ``data`` is all zeros, which manifests as a
        ``key but no graph`` artefact in the panel.  The renderer should
        instead emit the same empty-state paragraph the table uses so
        both panels stay consistent.
        """
        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
        )
        assert 'id="routing-exclusion-taxonomy"' not in html
        assert '<p class="empty">No exclusion data in this period.</p>' in html
        assert 'class="static-chart-data"' not in html

    def test_exclusion_chart_classifies_circuit_breaker(self) -> None:
        """``circuit_breaker`` is the only reason the coordinator writes
        to ``exclude_reasons_json`` (see ``request/coordinator.py``), so
        it must map to the ``suppressive`` bucket — otherwise every
        real-world exclusion silently lands in the ``unknown`` bucket.
        """
        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[
                {
                    "account_name": "alpha",
                    "reason": "circuit_breaker",
                    "exclusion_count": 4,
                },
            ],
        )
        match = re.search(
            r'<script type="application/json"\s+class="static-chart-data"\s+'
            r'data-chart-id="routing-exclusion-taxonomy">(?P<payload>.*?)</script>',
            html,
            re.DOTALL,
        )
        assert match is not None
        payload = json.loads(match.group("payload"))
        dataset = payload["datasets"][0]
        data = dataset["data"]
        labels = payload["labels"]
        suppressive_index = labels.index("Suppressive")
        advisory_index = labels.index("Advisory")
        unknown_index = labels.index("Unknown")
        assert data[suppressive_index] == 4
        assert data[advisory_index] == 0
        assert data[unknown_index] == 0


class TestRenderTraces:
    """Tests for the Traces page renderer."""

    def test_renders_empty(self) -> None:
        html = render_traces(
            period="recent",
            limit=50,
            recent_requests=[],
        )
        assert "Traces" in html
        assert "No recent requests" in html
        assert "Auth-gated" in html

    def test_renders_request_row_without_sensitive_fields(self) -> None:
        html = render_traces(
            period="recent",
            limit=50,
            recent_requests=[
                {
                    "started_at": "2024-01-01 12:00:00",
                    "account_name": "alpha",
                    "provider_id": "opencode-go",
                    "model_id": "gpt-x",
                    "protocol": "openai",
                    "status": "completed",
                    "status_code": 200,
                    "error_class": None,
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "upstream_latency_ms": 250.0,
                    "proxy_request_id": "abcdef1234567890",
                    "client_ip": "1.2.3.4",
                    "error_detail": "secret upstream detail",
                }
            ],
        )
        assert "alpha" in html
        assert "opencode-go" in html
        assert "completed (200)" in html
        assert "gpt-x" in html
        assert "abcdef12" in html
        # Never leak client_ip or error_detail
        assert "1.2.3.4" not in html
        assert "secret upstream detail" not in html

    def test_renders_error_class_only(self) -> None:
        html = render_traces(
            period="recent",
            limit=50,
            recent_requests=[
                {
                    "started_at": "2024-01-01 12:00:00",
                    "account_name": "alpha",
                    "provider_id": "opencode-go",
                    "model_id": "gpt-x",
                    "protocol": "openai",
                    "status": "error",
                    "status_code": 429,
                    "error_class": "RateLimitError",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "upstream_latency_ms": 0.0,
                }
            ],
        )
        assert "RateLimitError" in html
        assert "error (429)" in html

    def test_does_not_load_chart_js(self) -> None:
        html = render_traces(
            period="recent",
            limit=50,
            recent_requests=[],
        )
        assert "/static/chart.js" not in html


class TestRenderLatencyExtension:
    """Tests for the new latency-phases feature on the latency page."""

    def test_renders_phases_chart_when_data(self) -> None:
        html = render_latency(
            provider_ttft=[],
            model_ttft=[],
            period="24h",
            phases={
                "phases": {
                    "upstream_connect_ms": {
                        "sample_count": 5,
                        "avg_ms": 50.0,
                        "p50_ms": 40.0,
                        "p99_ms": 90.0,
                    },
                    "upstream_read_ms": {
                        "sample_count": 5,
                        "avg_ms": 200.0,
                        "p50_ms": 180.0,
                        "p99_ms": 400.0,
                    },
                    "coordinator_overhead_ms": {
                        "sample_count": 5,
                        "avg_ms": 25.0,
                        "p50_ms": 20.0,
                        "p99_ms": 50.0,
                    },
                }
            },
        )
        assert "latency-phases" in html
        assert "Latency phases" in html
        assert "/static/chart.js" in html
        assert (
            re.search(
                r'<script type="application/json"\s+class="static-chart-data"\s+'
                r'data-chart-id="latency-phases">',
                html,
            )
            is not None
        )
        assert "new Chart(" not in html

    def test_no_chart_when_phases_empty(self) -> None:
        html = render_latency(
            provider_ttft=[],
            model_ttft=[],
            period="24h",
            phases={"phases": {}},
        )
        assert "latency-phases" not in html
        assert "/static/chart.js" not in html

    def test_no_chart_when_phases_missing(self) -> None:
        html = render_latency(
            provider_ttft=[],
            model_ttft=[],
            period="24h",
        )
        assert "latency-phases" not in html
        assert "/static/chart.js" not in html

    def test_phases_column_in_model_table(self) -> None:
        html = render_latency(
            provider_ttft=[],
            model_ttft=[
                {
                    "provider_id": "opencode-go",
                    "model_id": "gpt-x",
                    "request_count": 10,
                    "avg_ttft_ms": 200.0,
                    "p50_ttft_ms": 150.0,
                    "p99_ttft_ms": 700.0,
                    "phase_connect_ms": 30.0,
                    "phase_read_ms": 200.0,
                    "phase_overhead_ms": 25.0,
                }
            ],
            period="24h",
            phases={
                "phases": {"upstream_connect_ms": {"sample_count": 1, "avg_ms": 30.0}}
            },
        )
        assert '<th data-priority="3">Phases ms (c/r/o)</th>' in html
        assert "30/200/25" in html


class TestRenderAccountsExtension:
    """Tests for the new exactness columns on the accounts page."""

    def test_exactness_columns_rendered(self) -> None:
        accounts = [
            {
                "account_name": "alpha",
                "account_enabled": 1,
                "provider_id": "opencode-go",
                "request_count": 10,
                "error_count": 0,
                "input_tokens": 100,
                "output_tokens": 200,
                "total_tokens": 300,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 100.0,
                "reserved_microdollars": 0,
                "exact_count": 5,
                "derived_count": 2,
                "estimated_count": 1,
                "unknown_count": 2,
                "estimated_cost_fraction": 0.1,
                "cache_read_ratio": 0.25,
                "cache_write_ratio": 0.05,
                "reasoning_output_ratio": 0.1,
                "avg_cost_per_request": 100_000,
                "avg_cost_per_1k_tokens": 333.0,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert '<th data-priority="2">Exactness</th>' in html
        assert 'class="exactness-badge' in html
        assert "e:5,d:2,p:0,~:1,?:2" in html
        assert '<th data-priority="3">Est. cost</th>' in html
        assert '<th data-priority="3">Cache R</th>' in html
        assert '<th data-priority="3">Cache W</th>' in html
        assert '<th data-priority="3">Reasoning</th>' in html
        assert '<th data-priority="3">Avg cost/req</th>' in html
        assert '<th data-priority="3">Avg cost/1k tok</th>' in html
        assert "25.0%" in html  # cache_read_ratio
        assert "5.0%" in html  # cache_write_ratio

    def test_exactness_columns_dash_when_missing(self) -> None:
        accounts = [
            {
                "account_name": "alpha",
                "account_enabled": 1,
                "provider_id": "opencode-go",
                "request_count": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_microdollars": 0,
                "avg_latency_ms": 0.0,
                "reserved_microdollars": 0,
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        assert "exactness-badge" in html
        assert "—" in html  # placeholder for missing ratios


class TestRenderModelsExtension:
    """Tests for the new exactness columns on the models page."""

    def test_exactness_columns_rendered(self) -> None:
        models = [
            {
                "model_id": "gpt-x",
                "provider_id": "opencode-go",
                "request_count": 5,
                "error_count": 0,
                "input_tokens": 100,
                "output_tokens": 200,
                "total_tokens": 300,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 200.0,
                "avg_ttft_ms": 50.0,
                "tokens_per_second": 50.0,
                "exact_count": 3,
                "derived_count": 1,
                "estimated_count": 1,
                "unknown_count": 0,
                "estimated_cost_fraction": 0.2,
                "cache_read_ratio": 0.4,
                "cache_write_ratio": 0.1,
                "reasoning_output_ratio": 0.0,
                "avg_cost_per_request": 200_000,
                "avg_cost_per_1k_tokens": 500.0,
            }
        ]
        html = render_models(models=models, period="24h")
        assert '<th data-priority="1">Exactness</th>' in html
        assert 'class="exactness-badge' in html
        assert "e:3,d:1,p:0,~:1,?:0" in html
        assert "40.0%" in html
        assert "10.0%" in html


class TestRenderOverviewSystemHealth:
    """Tests for the overview System Health row."""

    def test_renders_when_data_present(self) -> None:
        html = _render_system_health(
            pending_health={
                "pending_count": 2,
                "oldest_pending_age_seconds": 30,
                "stale_pending_count": 0,
                "active_reservation_count": 1,
                "active_reserved_microdollars": 50_000,
            },
            attempt_stats={
                "total_attempts": 100,
                "success_attempts": 95,
                "retry_rate": 0.05,
            },
            operational_summary=[
                {
                    "event_type": "stale_request_finalizer",
                    "event_count": 2,
                }
            ],
        )
        assert "System Health" not in html  # the title is in section.cards
        assert "Pending requests" in html
        assert "Active reservations" in html
        assert "Finalizer" in html

    def test_warning_when_stale_pending(self) -> None:
        html = _render_system_health(
            pending_health={
                "pending_count": 3,
                "oldest_pending_age_seconds": 1800,
                "stale_pending_count": 2,
                "active_reservation_count": 0,
                "active_reserved_microdollars": 0,
            },
            attempt_stats=None,
            operational_summary=None,
        )
        assert 'class="card warning"' in html

    def test_empty_returns_empty(self) -> None:
        assert _render_system_health(None, None, None) == ""


class TestRenderNavUpdated:
    """Tests for the new reliability/routing/traces nav entries."""

    def test_reliability_link_present(self) -> None:
        html = _render_nav("overview", "24h")
        assert "/reliability" in html
        assert "Reliability" in html

    def test_routing_link_present(self) -> None:
        html = _render_nav("overview", "24h")
        assert "/routing" in html
        assert "Routing" in html

    def test_traces_link_present(self) -> None:
        html = _render_nav("overview", "24h")
        assert "/traces" in html
        assert "Traces" in html

    def test_active_routing_highlighted(self) -> None:
        html = _render_nav("routing", "24h")
        assert html.count('class="active"') == 1
        assert 'href="/routing' in html


class TestHamburgerNav:
    """Mobile navigation: burger button toggles a vertical dropdown menu.

    On viewports ≥761px the burger is hidden via CSS and the 12 page
    links render inline inside `.topnav-menu`.  On narrower viewports
    the burger is visible and the menu opens when JS toggles
    `.topnav-open` on the ancestor `nav.topnav`.  Theme selector and
    refresh button stay outside the menu so they remain reachable on
    every viewport.
    """

    def test_burger_button_with_inline_svg_present(self) -> None:
        html = _render_nav("overview", "24h")
        assert '<button class="topnav-burger"' in html
        assert 'class="topnav-burger-icon"' in html
        assert 'class="bar bar-1"' in html
        assert 'class="bar bar-2"' in html
        assert 'class="bar bar-3"' in html

    def test_burger_initial_aria_state(self) -> None:
        html = _render_nav("overview", "24h")
        burger_open = html.find('<button class="topnav-burger"')
        assert burger_open != -1
        assert 'aria-expanded="false"' in html[burger_open:]
        assert 'aria-controls="topnav-menu"' in html

    def test_topnav_menu_lives_after_burger(self) -> None:
        html = _render_nav("overview", "24h")
        burger_pos = html.find('<button class="topnav-burger"')
        menu_open_pos = html.find('<div class="topnav-menu" id="topnav-menu">')
        assert burger_pos != -1
        assert menu_open_pos > burger_pos
        assert menu_open_pos != -1

    def test_theme_selector_outside_menu(self) -> None:
        html = _render_nav("overview", "24h", available_themes=["dark"])
        menu_close = html.find("</div>", html.find('<div class="topnav-menu"'))
        theme_pos = html.find("theme-selector")
        assert menu_close != -1
        assert theme_pos > menu_close

    def test_refresh_button_outside_menu(self) -> None:
        html = _render_nav("overview", "24h")
        menu_close = html.find("</div>", html.find('<div class="topnav-menu"'))
        refresh_pos = html.find("topnav-refresh")
        assert menu_close != -1
        assert refresh_pos > menu_close

    def test_all_page_links_inside_menu(self) -> None:
        html = _render_nav("overview", "24h")
        menu_start = html.find('<div class="topnav-menu" id="topnav-menu">')
        menu_end = html.find("</div>", menu_start)
        for path in ("/reliability", "/routing", "/accounts", "/models"):
            assert html.find(path) > menu_start
            assert html.find(path) < menu_end


class TestDashboardScriptAlwaysLoaded:
    """`dashboard.js` must load on every page so the burger menu and
    update-command copy work even on non-chart pages.

    `chart.js` is ~200 KB and stays gated behind ``include_chart_js``;
    `dashboard.js` is small and unconditionally required because it
    wires ``initNavToggle`` (the mobile burger), ``initUpdateCommandCopy``,
    and the timeseries/timeseries-grouped init functions (each guards
    its own work with empty-result DOM queries, so they are safe to
    call on every page).

    Regression for the bug where bundling both scripts behind the
    chart flag left the burger click handler unattached on /accounts,
    /models, /events, /bandwidth, /pings, /runtime, and /traces —
    the menu never opened and no console errors were raised because
    the script never loaded in the first place.
    """

    @staticmethod
    def _assert_dashboard_js_loaded(html: str) -> None:
        assert '<script defer src="/static/dashboard.js"></script>' in html, (
            "dashboard.js must load on every page so the burger menu works"
        )

    def test_dashboard_js_loads_on_accounts(self) -> None:
        html = render_accounts(accounts=[], period="24h")
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_models(self) -> None:
        html = render_models(models=[], period="24h")
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_events(self) -> None:
        html = render_events(events=[], period="24h")
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_bandwidth(self) -> None:
        html = render_bandwidth(
            summary={},
            daily=[],
            timeseries=[],
            period="24h",
        )
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_pings(self) -> None:
        html = render_pings(ping_summary=[], recent_pings=[], period="24h")
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_runtime(self) -> None:
        html = render_runtime(snapshot={})
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_traces(self) -> None:
        html = render_traces(period="recent", limit=50, recent_requests=[])
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' not in html

    def test_dashboard_js_loads_on_overview(self) -> None:
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
        )
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' in html

    def test_dashboard_js_loads_on_reliability(self) -> None:
        html = render_reliability(
            period="24h",
            attempt_stats=None,
            retry_distribution=[],
            pending_health=None,
            operational_summary=[],
            recent_operational_events=[],
            timeseries=[],
        )
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' in html

    def test_dashboard_js_loads_on_routing(self) -> None:
        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
        )
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' in html

    def test_dashboard_js_loads_on_timeseries(self) -> None:
        html = render_timeseries(series=[], bucket="hour", period="24h")
        self._assert_dashboard_js_loaded(html)
        assert '<script defer src="/static/chart.js"></script>' in html

    def test_dashboard_js_loads_on_latency(self) -> None:
        html = render_latency(provider_ttft=[], model_ttft=[], period="24h")
        self._assert_dashboard_js_loaded(html)


class TestResponsiveColumns:
    """`data-priority` attribute drives responsive column hiding.

    P1 = always shown, P2 = hidden below 480px, P3 = hidden below 760px.
    Every <th> must carry the attribute and every <td> must match the
    priority of its <th> or rows misalign when the column is hidden.
    """

    def _th(self, label: str, *, priority: int = 1) -> str:
        return f'<th data-priority="{priority}">{label}</th>'

    def test_accounts_table_priority_breakdown(self) -> None:
        accounts = [
            {
                "account_name": "acct_a",
                "account_enabled": 1,
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 250,
                "total_tokens": 350,
                "tokens_per_second": 70.0,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 200.0,
                "reserved_microdollars": 0,
                "bytes_received": 0,
                "bytes_emitted": 0,
                "health_state": "healthy",
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        # P1 — operator quick-glance
        for label in ("Account", "Provider", "Enabled", "Requests", "Cost"):
            assert self._th(label) in html
        # P2 — diagnostic core
        for label in (
            "Health",
            "Errors",
            "Input tokens",
            "Output tokens",
            "Total tokens",
            "Avg latency",
            "TPS",
            "Exactness",
        ):
            assert self._th(label, priority=2) in html
        # P3 — deep diagnostic tail
        for label in (
            "Reserved",
            "Over budget",
            "Upstream backoff",
            "Backoff until",
            "Failures",
            "Auth fail",
            "Disabled",
        ):
            assert self._th(label, priority=3) in html

    def test_td_matches_th_priority_in_accounts(self) -> None:
        accounts = [
            {
                "account_name": "acct_a",
                "account_enabled": 1,
                "request_count": 5,
                "error_count": 1,
                "input_tokens": 100,
                "output_tokens": 250,
                "total_tokens": 350,
                "tokens_per_second": 70.0,
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 200.0,
                "reserved_microdollars": 0,
                "bytes_received": 0,
                "bytes_emitted": 0,
                "health_state": "healthy",
            }
        ]
        html = render_accounts(accounts=accounts, period="24h")
        # Each <th data-priority="N"> must have a matching <td data-priority="N">
        assert html.count('<td data-priority="1"') >= 1
        assert html.count('<td data-priority="2"') >= 1
        assert html.count('<td data-priority="3"') >= 1
        assert html.count('<th data-priority="1">') >= 1
        assert html.count('<th data-priority="2">') >= 1
        assert html.count('<th data-priority="3">') >= 1

    def test_models_table_priority_breakdown(self) -> None:
        models = [
            {
                "model_id": "gpt-x",
                "provider_id": "openai",
                "request_count": 5,
                "error_count": 0,
                "cost_microdollars": 1_000_000,
                "exact_count": 1,
                "avg_latency_ms": 200.0,
                "avg_ttft_ms": 50.0,
            }
        ]
        html = render_models(models=models, period="24h")
        for label in ("Model", "Provider", "Requests", "Cost", "Exactness"):
            assert self._th(label) in html
        for label in ("Errors", "Total tokens", "Avg latency", "TPS"):
            assert self._th(label, priority=2) in html
        for label in ("Est. cost", "Cache R", "Cache W", "Reasoning"):
            assert self._th(label, priority=3) in html

    def test_traces_table_priority_breakdown(self) -> None:
        traces = [
            {
                "started_at": "2024-01-01 12:00:00",
                "account_name": "acct_a",
                "provider_id": "openai",
                "model_id": "gpt-x",
                "protocol": "openai_chat",
                "status": "ok",
                "status_code": 200,
                "input_tokens": 10,
                "output_tokens": 20,
                "upstream_latency_ms": 200.0,
                "proxy_request_id": "abc123def456",
            }
        ]
        html = render_traces(
            period="24h",
            limit=10,
            recent_requests=traces,
        )
        for label in ("Time", "Account", "Model", "Status", "Latency"):
            assert self._th(label) in html
        for label in ("Provider", "Protocol", "In", "Out"):
            assert self._th(label, priority=2) in html
        assert self._th("ID", priority=3) in html

    def test_pings_table_priority_breakdown(self) -> None:
        pings = [
            {
                "provider_id": "openai",
                "account_name": "acct_a",
                "probed_at": "2024-01-01 12:00:00",
                "latency_ms": 200.0,
                "status_code": 200,
                "model_count": 5,
                "error": None,
            }
        ]
        html = render_pings(ping_summary=[], recent_pings=pings, period="24h")
        for label in ("Provider", "Time", "Latency", "Status"):
            assert self._th(label) in html
        for label in ("Account", "Models"):
            assert self._th(label, priority=2) in html
        assert self._th("Error", priority=3) in html


class TestChartWrap:
    """Chart.js canvases must sit inside a `.chart-wrap` div.

    `.chart-wrap { position: relative; width: 100% }` is the responsive
    container for all Chart.js canvases. Inline-style wrappers with
    `position: relative` are a regression because they create a fixed
    width that escapes the panel scroll behaviour.
    """

    def test_timeseries_chart_uses_chart_wrap(self) -> None:
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=[{"bucket": "2024-01-01 12:00:00", "request_count": 3}],
        )
        assert 'class="chart-wrap"' in html
        assert 'id="timeseries-chart"' in html
        # No inline `position: relative;` should remain
        assert "position: relative" not in html


class TestStaticChartDataIsland:
    """``_render_chart_canvas`` must defer initialisation to dashboard.js.

    Charts rendered through this helper are loaded with Chart.js via a
    ``defer`` ``<script>`` at the end of ``<body>``. An inline
    ``new Chart(...)`` would race that defer and throw
    ``ReferenceError: Chart is not defined`` on first paint, leaving the
    canvas empty. The chart payload must instead live in a sibling
    ``<script type="application/json">`` data island that
    ``EggPoolDashboard.initStaticCharts`` consumes on ``DOMContentLoaded``.
    """

    def test_emits_data_island_not_inline_init(self) -> None:
        from eggpool.dashboard.render import _render_chart_canvas

        labels = json.dumps(["a", "b", "c"])
        datasets = json.dumps([{"label": "x", "data": [1, 2, 3]}])
        options = json.dumps({"responsive": True})
        html = _render_chart_canvas("example-chart", "bar", labels, datasets, options)
        assert 'id="example-chart"' in html
        assert (
            re.search(
                r'<script type="application/json"\s+'
                r'class="static-chart-data"\s+'
                r'data-chart-id="example-chart">',
                html,
            )
            is not None
        )
        assert "new Chart(" not in html

    def test_data_island_round_trips_payload(self) -> None:
        from eggpool.dashboard.render import _render_chart_canvas

        labels = json.dumps(["a", "b", "c"])
        datasets = json.dumps(
            [{"label": "x", "data": [1, 2, 3], "backgroundColor": "red"}]
        )
        options = json.dumps({"plugins": {"legend": {"display": False}}})
        html = _render_chart_canvas(
            "example-chart", "doughnut", labels, datasets, options
        )
        match = re.search(
            r'<script type="application/json"\s+class="static-chart-data"\s+'
            r'data-chart-id="example-chart">([^<]+)</script>',
            html,
        )
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["type"] == "doughnut"
        assert payload["labels"] == ["a", "b", "c"]
        assert payload["datasets"] == [
            {"label": "x", "data": [1, 2, 3], "backgroundColor": "red"}
        ]
        assert payload["options"] == {"plugins": {"legend": {"display": False}}}


class TestUpdateIndicator:
    """Footer update indicator is only rendered when an update is available."""

    def test_no_indicator_when_update_info_is_none(self) -> None:
        from eggpool.dashboard.render import _render_update_indicator

        assert _render_update_indicator(None) == ""

    def test_no_indicator_when_update_not_available(self) -> None:
        from eggpool.dashboard.render import _render_update_indicator
        from eggpool.update_checker import UpdateInfo

        info = UpdateInfo(
            current_version="0.1.0",
            latest_version="0.1.0",
            update_available=False,
        )
        assert _render_update_indicator(info) == ""

    def test_indicator_rendered_when_update_available(self) -> None:
        from eggpool.dashboard.render import _render_update_indicator
        from eggpool.update_checker import UpdateInfo

        info = UpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            update_available=True,
            update_command="eggpool update",
        )
        html = _render_update_indicator(info)
        assert "update-indicator" in html
        assert "data-update-command" in html
        assert "eggpool update" in html
        assert "0.1.0" in html
        assert "0.2.0" in html

    def test_indicator_escapes_special_chars(self) -> None:
        """Defense-in-depth — the command is not user-supplied today, but
        the renderer must escape it so a future change cannot smuggle
        markup into the footer."""
        from eggpool.dashboard.render import _render_update_indicator
        from eggpool.update_checker import UpdateInfo

        info = UpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            update_available=True,
            update_command="<script>alert(1)</script>",
        )
        html = _render_update_indicator(info)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_overview_renders_footer_indicator_when_update_available(self) -> None:
        from eggpool.update_checker import UpdateInfo

        html = render_overview(
            overview={"summary": {}, "imbalance": {}},
            accounts=[],
            models=[],
            events=[],
            update_info=UpdateInfo(
                current_version="0.1.0",
                latest_version="0.2.0",
                update_available=True,
                update_command="eggpool update",
            ),
        )
        assert "update-indicator" in html
        assert "data-update-command" in html

    def test_overview_omits_indicator_when_no_update(self) -> None:
        html = render_overview(
            overview={"summary": {}, "imbalance": {}},
            accounts=[],
            models=[],
            events=[],
        )
        assert "update-indicator" not in html
        assert "data-update-command" not in html

    def test_overview_omits_indicator_with_explicit_none(self) -> None:
        html = render_overview(
            overview={"summary": {}, "imbalance": {}},
            accounts=[],
            models=[],
            events=[],
            update_info=None,
        )
        assert "update-indicator" not in html


class TestStickyTopbarStylesheet:
    """The topbar must use sticky positioning so it stays reachable on
    desktop viewports while scrolling long tables."""

    @staticmethod
    def _load_css() -> str:
        from pathlib import Path

        return (
            Path(__file__).parent.parent.parent
            / "src"
            / "eggpool"
            / "dashboard"
            / "static"
            / "dashboard.css"
        ).read_text()

    def test_topbar_uses_position_sticky(self) -> None:
        css = self._load_css()
        # Find the `header.topbar { ... }` block and assert it sets `position: sticky`.
        match = re.search(
            r"header\.topbar\s*\{([^}]*)\}",
            css,
            flags=re.DOTALL,
        )
        assert match is not None, "header.topbar rule not found"
        block = match.group(1)
        assert "position: sticky" in block
        assert "top: 0" in block
        # z-index must beat the body::before overlay (z-index: 2) and the
        # egg-background watermark (z-index: 0) so the bar is never occluded.
        z_match = re.search(r"z-index:\s*(\d+)", block)
        assert z_match is not None
        assert int(z_match.group(1)) > 2


class TestRenderRuntimeNetwork:
    """Tests for the network diagnostics section on the runtime page."""

    def test_renders_network_section(self) -> None:
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {
                "build_count": 1,
                "request_count": 100,
                "error_count": 2,
                "has_client": True,
            },
            "provider_client_pool": {
                "build_count": 3,
                "providers": {"openai": 1, "anthropic": 1, "opencode-go": 1},
            },
            "dns_cache": {
                "enabled": True,
                "size": 7,
                "hits": 500,
                "misses": 10,
                "negative_hits": 0,
                "stale_hits": 0,
                "evictions": 0,
                "resolution_errors": {},
                "hosts": [],
            },
        }
        html = render_runtime(snapshot)
        assert "enabled" in html
        assert "Outbound builds" in html
        assert "Outbound requests" in html
        assert "DNS cache" in html
        assert "DNS suppression" in html
        assert "Provider clients" in html

    def test_renders_dns_disabled(self) -> None:
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {"build_count": 0, "request_count": 0, "error_count": 0},
            "provider_client_pool": {"build_count": 0, "providers": {}},
            "dns_cache": {"enabled": False},
        }
        html = render_runtime(snapshot)
        assert "disabled" in html

    def test_renders_empty_network_data(self) -> None:
        """Runtime page renders without outbound_client/dns_cache keys."""
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
        }
        html = render_runtime(snapshot)
        assert "<html" in html

    def test_dns_hit_rate_calculation(self) -> None:
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
            "provider_client_pool": {"build_count": 0, "providers": {}},
            "dns_cache": {
                "enabled": True,
                "size": 3,
                "hits": 90,
                "misses": 10,
                "dns_suppression_rate": 0.9,
                "resolver_calls_total": 10,
            },
        }
        html = render_runtime(snapshot)
        assert "90.0%" in html

    def test_dns_hit_rate_zero_lookups(self) -> None:
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
            "provider_client_pool": {"build_count": 0, "providers": {}},
            "dns_cache": {
                "enabled": True,
                "size": 0,
                "hits": 0,
                "misses": 0,
            },
        }
        html = render_runtime(snapshot)
        assert "—" in html

    def test_no_api_keys_in_html(self) -> None:
        """Runtime page does not expose API keys via network metrics."""
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
            "provider_client_pool": {"build_count": 0, "providers": {}},
            "dns_cache": {"enabled": True, "size": 0, "hits": 0, "misses": 0},
        }
        html = render_runtime(snapshot)
        assert "test-key" not in html
        assert "OPENCODE" not in html

    def test_dns_max_entries_display(self) -> None:
        """DNS cache card shows entries / max when max_entries is present."""
        snapshot: dict[str, Any] = {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
            "provider_client_pool": {"build_count": 0, "providers": {}},
            "dns_cache": {
                "enabled": True,
                "size": 7,
                "max_entries": 50,
                "hits": 0,
                "misses": 0,
            },
        }
        html = render_runtime(snapshot)
        assert "7 / 50" in html


class TestRenderRuntimeDispatchAndLoad:
    """Tests for the dispatch-overhead and load cards on the runtime page."""

    def _base_snapshot(self) -> dict[str, Any]:
        return {
            "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 4},
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "outbound_client": {"build_count": 0, "request_count": 0, "error_count": 0},
            "provider_client_pool": {"build_count": 0, "providers": {}},
            "dns_cache": {"enabled": False},
            "load": {
                "available": True,
                "cpu_count": 2,
                "load_1m": 0.5,
                "load_5m": 0.3,
                "load_15m": 0.2,
                "normalized_1m": 0.25,
                "normalized_5m": 0.15,
                "normalized_15m": 0.1,
            },
            "dispatch_overhead": {
                "window_size": 100,
                "sample_count": 50,
                "avg_ms": 1.5,
                "p50_ms": 1.2,
                "p95_ms": 4.2,
                "p99_ms": 8.0,
                "max_ms": 12.0,
                "min_ms": 0.8,
            },
        }

    def test_drops_configured_thread_card(self) -> None:
        snapshot = self._base_snapshot()
        html = render_runtime(snapshot)
        assert "configured server threads" not in html
        assert "<h3>Threads</h3>" not in html
        assert "Active threads" in html
        assert "Load average" in html
        assert "Dispatch overhead" in html
        assert "<h3>Processes</h3>" not in html

    def test_renders_load_average_card_with_data(self) -> None:
        snapshot = self._base_snapshot()
        html = render_runtime(snapshot)
        assert "Load average" in html
        assert "0.50" in html

    def test_renders_dispatch_overhead_card_with_data(self) -> None:
        snapshot = self._base_snapshot()
        html = render_runtime(snapshot)
        assert "Dispatch overhead" in html
        assert "p95" in html
        assert 'data-tooltip="EggPool-local time spent before each upstream ' in html

    def test_load_unavailable_card(self) -> None:
        snapshot = self._base_snapshot()
        snapshot["load"] = {
            "available": False,
            "cpu_count": None,
            "load_1m": None,
            "load_5m": None,
            "load_15m": None,
            "normalized_1m": None,
            "normalized_5m": None,
            "normalized_15m": None,
        }
        html = render_runtime(snapshot)
        assert "Load average" in html
        assert "load average unavailable" in html

    def test_network_cards_have_tooltips(self) -> None:
        snapshot = self._base_snapshot()
        snapshot["dns_cache"] = {"enabled": True, "size": 5, "hits": 2, "misses": 1}
        html = render_runtime(snapshot)
        assert (
            'data-tooltip="Outbound DNS cache state and current entry count."' in html
        )

    def test_dispatch_overhead_no_samples(self) -> None:
        snapshot = self._base_snapshot()
        snapshot["dispatch_overhead"] = {
            "window_size": 100,
            "sample_count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
            "min_ms": None,
        }
        html = render_runtime(snapshot)
        assert "Dispatch overhead" in html
        assert "0 / 100 attempts" in html

    def test_process_count_warning_panel(self) -> None:
        snapshot = self._base_snapshot()
        snapshot["processes"] = {
            "process_count_warning": True,
            "eggpool_process_count": 5,
            "expected_worker_process_count": 2,
        }
        html = render_runtime(snapshot)
        assert "Process count warning" in html

    def test_no_process_warning_panel_when_no_warning(self) -> None:
        snapshot = self._base_snapshot()
        snapshot["processes"] = {
            "process_count_warning": False,
            "eggpool_process_count": 2,
            "expected_worker_process_count": 2,
        }
        html = render_runtime(snapshot)
        assert "Process count warning" not in html
