"""Tests for the /cache page rendering and handler contract."""

from __future__ import annotations

import re

from eggpool.dashboard.render import (
    CacheAdvancedState,
    _build_cache_advanced_state,
    _cache_advanced_state_label,
    _render_cache_request_shaping_fallback,
    _render_request_shaping_summary_panel,
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
            routing_runtime={
                "guardrails": {
                    "routing_cache_compression_mode": "custom-mode",
                    "routing_uses_cache_metrics": False,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                },
            },
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
        assert "mode custom-mode" in html
        assert "3 requests" in html
        assert "42 tokens saved" in html
        assert "4 recommendations" in html
        assert "Warnings" in html
        assert "Routing isolation" in html

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


def _base_summary(**overrides: object) -> dict[str, object]:
    """Return a quiet / clean request-shaping summary for /cache tests."""
    base: dict[str, object] = {
        "mode": {
            "compression": "off",
            "synthetic_cache": "off",
            "tuning": "off",
            "routing": "reporting_only",
        },
        "compression": {
            "requests_analyzed": 0,
            "requests_compressed": 0,
            "estimated_savings_tokens": 0,
            "actual_savings_tokens": 0,
            "failed_fallback_count": 0,
            "warning_count": 0,
        },
        "cache": {
            "cache_counter_reported_rate": 0.5,
            "cache_counter_reported_rows": 12,
            "cache_counter_known_rows": 20,
            "cached_input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "native_cache_observed_requests": 0,
        },
        "segmentation": {
            "requests_segmented": 0,
            "requests_not_collected": 0,
            "requests_empty_request": 0,
            "requests_parse_failure": 0,
            "protected_requests": 0,
            "compressible_candidate_requests": 0,
        },
        "synthetic_cache": {
            "dry_run_count": 0,
            "applied_count": 0,
            "candidate_count": 0,
            "warning_count": 0,
        },
        "tuning": {
            "recommendation_count": 0,
            "override_count": 0,
        },
        "guardrails": {
            "routing_uses_cache_metrics": False,
            "routing_uses_compression_metrics": False,
            "routing_uses_stable_prefix_hash": False,
            "routing_uses_compression_policy": False,
            "stable_prefix_preserved_rate": 1.0,
            "failed_fallback_count": 0,
            "policy_warning_count": 0,
        },
    }
    for key, value in overrides.items():
        base[key] = value
    return base


class TestRenderCacheSummaryCanonicalKeys:
    """Provider cache counters use canonical payload keys from the summary builder."""

    def test_provider_cache_subtext_uses_canonical_rows(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(),
        )
        assert "12 provider-reported rows" in html
        assert "20 classified rows" in html

    def test_synthetic_candidate_count_not_under_provider_cache(self) -> None:
        """Synthetic candidate count must not appear in the provider cache card."""
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                synthetic_cache={
                    "dry_run_count": 3,
                    "applied_count": 0,
                    "candidate_count": 7,
                    "warning_count": 0,
                },
            ),
        )
        provider_idx = html.find("Provider cache counters")
        annotation_idx = html.find("EggPool cache annotations")
        assert provider_idx != -1
        assert annotation_idx != -1
        provider_block = html[provider_idx:annotation_idx]
        # Synthetic candidate count is rendered only under the annotation card.
        assert "7 candidates" not in provider_block

    def test_synthetic_candidate_count_renders_under_annotation_card(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                synthetic_cache={
                    "dry_run_count": 3,
                    "applied_count": 0,
                    "candidate_count": 7,
                    "warning_count": 0,
                },
            ),
        )
        annotation_idx = html.find("EggPool cache annotations")
        assert annotation_idx != -1
        annotation_block = html[annotation_idx : annotation_idx + 1500]
        assert "7 candidates" in annotation_block
        assert "3 dry run" in annotation_block
        assert "0 applied" in annotation_block


class TestRenderCacheSummaryEggPoolAnnotationCard:
    """The summary includes a distinct EggPool cache annotations card."""

    def test_eggpool_card_present(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(),
        )
        assert "EggPool cache annotations" in html

    def test_dry_run_distinct_from_apply(self) -> None:
        dry_run_html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                mode={
                    "compression": "off",
                    "synthetic_cache": "dry_run",
                    "tuning": "off",
                    "routing": "reporting_only",
                },
            ),
        )
        apply_html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                mode={
                    "compression": "off",
                    "synthetic_cache": "apply",
                    "tuning": "off",
                    "routing": "reporting_only",
                },
            ),
        )
        # The card metric label differs between dry-run and apply.
        dry_idx = dry_run_html.find("EggPool cache annotations")
        apply_idx = apply_html.find("EggPool cache annotations")
        assert dry_idx != -1
        assert apply_idx != -1
        # Probe a slice after the card title for the metric text.
        assert "Dry run" in dry_run_html[dry_idx : dry_idx + 800]
        assert "Apply" in apply_html[apply_idx : apply_idx + 800]

    def test_synthetic_warning_marks_card_warning(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                synthetic_cache={
                    "dry_run_count": 0,
                    "applied_count": 0,
                    "candidate_count": 0,
                    "warning_count": 2,
                },
            ),
        )
        # The annotation card should be present; the safety card also lights up.
        assert "EggPool cache annotations" in html
        assert "Warnings" in html


