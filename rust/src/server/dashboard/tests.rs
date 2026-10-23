use std::{fs, path::PathBuf};

use super::html_escape;
use crate::server::middleware::{is_loopback_host, valid_key_shape, verify_api_key};
use axum::http::{HeaderMap, HeaderValue};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio_rusqlite::rusqlite::params;

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
    assert_eq!(css.matches("--").count(), 52);
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
            "",
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

/// The overview page and the summary JSON reported the cache read ratio over
/// two different denominators: the page used `cache_read / (cache_read +
/// input)` while the JSON used `cache_read / (input + cache_read +
/// cache_write)`. The same installation therefore showed two cache read
/// ratios for the same data.
#[tokio::test]
async fn overview_html_and_summary_json_report_the_same_cache_read_ratio() {
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
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env) VALUES (1, 'account-a', 'KEY_A')",
                [],
            )?;
            connection.execute("INSERT INTO models (model_id) VALUES ('model-a')", [])?;
            connection.execute(
                "INSERT INTO requests (account_id, model_id, status, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, started_at) \
                 VALUES (1, 'model-a', 'completed', 600, 0, 300, 100, datetime('now','-1 minute'))",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture rows insert");

    let summary = crate::db::UsageRollupRepository::new(&database)
        .dashboard_summary_basic("24h")
        .await
        .expect("summary reads");
    let json = super::render::summary_json(&summary, "24h");
    let ratio = json["cache_read_ratio"]
        .as_f64()
        .expect("the summary reports a cache read ratio");
    // Total input tokens are the fresh input plus the cache read and cache
    // write tokens: 600 + 300 + 100.
    assert!(
        (ratio - 0.3).abs() < 1e-9,
        "unexpected cache read ratio {ratio}"
    );

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
    // The page reports the same share the JSON reports.
    assert!(
        html.contains("<p class=\"metric\">30.0%</p>"),
        "the page and the JSON report different cache read ratios: {html}"
    );
    // The chosen denominator is the one the existing labels describe: the
    // card reports the cache reads as a share of total input tokens, and the
    // dashboard counts input tokens without the cache counters.
    assert!(html.contains("cache reads / total input"));
    assert!(html.contains("cache-read tokens as a share of total input tokens"));
    assert!(html.contains(
        "Input plus output tokens recorded for the selected period, excluding cache-read and cache-write counters."
    ));
    database.close().await.expect("database closes");
}

/// The grouped timeseries weighted its first-byte average by the request
/// count, so a group that mixed streamed and non-streamed requests reported
/// an average the ungrouped timeseries never produced.
#[tokio::test]
async fn the_grouped_timeseries_reports_the_ungrouped_first_byte_average() {
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
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env) VALUES (1, 'account-a', 'KEY_A')",
                [],
            )?;
            connection.execute("INSERT INTO models (model_id) VALUES ('model-a')", [])?;
            // Two groups: the first carries two streamed requests, the second
            // one streamed request next to three non-streamed ones, so an
            // average weighted by the request count differs from an average
            // weighted by the first-byte observations.
            for (id, provider, streamed, first_byte_ms) in [
                (1_i64, "provider-a", 1_i64, Some(100_i64)),
                (2, "provider-a", 1, Some(200)),
                (3, "provider-b", 1, Some(600)),
                (4, "provider-b", 0, None),
                (5, "provider-b", 0, None),
                (6, "provider-b", 0, None),
            ] {
                connection.execute(
                    "INSERT INTO requests (id, account_id, model_id, provider_id, status, streamed, first_byte_ms, started_at) \
                     VALUES (?1, 1, 'model-a', ?4, 'completed', ?2, ?3, datetime('now','-1 minute'))",
                    params![id, streamed, first_byte_ms, provider],
                )?;
            }
            Ok(())
        })
        .await
        .expect("fixture rows insert");

    let repository = crate::db::DashboardRepository::new(&database);
    let plain = repository
        .timeseries_json("24h".into(), "hour".into(), None, None)
        .await
        .expect("plain timeseries");
    let (grouped, from_rollups) = repository
        .grouped_timeseries_json(
            "24h".into(),
            "hour".into(),
            "provider_model".into(),
            None,
            None,
        )
        .await
        .expect("grouped timeseries");
    let projection =
        super::grouped_timeseries_projection(&grouped, "hour", "provider_model", 12, from_rollups);
    let totals = projection["bucket_totals"]
        .as_array()
        .expect("bucket totals array");
    assert!(
        !totals.is_empty(),
        "the grouped projection reported no totals"
    );
    for total in totals {
        let bucket = total["bucket"].as_str().unwrap_or_default();
        let plain_average = plain
            .iter()
            .find(|row| row["bucket"].as_str() == Some(bucket))
            .and_then(|row| row["avg_ttft_ms"].as_f64())
            .unwrap_or_else(|| panic!("the ungrouped timeseries reported no row for {bucket}"));
        let grouped_average = total["avg_ttft_ms"].as_f64().unwrap_or(0.0);
        assert!(
            (grouped_average - plain_average).abs() < 1e-9,
            "the timeseries endpoints report different first-byte averages for {bucket}: \
             grouped {grouped_average} vs ungrouped {plain_average}"
        );
        // (100 + 200 + 600) / 3 streamed rows with a first-byte observation.
        assert!(
            (plain_average - 300.0).abs() < 1e-9,
            "unexpected first-byte average {plain_average}"
        );
    }
    database.close().await.expect("database closes");
}

