use std::{fs, path::PathBuf};

use super::html_escape;
use crate::server::middleware::{is_loopback_host, valid_key_shape, verify_api_key};
use axum::http::{HeaderMap, HeaderValue};
use serde::Deserialize;
use sha2::{Digest, Sha256};

const RENDER_MODULE_SOURCES: [&str; 9] = [
    include_str!("render/mod.rs"),
    include_str!("render/layout.rs"),
    include_str!("render/overview.rs"),
    include_str!("render/accounts.rs"),
    include_str!("render/models.rs"),
    include_str!("render/telemetry.rs"),
    include_str!("render/diagnostics.rs"),
    include_str!("render/runtime.rs"),
    include_str!("render/cache.rs"),
];

#[test]
fn page_renderers_do_not_acquire_runtime_or_database_authority() {
    for source in RENDER_MODULE_SOURCES {
        assert!(!source.contains("DashboardRepository"));
        assert!(!source.contains("UsageRollupRepository"));
        assert!(!source.contains("AppState"));
        assert!(!source.contains(".await"));
    }
}

#[derive(Debug, Deserialize)]
struct AssetRecord {
    path: String,
    sha256: String,
}

#[test]
fn escape_covers_markup_and_quotes() {
    assert_eq!(
        html_escape("</script> & \" '"),
        "&lt;/script&gt; &amp; &quot; &#x27;"
    );
}

#[test]
fn runtime_age_uses_bounded_human_units() {
    assert_eq!(super::format_runtime_age(std::time::Duration::ZERO), "<1s");
    assert_eq!(
        super::format_runtime_age(std::time::Duration::from_secs(3_661)),
        "1h1m"
    );
}

#[test]
fn runtime_host_platform_label_is_stable_and_nonempty() {
    let label = super::host_platform_label();
    assert!(label.contains('-'));
    assert!(!label.contains(std::path::MAIN_SEPARATOR));
}

#[test]
fn load_average_summary_is_bounded_and_never_spawns_a_process() {
    let summary = super::load_average_summary();
    assert!(!summary.is_empty());
    #[cfg(not(target_os = "linux"))]
    assert_eq!(summary, "load average unavailable");
    #[cfg(target_os = "linux")]
    assert!(summary == "load average unavailable" || summary.ends_with(" CPUs"));
}

#[test]
fn theme_variables_match_dashboard_translation_contract() {
    let css = super::theme_variables("Catppuccin Latte");
    for declaration in [
        "--page-bg: #DCE0E8;",
        "--topbar-border: #9CA0B0;",
        "--nav-text: #6C6F85;",
        "--button-border: #cacdd6;",
        "--link-color-hover: #0951df;",
        "--tag-success-bg: #c4d6cb;",
        "--heatmap-3: #2c6f1e;",
    ] {
        assert!(css.contains(declaration), "missing {declaration}");
    }
    assert_eq!(css.matches("--").count(), 46);
}

#[test]
fn every_selectable_theme_resolves_to_a_full_variable_set() {
    // `default` is the first entry in the theme picker but has no TOML of its
    // own. It used to serve an empty stylesheet, and because dashboard.css
    // declares no custom properties of its own that left the whole page
    // unstyled (and the SVG watermark on its initial fill).
    for name in super::THEME_NAMES {
        let css = super::theme_variables(name);
        assert!(
            css.starts_with(":root {"),
            "theme {name:?} produced no :root block"
        );
        assert!(
            css.contains("--page-bg:")
                && css.contains("--card-bg:")
                && css.contains("--page-text:"),
            "theme {name:?} is missing a core variable"
        );
    }
    assert_eq!(
        super::theme_variables("default"),
        super::theme_variables(super::DEFAULT_THEME),
        "the `default` alias must match the configured default theme"
    );
    // A theme name outside the allowlist must still resolve rather than
    // degrade to an unstyled page.
    assert_eq!(super::selected_theme("not-a-theme"), super::DEFAULT_THEME);
}