class TestRenderCacheSummaryQuietStates:
    """Safety quiet state renders Clean, routing quiet state renders Isolated."""

    def test_quiet_safety_is_clean(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(),
        )
        # Safety card must show Clean in the quiet state.
        assert "Clean" in html

    def test_quiet_routing_is_isolated(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(),
        )
        # Routing card must show Isolated in the quiet state.
        assert "Isolated" in html

    def test_safety_warnings_trigger_warning_metric(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                guardrails={
                    "routing_uses_cache_metrics": False,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                    "stable_prefix_preserved_rate": 1.0,
                    "failed_fallback_count": 2,
                    "policy_warning_count": 1,
                },
            ),
        )
        # Failed fallback + policy warning both light up the safety card.
        assert "Warnings" in html
        # Routing still isolated because routing guardrails are healthy.
        assert "Isolated" in html

    def test_routing_unhealthy_metric_is_unexpected(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                guardrails={
                    "routing_uses_cache_metrics": True,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                    "stable_prefix_preserved_rate": 1.0,
                    "failed_fallback_count": 0,
                    "policy_warning_count": 0,
                },
            ),
        )
        assert "Unexpected" in html

    def test_routing_subtext_includes_raw_mode(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                mode={
                    "compression": "off",
                    "synthetic_cache": "off",
                    "tuning": "off",
                    "routing": "reporting_only",
                },
            ),
        )
        # Raw mode survives in the subtext.
        assert "mode reporting_only" in html


class TestRenderCacheAdvancedDiagnosticsState:
    """Advanced diagnostics open/closed decisions are server-decided."""

    def test_quiet_payload_keeps_details_collapsed(self) -> None:
        html = render_cache(period="24h")
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" not in m.group(0)

    def test_quiet_payload_shows_show_label(self) -> None:
        html = render_cache(period="24h")
        assert "Show advanced diagnostics" in html

    def test_segmentation_parse_failure_opens_advanced(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                segmentation={
                    "requests_segmented": 0,
                    "requests_not_collected": 0,
                    "requests_empty_request": 0,
                    "requests_parse_failure": 3,
                    "protected_requests": 0,
                    "compressible_candidate_requests": 0,
                },
            ),
        )
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" in m.group(0)

    def test_synthetic_warning_opens_advanced(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                synthetic_cache={
                    "dry_run_count": 0,
                    "applied_count": 0,
                    "candidate_count": 0,
                    "warning_count": 1,
                },
            ),
        )
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" in m.group(0)

    def test_synthetic_applied_count_opens_advanced(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                synthetic_cache={
                    "dry_run_count": 0,
                    "applied_count": 4,
                    "candidate_count": 4,
                    "warning_count": 0,
                },
            ),
        )
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" in m.group(0)

    def test_tuning_recommendations_open_advanced(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                tuning={
                    "recommendation_count": 3,
                    "override_count": 0,
                },
            ),
        )
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" in m.group(0)

    def test_routing_guardrail_violation_opens_advanced(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                guardrails={
                    "routing_uses_cache_metrics": True,
                    "routing_uses_compression_metrics": False,
                    "routing_uses_stable_prefix_hash": False,
                    "routing_uses_compression_policy": False,
                    "stable_prefix_preserved_rate": 1.0,
                    "failed_fallback_count": 0,
                    "policy_warning_count": 0,
                },
            ),
        )
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" in m.group(0)

    def test_compression_warning_opens_advanced(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                compression={
                    "requests_analyzed": 0,
                    "requests_compressed": 0,
                    "estimated_savings_tokens": 0,
                    "actual_savings_tokens": 0,
                    "failed_fallback_count": 0,
                    "warning_count": 5,
                },
            ),
        )
        m = re.search(
            r'<details[^>]*id="advanced-diagnostics"[^>]*>',
            html,
        )
        assert m is not None
        assert " open" in m.group(0)

    def test_advanced_label_includes_needs_review_when_warnings(self) -> None:
        html = render_cache(
            period="24h",
            request_shaping_summary=_base_summary(
                segmentation={
                    "requests_segmented": 0,
                    "requests_not_collected": 0,
                    "requests_empty_request": 0,
                    "requests_parse_failure": 1,
                    "protected_requests": 0,
                    "compressible_candidate_requests": 0,
                },
            ),
        )
        assert "needs review" in html