/// The routing-policy facts the dashboard reports are derived from the
/// routing module's own reported capability surface, and a guard fails when
/// the reported fact diverges from that source.
#[tokio::test]
async fn reported_routing_policy_facts_are_derived_and_guarded() {
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

    let facts = crate::db::repositories::routing_policy_facts();
    assert!(
        facts.routing_isolated(),
        "the routing module reports no cache or compression signal: {facts:?}"
    );
    assert_eq!(facts.mode, "reporting_only");

    let stats = crate::db::DashboardRepository::new(&database)
        .observability_stats("24h")
        .await
        .expect("observability stats project");
    assert_eq!(
        crate::db::repositories::routing_policy_guard(&stats["request_shaping"], &facts),
        None,
        "the reported routing-policy facts diverge from the routing module"
    );

    // Enabling the capability in the routing module changes the derived
    // facts, and the guard then fails against a projection that still
    // reports the old value.
    let enabled = crate::db::repositories::routing_policy_facts_for(
        &crate::db::repositories::RoutingCapabilitySurface::from_components([
            "quota_score".to_owned(),
            "cache_read_tokens".to_owned(),
        ]),
    );
    assert!(enabled.uses_cache_metrics);
    assert!(!enabled.routing_isolated());
    assert_eq!(enabled.mode, "routing_aware");
    let divergence =
        crate::db::repositories::routing_policy_guard(&stats["request_shaping"], &enabled).expect(
            "the guard must fail when the capability is enabled without the projection changing",
        );
    assert!(
        divergence.contains("routing_uses_cache_metrics"),
        "unexpected divergence: {divergence}"
    );
    database.close().await.expect("database closes");
}

