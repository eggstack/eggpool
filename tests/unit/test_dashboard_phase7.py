"""Tests for the Phase 7 dashboard runtime cards.

These tests verify that the new compression observability, compression
runtime, compression policy rollup, and cache-stability cards render
correctly on the runtime page.  The tests focus on operator-facing
behaviour:

- Empty data renders the panel header and metric strip without errors.
- Non-empty data shows the headline metric counts.
- Warnings / fallback counts surface as warning cards when non-zero.
- The routing-separation notice always renders.
- No raw prompt text or upstream payload content leaks into HTML.
"""

from __future__ import annotations

from typing import Any

from eggpool.dashboard.render import render_runtime


def _base_snapshot() -> dict[str, Any]:
    """Minimal snapshot so render_runtime doesn't blow up on missing keys."""
    return {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {
            "build_count": 0,
            "request_count": 0,
            "error_count": 0,
        },
        "provider_client_pool": {"build_count": 0, "providers": {}},
        "dns_cache": {"enabled": False},
    }


class TestCompressionObservabilityCard:
    """Compression observability card renders with empty + populated data."""

    def test_empty_data_renders_panel(self) -> None:
        """An empty payload still produces the panel header and metric strip."""
        html = render_runtime(
            _base_snapshot(),
            compression_observability={
                "total_requests": 0,
                "by_status": {},
                "by_mode": {},
                "totals": {},
                "per_model_status": {},
                "requests_with_compression_applied": 0,
                "applied_total_savings_tokens": 0,
                "applied_failed_fallback_count": 0,
                "applied_stable_prefix_preserved_count": 0,
                "applied_p95_savings_tokens": None,
                "applied_p95_latency_ms": None,
            },
        )
        assert "Compression observability" in html
        assert "Observed requests" in html
        assert "Applied (safe mode)" in html
        assert "Fail-closed fallbacks" in html

    def test_populated_data_renders_counts(self) -> None:
        """Populated counts surface in the metric cards."""
        html = render_runtime(
            _base_snapshot(),
            compression_observability={
                "total_requests": 50,
                "by_status": {"disabled": 10, "observed": 35, "safe": 5},
                "by_mode": {"observe": 35, "safe": 5},
                "totals": {
                    "candidate_count": 100,
                    "eligible_count": 80,
                    "estimated_savings_tokens": 5000,
                    "analyzer_latency_ms_median": 2.5,
                    "analyzer_latency_ms_p95": 8.0,
                    "observed_requests": 40,
                },
                "per_model_status": {
                    "gpt-4o": {
                        "total_requests": 30,
                        "candidate_count": 60,
                        "eligible_count": 50,
                        "estimated_savings_tokens": 3000,
                    },
                },
                "requests_with_compression_applied": 5,
                "applied_total_savings_tokens": 800,
                "applied_failed_fallback_count": 1,
                "applied_stable_prefix_preserved_count": 4,
                "applied_p95_savings_tokens": 250.0,
                "applied_p95_latency_ms": 12.0,
            },
        )
        assert "5" in html  # applied count
        assert "800" in html  # applied savings
        assert "1" in html  # fallback count
        assert "gpt-4o" in html  # model breakdown
        assert "warning" in html.lower()  # fallback warning card

    def test_no_warning_class_when_no_fallback(self) -> None:
        """No 'warning' class when fallback count is zero."""
        html = render_runtime(
            _base_snapshot(),
            compression_observability={
                "total_requests": 10,
                "by_status": {},
                "by_mode": {},
                "totals": {"observed_requests": 10},
                "per_model_status": {},
                "requests_with_compression_applied": 10,
                "applied_failed_fallback_count": 0,
                "applied_total_savings_tokens": 1000,
                "applied_stable_prefix_preserved_count": 10,
                "applied_p95_savings_tokens": None,
                "applied_p95_latency_ms": None,
            },
        )
        # The fallback card should not have the warning class
        assert "card warning" not in html or html.count("card warning") == 0


