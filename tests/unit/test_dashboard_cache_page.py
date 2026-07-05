"""Tests for the /cache page rendering and handler contract."""

from __future__ import annotations

from eggpool.dashboard.render import (
    _render_cache_request_shaping_fallback,
    render_cache,
)


class TestRenderCacheBasic:
    """render_cache produces valid HTML with period selector and title."""

    def test_renders_html_with_cache_title(self) -> None:
        html = render_cache(period="24h")
        assert "<title>" in html
        assert "Cache" in html

    def test_period_selector_present(self) -> None:
        html = render_cache(period="24h")
        assert "24h" in html

    def test_period_1h_renders(self) -> None:
        html = render_cache(period="1h")
        assert "1h" in html

    def test_period_7d_renders(self) -> None:
        html = render_cache(period="7d")
        assert "7d" in html


class TestRenderCacheSectionPanels:
    """render_cache with minimal kwargs renders all section panels."""

    def test_all_section_panels_present(self) -> None:
        html = render_cache(
            period="24h",
            cache_stability={
                "transcoded_request_count": 0,
                "notes": "",
            },
            compression_observability={
                "total_requests": 0,
                "by_status": {},
                "by_mode": {},
                "totals": {},
                "per_model_status": {},
            },
            compression_runtime={
                "window": {"seconds": 0, "request_count": 0},
                "mode_counts": {},
                "applied_count": 0,
                "failed_fallback_count": 0,
                "candidate_count": 0,
                "estimated_savings_tokens": 0,
                "actual_savings_tokens": 0,
                "latency_ms": {"avg": None, "p50": None, "p95": None, "max": None},
                "transforms": {},
                "warnings": {},
                "cache_safety": {
                    "stable_prefix_preserved": 0,
                    "stable_prefix_mismatch": 0,
                },
            },
            compression_policy_stats={
                "policy_counts": [],
                "total_requests": 0,
                "total_policies": 0,
            },
        )
        for label in (
            "Request shaping",
            "Cache reporting",
            "Native cache preservation",
            "Compression opportunities",
            "Safe compression",
            "Policy overrides",
            "Advisory tuning",
            "Routing guardrails",
        ):
            assert label in html, f"section panel {label!r} missing from /cache"


class TestRenderCacheNavLinks:
    """render_cache renders a local nav with anchor links to each section."""

    def test_nav_page_index_present(self) -> None:
        html = render_cache(period="24h")
        assert '<nav class="page-index">' in html
        assert '<a href="#cache-summary">Summary</a>' in html
        assert '<a href="#cache-reporting">Cache reporting</a>' in html
        assert '<a href="#cache-stability">' in html
        assert '<a href="#compression">Compression</a>' in html
        assert '<a href="#advisory-tuning">Advisory tuning</a>' in html
        assert '<a href="#routing-guardrails">Routing guardrails</a>' in html


class TestRenderCachePeriodAwareness:
    """render_cache passes the period string into the rendered page."""

    def test_period_1h_with_compression_data(self) -> None:
        html = render_cache(
            period="1h",
            compression_observability={"totals": {"observed_requests": 7}},
        )
        assert "1h" in html


