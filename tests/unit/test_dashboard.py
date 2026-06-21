"""Tests for the dashboard HTML rendering and escape utilities."""

from __future__ import annotations

from html.parser import HTMLParser

from eggpool.dashboard.escape import (
    escape,
    escape_attr,
    format_bytes,
    format_latency,
    format_microdollars,
    format_percent,
    format_tokens,
    sanitize_class_name,
    truncate,
)
from eggpool.dashboard.render import (
    _render_bandwidth_heatmap,
    _render_nav,
    render_accounts,
    render_bandwidth,
    render_events,
    render_latency,
    render_models,
    render_overview,
    render_pings,
    render_timeseries,
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
        assert format_microdollars(1_000_000) == "$1.000000"
        assert format_microdollars(0) == "$0.000000"
        assert format_microdollars(None) == "$0.000000"

    def test_format_tokens(self) -> None:
        assert format_tokens(1_000_000) == "1,000,000"
        assert format_tokens(0) == "0"

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
        assert "$1.500000" in html
        assert "20.00%" in html
        assert 'id="dashboard-content"' in html
        assert "setInterval" in html
        assert "/static/dashboard.css" in html

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
        assert 'preserveAspectRatio="xMidYMid slice"' in html
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
        assert "$1.000000" in html

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
                "cost_microdollars": 1_000_000,
                "avg_latency_ms": 200.0,
            }
        ]
        html = render_models(models=models, period="24h")
        assert "gpt-x" in html
        assert "$1.000000" in html

    def test_account_filter_shown(self) -> None:
        html = render_models(models=[], account_filter="acct_a", period="24h")
        assert 'value="acct_a"' in html


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
                "cost_microdollars": 500_000,
            }
        ]
        html = render_timeseries(series=series, bucket="hour", period="24h")
        assert "2024-01-01 12:00:00" in html


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
        assert "$1.500000" in text


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
        assert "No bandwidth data" in html

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

    def test_bandwidth_heatmap_section(self) -> None:
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
                {"day": "2024-06-01", "bytes_received": 1000, "bytes_emitted": 500},
            ],
        )
        assert "Bandwidth activity" in html
        assert "<svg" in html

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


class TestRenderPings:
    """Tests for the pings page renderer."""

    def test_renders_empty(self) -> None:
        html = render_pings(ping_summary=[], recent_pings=[], period="24h")
        assert "Provider Pings" in html
        assert "No ping data yet" in html
        assert "No pings recorded" in html

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
