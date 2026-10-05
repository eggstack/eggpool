use std::io::IsTerminal;

use super::*;

#[derive(Debug, Deserialize)]
pub(super) struct PeriodQuery {
    period: Option<String>,
    theme: Option<String>,
    show_disabled: Option<String>,
    account: Option<String>,
    used: Option<String>,
    info_status: Option<String>,
    availability: Option<String>,
    #[serde(rename = "type")]
    event_type: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, Default)]
pub(super) struct ModelFilters {
    account: Option<String>,
    used: Option<String>,
    info_status: Option<String>,
    availability: Option<String>,
    event_type: Option<String>,
    trace_limit: Option<usize>,
    /// Grouped-timeseries controls from the `/timeseries` filter form. These
    /// were previously rendered but never read, so submitting the form
    /// produced a byte-identical page while the controls looked live. Nested
    /// rather than a separate parameter so the page helpers stay within the
    /// clippy argument budget.
    timeseries: Option<TimeseriesFilters>,
    /// Bandwidth-page account filter. The page rendered this select and read
    /// nothing, so submitting it produced a byte-identical page while the
    /// control looked live — the same defect the timeseries controls had.
    bandwidth: Option<BandwidthFilters>,
}

/// Grouped-timeseries controls from the `/timeseries` filter form. These were
/// previously rendered but never read, so submitting the form produced a
/// byte-identical page while the controls appeared to be live.
#[derive(Debug, Default, Clone)]
pub(super) struct TimeseriesFilters {
    bucket: Option<String>,
    group_by: Option<String>,
    metric: Option<String>,
    limit: Option<usize>,
    account: Option<String>,
    model: Option<String>,
}

/// Account filter from the `/bandwidth` filter form. The totals it scopes are
/// per-account byte counters; the 180-day heatmap is an all-account rollup and
/// says so on the page.
#[derive(Debug, Default, Clone)]
pub(super) struct BandwidthFilters {
    account: Option<String>,
}

#[derive(Debug, Clone)]
pub(super) struct RoutingTraceSnapshot {
    mode: String,
    sample_rate: f64,
    status: String,
    accepted: u64,
    written: u64,
    dropped: u64,
    queue_depth: u64,
    queue_capacity: u64,
}

impl Default for RoutingTraceSnapshot {
    fn default() -> Self {
        Self {
            mode: "off".to_owned(),
            sample_rate: 0.0,
            status: "Off".to_owned(),
            accepted: 0,
            written: 0,
            dropped: 0,
            queue_depth: 0,
            queue_capacity: 0,
        }
    }
}

#[derive(Debug, Deserialize)]
pub(super) struct TimeseriesQuery {
    period: Option<String>,
    bucket: Option<String>,
    account: Option<String>,
    model: Option<String>,
    group_by: Option<String>,
    metric: Option<String>,
    limit: Option<usize>,
    theme: Option<String>,
}

#[derive(Debug, Deserialize)]
pub(super) struct ThemeQuery {
    theme: Option<String>,
}

pub(super) struct OverviewPage<'a> {
    accounts: &'a [db::Account],
    page_data: &'a db::DashboardData,
    period: &'a str,
    theme: &'a str,
    refresh_interval_s: u64,
    show_disabled: bool,
    health_snapshots: &'a [crate::health::AccountHealthSnapshot],
}

mod api;
mod assets;
mod format;
mod render;
mod response;
mod routes;
#[cfg(test)]
mod tests;
mod theme;

use api::{
    escape_script_end_tags, grouped_timeseries_json, grouped_timeseries_projection,
    normalize_period, normalized_bucket, normalized_group_by,
};
use format::*;
use render::{
    dashboard_page_with_body, render_dashboard_page_body, render_model_detail, render_overview,
    summary_json,
};
#[cfg(test)]
use render::{
    format_runtime_age, host_platform_label, load_average_summary, render_bandwidth_heatmap,
    render_bandwidth_page, render_models_page, render_timeseries_page, render_token_heatmap,
    render_traces_page,
};
use response::{html_response, static_response};
use theme::*;

pub(super) use api::{
    grouped_timeseries_api, stats_cache_observability, stats_cache_stability,
    stats_request_segmentation, stats_request_shaping, stats_transcoding, summary, timeseries_api,
};
pub(super) use assets::{static_chart_js, static_css, static_favicon, static_js, theme_css};
pub(super) use response::{degraded, json_response};
pub(super) use routes::{
    accounts_page, bandwidth_page, cache_page, events_page, latency_page, model_detail_page,
    models_page, overview, pings_page, reliability_page, routing_page, runtime_page, sync_accounts,
    timeseries_page, traces_page,
};