class TestRenderCacheFallbackSummary:
    """_render_cache_request_shaping_fallback is exercised when
    request_shaping_summary is None.
    """

    def test_supplied_summary_is_rendered(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary={
                "mode": {
                    "compression": "safe",
                    "synthetic_cache": "apply",
                    "tuning": "recommend",
                    "routing": "custom-mode",
                },
                "compression": {
                    "requests_compressed": 3,
                    "actual_savings_tokens": 42,
                },
                "cache": {"cache_counter_reported_rate": 0.75},
                "synthetic_cache": {"candidate_count": 7},
                "guardrails": {
                    "stable_prefix_preserved_rate": 0.9,
                    "failed_fallback_count": 2,
                    "policy_warning_count": 1,
                },
                "tuning": {"recommendation_count": 4, "override_count": 5},
                "segmentation": {"requests_segmented": 6},
            },
        )
        assert "custom-mode" in html
        assert "3 requests compressed" in html
        assert "7 candidates" in html
        assert "4 recommendations" in html
        assert "segmented 6" in html

    def test_fallback_produces_summary_panel(self) -> None:
        fallback = _render_cache_request_shaping_fallback(
            cache_stability=None,
            compression_observability=None,
            compression_runtime=None,
            synthetic_cache_summary=None,
            guardrails={},
        )
        assert isinstance(fallback, dict)
        html = render_cache(period="24h", request_shaping_summary=fallback)
        assert "Request shaping" in html
        assert 'id="cache-summary"' in html

    def test_fallback_exercised_when_none(self) -> None:
        html = render_cache(period="24h")
        assert "Request shaping" in html


class TestRenderCacheCacheReporting:
    """render_cache renders the cache reporting section when data is present."""

    def test_cache_reporting_section_header(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 10,
                "by_status": {"reported": 10},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
        )
        assert "Cache reporting" in html


class TestRenderCacheSyntheticControls:
    """render_cache renders synthetic cache controls when data is present."""

    def test_synthetic_cache_controls_rendered(self) -> None:
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 5,
                "status_counts": {"applied": 3, "disabled": 2},
                "dry_run_count": 0,
                "applied_count": 3,
                "candidate_count_total": 8,
                "applied_count_total": 3,
                "warning_count_total": 0,
                "warning_counts": {},
                "by_policy": [],
            },
        )
        import re

        m = re.search(
            r'<div id="synthetic-cache-controls">(.*?)</div>', html, re.DOTALL
        )
        assert m is not None
        assert m.group(1).strip() != ""

    def test_synthetic_cache_controls_not_rendered_when_none(self) -> None:
        html = render_cache(period="24h")
        import re

        m = re.search(
            r'<div id="synthetic-cache-controls">(.*?)</div>', html, re.DOTALL
        )
        assert m is not None
        assert m.group(1).strip() == ""


class TestRenderCacheRoutingGuardrails:
    """render_cache renders routing guardrails with explicit routing_runtime."""

    def test_guardrails_with_mode(self) -> None:
        html = render_cache(
            period="24h",
            routing_runtime={
                "guardrails": {
                    "routing_cache_compression_mode": "reporting_only",
                    "routing_uses_cache_metrics": False,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                    "route_scorer_inputs": [],
                },
            },
        )
        assert "Routing guardrails" in html
        assert "reporting_only" in html


class TestRenderCacheAnchorIntegrity:
    """Every href="#..." in the local page-index resolves to a real element id."""

    def test_all_index_anchors_resolve(self) -> None:
        import re

        html = render_cache(period="24h")
        index_match = re.search(r'<nav class="page-index">(.*?)</nav>', html, re.DOTALL)
        assert index_match is not None
        index_html = index_match.group(1)
        anchors = re.findall(r'href="#([^"]+)"', index_html)
        assert len(anchors) > 0, "page-index should have at least one anchor link"
        for anchor in anchors:
            anchor_id = f'id="{anchor}"'
            assert anchor_id in html, (
                f"page-index links to #{anchor} but no element with "
                f"{anchor_id} found in rendered HTML"
            )


class TestRenderCacheAdversarialEscaping:
    """Dynamic provider/account/model/policy strings are HTML-escaped."""

    def test_account_name_escaped(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 1,
                "by_status": {"reported": 1},
                "per_protocol_status": {},
                "per_account_status": {'acct"onclick="x': {"reported": 1}},
                "per_model_status": {},
            },
        )
        assert 'acct"onclick="x' not in html

    def test_model_id_escaped(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 1,
                "by_status": {"reported": 1},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {"<script>alert(1)</script>": {"reported": 1}},
            },
        )
        # The model ID itself must be escaped; other <script> tags
        # (e.g. dashboard.js) are expected
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