#[test]
fn shell_navigation_only_ever_links_real_periods() {
    // The shell used to substitute sentinel values ("runtime", "recent") for
    // the period, which every page rejected with 400 — so on /runtime and
    // /traces all 13 nav links, the logo, and the theme selector were dead.
    for active_nav in ["runtime", "traces", "overview", "accounts"] {
        let html = super::render::render_dashboard_layout(
            "Page",
            active_nav,
            "24h",
            "Nord",
            15,
            "<p>body</p>".to_owned(),
            false,
        );
        // No non-canonical period token may appear in any generated link.
        for bad in ["period=runtime", "period=recent"] {
            assert!(
                !html.contains(bad),
                "{active_nav} shell emitted a rejected period token: {bad}"
            );
        }
        assert!(html.contains("period=24h"));
    }
}

#[test]
fn traces_form_submits_a_canonical_period() {
    // The traces page passed "recent" as the period argument, which landed in
    // the limit form's hidden input and the period selector, so Apply always
    // returned 400.
    let html = super::render_traces_page(&crate::db::DashboardData::default(), "24h", "Nord", 50);
    assert!(html.contains("name=\"period\" value=\"24h\""));
    assert!(!html.contains("value=\"recent\""));
    assert!(html.contains("<option value=\"24h\" selected=\"selected\">"));
}

#[test]
fn timeseries_chart_contract_uses_a_canvas_even_without_rows() {
    let empty = serde_json::json!({
        "bucket": "hour",
        "group_by": "provider_model",
        "metric": "requests",
        "limit": 12,
        "source": "empty",
        "degraded_reason": "rollup_empty",
        "buckets": [],
        "series": [],
        "points": [],
        "bucket_totals": [],
    });
    let html = super::render_timeseries_page(
        &crate::db::DashboardData::default(),
        "24h",
        "default",
        &empty,
    );
    assert!(html.contains("class=\"chart-container\""));
    assert!(html.contains("data-timeseries-controls"));
    assert!(html.contains("class=\"grouped-timeseries-chart\""));
    assert!(html.contains("class=\"grouped-timeseries-data\""));
    assert!(html.contains("Aggregate per bucket"));
    assert!(!html.contains("<section class=\"panel\" id=\"timeseries-chart\""));
}

#[test]
fn model_catalog_status_does_not_use_usage_as_availability() {
    let mut data = crate::db::DashboardData::default();
    data.models.push(crate::db::DashboardModelRow {
        model_id: "vendor/model".into(),
        provider_id: "provider".into(),
        resolution_status: "unresolved".into(),
        requests: 18,
        errors: 0,
        cost_microdollars: 0,
        input_tokens: 0,
        output_tokens: 0,
        avg_latency_ms: 0.0,
        ttft_requests: 0,
        avg_ttft_ms: 0.0,
        exact_count: 0,
        derived_count: 0,
        partial_count: 0,
        estimated_count: 0,
        unknown_count: 0,
        provider_reported_count: 0,
        estimated_cost_fraction: 0.0,
        cache_read_ratio: None,
        cache_write_ratio: None,
        reasoning_output_ratio: None,
        avg_cost_per_request: None,
        avg_cost_per_1k_tokens: None,
    });
    let provider_priorities = std::collections::BTreeMap::from([("provider".to_owned(), 17)]);
    let html = super::render_models_page(
        &data,
        "24h",
        "Nord",
        &[],
        &super::ModelFilters::default(),
        &provider_priorities,
    );
    assert!(html.contains("pill-configured\">configured"));
    assert!(html.contains("href=\"/models/vendor%2Fmodel?theme=Nord\""));
    assert!(html.contains("No model info available"));
    assert!(html.contains("<td data-priority=\"3\">17</td>"));
    let html = super::render_models_page(
        &data,
        "24h",
        "Nord",
        &[],
        &super::ModelFilters {
            availability: Some("available".into()),
            ..super::ModelFilters::default()
        },
        &std::collections::BTreeMap::new(),
    );
    assert!(html.contains("No models match the selected filters."));
}

