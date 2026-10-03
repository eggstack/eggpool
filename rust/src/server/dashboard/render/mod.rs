use super::*;

mod accounts;
mod cache;
mod diagnostics;
mod layout;
mod models;
mod overview;
mod runtime;
mod telemetry;

pub(super) use accounts::*;
pub(super) use cache::*;
pub(super) use diagnostics::*;
pub(super) use layout::*;
pub(super) use models::*;
pub(super) use overview::*;
pub(super) use runtime::*;
pub(super) use telemetry::*;

#[allow(clippy::too_many_arguments)]
pub(in crate::server::dashboard) fn render_dashboard_page_body(
    title: &str,
    active_nav: &str,
    period: &str,
    theme: &str,
    data: &db::DashboardData,
    summary: &db::DashboardSummary,
    observability: &Value,
    runtime_diagnostics: Option<&crate::runtime_lifecycle::RuntimeDiagnosticsSnapshot>,
    runtime_uptime: std::time::Duration,
    model_info: &[Value],
    show_disabled: bool,
    model_filters: &ModelFilters,
    health_snapshots: &[crate::health::AccountHealthSnapshot],
    provider_priorities: &std::collections::BTreeMap<String, u32>,
    timeseries_projection: Option<&Value>,
    routing_trace: &RoutingTraceSnapshot,
) -> String {
    let mut body = if matches!(
        active_nav,
        "accounts"
            | "models"
            | "latency"
            | "events"
            | "timeseries"
            | "bandwidth"
            | "pings"
            | "reliability"
            | "routing"
            | "traces"
            | "runtime"
            | "cache"
    ) {
        String::new()
    } else {
        dashboard_header(title, period, theme)
    };
    match active_nav {
        "accounts" => body.push_str(&render_accounts_page(
            data,
            period,
            theme,
            show_disabled,
            health_snapshots,
        )),
        "models" => body.push_str(&render_models_page(
            data,
            period,
            theme,
            model_info,
            model_filters,
            provider_priorities,
        )),
        "latency" => body.push_str(&render_latency_page(data, period, theme)),
        "events" => body.push_str(&render_events_page(
            data,
            period,
            theme,
            model_filters.event_type.as_deref().unwrap_or_default(),
        )),
        "timeseries" => body.push_str(&render_timeseries_page(
            data,
            period,
            theme,
            timeseries_projection.unwrap_or(&Value::Null),
        )),
        "bandwidth" => body.push_str(&render_bandwidth_page(data, period, theme)),
        "pings" => body.push_str(&render_pings_page(data, period, theme)),
        "reliability" => body.push_str(&render_reliability_page(data, period, theme)),
        "routing" => body.push_str(&render_routing_page(data, period, theme, routing_trace)),
        "traces" => body.push_str(&render_traces_page(
            data,
            "recent",
            theme,
            model_filters.trace_limit.unwrap_or(50).clamp(10, 500),
        )),
        "runtime" => body.push_str(&render_runtime_page(
            data,
            summary,
            observability,
            runtime_diagnostics,
            runtime_uptime,
        )),
        "cache" => body.push_str(&render_cache_page(data, observability, period, theme)),
        _ => body.push_str(&dashboard_empty(title, "No data available.")),
    }
    body
}