/// The telemetry renderer indexed a twelve-element month-name array with a
/// month value converted to `usize`, so an out-of-range month indexed out of
/// bounds.
#[test]
fn out_of_range_month_values_render_without_panicking() {
    for month in [0_i64, 13, -1, i64::MIN, i64::MAX] {
        let label = super::format::month_name(month);
        assert!(!label.is_empty(), "month {month} produced an empty label");
    }
    assert_eq!(super::format::month_name(1), "Jan");
    assert_eq!(super::format::month_name(12), "Dec");

    // The renderer routes every month label through the total lookup.
    let source = include_str!("render/telemetry.rs");
    assert!(
        !source.contains("months[("),
        "the telemetry renderer indexes a month array directly"
    );

    // The heatmap still renders its month labels.
    let rows = [crate::db::repositories::DashboardTokenActivityRow {
        day: "2026-10-01".to_owned(),
        total_tokens: 1234,
        requests: 2,
        bytes_received: 2048,
        bytes_emitted: 4096,
    }];
    let html = super::render_token_heatmap(&rows, "Cyber Red");
    assert!(html.contains("heatmap-label"));
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
    // The container is a horizontal scroller, so it carries the region role
    // and a focusable target — without them a keyboard user cannot reach the
    // weeks scrolled off the inline end. See the `.heatmap` rules in
    // `static/dashboard.css`.
    assert!(html.starts_with(
        "<div class=\"heatmap\" tabindex=\"0\" role=\"region\" aria-label=\"Token activity heatmap, scroll horizontally for older weeks\"><svg"
    ));
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

/// The dashboard stylesheet, read as text. The guards below are about the
/// stylesheet and the script as shipped, so they read the same bytes the
/// server serves.
const DASHBOARD_CSS: &str = include_str!("../../../assets/dashboard/static/dashboard.css");
const DASHBOARD_JS: &str = include_str!("../../../assets/dashboard/static/dashboard.js");

/// The sources that emit dashboard markup. `admin.rs` is here but not in
/// `RENDER_MODULE_SOURCES`: it is the page module, so it legitimately holds
/// the database handles the render modules must not touch.
const DASHBOARD_MARKUP_SOURCES: [&str; 11] = [
    include_str!("admin.rs"),
    include_str!("render/mod.rs"),
    include_str!("render/layout.rs"),
    include_str!("render/overview.rs"),
    include_str!("render/accounts.rs"),
    include_str!("render/models.rs"),
    include_str!("render/telemetry.rs"),
    include_str!("render/diagnostics.rs"),
    include_str!("render/runtime.rs"),
    include_str!("render/cache.rs"),
    include_str!("render/admin.rs"),
];

/// Selectors that were retired because nothing in the renderers or the script
/// produces them any more. Reintroducing one is a regression: it is a rule the
/// browser applies to markup that never arrives.
const RETIRED_CSS_SELECTORS: [&str; 9] = [
    "footer .footer-update-indicator",
    "footer .update-indicator",
    ".update-versions",
    ".update-command",
    ".update-copied",
    ".panel-header form.account-breakdown-filter",
    "td.yes",
    "td.no",
    ".chart-container-compact",
];

/// Script exports that were retired because no caller used them.
const RETIRED_SCRIPT_EXPORTS: [&str; 6] = [
    "fetchStats",
    "formatAgeSeconds",
    "formatPercent",
    "formatBytes",
    "formatMicrodollars",
    "initUpdateCommandCopy",
];

/// The class names the stylesheet uses that no literal producer can name:
/// either the renderers build them from data, or the class is a reserved
/// variant. Every other class in the stylesheet must have a producer.
const DYNAMIC_CSS_CLASSES: [&str; 21] = [
    "active",
    "warning",
    "pill-fresh",
    "pill-partial",
    "pill-sparse",
    "pill-stale",
    "pill-conflict",
    "pill-unmatched",
    "pill-source-unavailable",
    "pill-unknown",
    "auth_failed",
    "cooldown_active",
    "model_appeared",
    "model_discovered",
    "circuit_open",
    "circuit_close",
    "circuit_closed",
    "reservation_recovered",
    "catalog_refresh_failed",
    "pill-configured",
    "panel",
];

/// The stylesheet with its comments removed. A comment may name a retired
/// selector to explain why it went; only a rule brings one back.
fn dashboard_css_rules() -> String {
    let mut rules = String::new();
    let mut in_comment = false;
    for line in DASHBOARD_CSS.lines() {
        let mut remainder = line;
        let mut kept = String::new();
        loop {
            if in_comment {
                match remainder.find("*/") {
                    Some(end) => {
                        in_comment = false;
                        remainder = &remainder[end + 2..];
                    }
                    None => break,
                }
            } else {
                match remainder.find("/*") {
                    Some(start) => {
                        kept.push_str(&remainder[..start]);
                        in_comment = true;
                        remainder = &remainder[start + 2..];
                    }
                    None => {
                        kept.push_str(remainder);
                        break;
                    }
                }
            }
        }
        rules.push_str(&kept);
        rules.push('\n');
    }
    rules
}

#[test]
fn retired_stylesheet_selectors_stay_retired() {
    let rules = dashboard_css_rules();
    for selector in RETIRED_CSS_SELECTORS {
        assert!(
            !rules.contains(selector),
            "retired selector {selector} is back in the stylesheet"
        );
        // The script must not keep querying a retired class either: a query
        // for markup no renderer produces is a dead branch that reads like a
        // live one.
        assert!(
            !DASHBOARD_JS.contains(selector),
            "retired selector {selector} is still queried by the script"
        );
    }
}

#[test]
fn retired_script_exports_stay_retired() {
    for export in RETIRED_SCRIPT_EXPORTS {
        assert!(
            !DASHBOARD_JS.contains(&format!("namespace.{export} =")),
            "retired export {export} is back in the script"
        );
    }
}

#[test]
fn every_script_export_has_a_caller() {
    // An export nobody calls is dead weight that looks like an API. The
    // internal state holders are namespaced with a double underscore and are
    // not exports.
    for line in DASHBOARD_JS.lines() {
        let trimmed = line.trim();
        if !trimmed.starts_with("namespace.") || !trimmed.contains('=') {
            continue;
        }
        let rest = &trimmed["namespace.".len()..];
        let Some(equals) = rest.find('=') else {
            continue;
        };
        let name = rest[..equals].trim();
        if name.is_empty()
            || name.starts_with("__")
            || !name
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '_')
        {
            continue;
        }
        let uses = DASHBOARD_JS.matches(name).count()
            + DASHBOARD_MARKUP_SOURCES
                .iter()
                .map(|source| source.matches(name).count())
                .sum::<usize>();
        assert!(uses >= 2, "script export {name} has no caller");
    }
}