#[test]
fn model_detail_uses_canonical_info_and_escapes_values() {
    let info = serde_json::json!({
        "status": "fresh",
        "summary": "<script>bad()</script>",
        "sparse": true,
        "last_seen_at": "2026-09-01T00:00:00Z",
        "last_refreshed_at": "2026-09-01T00:00:00Z",
        "next_refresh_at": "2026-10-03T00:00:00Z",
        "provenance": {"sources": {"<catalog>": {"source": "<catalog>"}}, "reconciled_at": "2026-09-01T00:00:00Z"},
        "conflicts": {"family": {"sources": {"source<&": "value<&"}, "selected": "<selected>", "reason": "<reason>"}},
        "detail": {
            "display_name": "<display>",
            "providers": ["provider<&"],
            "limits": {"effective_context": 32000, "external_output": 4096},
            "modalities": ["text", "image"],
            "supports_tools": true,
            "family": "<family>",
            "license": "MIT",
            "release_date": "2026-01-01",
            "external_ids": {"source<&": "id<&"},
            "benchmarks": [{"name": "Artificial Analysis Intelligence", "score": 42.5, "source": "artificial_analysis", "observed_at": "2026-09-01T00:00:00Z"}],
            "huggingface_metadata": {"downloads": 10, "tags": ["safe", "<tag>"]}
        }
    });
    let html = super::render_model_detail("model", Some(&info), &[]);
    assert!(html.contains("&lt;script&gt;bad()&lt;/script&gt;"));
    assert!(!html.contains("<script>bad()</script>"));
    assert!(html.contains("32,000"));
    for section in [
        "Summary",
        "Provider / Callability",
        "Metadata",
        "Benchmarks",
        "Hugging Face",
        "Conflicts",
        "Provenance",
    ] {
        assert!(
            html.contains(section),
            "missing model-info section {section}"
        );
    }
    assert!(html.contains("&lt;display&gt;"));
    assert!(html.contains("&lt;catalog&gt;"));
    assert!(!html.contains("<display>"));
    // A sparse model with a non-sparse status takes the sparse pill; the label
    // still carries the underlying status, so both must be present.
    assert!(html.contains("pill-sparse"));
    assert!(html.contains("fresh (sparse)"));
    assert!(!html.contains("pill-fresh"));
    assert!(html.contains("Last seen"));
}

#[test]
fn model_detail_observations_render_only_compact_metadata() {
    let observations = vec![serde_json::json!({
        "source": "<catalog>",
        "source_model_id": "model",
        "provider_id": "provider",
        "observed_at": "2026-01-01",
        "confidence": 0.8,
        "raw_json": "DO_NOT_RENDER_SENTINEL",
        "raw_hash": "DO_NOT_RENDER_HASH"
    })];
    let info = serde_json::json!({"status": "partial", "summary": "summary", "detail": {}});
    let html = super::render_model_detail("model", Some(&info), &observations);
    assert!(html.contains("&lt;catalog&gt;"));
    assert!(!html.contains("DO_NOT_RENDER"));
    assert!(html.contains("Observations"));
}