class TestCacheAdvancedStateBuilder:
    """_build_cache_advanced_state produces structured open/closed state."""

    def test_quiet_state_is_collapsed(self) -> None:
        state = _build_cache_advanced_state(
            compression_runtime=None,
            guardrails={},
            request_shaping_summary=_base_summary(),
            transcoding_loss_warnings=0,
            has_any_data=False,
        )
        assert isinstance(state, CacheAdvancedState)
        assert state.open_by_default is False
        assert state.warning is False
        assert state.reasons == ()
        assert _cache_advanced_state_label(state) == "Show advanced diagnostics"

    def test_segmentation_parse_failure_adds_reason(self) -> None:
        state = _build_cache_advanced_state(
            compression_runtime=None,
            guardrails={},
            request_shaping_summary=_base_summary(
                segmentation={
                    "requests_segmented": 0,
                    "requests_not_collected": 0,
                    "requests_empty_request": 0,
                    "requests_parse_failure": 1,
                    "protected_requests": 0,
                    "compressible_candidate_requests": 0,
                },
            ),
            transcoding_loss_warnings=0,
            has_any_data=True,
        )
        assert "segmentation parse failures" in state.reasons
        assert state.open_by_default is True
        assert state.warning is True

    def test_synthetic_applied_is_active_not_warning(self) -> None:
        state = _build_cache_advanced_state(
            compression_runtime=None,
            guardrails={},
            request_shaping_summary=_base_summary(
                synthetic_cache={
                    "dry_run_count": 0,
                    "applied_count": 1,
                    "candidate_count": 1,
                    "warning_count": 0,
                },
            ),
            transcoding_loss_warnings=0,
            has_any_data=True,
        )
        assert "EggPool annotation applied" in state.reasons
        assert state.warning is False
        assert state.open_by_default is True
        assert _cache_advanced_state_label(state) == "Advanced diagnostics (1 active)"


class TestRenderCacheProviderCacheLabels:
    """Provider cache counter labels are operator-facing."""

    def test_renamed_card_titles_present(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 10,
                "by_status": {"reported": 4, "not_reported": 6},
                "per_protocol_status": {},
                "per_account_status": {
                    "acct-1": {
                        "total_requests": 5,
                        "total_cached_input_tokens": 100,
                    },
                },
                "per_model_status": {},
            },
        )
        assert "Rows with cache counters" in html
        assert "Rows without cache counters" in html
        assert "Unrecognized payload shape" in html
        assert "Read tokens (canonical)" in html

    def test_protocol_table_uses_renamed_columns(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 10,
                "by_status": {"reported": 4, "not_reported": 6},
                "per_protocol_status": {
                    ("prov-a", "openai"): {
                        "reported": 3,
                        "not_reported": 0,
                        "unknown_format": 0,
                    },
                },
                "per_account_status": {},
                "per_model_status": {},
            },
        )
        assert "With counters" in html
        assert "Without counters" in html
        assert "Unrecognized" in html

    def test_summary_table_uses_provider_reported_label(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 10,
                "by_status": {"reported": 4},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
        )
        assert "Read tokens (canonical)" in html

    def test_old_short_labels_absent(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 10,
                "by_status": {"reported": 4, "not_reported": 6},
                "per_protocol_status": {},
                "per_account_status": {},
                "per_model_status": {},
            },
        )
        # Old short labels are gone from the rendered output.
        assert ">Reported<" not in html
        assert ">Not reported<" not in html
        assert ">Unknown shape<" not in html
        assert "Cached input tokens (Reported)" not in html
        assert "Cached tokens (Reported)" not in html

    def test_panel_explains_missing_is_not_cache_miss(self) -> None:
        html = render_cache(
            period="24h",
            cache_observability={
                "total_requests": 10,
                "by_status": {"reported": 4, "not_reported": 6},
            },
        )
        # Panel copy must explain that missing counters are not cache misses.
        # The copy may wrap across whitespace so we collapse it before matching.
        collapsed = re.sub(r"\s+", " ", html)
        assert "are not cache misses" in collapsed


class TestRenderCachePanelIsolation:
    """Summary panel helper accepts structured input and renders cards."""

    def test_summary_panel_renders_eggpool_card(self) -> None:
        html = _render_request_shaping_summary_panel(
            _base_summary(),
            period="24h",
            guardrails_mode="reporting_only",
        )
        assert "EggPool cache annotations" in html

    def test_summary_panel_quiet_safety_clean(self) -> None:
        html = _render_request_shaping_summary_panel(
            _base_summary(),
            period="24h",
            guardrails_mode="reporting_only",
        )
        assert "Clean" in html
        assert "Isolated" in html