#[test]
fn every_stylesheet_class_has_a_producer() {
    let markup = DASHBOARD_MARKUP_SOURCES.join("\n");
    let rules = dashboard_css_rules();
    let mut classes: Vec<String> = Vec::new();
    for (index, character) in rules.char_indices() {
        if character != '.' {
            continue;
        }
        // A class name starts a selector, so the dot cannot follow a digit or
        // a word character: `0.025` is a length, not a class.
        if index > 0 {
            let previous = rules[..index].chars().next_back().unwrap_or(' ');
            if previous.is_ascii_alphanumeric() || previous == '-' || previous == '_' {
                continue;
            }
        }
        let name: String = rules[index + 1..]
            .chars()
            .take_while(|character| {
                character.is_ascii_alphanumeric() || *character == '-' || *character == '_'
            })
            .collect();
        if name.is_empty() || classes.contains(&name) {
            continue;
        }
        classes.push(name);
    }
    for class in classes {
        if DYNAMIC_CSS_CLASSES.contains(&class.as_str()) {
            continue;
        }
        assert!(
            markup.contains(&class) || DASHBOARD_JS.contains(&class),
            "stylesheet class .{class} has no producer"
        );
    }
}

#[test]
fn every_emitted_table_carries_the_table_class() {
    // The stylesheet scopes its table chrome to `table.data`, so a table
    // without the class renders unstyled. The renderers open their tables
    // through the shared constant, so the next table cannot forget it.
    for source in DASHBOARD_MARKUP_SOURCES {
        let mut rest = source;
        while let Some(index) = rest.find("<table") {
            rest = &rest[index..];
            let end = rest.find('>').map(|end| end + 1).unwrap_or(rest.len());
            let tag = &rest[..end];
            // The two shared constants spell the opening tag out; they are the
            // source of the tag every renderer uses, not a table of their own.
            let unescaped = tag.replace("\\\"", "\"");
            assert!(
                [
                    super::DASHBOARD_TABLE_OPEN,
                    super::DASHBOARD_TABLE_OPEN_COMPACT
                ]
                .contains(&unescaped.as_str()),
                "a table is opened without the shared class: {tag}"
            );
            rest = &rest[end..];
        }
    }
}