#[tokio::test]
async fn overview_uses_the_shared_layout_and_keeps_valid_empty_account_markup() {
    let directory = tempfile::tempdir().expect("temporary dashboard database");
    let database = crate::db::Database::open(crate::db::DatabaseConfig {
        path: directory
            .path()
            .join("dashboard.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..crate::db::DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    crate::db::MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let summary = crate::db::UsageRollupRepository::new(&database)
        .dashboard_summary_basic("24h")
        .await
        .expect("empty summary reads");
    let html = super::render_overview(
        &summary,
        super::OverviewPage {
            accounts: &[],
            page_data: &crate::db::DashboardData::default(),
            period: "24h",
            theme: "Nord",
            refresh_interval_s: 60,
            show_disabled: false,
            health_snapshots: &[],
        },
    );
    assert_eq!(html.matches("<!DOCTYPE html>").count(), 1);
    assert!(html.contains("class=\"topnav-menu\" id=\"topnav-menu\""));
    assert!(html.contains("/static/theme.css?theme=Nord"));
    assert!(html.contains("name=\"theme\" value=\"Nord\""));
    assert!(html.contains("<p class=\"empty\">No accounts configured.</p>"));
    assert!(html.contains("<canvas id=\"timeseries-chart\""));
    assert!(html.contains("data-chart-endpoint=\"/api/timeseries?period=24h&amp;bucket=hour\""));
    assert!(html.contains("Pending requests"));
    assert!(html.contains("First-attempt success"));
    assert!(html.contains("Top models"));
    assert!(html.contains("Token activity (last 180 days)"));
    assert!(html.contains("No activity data available."));
    assert_eq!(html.matches("id=\"dashboard-content\"").count(), 1);
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn dashboard_repository_loads_empty_telemetry_views() {
    let directory = tempfile::tempdir().expect("temporary dashboard database");
    let database = crate::db::Database::open(crate::db::DatabaseConfig {
        path: directory
            .path()
            .join("dashboard.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..crate::db::DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    crate::db::MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let data = crate::db::DashboardRepository::new(&database)
        .load("24h")
        .await
        .expect("dashboard view-model queries succeed");
    assert!(data.latency_percentiles.is_empty());
    assert!(data.routing_selection.is_empty());
    let stats = crate::db::DashboardRepository::new(&database)
        .observability_stats("24h")
        .await
        .expect("empty cache and runtime stats project");
    assert_eq!(
        stats["transcoding"],
        serde_json::json!({
            "native_count": 0, "per_direction": {}, "top_loss_warnings": [],
            "total": 0, "transcoded_count": 0
        })
    );
    assert_eq!(stats["cache_stability"]["transcoded_request_count"], 0);
    assert_eq!(
        stats["cache_observability"]["cache_counter_coverage_rate"],
        serde_json::Value::Null
    );
    assert_eq!(
        stats["cache_observability"]["provider_cache_hit_rate"],
        serde_json::Value::Null
    );
    assert_eq!(
        stats["cache_observability"],
        serde_json::json!({
            "by_status":{"not_reported":0,"reported":0,"unknown_format":0},
            "cache_benefited_request_rate":null,"cache_benefited_requests":0,
            "cache_counter_coverage_rate":null,"cache_counter_not_reported_requests":0,
            "cache_counter_reported_requests":0,"cache_counter_unknown_requests":0,
            "cache_eligible_input_tokens":0,"cache_eligible_requests":0,
            "cache_hit_ratio_known_only":null,"cache_read_tokens_canonical":0,
            "cache_write_rate":null,"cache_write_tokens_canonical":0,
            "inconsistent_cache_counter_rows":0,"input_tokens_total":0,"output_tokens_total":0,
            "per_account_status":{},"per_model_status":{},"per_protocol_status":{},
            "provider_cache_hit_rate":null,"requests_total":0,
            "total_cache_creation_input_tokens":0,"total_cache_read_input_tokens":0,
            "total_cache_write_input_tokens":0,"total_cached_input_tokens":0,
            "total_requests":0,"transcoded_requests":0
        })
    );
    assert_eq!(
        stats["canonical_request_segmentation"]["by_status"],
        serde_json::json!({
            "segmented": 0, "not_collected": 0, "parse_failure": 0, "empty_request": 0
        })
    );
    assert_eq!(stats["request_shaping"]["period"], "24h");
    assert_eq!(
        stats["request_shaping"],
        serde_json::json!({
            "cache":{"cache_counter_known_rows":0,"cache_counter_reported_rate":null,
                "cache_counter_reported_rows":0,"cache_read_tokens":0,"cache_write_tokens":0,
                "cached_input_tokens":0,"native_cache_observed_requests":0},
            "guardrails":{"routing_uses_cache_metrics":false,"routing_uses_stable_prefix_hash":false},
            "mode":{"routing":"reporting_only"},"period":"24h",
            "segmentation":{"compressible_candidate_requests":0,"protected_requests":0,
                "requests_empty_request":0,"requests_not_collected":0,"requests_parse_failure":0,
                "requests_segmented":0}
        })
    );
    database.close().await.expect("database closes");
}

#[test]
fn trace_renderer_does_not_emit_prohibited_error_content() {
    let mut data = crate::db::DashboardData::default();
    data.requests
        .push(crate::db::repositories::DashboardRequestRow {
            started_at: "2026-10-02 00:00:00".into(),
            account_name: "fixture-account".into(),
            provider_id: "fixture-provider".into(),
            model_id: "fixture-model".into(),
            status: "error".into(),
            status_code: Some(500),
            latency_ms: Some(1.0),
            input_tokens: 0,
            output_tokens: 0,
            error_class: Some("upstream_error".into()),
            error_message: Some(
                "PROMPT_SENTINEL BODY_SENTINEL TOOL_ARGS_SENTINEL CACHE_KEY_SENTINEL AUTH_SENTINEL"
                    .into(),
            ),
            protocol: "openai".into(),
            proxy_request_id: Some("safe-request-id".into()),
            reasoning_tokens: 0,
            thinking_characters: 0,
        });
    let html = super::render_traces_page(&data, "recent", "Nord", 50);
    for sentinel in [
        "PROMPT_SENTINEL",
        "BODY_SENTINEL",
        "TOOL_ARGS_SENTINEL",
        "CACHE_KEY_SENTINEL",
        "AUTH_SENTINEL",
    ] {
        assert!(!html.contains(sentinel), "trace page exposed {sentinel}");
    }
    assert!(html.contains("upstream_error"));
    assert!(html.contains("safe-req"));
}

#[test]
fn token_heatmap_emits_bounded_theme_aware_calendar_markup() {
    let rows = [crate::db::repositories::DashboardTokenActivityRow {
        day: "2026-10-01".to_owned(),
        total_tokens: 1234,
        requests: 2,
        bytes_received: 2048,
        bytes_emitted: 4096,
    }];
    let html = super::render_token_heatmap(&rows, "Cyber Red");
    assert!(html.starts_with("<div class=\"heatmap\"><svg"));
    assert!(html.contains("class=\"heatmap-cell\""));
    assert!(html.contains("fill=\"#16090c\""));
    assert!(html.contains("data-tooltip=\""));
    let hitboxes = html.matches("class=\"heatmap-hitbox\"").count();
    assert!(matches!(hitboxes, 182 | 189));
}

#[test]
fn bandwidth_heatmap_uses_byte_totals_and_byte_tooltips() {
    let rows = [crate::db::repositories::DashboardTokenActivityRow {
        day: "2026-10-01".to_owned(),
        total_tokens: 1234,
        requests: 2,
        bytes_received: 2048,
        bytes_emitted: 4096,
    }];
    let html = super::render_bandwidth_heatmap(&rows, "Cyber Red");
    assert!(html.contains("aria-label=\"Bandwidth activity (last 180 days)\""));
    assert!(html.contains("2.0 KB in · 4.1 KB out · 2 requests"));
    assert!(!html.contains("1234 tokens"));
}

#[test]
fn bandwidth_page_scopes_its_totals_to_the_submitted_account() {
    let account = |name: &str, received: i64, emitted: i64| crate::db::DashboardAccountRow {
        name: name.to_owned(),
        provider_id: "provider".into(),
        enabled: true,
        requests: 1,
        errors: 0,
        cost_microdollars: 0,
        input_tokens: 0,
        output_tokens: 0,
        exact_count: 0,
        derived_count: 0,
        partial_count: 0,
        estimated_count: 0,
        unknown_count: 0,
        provider_reported_count: 0,
        avg_latency_ms: 0.0,
        reserved_microdollars: 0,
        active_reservations: 0,
        bytes_received: received,
        bytes_emitted: emitted,
        estimated_cost_fraction: 0.0,
        cache_read_ratio: None,
        cache_write_ratio: None,
        reasoning_output_ratio: None,
        avg_cost_per_request: None,
        avg_cost_per_1k_tokens: None,
        utilization_5h: 0,
        utilization_7d: 0,
        utilization_30d: 0,
    };
    let data = crate::db::DashboardData {
        accounts: vec![
            account("account-a", 2048, 4096),
            account("account-b", 8192, 16384),
        ],
        cache: crate::db::DashboardCacheSummary {
            total_bytes_received: 10240,
            total_bytes_emitted: 20480,
            ..crate::db::DashboardCacheSummary::default()
        },
        ..crate::db::DashboardData::default()
    };

    // The account select used to be rendered and read by nothing, so a
    // submitted account produced a byte-identical unfiltered page.
    let filtered = super::render_bandwidth_page(&data, "24h", "default", Some("account-b"));
    assert!(filtered.contains(">8.2 KB<"), "{filtered}");
    assert!(filtered.contains(">16.4 KB<"), "{filtered}");
    assert!(
        filtered.contains("<option value=\"account-b\" selected>"),
        "{filtered}"
    );
    assert!(!filtered.contains(">10.2 KB<"), "{filtered}");
    // The 180-day heatmap has no account dimension; say so instead of letting
    // a filtered page imply it is scoped.
    assert!(
        filtered.contains("last 180 days, all accounts"),
        "{filtered}"
    );

    // The `(all accounts)` option submits an empty value, which means no
    // filter rather than an account named "".
    for unfiltered in [
        super::render_bandwidth_page(&data, "24h", "default", None),
        super::render_bandwidth_page(&data, "24h", "default", Some("")),
    ] {
        assert!(unfiltered.contains(">10.2 KB<"), "{unfiltered}");
        assert!(unfiltered.contains(">20.5 KB<"), "{unfiltered}");
        assert!(
            unfiltered.contains("<option value=\"\" selected>"),
            "{unfiltered}"
        );
        assert!(!unfiltered.contains("selected>account"), "{unfiltered}");
        assert!(
            !unfiltered.contains("180 days, all accounts"),
            "{unfiltered}"
        );
    }
}

#[test]
fn authentication_accepts_bearer_and_x_api_key() {
    let mut headers = HeaderMap::new();
    headers.insert("authorization", HeaderValue::from_static("Bearer test-key"));
    assert!(verify_api_key(&headers, "test-key"));
    headers.clear();
    headers.insert("x-api-key", HeaderValue::from_static("test-key"));
    assert!(verify_api_key(&headers, "test-key"));
}

#[test]
fn startup_key_shape_and_loopback_rules_are_bounded() {
    assert!(valid_key_shape("test-key"));
    assert!(!valid_key_shape("short"));
    assert!(is_loopback_host("127.0.0.1"));
    assert!(is_loopback_host("[::1]"));
    assert!(!is_loopback_host("0.0.0.0"));
}

#[test]
fn dashboard_asset_manifest_is_complete_and_stable() {
    let manifest: Vec<AssetRecord> =
        serde_json::from_str(include_str!("../../../assets/dashboard/manifest.json"))
            .expect("asset manifest is valid JSON");
    assert_eq!(manifest.len(), 54);
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for asset in manifest {
        let copied = root.join("assets/dashboard").join(&asset.path);
        let copied_bytes = fs::read(&copied).expect("copied asset exists");
        let digest = Sha256::digest(&copied_bytes);
        let actual = digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(actual, asset.sha256, "manifest drift: {}", asset.path);
    }
}
