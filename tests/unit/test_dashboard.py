"""Tests for the dashboard HTML rendering and escape utilities."""

from __future__ import annotations

from html.parser import HTMLParser

from go_aggregator.dashboard.escape import (
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
from go_aggregator.dashboard.render import (
    _render_bandwidth_heatmap,
    _render_nav,
    render_accounts,
    render_bandwidth,
    render_events,
    render_models,
    render_overview,
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
        assert sanitize_class_name("hello world!") == "hello world_"
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

    def test_renders_account_table(self) -> None:
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