#[test]
fn the_theme_variable_count_is_pinned() {
    // The pill colours add six derived variables to the set the server
    // publishes. The count is asserted so a variable cannot be added or
    // dropped without the guard noticing.
    let css = super::theme_variables("Catppuccin Latte");
    assert_eq!(css.matches("--").count(), 52);
    for variable in [
        "--pill-neutral",
        "--pill-success",
        "--pill-info",
        "--pill-warning",
        "--pill-sparse",
        "--pill-error",
    ] {
        assert!(css.contains(&format!("{variable}: ")), "missing {variable}");
    }
}

#[test]
fn the_standalone_shells_resolve_the_theme() {
    // The on-prem shells used to link only the dashboard stylesheet, so every
    // `var()` they relied on was invalid and the page rendered unstyled.
    let usage = super::render_usage_admin(&[], "", "Nord");
    assert!(usage.contains("/static/dashboard.css"));
    assert!(usage.contains("/static/theme.css?theme=Nord"));
    let my_usage = super::render_my_usage("operator", &[], &[], "Catppuccin Latte");
    assert!(my_usage.contains("/static/theme.css?theme=Catppuccin%20Latte"));
    let standalone = super::dashboard_standalone_page("Audit", "usage", "Nord", "<p>body</p>");
    assert!(standalone.contains("/static/theme.css?theme=Nord"));
    // An unknown theme falls back to the default and still resolves a
    // complete variable set.
    let fallback = super::dashboard_standalone_page("Audit", "usage", "not-a-theme", "<p>b</p>");
    assert!(fallback.contains("/static/theme.css?theme=Cyber%20Red"));
    assert!(!super::theme_variables(super::selected_theme("not-a-theme")).is_empty());
}

#[test]
fn the_standalone_shells_carry_the_navigation_chrome() {
    for html in [
        super::render_usage_admin(&[], "", "Nord"),
        super::render_my_usage("operator", &[], &[], "Nord"),
        super::dashboard_standalone_page("Audit", "usage", "Nord", "<p>body</p>"),
    ] {
        assert!(
            html.contains("class=\"topnav-menu\""),
            "the shell has no navigation chrome"
        );
        assert!(
            html.contains("href=\"/?period=24h&amp;theme=Nord\">Overview</a>"),
            "the shell has no route back to the overview"
        );
        assert!(html.contains("id=\"dashboard-content\""));
    }
}

#[test]
fn a_truncated_usage_table_says_so() {
    // The notice is derived from the same cap the query applies, so the page
    // cannot claim a complete table when the query truncated it.
    let below_cap = super::usage_truncation_notice(super::USAGE_ROW_CAP - 1);
    assert!(below_cap.is_empty());
    let at_cap = super::usage_truncation_notice(super::USAGE_ROW_CAP);
    assert!(at_cap.contains("truncated"));
    assert!(at_cap.contains(&super::USAGE_ROW_CAP.to_string()));
}

#[test]
fn the_dashboard_shell_carries_a_skip_link_and_live_regions() {
    let html = super::render::render_dashboard_layout(
        "Page",
        "overview",
        "24h",
        "Nord",
        15,
        "<p>body</p>".to_owned(),
        false,
        "",
    );
    let first_focusable = html.find("<a ").expect("the first anchor");
    assert!(
        html[first_focusable..].starts_with("<a class=\"skip-link\""),
        "the skip link is not the first focusable element"
    );
    assert!(
        first_focusable < html.find("<header").expect("the topbar"),
        "the skip link is not the first focusable element"
    );
    assert!(html.contains("href=\"#dashboard-content\""));
    assert!(html.contains("id=\"dashboard-updated\" role=\"status\" aria-live=\"polite\""));
}

