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
    format_tokens_per_second,
    sanitize_class_name,
    truncate,
)
from eggpool.dashboard.render import (
    _format_tooltip_date,
    _render_bandwidth_heatmap,
    _render_nav,
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
        assert format_microdollars(1_000_000) == "$1.000000"
        assert format_microdollars(0) == "$0.000000"
        assert format_microdollars(None) == "$0.000000"

    def test_format_tokens(self) -> None:
        assert format_tokens(1_000_000) == "1,000,000"
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
        assert "$1.500000" in html
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
        """The chart script must seed itself from the inlined payload."""
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
        assert "/api/timeseries" in html  # refresh URL preserved

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
        assert "<th>Total tokens</th>" in html
        assert "<th>TPS</th>" in html
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
        assert "<th>Over budget</th>" in html
        assert "<th>Upstream backoff</th>" in html
        assert "<th>Backoff until</th>" in html
        assert "<th>Failures</th>" in html
        assert "<th>Auth fail</th>" in html
        assert "<th>Disabled</th>" in html
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
        assert "$1.000000" in html
        assert "<th>Total tokens</th>" in html
        assert "<th>TPS</th>" in html
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
        assert "<th>Total tokens</th>" in html
        assert "300" in html


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
        # hitboxes are emitted once per visible day, not once per data row.
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
        assert html.count('class="heatmap-hitbox"') == n_days

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
        css = self._load_css()
        assert ".heatmap-overlay" in css
        assert "repeat(13, 13px)" in css
        assert "repeat(7, 13px)" in css

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
        assert "<th>Phases ms (c/r/o)</th>" in html
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
        assert "<th>Exactness</th>" in html
        assert "5/2/1/2" in html
        assert "<th>Est. cost</th>" in html
        assert "<th>Cache R</th>" in html
        assert "<th>Cache W</th>" in html
        assert "<th>Reasoning</th>" in html
        assert "<th>Avg cost/req</th>" in html
        assert "<th>Avg cost/1k tok</th>" in html
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
        assert "0/0/0/0" in html
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
        assert "<th>Exactness</th>" in html
        assert "3/1/1/0" in html
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
