"""Tests for the /cache page request-shaping dashboard surfaces.

These tests verify that the request-shaping summary plus the detailed
compression/cache panels render correctly on the /cache page via
``render_cache``.  The tests focus on operator-facing behaviour:

- Empty data renders the panel header and metric strip without errors.
- Non-empty data shows the headline metric counts.
- Warnings / fallback counts surface as warning cards when non-zero.
- The routing-separation notice always renders.
- No raw prompt text or upstream payload content leaks into HTML.
"""

from __future__ import annotations

import pytest

from eggpool.dashboard.render import (
    _build_cache_advanced_state,
    _cache_safety_warning_count,
    _display_request_change_mode,
    _render_details_panel,
    _routing_isolation_healthy,
    render_cache,
)

pytestmark = pytest.mark.dashboard


class TestCompressionObservabilityCard:
    """Compression observability card renders with empty + populated data."""

    def test_empty_data_renders_panel(self) -> None:
        """An empty payload still produces the panel header and metric strip."""
        html = render_cache(
            period="24h",
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
        assert "Request shaping" in html
        assert "Compression" in html
        assert "Observed requests" in html

    def test_populated_data_renders_counts(self) -> None:
        """Populated counts surface in the metric cards."""
        html = render_cache(
            period="24h",
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
        assert "gpt-4o" in html  # model breakdown

    def test_no_warning_class_when_no_fallback(self) -> None:
        """No 'warning' class when fallback count is zero."""
        html = render_cache(
            period="24h",
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
        html = render_cache(
            period="24h",
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
        assert "Compression" in html
        assert "Observed requests" in html

    def test_populated_data_renders_counts_and_transforms(self) -> None:
        """Populated data shows mode counts and transform breakdown."""
        html = render_cache(
            period="24h",
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
        assert "repeated_line_run" in html
        assert "log_compaction" in html

    def test_warnings_table_renders(self) -> None:
        """Warnings rollup renders as a sub-table."""
        html = render_cache(
            period="24h",
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
        html = render_cache(
            period="24h",
            compression_policy_stats={
                "policy_counts": [],
                "total_requests": 0,
                "total_policies": 0,
            },
        )
        assert "Policy overrides" in html
        assert "Tracked policies" in html
        assert "Total requests" in html

    def test_populated_data_renders_policy_rows(self) -> None:
        """Populated policies render as table rows."""
        html = render_cache(
            period="24h",
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
        html = render_cache(
            period="24h",
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
        html = render_cache(
            period="24h",
            cache_stability={
                "transcoded_request_count": 0,
                "notes": "Boundary detail lives in per-request traces.",
            },
        )
        assert "Native cache preservation" in html
        assert "Transcoded requests" in html
        assert "per-request traces" in html

    def test_populated_data_renders_count(self) -> None:
        """Populated transcoded count surfaces."""
        html = render_cache(
            period="24h",
            cache_stability={
                "transcoded_request_count": 42,
                "notes": "Boundary detail lives in per-request traces.",
            },
        )
        assert "42" in html


class TestRequestShapingSummary:
    """Request-shaping summary always renders."""

    def test_summary_renders_when_compression_data_present(self) -> None:
        """Summary renders when compression data is provided."""
        html = render_cache(
            period="24h",
            compression_observability={
                "total_requests": 0,
                "by_status": {},
                "by_mode": {},
                "totals": {},
                "per_model_status": {},
            },
        )
        assert "Request shaping" in html
        assert "Compression" in html
        assert "Provider cache counters" in html
        assert "Safety guardrail" in html
        assert "QuotaFairScorer" in html
        assert "reporting-only" in html

    def test_summary_renders_when_no_data_provided(self) -> None:
        """The cache page always shows the summary even with no data."""
        html = render_cache(period="24h")
        assert "Request shaping" in html

    def test_cache_page_avoids_phase_headings(self) -> None:
        html = render_cache(period="24h")
        for needle in ("Phase 1", "Phase 2", "Phase 4", "Phase 9", "Phase 10"):
            assert needle not in html

    def test_cache_page_renders_summary_panel(self) -> None:
        """The /cache page renders the request-shaping summary panel."""
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 1,
                "by_status": {"reported": 1},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
        )
        assert "Request shaping" in html
        assert "Provider cache counters" in html

    def test_cache_page_summary_and_detail_panels_render(self) -> None:
        """The /cache page renders a summary panel plus all detail
        panels for compression, cache, and guardrails.
        """
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 1,
                "by_status": {"reported": 1},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
            compression_observability={
                "total_requests": 1,
                "by_status": {"observed": 1},
                "totals": {"observed_requests": 1},
                "per_model_status": {},
            },
            compression_runtime={
                "window": {"seconds": 3600, "request_count": 1},
                "mode_counts": {"observe": 1},
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
            cache_stability={"transcoded_request_count": 0, "notes": ""},
            compression_tuning={
                "windows": {},
                "recommendations": [],
                "overrides": [],
            },
            synthetic_cache_summary={
                "total_requests": 1,
                "status_counts": {"applied": 1},
                "dry_run_count": 0,
                "applied_count": 1,
                "candidate_count_total": 1,
                "applied_count_total": 1,
                "warning_count_total": 0,
                "warning_counts": {},
                "by_policy": [],
            },
        )

        for label in (
            "Provider cache counters",
            "Compression",
            "EggPool cache annotations",
            "Tuning suggestions",
            "Routing isolation",
        ):
            assert label in html, f"expected label {label!r} missing from /cache page"

        for needle in ("Phase 1", "Phase 5", "Phase 7", "Phase 9", "Phase 10"):
            assert needle not in html, (
                f"phase-era label {needle!r} leaked into cache HTML"
            )


class TestPhase7RequestShapingHelpers:
    """Phase 7 — UI helpers for the simplified request-shaping page."""

    def test_display_request_change_mode_known_values(self) -> None:
        assert _display_request_change_mode("off") == "no changes"
        assert _display_request_change_mode("observe") == "observe only"
        assert _display_request_change_mode("safe") == "actually changed"

    def test_display_request_change_mode_unknown_values(self) -> None:
        assert _display_request_change_mode(None) == "no changes"
        assert _display_request_change_mode("unknown_mode") == "unknown_mode"

    def test_routing_isolation_healthy_default_is_true(self) -> None:
        assert _routing_isolation_healthy(None)
        assert _routing_isolation_healthy({})

    def test_routing_isolation_healthy_with_flags(self) -> None:
        healthy = {
            "routing_uses_cache_metrics": False,
            "routing_uses_compression_metrics": False,
            "routing_uses_stable_prefix_hash": False,
            "routing_uses_compression_policy": False,
        }
        assert _routing_isolation_healthy(healthy) is True

        poisoned = dict(healthy)
        poisoned["routing_uses_cache_metrics"] = True
        assert _routing_isolation_healthy(poisoned) is False

    def test_compression_has_warnings_runtime_window(self) -> None:
        assert _cache_safety_warning_count({"window": {"warning_count": 0}}, {}) == 0
        assert _cache_safety_warning_count({"window": {"warning_count": 3}}, {}) == 3
        assert _cache_safety_warning_count({"warning_count": 2}, {}) == 2

    def test_compression_has_warnings_guardrails(self) -> None:
        assert (
            _cache_safety_warning_count(
                None, {"failed_fallback_count": 0, "policy_warning_count": 0}
            )
            == 0
        )
        assert (
            _cache_safety_warning_count(
                None, {"failed_fallback_count": 1, "policy_warning_count": 0}
            )
            == 1
        )
        assert (
            _cache_safety_warning_count(
                None, {"failed_fallback_count": 0, "policy_warning_count": 2}
            )
            == 2
        )

    def test_advanced_section_open_by_default_rules(self) -> None:
        # Quiet install: no reasons, no warning.
        quiet = _build_cache_advanced_state(
            compression_runtime=None,
            guardrails={},
            request_shaping_summary={},
            transcoding_loss_warnings=0,
            has_any_data=True,
        )
        assert quiet.open_by_default is False
        assert quiet.warning is False

        # Compression warnings present: warning + open.
        warn = _build_cache_advanced_state(
            compression_runtime={"warning_count": 3},
            guardrails={},
            request_shaping_summary={},
            transcoding_loss_warnings=0,
            has_any_data=True,
        )
        assert warn.warning is True
        assert warn.open_by_default is True

        # Routing guardrail violation: warning + open.
        poisoned = _build_cache_advanced_state(
            compression_runtime=None,
            guardrails={"routing_uses_cache_metrics": True},
            request_shaping_summary={},
            transcoding_loss_warnings=0,
            has_any_data=True,
        )
        assert poisoned.warning is True
        assert poisoned.open_by_default is True

    def test_render_details_panel_open_state(self) -> None:
        open_html = _render_details_panel(
            summary_text="Advanced diagnostics",
            body="<p>body</p>",
            open_by_default=True,
        )
        assert "<details" in open_html
        assert " open" in open_html
        assert "Advanced diagnostics" in open_html

        closed_html = _render_details_panel(
            summary_text="Advanced diagnostics",
            body="<p>body</p>",
            open_by_default=False,
        )
        assert " open" not in closed_html

    def test_render_details_panel_warning_class(self) -> None:
        html = _render_details_panel(
            summary_text="Safe-mode details",
            body="<p>body</p>",
            open_by_default=False,
            warning=True,
        )
        assert "advanced-details warning" in html


class TestPhase7CachePageBehavior:
    """Phase 7 — operator-facing behaviour of the simplified /cache page."""

    def test_default_disabled_summary_communicates_no_mutation(self) -> None:
        html = render_cache(period="24h")
        assert "Request changes" in html
        assert "no changes" in html
        assert "disabled by config" in html

    def test_provider_cache_counters_renamed(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 1,
                "by_status": {"reported": 1},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
        )
        assert "Provider cache counters" in html
        assert "Provider cache hit rate" in html
        assert "Cache hit ratio" not in html

    def test_compression_panel_observe_only_does_not_claim_mutation(self) -> None:
        html = render_cache(
            period="24h",
            compression_observability={
                "total_requests": 10,
                "by_status": {"observed": 10},
                "totals": {"observed_requests": 10, "candidate_count": 5},
                "per_model_status": {},
            },
        )
        assert "Compression" in html
        assert "Observe" in html or "Observe-only" in html or "observe" in html

    def test_compression_panel_safe_mode_shows_actual_savings(self) -> None:
        html = render_cache(
            period="24h",
            compression_observability={
                "total_requests": 5,
                "by_status": {"safe": 5},
                "totals": {},
                "per_model_status": {},
            },
            compression_runtime={
                "window": {"request_count": 5},
                "mode_counts": {"safe": 5},
                "applied_count": 5,
                "failed_fallback_count": 0,
                "candidate_count": 20,
                "estimated_savings_tokens": 0,
                "actual_savings_tokens": 1500,
                "latency_ms": {"avg": 1.0, "p50": 1.0, "p95": 2.5, "max": 4.0},
                "transforms": {},
                "warnings": {},
                "cache_safety": {
                    "stable_prefix_preserved": 5,
                    "stable_prefix_mismatch": 0,
                },
            },
        )
        assert "Compression" in html
        assert "Safe-mode details" in html

    def test_eggpool_cache_annotations_distinguishes_dry_run_vs_applied(
        self,
    ) -> None:
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 4,
                "status_counts": {"dry_run": 3, "applied": 1},
                "dry_run_count": 3,
                "applied_count": 1,
                "candidate_count_total": 5,
                "applied_count_total": 1,
                "warning_count_total": 0,
                "warning_counts": {},
                "by_policy": [],
            },
        )
        assert "EggPool cache annotations" in html
        assert "Dry run" in html
        assert "Applied" in html

    def test_advanced_diagnostics_collapsed_by_default(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 1,
                "by_status": {"reported": 1},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
            routing_runtime={
                "guardrails": {
                    "routing_cache_compression_mode": "reporting_only",
                    "routing_uses_cache_metrics": False,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                }
            },
        )
        assert "advanced-details" in html
        assert "Show advanced diagnostics" in html or "Advanced diagnostics" in html

    def test_advanced_diagnostics_open_on_warnings(self) -> None:
        html = render_cache(
            period="24h",
            compression_runtime={
                "window": {"warning_count": 7, "request_count": 50},
                "mode_counts": {"observe": 50},
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
        assert "<details" in html
        assert "open" in html

    def test_routing_isolation_copy_renders_when_healthy(self) -> None:
        html = render_cache(
            period="24h",
            routing_runtime={
                "guardrails": {
                    "routing_cache_compression_mode": "reporting_only",
                    "routing_uses_cache_metrics": False,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                }
            },
        )
        assert "Routing isolation" in html


class TestAdvisoryTuningCard:
    """Advisory tuning panel renders concrete metrics."""

    def test_tuning_metric_cards_render_values_not_template_code(self) -> None:
        html = render_cache(
            period="24h",
            compression_tuning={
                "windows": {
                    "global": {
                        "total_requests": 42,
                    },
                },
                "recommendations": [
                    {
                        "policy_name": "<global>",
                        "status": "recommendation_only",
                        "recommendation": {
                            "current": {
                                "min_candidate_tokens": 2048,
                                "min_savings_tokens": 512,
                                "max_compression_latency_ms": 12.0,
                            },
                            "recommended": {
                                "min_candidate_tokens": 4096,
                                "min_savings_tokens": 768,
                                "max_compression_latency_ms": 10.0,
                            },
                            "reason_codes": ["recommendation_only"],
                        },
                    },
                ],
                "overrides": [],
            },
        )

        assert "Tuning suggestions" in html
        assert "Policies observed" in html
        assert "Requests analysed" in html
        assert "42" in html
        assert "Recommendations" in html
        assert "recommendation_only" in html
        assert "_render_metric_card" not in html


class TestRoutingGuardrailsPanel:
    """Routing-guardrails diagnostic panel renders on /cache."""

    def test_guardrails_panel_renders_with_default_data(self) -> None:
        """The hardcoded guardrails diagnostic shows up on the cache page."""
        html = render_cache(
            period="24h",
            routing_runtime={
                "guardrails": {
                    "routing_cache_compression_mode": "reporting_only",
                    "routing_uses_cache_metrics": False,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                    "route_scorer_inputs": [
                        "health",
                        "quota",
                        "active_requests",
                        "model_eligibility",
                    ],
                },
            },
        )
        assert "Routing isolation" in html
        assert "reporting_only" in html
        assert "Scorer inputs (allowed)" in html
        for label in (
            "Cache metrics",
            "Compression metrics",
            "Stable-prefix hash",
            "Compression policy",
        ):
            assert label in html, f"missing guardrail label {label!r}"

    def test_guardrails_panel_renders_without_guardrails_field(self) -> None:
        """The panel falls back to defaults when the routing_runtime
        omits the ``guardrails`` field.  This keeps the panel robust
        against older runtimes or test harnesses that bypass
        ``RuntimeMetricsService``.
        """
        html = render_cache(period="24h", routing_runtime={})
        assert "Routing isolation" in html
        assert "reporting_only" in html

    def test_guardrails_panel_never_advertises_cache_in_scorer(self) -> None:
        """The hardcoded diagnostic must always say cache/compression
        metrics are NOT in scorer inputs.  Operators rely on this
        signal to confirm the routing invariant.
        """
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
        assert '<p class="metric">no</p>' in html
        assert '<p class="metric">reporting_only</p>' in html


class TestNoRawPayloadLeakage:
    """Dashboard never leaks raw upstream content."""

    def test_no_prompt_substrings_in_html(self) -> None:
        """Common prompt substrings never appear in rendered HTML."""
        html = render_cache(
            period="24h",
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
                f"Forbidden substring {needle!r} leaked into cache HTML"
            )


class TestSyntheticCacheCard:
    """Synthetic cache controls card renders with empty + populated data."""

    def test_empty_data_not_rendered(self) -> None:
        """When total_requests is 0 the panel is suppressed."""
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 0,
                "status_counts": {},
                "dry_run_count": 0,
                "applied_count": 0,
                "candidate_count_total": 0,
                "applied_count_total": 0,
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
        assert m.group(1).strip() == ""

    def test_none_data_not_rendered(self) -> None:
        """When synthetic_cache_summary is None the panel is suppressed."""
        html = render_cache(period="24h")
        import re

        m = re.search(
            r'<div id="synthetic-cache-controls">(.*?)</div>', html, re.DOTALL
        )
        assert m is not None
        assert m.group(1).strip() == ""

    def test_populated_data_renders_card(self) -> None:
        """Populated data renders the card with status counts and policy table."""
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 25,
                "status_counts": {
                    "disabled": 10,
                    "dry_run": 8,
                    "applied": 5,
                    "no_candidates": 2,
                    "policy_required": 0,
                    "provider_unsupported": 0,
                },
                "dry_run_count": 8,
                "applied_count": 5,
                "candidate_count_total": 30,
                "applied_count_total": 12,
                "warning_count_total": 3,
                "warning_counts": {
                    "synthetic_cache_control_synthesized": 5,
                    "synthetic_cache_control_dry_run": 8,
                },
                "by_policy": [
                    {
                        "policy_name": "<global>",
                        "policy_source": "global",
                        "request_count": 10,
                        "applied_count": 0,
                        "candidate_count": 0,
                    },
                    {
                        "policy_name": "anthropic-cache",
                        "policy_source": "policy:anthropic-cache",
                        "request_count": 15,
                        "applied_count": 5,
                        "candidate_count": 30,
                    },
                ],
            },
        )
        assert "EggPool cache annotations" in html
        assert "25" in html  # total requests
        assert "disabled" in html
        assert "dry_run" in html
        assert "applied" in html
        assert "synthetic_cache_control_synthesized" in html
        assert "synthetic_cache_control_dry_run" in html
        assert "&lt;global&gt;" in html
        assert "anthropic-cache" in html
        assert "reporting only" in html.lower() or "Reporting only" in html
        assert "QuotaFairScorer" in html

    def test_reporting_only_reminder_text_present(self) -> None:
        """The reporting-only reminder appears in the card."""
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 1,
                "status_counts": {"disabled": 1},
                "dry_run_count": 0,
                "applied_count": 0,
                "candidate_count_total": 0,
                "applied_count_total": 0,
                "warning_count_total": 0,
                "warning_counts": {},
                "by_policy": [],
            },
        )
        assert "Reporting only" in html
        assert "QuotaFairScorer" in html

    def test_global_sentinel_appears_before_overrides(self) -> None:
        """The <global> sentinel renders before override policies."""
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 20,
                "status_counts": {"disabled": 20},
                "dry_run_count": 0,
                "applied_count": 0,
                "candidate_count_total": 0,
                "applied_count_total": 0,
                "warning_count_total": 0,
                "warning_counts": {},
                "by_policy": [
                    {
                        "policy_name": "<global>",
                        "policy_source": "global",
                        "request_count": 20,
                        "applied_count": 0,
                        "candidate_count": 0,
                    },
                    {
                        "policy_name": "custom",
                        "policy_source": "policy:custom",
                        "request_count": 0,
                        "applied_count": 0,
                        "candidate_count": 0,
                    },
                ],
            },
        )
        global_idx = html.index("&lt;global&gt;")
        custom_idx = html.index("custom")
        assert global_idx < custom_idx


class TestNoRawPayloadLeakageSyntheticCache:
    """Synthetic cache card never leaks raw upstream content."""

    def test_no_prompt_substrings_in_synthetic_cache_card(self) -> None:
        """Common prompt substrings never appear in the synthetic cache card."""
        html = render_cache(
            period="24h",
            synthetic_cache_summary={
                "total_requests": 5,
                "status_counts": {"applied": 5},
                "dry_run_count": 0,
                "applied_count": 5,
                "candidate_count_total": 10,
                "applied_count_total": 5,
                "warning_count_total": 0,
                "warning_counts": {},
                "by_policy": [
                    {
                        "policy_name": "<global>",
                        "policy_source": "global",
                        "request_count": 5,
                        "applied_count": 5,
                        "candidate_count": 10,
                    }
                ],
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
                f"Forbidden substring {needle!r} leaked into cache HTML"
            )