#[test]
fn the_theme_picker_preserves_the_page_query_parameters() {
    let html = super::render::render_dashboard_layout(
        "Page",
        "models",
        "24h",
        "Nord",
        15,
        "<p>body</p>".to_owned(),
        false,
        &super::dashboard_query_inputs(&[("account", "vendor/model"), ("used", "1")]),
    );
    assert!(html.contains("<input type=\"hidden\" name=\"account\" value=\"vendor/model\">"));
    assert!(html.contains("<input type=\"hidden\" name=\"used\" value=\"1\">"));
    assert!(html.contains("<input type=\"hidden\" name=\"period\" value=\"24h\">"));
}

#[test]
fn every_chart_canvas_carries_an_accessible_name() {
    let html = super::render_timeseries_page(
        &crate::db::DashboardData::default(),
        "24h",
        "Nord",
        &serde_json::json!({
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
        }),
    );
    assert!(html.contains("role=\"img\""));
    assert!(html.contains("aria-label=\"Usage breakdown grouped by"));
    // The chart loading shell is a live region: the operator has to learn
    // that the chart is loading, and that it failed.
    let overview = super::render_overview(
        &crate::db::DashboardSummary {
            total_requests: 0,
            successful_requests: 0,
            error_requests: 0,
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_cost_microdollars: 0,
            avg_latency_ms: 0.0,
            total_cache_read_tokens: 0,
            total_cache_write_tokens: 0,
            total_reasoning_tokens: 0,
            streamed_requests: 0,
            non_streamed_requests: 0,
            exact_count: 0,
            derived_count: 0,
            partial_count: 0,
            estimated_count: 0,
            unknown_count: 0,
            provider_reported_count: 0,
            provider_reported_cost_microdollars: 0,
            estimated_cost_sum_microdollars: 0,
            reservation_fallback_rows: 0,
            reservation_fallback_excess_microdollars: 0,
            total_bytes_received: 0,
            total_bytes_emitted: 0,
            total_providers: 0,
            avg_ttft_ms: 0.0,
            tokens_per_second: 0.0,
            p50_ttft_ms: 0.0,
            p99_ttft_ms: 0.0,
        },
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
    assert!(
        overview.contains("class=\"chart-loading-shell\" role=\"status\" aria-live=\"polite\"")
    );
    assert!(overview.contains("aria-label=\"Requests and errors over the selected period\""));
}

#[test]
fn every_stylesheet_variable_resolves_in_every_bundled_theme() {
    // The stylesheet declares no custom properties of its own, so every
    // `var()` it reads has to come from the theme. A variable the theme does
    // not publish leaves the declaration invalid, and the browser drops it.
    let rules = dashboard_css_rules();
    let mut required: Vec<String> = Vec::new();
    let mut rest = rules.as_str();
    while let Some(index) = rest.find("var(") {
        rest = &rest[index + 4..];
        let Some(close) = rest.find(')') else {
            break;
        };
        let argument = &rest[..close];
        // A fallback makes the declaration valid on its own.
        if argument.contains(',') {
            continue;
        }
        let name = argument.trim().to_owned();
        if !name.starts_with("--") {
            continue;
        }
        if !required.contains(&name) {
            required.push(name);
        }
        rest = &rest[close..];
    }
    assert!(!required.is_empty(), "no stylesheet variable was found");
    for theme in super::THEME_NAMES {
        let css = super::theme_variables(theme);
        for name in &required {
            assert!(
                css.contains(&format!("{name}: ")),
                "theme {theme:?} does not publish {name}, which the stylesheet reads"
            );
        }
    }
}

#[test]
fn the_stylesheet_declares_no_custom_property_of_its_own() {
    // A property the stylesheet declares would have to be published by every
    // theme as well; the themes are the only owner of the variable set.
    let rules = dashboard_css_rules();
    let mut rest = rules.as_str();
    while let Some(index) = rest.find("--") {
        rest = &rest[index..];
        let name: String = rest
            .chars()
            .take_while(|character| {
                character.is_ascii_alphanumeric() || *character == '-' || *character == '_'
            })
            .collect();
        let after = &rest[name.len()..];
        if let Some(tail) = after.strip_prefix(':') {
            assert!(
                tail.trim_start().starts_with("var("),
                "the stylesheet declares {name}, which the themes do not publish"
            );
        }
        rest = after;
    }
}
