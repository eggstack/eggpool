"""Tests for the /cache page rendering and handler contract."""

from __future__ import annotations

import pytest

from eggpool.dashboard.render import (
    _render_cache_request_shaping_fallback,
    render_cache,
)

pytestmark = pytest.mark.dashboard


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
            "Provider cache counters",
            "Native cache preservation",
            "Compression",
            "Policy overrides",
            "Routing isolation",
        ):
            assert label in html, f"section panel {label!r} missing from /cache"


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
                "segmentation": {
                    "requests_segmented": 6,
                    "requests_not_collected": 2,
                    "requests_empty_request": 1,
                    "requests_parse_failure": 0,
                },
            },
        )
        assert "custom-mode" in html
        assert "3 requests" in html
        assert "42 tokens saved" in html
        assert "4 recommendations" in html

    def test_fallback_uses_canonical_segmentation_counts(self) -> None:
        fallback = _render_cache_request_shaping_fallback(
            cache_stability=None,
            compression_observability=None,
            compression_runtime=None,
            synthetic_cache_summary=None,
            guardrails={},
        )
        assert isinstance(fallback, dict)
        html = render_cache(
            period="24h",
            canonical_request_segmentation={
                "by_status": {
                    "segmented": 6,
                    "not_collected": 2,
                    "empty_request": 1,
                    "parse_failure": 0,
                },
                "protected_requests": 3,
                "compressible_candidate_requests": 1,
                "token_totals": {
                    "stable_prefix": 0,
                    "semi_stable": 0,
                    "volatile": 0,
                    "all": 0,
                },
                "byte_totals": {
                    "stable_prefix": 0,
                    "semi_stable": 0,
                    "volatile": 0,
                    "all": 0,
                },
                "per_model_status": {},
            },
        )
        assert "Request shaping" in html
        assert "Not collected" in html
        assert "Empty request" in html

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
        assert "Provider cache counters" in html


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
        assert "Routing isolation" in html
        assert "reporting_only" in html


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