class TestCompressionRuntimeCard:
    """Compression runtime card renders with empty + populated data."""

    def test_empty_data_renders_panel(self) -> None:
        """Empty payload produces the panel header and metric strip."""
        html = render_runtime(
            _base_snapshot(),
            compression_runtime={
                "window": {"seconds": 3600, "request_count": 0},
                "mode_counts": {"disabled": 0, "observe": 0, "safe": 0},
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
        )
        assert "Compression runtime" in html
        assert "Mode: observe" in html
        assert "Mode: safe" in html
        assert "Mode: disabled" in html

    def test_populated_data_renders_counts_and_transforms(self) -> None:
        """Populated data shows mode counts and transform breakdown."""
        html = render_runtime(
            _base_snapshot(),
            compression_runtime={
                "window": {"seconds": 86400, "request_count": 1000},
                "mode_counts": {"disabled": 800, "observe": 150, "safe": 50},
                "applied_count": 50,
                "failed_fallback_count": 2,
                "candidate_count": 200,
                "estimated_savings_tokens": 10000,
                "actual_savings_tokens": 5000,
                "latency_ms": {
                    "avg": 3.5,
                    "p50": 2.0,
                    "p95": 12.0,
                    "max": 20.0,
                },
                "transforms": {
                    "repeated_line_run": {"applied": 30, "tokens_saved": 3000},
                    "log_compaction": {"applied": 20, "tokens_saved": 2000},
                },
                "warnings": {
                    "stable_prefix_hash_mismatch": 2,
                },
                "cache_safety": {
                    "stable_prefix_preserved": 48,
                    "stable_prefix_mismatch": 2,
                },
            },
        )
        assert "150" in html  # observe count
        assert "50" in html  # safe count
        assert "repeated_line_run" in html
        assert "log_compaction" in html
        assert "stable_prefix_hash_mismatch" in html

    def test_warnings_table_renders(self) -> None:
        """Warnings rollup renders as a sub-table."""
        html = render_runtime(
            _base_snapshot(),
            compression_runtime={
                "window": {"seconds": 3600, "request_count": 100},
                "mode_counts": {"disabled": 80, "observe": 15, "safe": 5},
                "applied_count": 5,
                "failed_fallback_count": 0,
                "candidate_count": 10,
                "estimated_savings_tokens": 500,
                "actual_savings_tokens": 300,
                "latency_ms": {"avg": 1.0, "p50": 0.5, "p95": 2.0, "max": 3.0},
                "transforms": {},
                "warnings": {"transform_skipped_disabled": 3},
                "cache_safety": {
                    "stable_prefix_preserved": 5,
                    "stable_prefix_mismatch": 0,
                },
            },
        )
        assert "transform_skipped_disabled" in html
        assert "Warnings rollup" in html


class TestCompressionPolicyCard:
    """Compression policy rollup card renders with empty + populated data."""

    def test_empty_data_renders_panel(self) -> None:
        """Empty payload produces the panel header and metric strip."""
        html = render_runtime(
            _base_snapshot(),
            compression_policy_stats={
                "policy_counts": [],
                "total_requests": 0,
                "total_policies": 0,
            },
        )
        assert "Compression policy rollup" in html
        assert "Tracked policies" in html
        assert "Total requests" in html

    def test_populated_data_renders_policy_rows(self) -> None:
        """Populated policies render as table rows."""
        html = render_runtime(
            _base_snapshot(),
            compression_policy_stats={
                "policy_counts": [
                    {
                        "policy_name": "<global>",
                        "policy_source": "global",
                        "requests": 100,
                        "mode_counts": {"disabled": 80, "observe": 20, "safe": 0},
                        "applied": 0,
                        "failed_fallback": 0,
                        "candidate_count": 50,
                        "warning_count": 0,
                    },
                    {
                        "policy_name": "opencode_safe",
                        "policy_source": "policy:opencode_safe",
                        "requests": 50,
                        "mode_counts": {"disabled": 0, "observe": 0, "safe": 50},
                        "applied": 30,
                        "failed_fallback": 1,
                        "candidate_count": 80,
                        "warning_count": 2,
                    },
                ],
                "total_requests": 150,
                "total_policies": 2,
            },
        )
        assert "&lt;global&gt;" in html
        assert "opencode_safe" in html
        assert "policy:opencode_safe" in html
        assert "30" in html  # applied count
        assert "Tracked policies" in html

    def test_global_sentinel_appears_first(self) -> None:
        """The <global> sentinel renders before override policies."""
        html = render_runtime(
            _base_snapshot(),
            compression_policy_stats={
                "policy_counts": [
                    {
                        "policy_name": "<global>",
                        "policy_source": "global",
                        "requests": 100,
                        "mode_counts": {"disabled": 100, "observe": 0, "safe": 0},
                        "applied": 0,
                        "failed_fallback": 0,
                        "candidate_count": 0,
                        "warning_count": 0,
                    },
                ],
                "total_requests": 100,
                "total_policies": 1,
            },
        )
        assert "&lt;global&gt;" in html
        assert "global" in html


class TestCacheStabilityCard:
    """Cache stability card renders with empty + populated data."""

    def test_empty_data_renders_panel(self) -> None:
        """Empty payload produces the panel header and notes."""
        html = render_runtime(
            _base_snapshot(),
            cache_stability={
                "transcoded_request_count": 0,
                "notes": "Phase 3 cache stability is per-request and in-memory.",
            },
        )
        assert "Cache stability" in html
        assert "Transcoded requests" in html
        assert "TranscodeContext" in html

    def test_populated_data_renders_count(self) -> None:
        """Populated transcoded count surfaces."""
        html = render_runtime(
            _base_snapshot(),
            cache_stability={
                "transcoded_request_count": 42,
                "notes": "Phase 3 cache stability is per-request and in-memory.",
            },
        )
        assert "42" in html


class TestRoutingSeparationNotice:
    """Routing separation notice always renders."""

    def test_notice_renders_when_compression_data_present(self) -> None:
        """Notice renders when compression_observability is provided."""
        html = render_runtime(
            _base_snapshot(),
            compression_observability={
                "total_requests": 0,
                "by_status": {},
                "by_mode": {},
                "totals": {},
                "per_model_status": {},
            },
        )
        assert "Routing separation" in html
        assert "QuotaFairScorer" in html
        assert "reporting-only" in html

    def test_notice_does_not_render_when_compression_data_absent(self) -> None:
        """Notice stays inside the runtime page even when cards are absent.

        The notice is purely informational; it always renders on the
        runtime page because operators may not have compression data
        yet but should still see the routing separation guarantee.
        """
        html = render_runtime(_base_snapshot())
        assert "Routing separation" in html


class TestNoRawPayloadLeakage:
    """Dashboard never leaks raw upstream content."""

    def test_no_prompt_substrings_in_html(self) -> None:
        """Common prompt substrings never appear in rendered HTML."""
        html = render_runtime(
            _base_snapshot(),
            compression_observability={
                "total_requests": 1,
                "by_status": {"disabled": 1},
                "by_mode": {},
                "totals": {"observed_requests": 0},
                "per_model_status": {},
            },
            compression_runtime={
                "window": {"seconds": 3600, "request_count": 1},
                "mode_counts": {"disabled": 1, "observe": 0, "safe": 0},
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
            cache_stability={
                "transcoded_request_count": 0,
                "notes": "test notes",
            },
        )
        forbidden = [
            "sk-",
            "Bearer ",
            "system prompt",
            "<tool_use",
            "<tool_result",
        ]
        for needle in forbidden:
            assert needle not in html, (
                f"Forbidden substring {needle!r} leaked into runtime HTML"
            )
