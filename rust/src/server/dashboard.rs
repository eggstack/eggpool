const DEFAULT_THEME: &str = "Cyber Red";
const MAX_THEME_NAME_BYTES: usize = 128;

use super::*;

const DASHBOARD_CSS: &[u8] = include_bytes!("../../assets/dashboard/static/dashboard.css");
const DASHBOARD_JS: &[u8] = include_bytes!("../../assets/dashboard/static/dashboard.js");
const CHART_JS: &[u8] = include_bytes!("../../assets/dashboard/static/chart.umd.min.js");
const FAVICON_SVG: &[u8] = include_bytes!("../../assets/dashboard/static/favicon.svg");

const THEME_NAMES: &[&str] = &[
    "default",
    "Booberry",
    "Catppuccin Latte",
    "Catppuccin Macchiato",
    "Catppuccin Mocha",
    "Cyber Red",
    "Cyberpunk",
    "Dark Green",
    "Discord (80_ Saturation)",
    "Discord",
    "Dracula",
    "Ferra Light",
    "Flexor Dark",
    "Gruvbox",
    "Halcyon Dark",
    "IntelliJ Light",
    "Kanagawa",
    "Macaw Dark",
    "Macaw Light",
    "Matrix",
    "Noctis Lilac",
    "Nord",
    "Nostromo Terminal",
    "One Dark",
    "Oxocarbon",
    "Rose Pine Dawn",
    "Rose Pine Moon",
    "Rose Pine",
    "Solarized Dark",
    "Sonokai",
    "Tokyo Night Storm",
    "VESPER",
    "Zenburn",
    "acton",
    "bam",
    "base16-atelier-forest-light",
    "berlin",
    "black but with important highlights",
    "broc",
    "cork",
    "ferra",
    "forest",
    "lisbon",
    "midnight",
    "oslo",
    "plum",
    "portland",
    "sunset",
    "tofino",
    "vanimo",
    "vik",
];

#[derive(Debug, Deserialize)]
pub(super) struct PeriodQuery {
    period: Option<String>,
    theme: Option<String>,
}

/// Start the development server using the configured address and database.
pub(super) async fn overview(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let repository = db::UsageRollupRepository::new(&state.database);
    let summary = match repository.dashboard_summary_basic(period).await {
        Ok(summary) => summary,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let accounts = match db::AccountRepository::new(&state.database)
        .list_enabled()
        .await
    {
        Ok(accounts) => accounts,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let theme_name = selected_theme(
        query
            .theme
            .as_deref()
            .unwrap_or(&state.server.dashboard_theme),
    );
    let html = render_overview(
        &summary,
        &accounts,
        period,
        theme_name,
        state.server.dashboard_refresh_interval_s,
    );
    html_response(html)
}

pub(super) async fn accounts_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Accounts", "accounts", query.period, query.theme).await
}

pub(super) async fn models_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Models", "models", query.period, query.theme).await
}

pub(super) async fn model_detail_page(
    State(state): State<AppState>,
    AxumPath(model_id): AxumPath<String>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    let model_id = model_id.trim_start_matches('/');
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let theme = selected_theme(
        query
            .theme
            .as_deref()
            .unwrap_or(&state.server.dashboard_theme),
    );
    let body = match db::DashboardRepository::new(&state.database)
        .load(period)
        .await
    {
        Ok(data) => render_model_detail(&data, model_id),
        Err(_) => return degraded("dashboard data unavailable"),
    };
    dashboard_page_with_body(
        &state,
        &format!("Model: {model_id}"),
        "models",
        Some(period.to_owned()),
        Some(theme.to_owned()),
        body,
    )
}

pub(super) async fn latency_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Latency", "latency", query.period, query.theme).await
}

pub(super) async fn events_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Events", "events", query.period, query.theme).await
}

pub(super) async fn timeseries_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(
        &state,
        "Timeseries",
        "timeseries",
        query.period,
        query.theme,
    )
    .await
}

pub(super) async fn bandwidth_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Bandwidth", "bandwidth", query.period, query.theme).await
}

pub(super) async fn pings_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Provider Pings", "pings", query.period, query.theme).await
}

pub(super) async fn reliability_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(
        &state,
        "Reliability",
        "reliability",
        query.period,
        query.theme,
    )
    .await
}

pub(super) async fn routing_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Routing", "routing", query.period, query.theme).await
}

pub(super) async fn traces_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Traces", "traces", query.period, query.theme).await
}

pub(super) async fn runtime_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Runtime", "runtime", query.period, query.theme).await
}

pub(super) async fn cache_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Cache", "cache", query.period, query.theme).await
}

pub(super) async fn summary(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let summary = match db::UsageRollupRepository::new(&state.database)
        .dashboard_summary_basic(period)
        .await
    {
        Ok(summary) => summary,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    json_response(StatusCode::OK, summary_json(&summary, period))
}

pub(super) fn normalize_period(value: Option<&str>) -> Result<&'static str, Box<Response>> {
    match value.unwrap_or("24h") {
        "1h" => Ok("1h"),
        "24h" => Ok("24h"),
        "7d" => Ok("7d"),
        "30d" => Ok("30d"),
        _ => Err(Box::new(json_response(
            StatusCode::BAD_REQUEST,
            json!({"detail": "Invalid period"}),
        ))),
    }
}

/// Thin Axum handlers: admission/auth/body limits are existing boundaries;
/// each handler invokes exactly one coordinator entry point and translates
/// its typed result to the established client surface. No routing, retry,
pub(super) async fn static_css() -> Response {
    static_response(DASHBOARD_CSS, "text/css", "public, max-age=300")
}

pub(super) async fn static_js() -> Response {
    static_response(
        DASHBOARD_JS,
        "application/javascript",
        "public, max-age=86400",
    )
}

pub(super) async fn static_chart_js() -> Response {
    static_response(CHART_JS, "application/javascript", "public, max-age=86400")
}

pub(super) async fn static_favicon() -> Response {
    static_response(FAVICON_SVG, "image/svg+xml", "public, max-age=86400")
}

pub(super) async fn theme_css(Query(query): Query<ThemeQuery>) -> Response {
    let requested = query.theme.unwrap_or_else(|| "default".to_owned());
    if requested == "default" || !THEME_NAMES.contains(&requested.as_str()) {
        return static_response(b"", "text/css", "public, max-age=300");
    }
    let css = theme_variables(&requested);
    static_response(css.as_bytes(), "text/css", "public, max-age=300")
}

#[derive(Debug, Deserialize)]
pub(super) struct ThemeQuery {
    theme: Option<String>,
}

pub(super) fn static_response(body: &[u8], content_type: &str, cache_control: &str) -> Response {
    (
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, cache_control),
        ],
        body.to_vec(),
    )
        .into_response()
}

pub(super) fn json_response(status: StatusCode, value: Value) -> Response {
    (status, axum::Json(value)).into_response()
}

pub(super) fn degraded(reason: &str) -> Response {
    json_response(
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"status": "degraded", "reason": reason}),
    )
}

pub(super) fn html_response(body: String) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        body,
    )
        .into_response()
}

pub(super) async fn sync_accounts(
    config: &Config,
    database: &db::Database,
) -> Result<(), ServerError> {
    let accounts = config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider
                .accounts
                .iter()
                .map(move |account| db::AccountConfig {
                    name: account.name.clone(),
                    api_key_env: account.api_key_env.clone(),
                    enabled: account.enabled,
                    weight: account.weight,
                    provider_id: provider_id.clone(),
                })
        })
        .collect();
    db::AccountRepository::new(database)
        .sync_from_config(accounts)
        .await
        .map(|_| ())
        .map_err(ServerError::Database)
}

pub(super) fn selected_theme(configured: &str) -> &str {
    if configured.len() <= MAX_THEME_NAME_BYTES && THEME_NAMES.contains(&configured) {
        configured
    } else {
        DEFAULT_THEME
    }
}

pub(super) async fn dashboard_data_page(
    state: &AppState,
    title: &str,
    active_nav: &str,
    period: Option<String>,
    theme: Option<String>,
) -> Response {
    let period = match normalize_period(period.as_deref()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let theme = selected_theme(theme.as_deref().unwrap_or(&state.server.dashboard_theme));
    let data = match db::DashboardRepository::new(&state.database)
        .load(period)
        .await
    {
        Ok(data) => data,
        Err(error) => {
            eprintln!("dashboard data read failed: {error}");
            return degraded("dashboard data unavailable");
        }
    };
    let summary = match db::UsageRollupRepository::new(&state.database)
        .dashboard_summary_basic(period)
        .await
    {
        Ok(summary) => summary,
        Err(error) => {
            eprintln!("dashboard summary read failed: {error}");
            return degraded("dashboard data unavailable");
        }
    };
    let body = render_dashboard_page_body(title, active_nav, period, &data, &summary);
    dashboard_page_with_body(
        state,
        title,
        active_nav,
        Some(period.to_owned()),
        Some(theme.to_owned()),
        body,
    )
}

pub(super) fn dashboard_header(title: &str, period: &str) -> String {
    format!(
        "<h2>{}</h2><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"Cyber Red\"></form>",
        html_escape(title),
        period_options(period),
    )
}

pub(super) fn dashboard_empty(title: &str, message: &str) -> String {
    format!(
        "<section class=\"panel\"><div class=\"panel-header\"><h2>{}</h2></div><p class=\"empty\" role=\"status\">{}</p></section>",
        html_escape(title),
        html_escape(message),
    )
}

pub(super) fn render_dashboard_page_body(
    title: &str,
    active_nav: &str,
    period: &str,
    data: &db::DashboardData,
    summary: &db::DashboardSummary,
) -> String {
    let mut body = dashboard_header(title, period);
    match active_nav {
        "accounts" => body.push_str(&render_accounts_page(data)),
        "models" => body.push_str(&render_models_page(data)),
        "latency" => body.push_str(&render_latency_page(data)),
        "events" => body.push_str(&render_events_page(data)),
        "timeseries" => body.push_str(&render_timeseries_page(data)),
        "bandwidth" => body.push_str(&render_bandwidth_page(data)),
        "pings" => body.push_str(&render_pings_page(data)),
        "reliability" => body.push_str(&render_reliability_page(data)),
        "routing" => body.push_str(&render_routing_page(data)),
        "traces" => body.push_str(&render_traces_page(data)),
        "runtime" => body.push_str(&render_runtime_page(data, summary)),
        "cache" => body.push_str(&render_cache_page(data)),
        _ => body.push_str(&dashboard_empty(title, "No data available.")),
    }
    body
}

pub(super) fn render_accounts_page(data: &db::DashboardData) -> String {
    if data.accounts.is_empty() {
        return dashboard_empty("Accounts", "No accounts configured.");
    }
    let rows = data
        .accounts
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.name),
                html_escape(&row.provider_id),
                if row.enabled { "yes" } else { "no" },
                row.requests,
                format_microdollars(row.cost_microdollars),
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Account health</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Account</th><th>Provider</th><th>Enabled</th><th>Requests</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_models_page(data: &db::DashboardData) -> String {
    let models = data
        .models
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .collect::<Vec<_>>();
    if models.is_empty() {
        return dashboard_empty("Models", "No models discovered from configured providers.");
    }
    let rows = models
        .iter()
        .map(|row| {
            let availability = if row.requests > 0
                || row.resolution_status == "available"
                || row.resolution_status == "resolved"
            {
                "available"
            } else {
                "configured"
            };
            format!(
                "<tr><td><a href=\"/models/{}\">{}</a></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                query_component(&row.model_id),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                availability,
                row.requests,
                format_microdollars(row.cost_microdollars),
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Catalog models</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Model</th><th>Provider</th><th>Avail.</th><th>Requests</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_model_detail(data: &db::DashboardData, model_id: &str) -> String {
    let Some(model) = data
        .models
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .find(|row| row.model_id == model_id)
    else {
        return format!(
            "<h2>Model: {}</h2><p class=\"empty\">Model info not available.</p>",
            html_escape(model_id)
        );
    };
    format!(
        "<h2>Model: {}</h2><section class=\"cards\"><div class=\"card\"><h3>Provider</h3><p class=\"metric\">{}</p></div><div class=\"card\"><h3>Status</h3><p class=\"metric\">{}</p></div><div class=\"card\"><h3>Requests</h3><p class=\"metric\">{}</p></div></section><section class=\"panel\"><h3>Model information</h3><p class=\"empty\">Model info not available.</p></section>",
        html_escape(model_id),
        html_escape(&model.provider_id),
        html_escape(&model.resolution_status),
        model.requests,
    )
}

pub(super) fn render_latency_page(data: &db::DashboardData) -> String {
    if data.models.iter().all(|row| row.ttft_requests == 0) {
        return "<p class=\"empty\">No TTFT data for this period.</p><section class=\"panel\"><h3>Per-model breakdown</h3><p class=\"empty\">No model data for this period.</p></section>".to_owned();
    }
    let mut provider_totals: Vec<(&str, f64, i64)> = Vec::new();
    for row in data.models.iter().filter(|row| row.ttft_requests > 0) {
        if let Some((_, total, requests)) = provider_totals
            .iter_mut()
            .find(|(provider, _, _)| *provider == row.provider_id)
        {
            *total += row.avg_ttft_ms * row.ttft_requests as f64;
            *requests += row.ttft_requests;
        } else {
            provider_totals.push((
                &row.provider_id,
                row.avg_ttft_ms * row.ttft_requests as f64,
                row.ttft_requests,
            ));
        }
    }
    let cards = provider_totals
        .iter()
        .map(|(provider, total, requests)| {
            format!(
                "<div class=\"card\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">{} requests</p></div>",
                html_escape(provider),
                format_latency(total / *requests as f64),
                requests,
            )
        })
        .collect::<String>();
    let mut latency_models = data
        .models
        .iter()
        .filter(|row| row.ttft_requests > 0)
        .collect::<Vec<_>>();
    latency_models.sort_by(|left, right| {
        left.avg_ttft_ms
            .partial_cmp(&right.avg_ttft_ms)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let rows = latency_models
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                row.ttft_requests,
                format_latency(row.avg_ttft_ms),
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"cards\">{cards}</section><section class=\"panel\"><h3>Per-model breakdown</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Provider</th><th>Model</th><th>Requests</th><th>Avg TTFT</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_events_page(data: &db::DashboardData) -> String {
    if data.events.is_empty() {
        return dashboard_empty("Events", "No events recorded.");
    }
    let rows = data
        .events
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.created_at),
                html_escape(&row.account_name),
                html_escape(&row.event_type),
                html_escape(&row.details),
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Recent events</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>When</th><th>Account</th><th>Type</th><th>Details</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_timeseries_page(data: &db::DashboardData) -> String {
    if data.timeseries.is_empty() {
        return dashboard_empty("Timeseries", "No requests in this window.");
    }
    let rows = data
        .timeseries
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.bucket),
                html_escape(&row.series),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                row.requests,
                format_microdollars(row.cost_microdollars),
                row.errors,
                format_tokens(row.total_tokens),
            )
        })
        .collect::<String>();
    let chart_data = data
        .timeseries
        .iter()
        .map(|row| format!("[\"{}\",{}]", json_escape(&row.bucket), row.requests))
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "<section class=\"panel\" id=\"timeseries-chart\"><h3>Usage breakdown</h3><script type=\"application/json\" id=\"timeseries-initial-data\">[{chart_data}]</script><p class=\"empty\" style=\"display:none\">No requests in this window.</p><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Bucket</th><th>Series</th><th>Provider</th><th>Model</th><th>Requests</th><th>Cost</th><th>Errors</th><th>Total tokens</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_bandwidth_page(data: &db::DashboardData) -> String {
    let detail = if data.cache.total_bytes_received == 0 && data.cache.total_bytes_emitted == 0 {
        "<p class=\"empty\">No activity data available.</p>"
    } else {
        "<p class=\"status\">Persisted transfer totals for the selected period.</p>"
    };
    format!(
        "<section class=\"cards\"><div class=\"card\"><h3>Total received</h3><p class=\"metric\">{}</p></div><div class=\"card\"><h3>Total emitted</h3><p class=\"metric\">{}</p></div></section><section class=\"panel\"><h3>Bandwidth by request</h3>{detail}</section>",
        format_bytes(data.cache.total_bytes_received),
        format_bytes(data.cache.total_bytes_emitted),
    )
}

pub(super) fn render_pings_page(data: &db::DashboardData) -> String {
    if data.pings.is_empty() {
        return "<section class=\"panel\"><div class=\"panel-header\"><h2>Provider Pings</h2></div><p class=\"empty\" role=\"status\">No ping data yet. Data appears after the first catalog refresh.</p><p class=\"empty\">No pings recorded yet.</p></section>".to_owned();
    }
    let mut provider_totals: Vec<(&str, f64, i64)> = Vec::new();
    for row in &data.pings {
        let latency = row.latency_ms.unwrap_or_default() as f64;
        if let Some((_, total, count)) = provider_totals
            .iter_mut()
            .find(|(provider, _, _)| *provider == row.provider_id)
        {
            *total += latency;
            *count += 1;
        } else {
            provider_totals.push((&row.provider_id, latency, 1));
        }
    }
    provider_totals.sort_by(|left, right| left.0.cmp(right.0));
    let cards = provider_totals
        .iter()
        .map(|(provider, total, count)| {
            format!(
                "<div class=\"card\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">{} models · status {}</p></div>",
                html_escape(provider),
                format_latency(total / *count as f64),
                data.pings
                    .iter()
                    .find(|row| row.provider_id == *provider)
                    .map_or(0, |row| row.model_count),
                data.pings
                    .iter()
                    .find(|row| row.provider_id == *provider)
                    .and_then(|row| row.status_code)
                    .map_or_else(|| "—".to_owned(), |v| v.to_string()),
            )
        })
        .collect::<String>();
    let rows = data
        .pings
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.provider_id),
                html_escape(&row.probed_at),
                format_latency(row.latency_ms.unwrap_or_default() as f64),
                row.status_code
                    .map_or_else(|| "—".to_owned(), |v| v.to_string()),
                html_escape(&row.account_name),
                row.model_count,
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"cards\">{cards}</section><section class=\"panel\"><h3>Recent pings</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Provider</th><th>Time</th><th>Latency</th><th>Status</th><th>Account</th><th>Models</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_reliability_page(data: &db::DashboardData) -> String {
    let attempts: i64 = data.retries.iter().map(|row| row.attempts).sum();
    let failures: i64 = data.retries.iter().map(|row| row.failures).sum();
    let successes: i64 = data.retries.iter().map(|row| row.successes).sum();
    let retry_rows = data
        .retries
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.category),
                row.attempts,
                row.retry_outcomes,
                row.successes,
                row.failures,
                format_latency(row.avg_latency_ms),
            )
        })
        .collect::<String>();
    let retry_attempts: i64 = data.retries.iter().map(|row| row.retry_outcomes).sum();
    let distribution = if retry_rows.is_empty() {
        "<p class=\"empty\">No attempt data for this period.</p><p class=\"empty\">No operational events in this window.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Category</th><th>Attempts</th><th>Retry outcomes</th><th>Successes</th><th>Failures</th><th>Avg attempt latency</th></tr></thead><tbody>{retry_rows}</tbody></table></div>"
        )
    };
    format!(
        "<section class=\"cards\"><div class=\"card\"><h3>Total attempts</h3><p class=\"metric\">{attempts}</p></div><div class=\"card\"><h3>Success attempts</h3><p class=\"metric\">{successes}</p></div><div class=\"card\"><h3>Retry attempts</h3><p class=\"metric\">{retry_attempts}</p></div><div class=\"card\"><h3>Failed attempts</h3><p class=\"metric\">{failures}</p></div></section><section class=\"panel\"><h3>Retry distribution</h3>{distribution}</section>"
    )
}

pub(super) fn render_routing_page(data: &db::DashboardData) -> String {
    let decisions: i64 = data.routing.iter().map(|row| row.decisions).sum();
    let avg_eligible = if data.routing.is_empty() {
        0.0
    } else {
        data.routing.iter().map(|row| row.avg_eligible).sum::<f64>() / data.routing.len() as f64
    };
    let distinct = data
        .routing
        .iter()
        .map(|row| row.distinct_accounts)
        .sum::<i64>();
    let rows = data
        .routing
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{:.2}</td><td>{:.2}</td><td>{:.2}</td><td>{}</td></tr>",
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                row.decisions,
                row.avg_eligible,
                row.avg_scored,
                row.avg_excluded,
                row.distinct_accounts,
            )
        })
        .collect::<String>();
    let distribution = if rows.is_empty() {
        "<p class=\"empty\">No routing decisions in this period.</p><p class=\"empty\">No selection data in this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Model</th><th>Provider</th><th>Decisions</th><th>Avg eligible</th><th>Avg scored</th><th>Avg excluded</th><th>Distinct accounts</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    format!(
        "<section class=\"cards\"><div class=\"card\"><h3>Routing decisions</h3><p class=\"metric\">{decisions}</p></div><div class=\"card\"><h3>Avg eligible / decision</h3><p class=\"metric\">{avg_eligible:.2}</p></div><div class=\"card\"><h3>Distinct selected accounts</h3><p class=\"metric\">{distinct}</p></div></section><section class=\"panel\"><h3>Routing distribution</h3><p class=\"empty\">No exclusion data in this period.</p>{distribution}</section>"
    )
}

pub(super) fn render_traces_page(data: &db::DashboardData) -> String {
    if data.requests.is_empty() {
        return dashboard_empty("Traces", "No recent requests.");
    }
    let rows = data
        .requests
        .iter()
        .map(|row| {
            let status = row.status_code.map_or_else(
                || row.status.clone(),
                |code| format!("{} ({code})", row.status),
            );
            let latency = row
                .latency_ms
                .filter(|value| *value > 0.0)
                .map_or_else(|| "—".to_owned(), format_latency);
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.started_at),
                html_escape(&row.account_name),
                html_escape(&row.model_id),
                html_escape(&status),
                latency,
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Recent requests</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Time</th><th>Account</th><th>Model</th><th>Status</th><th>Latency</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_runtime_page(
    _data: &db::DashboardData,
    summary: &db::DashboardSummary,
) -> String {
    format!(
        "<section class=\"cards\"><div class=\"card\"><h3>Outbound builds</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Outbound requests</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Provider clients</h3><p class=\"metric\">{}</p></div></section><section class=\"panel\"><h3>Runtime snapshot</h3><p class=\"status\">{} dashboard records are available without exposing request content.</p><p class=\"empty\">No loss warnings recorded in this period.</p><p class=\"empty\">No health state data.</p></section>",
        summary.total_providers, summary.total_requests,
    )
}

pub(super) fn render_cache_page(data: &db::DashboardData) -> String {
    let empty = "";
    let provider_cache_counters = if data.requests.is_empty() {
        "—"
    } else {
        "0.0%"
    };
    format!(
        "<section class=\"cards\"><div class=\"card\"><h3>Request changes</h3><p class=\"metric\">no changes</p></div><div class=\"card\"><h3>Provider cache counters</h3><p class=\"metric\">{}</p></div><div class=\"card\"><h3>Safety guardrail</h3><p class=\"metric\">Clean</p></div><div class=\"card\"><h3>Routing isolation</h3><p class=\"metric\">Isolated</p></div><div class=\"card\"><h3>Rows with cache counters</h3><p class=\"metric\">{}</p></div><div class=\"card\"><h3>Rows without cache counters</h3><p class=\"metric\">{}</p></div><div class=\"card\"><h3>Unrecognized payload shape</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Provider cache hit rate</h3><p class=\"metric\">—</p></div><div class=\"card\"><h3>Cache write/warmup rate</h3><p class=\"metric\">—</p></div><div class=\"card\"><h3>Transcoded requests</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Segmented</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Not collected</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Empty request</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Parse failure</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>With protected prefix</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>With volatile suffix</h3><p class=\"metric\">0</p></div><div class=\"card\"><h3>Mode</h3><p class=\"metric\">reporting_only</p></div><div class=\"card\"><h3>Cache metrics</h3><p class=\"metric\">no</p></div><div class=\"card\"><h3>Compression metrics</h3><p class=\"metric\">no</p></div><div class=\"card\"><h3>Stable-prefix hash</h3><p class=\"metric\">no</p></div><div class=\"card\"><h3>Compression policy</h3><p class=\"metric\">no</p></div></section><section class=\"panel\"><h3>Cache observations</h3>{}<p class=\"status\">Counters are aggregated from persisted request metadata.</p></section>",
        provider_cache_counters,
        data.cache.rows_with_read + data.cache.rows_with_write,
        data.requests.len() as i64 - data.cache.rows_with_read - data.cache.rows_with_write,
        empty,
    )
}

pub(super) fn format_microdollars(value: i64) -> String {
    format!("${:.2}", value as f64 / 1_000_000.0)
}

pub(super) fn format_tokens(value: i64) -> String {
    let digits = value.unsigned_abs().to_string();
    let grouped = digits
        .as_bytes()
        .rchunks(3)
        .rev()
        .map(|chunk| std::str::from_utf8(chunk).unwrap_or("0"))
        .collect::<Vec<_>>()
        .join(",");
    if value < 0 {
        format!("-{grouped}")
    } else {
        grouped
    }
}

pub(super) fn format_latency(value: f64) -> String {
    format!("{value:.1} ms")
}

pub(super) fn format_bytes(value: i64) -> String {
    if value < 1_000 {
        return format!("{value} B");
    }
    let units = ["KB", "MB", "GB", "TB"];
    let mut scaled = value as f64;
    let mut unit = "B";
    for candidate in units {
        scaled /= 1_000.0;
        unit = candidate;
        if scaled < 1_000.0 {
            break;
        }
    }
    format!("{scaled:.1} {unit}")
}

pub(super) fn json_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
}

pub(super) fn dashboard_page_with_body(
    state: &AppState,
    title: &str,
    active_nav: &str,
    period: Option<String>,
    theme: Option<String>,
    body: String,
) -> Response {
    let period = period.as_deref().unwrap_or("24h");
    let period = match normalize_period(Some(period)) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let theme = selected_theme(theme.as_deref().unwrap_or(&state.server.dashboard_theme));
    html_response(render_dashboard_layout(
        title,
        active_nav,
        period,
        theme,
        state.server.dashboard_refresh_interval_s,
        body,
        true,
    ))
}

pub(super) fn period_options(current: &str) -> String {
    [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    .iter()
    .map(|(value, label)| {
        let selected = if *value == current {
            " selected=\"selected\""
        } else {
            ""
        };
        format!("<option value=\"{}\"{}>{}</option>", value, selected, label)
    })
    .collect()
}

pub(super) fn query_component(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    encoded
}

pub(super) fn render_dashboard_layout(
    title: &str,
    active_nav: &str,
    period: &str,
    theme: &str,
    refresh_interval_s: u64,
    body: String,
    include_chart_js: bool,
) -> String {
    let query = format!(
        "period={}&amp;theme={}",
        query_component(period),
        query_component(theme)
    );
    let navigation = [
        ("overview", "/", "Overview"),
        ("reliability", "/reliability", "Reliability"),
        ("routing", "/routing", "Routing"),
        ("cache", "/cache", "Cache"),
        ("accounts", "/accounts", "Accounts"),
        ("models", "/models", "Models"),
        ("latency", "/latency", "Latency"),
        ("pings", "/pings", "Pings"),
        ("bandwidth", "/bandwidth", "Bandwidth"),
        ("traces", "/traces", "Traces"),
        ("events", "/events", "Events"),
        ("timeseries", "/timeseries", "Timeseries"),
        ("runtime", "/runtime", "Runtime"),
    ]
    .iter()
    .map(|(key, href, label)| {
        let class = if *key == active_nav {
            " class=\"active\""
        } else {
            ""
        };
        format!(
            "<a{} href=\"{}?{}\">{}</a>",
            class,
            href,
            query,
            html_escape(label)
        )
    })
    .collect::<String>();
    let theme_options = THEME_NAMES
        .iter()
        .map(|name| {
            let selected = if *name == theme { " selected" } else { "" };
            format!(
                "<option value=\"{}\"{}>{}</option>",
                html_escape(name),
                selected,
                html_escape(name)
            )
        })
        .collect::<String>();
    let chart_preload = if include_chart_js {
        "<link rel=\"preload\" href=\"/static/chart.js\" as=\"script\">"
    } else {
        ""
    };
    let chart_script = if include_chart_js {
        "<script defer src=\"/static/chart.js\"></script>"
    } else {
        ""
    };
    let navigation_markup = format!(
        "<div class=\"topnav-menu\" id=\"topnav-menu\">{}<form method=\"get\" class=\"theme-selector\" aria-label=\"Switch dashboard theme\"><select name=\"theme\" onchange=\"this.form.submit()\">{}</select><input type=\"hidden\" name=\"period\" value=\"{}\"></form></div>",
        navigation,
        theme_options,
        html_escape(period)
    );
    format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>{}</title>\n<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\">\n<link rel=\"preload\" href=\"/static/dashboard.css\" as=\"style\">\n<link rel=\"stylesheet\" href=\"/static/dashboard.css\">\n<link rel=\"stylesheet\" href=\"/static/theme.css?theme={}\">\n{}\n</head>\n<body>\n<svg class=\"egg-background\" viewBox=\"0 0 256 256\" preserveAspectRatio=\"xMidYMid meet\" aria-hidden=\"true\" focusable=\"false\"><path class=\"shape\" d=\"M128 30 C82 30 55 88 57 145 C59 202 89 231 128 231 C167 231 197 202 199 145 C201 88 174 30 128 30 Z\" /><path class=\"thin\" d=\"M86 132 H112 L126 111 L144 158 L159 132 H174\" /><circle class=\"shape\" cx=\"85\" cy=\"132\" r=\"5\" /><circle class=\"shape\" cx=\"174\" cy=\"132\" r=\"5\" /></svg>\n<header class=\"topbar\"><button class=\"topnav-burger\" type=\"button\" aria-label=\"Open page menu\" aria-expanded=\"false\" aria-controls=\"topnav-menu\"><svg class=\"topnav-burger-icon\" viewBox=\"0 0 24 24\" width=\"24\" height=\"24\" aria-hidden=\"true\" focusable=\"false\"><rect class=\"bar bar-1\" x=\"0\" y=\"0\" width=\"24\" height=\"2\" rx=\"1\"/><rect class=\"bar bar-2\" x=\"0\" y=\"11\" width=\"24\" height=\"2\" rx=\"1\"/><rect class=\"bar bar-3\" x=\"0\" y=\"22\" width=\"24\" height=\"2\" rx=\"1\"/></svg></button><h1><a href=\"/?{}\">EggPool</a></h1><nav class=\"topnav\">{}<button type=\"button\" class=\"topnav-refresh\" aria-label=\"Reload this page\" onclick=\"window.location.reload()\">↻</button></nav></header>\n<main id=\"dashboard-content\">\n{}\n</main>\n<footer><small>Period: <span class=\"period-label\">{}</span> &middot; auto-refresh {}s &middot; <span id=\"dashboard-updated\">ready</span></small></footer>\n<script defer src=\"/static/dashboard.js\"></script>{}\n</body>\n</html>",
        html_escape(title),
        query_component(theme),
        chart_preload,
        query,
        navigation_markup,
        body,
        html_escape(period),
        refresh_interval_s,
        chart_script
    )
}

pub(super) fn html_escape(value: impl std::fmt::Display) -> String {
    value
        .to_string()
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

pub(super) fn render_overview(
    summary: &db::DashboardSummary,
    accounts: &[db::Account],
    period: &str,
    theme: &str,
    refresh_interval_s: u64,
) -> String {
    let total = summary.total_requests;
    let success = summary.successful_requests;
    let errors = summary.error_requests;
    let error_rate = if total == 0 {
        0.0
    } else {
        errors as f64 / total as f64 * 100.0
    };
    let fresh_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        fresh_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let nav = THEME_NAMES
        .iter()
        .map(|name| {
            let selected = if *name == theme { " selected" } else { "" };
            format!(
                "<option value=\"{}\"{}>{}</option>",
                html_escape(name),
                selected,
                html_escape(name)
            )
        })
        .collect::<String>();
    let period_options = [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    .iter()
    .map(|(value, label)| {
        let selected = if *value == period {
            " selected=\"selected\""
        } else {
            ""
        };
        format!("<option value=\"{}\"{}>{}</option>", value, selected, label)
    })
    .collect::<String>();
    let nav_links = [
        ("/", "Overview"),
        ("/reliability", "Reliability"),
        ("/routing", "Routing"),
        ("/cache", "Cache"),
        ("/accounts", "Accounts"),
        ("/models", "Models"),
        ("/latency", "Latency"),
        ("/pings", "Pings"),
        ("/bandwidth", "Bandwidth"),
        ("/traces", "Traces"),
        ("/events", "Events"),
        ("/timeseries", "Timeseries"),
        ("/runtime", "Runtime"),
    ]
    .iter()
    .map(|(href, label)| {
        let class = if *href == "/" {
            " class=\"active\""
        } else {
            ""
        };
        format!(
            "<a{} href=\"{}?period={}&amp;theme={}\">{}</a>",
            class,
            href,
            html_escape(period),
            html_escape(theme),
            html_escape(label)
        )
    })
    .collect::<String>();
    let account_table = if accounts.is_empty() {
        "<p class=\"empty-state\">No accounts configured.</p><p class=\"empty-state\">No model activity in this period.</p><p class=\"empty-state\">No recent events.</p><p class=\"empty-state\">No activity data available.</p>".to_owned()
    } else {
        let rows = accounts
            .iter()
            .map(|account| {
                format!(
                    "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
                    html_escape(&account.name),
                    html_escape(&account.provider_id),
                    if account.enabled { "yes" } else { "no" }
                )
            })
            .collect::<String>();
        format!(
            "<table><thead><tr><th>Account</th><th>Provider</th><th>Enabled</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    };
    let html = format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Overview</title>\n<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\">\n<link rel=\"preload\" href=\"/static/dashboard.css\" as=\"style\">\n<link rel=\"stylesheet\" href=\"/static/dashboard.css\">\n<link rel=\"preload\" href=\"/static/chart.js\" as=\"script\">\n<link rel=\"stylesheet\" href=\"/static/theme.css?theme={}\">\n</head>\n<body>\n<svg class=\"egg-background\" viewBox=\"0 0 256 256\" preserveAspectRatio=\"xMidYMid meet\" aria-hidden=\"true\" focusable=\"false\"><path class=\"shape\" d=\"M128 30 C82 30 55 88 57 145 C59 202 89 231 128 231 C167 231 197 202 199 145 C201 88 174 30 128 30 Z\" /><path class=\"thin\" d=\"M86 132 H112 L126 111 L144 158 L159 132 H174\" /><circle class=\"shape\" cx=\"85\" cy=\"132\" r=\"5\" /><circle class=\"shape\" cx=\"174\" cy=\"132\" r=\"5\" /></svg>\n<header class=\"topbar\"><button class=\"topnav-burger\" type=\"button\" aria-label=\"Open page menu\" aria-expanded=\"false\" aria-controls=\"topnav-menu\">☰</button><h1><a href=\"/?period={}&amp;theme={}\">EggPool</a></h1><nav class=\"topnav\"><div class=\"topnav-menu\" id=\"topnav-menu\">{}<form method=\"get\" class=\"theme-selector\"><select name=\"theme\" onchange=\"this.form.submit()\">{}</select><input type=\"hidden\" name=\"period\" value=\"{}\"></form></div><button type=\"button\" class=\"topnav-refresh\" aria-label=\"Reload this page\" onclick=\"window.location.reload()\">↻</button></nav></header>\n<main id=\"dashboard-content\"><h2>Overview</h2><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\"><option value=\"1h\">Last hour</option><option value=\"24h\" selected=\"selected\">Last 24 hours</option><option value=\"7d\">Last 7 days</option><option value=\"30d\">Last 30 days</option></select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form><section class=\"cards\"><div class=\"card\"><h3>Requests</h3><p class=\"metric\">{}</p><p class=\"sub\">Success {} · Errors {}</p></div><div class=\"card\"><h3>Error rate</h3><p class=\"metric\">{:.2}%</p><p class=\"sub\">avg latency {:.1} ms</p></div><div class=\"card\"><h3>Total tokens</h3><p class=\"metric\">{}</p><p class=\"sub\">fresh {} · cache read {} · cache write {}</p></div><div class=\"card\"><h3>Total cost</h3><p class=\"metric\">${:.2}</p><p class=\"sub\">in {} · out {}</p></div></section><section class=\"panel\"><div class=\"panel-header\"><h2>Account breakdown</h2></div><table><thead><tr><th>Account</th><th>Provider</th><th>Enabled</th></tr></thead><tbody>{}</tbody></table></section><section class=\"panel\" id=\"timeseries-chart\"><h3>Timeseries</h3><script type=\"application/json\" id=\"timeseries-initial-data\">[]</script></section></main><footer><small>Period: <span class=\"period-label\">{}</span> · auto-refresh {}s · <span id=\"dashboard-updated\">ready</span></small></footer><script defer src=\"/static/dashboard.js\"></script><script defer src=\"/static/chart.js\"></script>\n</body>\n</html>",
        html_escape(theme),
        html_escape(period),
        html_escape(theme),
        nav_links,
        nav,
        html_escape(period),
        html_escape(theme),
        total,
        success,
        errors,
        error_rate,
        summary.avg_latency_ms,
        format_tokens(accounted_tokens),
        fresh_tokens,
        summary.total_cache_read_tokens,
        summary.total_cache_write_tokens,
        summary.total_cost_microdollars as f64 / 1_000_000.0,
        summary.total_input_tokens,
        summary.total_output_tokens,
        account_table,
        html_escape(period),
        refresh_interval_s
    );
    html.replace(
        r#"<option value="1h">Last hour</option><option value="24h" selected="selected">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option>"#,
        &period_options,
    )
}

pub(super) fn summary_json(summary: &db::DashboardSummary, period: &str) -> Value {
    let total_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        total_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let cache_denominator = summary.total_input_tokens
        + summary.total_cache_read_tokens
        + summary.total_cache_write_tokens;
    json!({
        "period": period,
        "total_requests": summary.total_requests,
        "successful_requests": summary.successful_requests,
        "error_requests": summary.error_requests,
        "error_rate": if summary.total_requests > 0 { summary.error_requests as f64 / summary.total_requests as f64 } else { 0.0 },
        "total_input_tokens": summary.total_input_tokens,
        "total_output_tokens": summary.total_output_tokens,
        "total_tokens": total_tokens,
        "fresh_tokens": total_tokens,
        "accounted_tokens": accounted_tokens,
        "total_cost_microdollars": summary.total_cost_microdollars,
        "avg_latency_ms": summary.avg_latency_ms,
        "total_cache_read_tokens": summary.total_cache_read_tokens,
        "total_cache_write_tokens": summary.total_cache_write_tokens,
        "total_reasoning_tokens": summary.total_reasoning_tokens,
        "cache_read_ratio": if cache_denominator > 0 { Some(summary.total_cache_read_tokens as f64 / cache_denominator as f64) } else { None },
        "streamed_requests": summary.streamed_requests,
        "non_streamed_requests": summary.non_streamed_requests,
        "exact_count": summary.exact_count,
        "derived_count": summary.derived_count,
        "partial_count": summary.partial_count,
        "estimated_count": summary.estimated_count,
        "unknown_count": summary.unknown_count,
        "provider_reported_count": summary.provider_reported_count,
        "provider_reported_cost_microdollars": summary.provider_reported_cost_microdollars,
        "estimated_cost_sum_microdollars": summary.estimated_cost_sum_microdollars,
        "reservation_fallback_rows": summary.reservation_fallback_rows,
        "reservation_fallback_excess_microdollars": summary.reservation_fallback_excess_microdollars,
        "total_bytes_received": summary.total_bytes_received,
        "total_bytes_emitted": summary.total_bytes_emitted,
        "total_providers": summary.total_providers,
        "avg_ttft_ms": summary.avg_ttft_ms,
        "tokens_per_second": summary.tokens_per_second,
        "p50_ttft_ms": summary.p50_ttft_ms,
        "p99_ttft_ms": summary.p99_ttft_ms,
    })
}

pub(super) fn theme_variables(name: &str) -> String {
    let fallback = "#1e1e2e";
    let Some(bytes) = theme_bytes(name) else {
        return ":root {\n  --page-bg: #1e1e2e;\n  --page-text: #cdd6f4;\n  --topbar-bg: #1e1e2e;\n  --card-bg: #1e1e2e;\n  --card-border: #45475a;\n  --link-color: #89b4fa;\n  --color-success: #a6e3a1;\n  --color-error: #f38ba8;\n  --color-warning: #fab387;\n}\n".to_owned();
    };
    let value = std::str::from_utf8(bytes)
        .unwrap_or("")
        .parse::<toml::Value>()
        .unwrap_or_else(|_| toml::Value::Table(Default::default()));
    let background = theme_value(&value, &["general", "background"], fallback);
    let primary = theme_value(&value, &["text", "primary"], "#cdd6f4");
    let border = theme_value(&value, &["general", "border"], "#45475a");
    let success = theme_value(&value, &["text", "success"], "#a6e3a1");
    let error = theme_value(&value, &["text", "error"], "#f38ba8");
    format!(
        ":root {{\n  --page-bg: {};\n  --page-text: {};\n  --topbar-bg: {};\n  --topbar-text: {};\n  --card-bg: {};\n  --card-border: {};\n  --link-color: {};\n  --color-success: {};\n  --color-error: {};\n  --color-warning: {};\n}}\n",
        background,
        primary,
        background,
        primary,
        background,
        border,
        theme_value(&value, &["buffer", "url"], "#89b4fa"),
        success,
        error,
        theme_value(&value, &["buffer", "action"], "#fab387")
    )
}

pub(super) fn theme_bytes(name: &str) -> Option<&'static [u8]> {
    Some(match name {
        "Booberry" => include_bytes!("../../assets/dashboard/themes/Booberry.toml"),
        "Catppuccin Latte" => include_bytes!("../../assets/dashboard/themes/Catppuccin Latte.toml"),
        "Catppuccin Macchiato" => {
            include_bytes!("../../assets/dashboard/themes/Catppuccin Macchiato.toml")
        }
        "Catppuccin Mocha" => include_bytes!("../../assets/dashboard/themes/Catppuccin Mocha.toml"),
        "Cyber Red" => include_bytes!("../../assets/dashboard/themes/Cyber Red.toml"),
        "Cyberpunk" => include_bytes!("../../assets/dashboard/themes/Cyberpunk.toml"),
        "Dark Green" => include_bytes!("../../assets/dashboard/themes/Dark Green.toml"),
        "Discord (80_ Saturation)" => {
            include_bytes!("../../assets/dashboard/themes/Discord (80_ Saturation).toml")
        }
        "Discord" => include_bytes!("../../assets/dashboard/themes/Discord.toml"),
        "Dracula" => include_bytes!("../../assets/dashboard/themes/Dracula.toml"),
        "Ferra Light" => include_bytes!("../../assets/dashboard/themes/Ferra Light.toml"),
        "Flexor Dark" => include_bytes!("../../assets/dashboard/themes/Flexor Dark.toml"),
        "Gruvbox" => include_bytes!("../../assets/dashboard/themes/Gruvbox.toml"),
        "Halcyon Dark" => include_bytes!("../../assets/dashboard/themes/Halcyon Dark.toml"),
        "IntelliJ Light" => include_bytes!("../../assets/dashboard/themes/IntelliJ Light.toml"),
        "Kanagawa" => include_bytes!("../../assets/dashboard/themes/Kanagawa.toml"),
        "Macaw Dark" => include_bytes!("../../assets/dashboard/themes/Macaw Dark.toml"),
        "Macaw Light" => include_bytes!("../../assets/dashboard/themes/Macaw Light.toml"),
        "Matrix" => include_bytes!("../../assets/dashboard/themes/Matrix.toml"),
        "Noctis Lilac" => include_bytes!("../../assets/dashboard/themes/Noctis Lilac.toml"),
        "Nord" => include_bytes!("../../assets/dashboard/themes/Nord.toml"),
        "Nostromo Terminal" => {
            include_bytes!("../../assets/dashboard/themes/Nostromo Terminal.toml")
        }
        "One Dark" => include_bytes!("../../assets/dashboard/themes/One Dark.toml"),
        "Oxocarbon" => include_bytes!("../../assets/dashboard/themes/Oxocarbon.toml"),
        "Rose Pine Dawn" => include_bytes!("../../assets/dashboard/themes/Rose Pine Dawn.toml"),
        "Rose Pine Moon" => include_bytes!("../../assets/dashboard/themes/Rose Pine Moon.toml"),
        "Rose Pine" => include_bytes!("../../assets/dashboard/themes/Rose Pine.toml"),
        "Solarized Dark" => include_bytes!("../../assets/dashboard/themes/Solarized Dark.toml"),
        "Sonokai" => include_bytes!("../../assets/dashboard/themes/Sonokai.toml"),
        "Tokyo Night Storm" => {
            include_bytes!("../../assets/dashboard/themes/Tokyo Night Storm.toml")
        }
        "VESPER" => include_bytes!("../../assets/dashboard/themes/VESPER.toml"),
        "Zenburn" => include_bytes!("../../assets/dashboard/themes/Zenburn.toml"),
        "acton" => include_bytes!("../../assets/dashboard/themes/acton.toml"),
        "bam" => include_bytes!("../../assets/dashboard/themes/bam.toml"),
        "base16-atelier-forest-light" => {
            include_bytes!("../../assets/dashboard/themes/base16-atelier-forest-light.toml")
        }
        "berlin" => include_bytes!("../../assets/dashboard/themes/berlin.toml"),
        "black but with important highlights" => {
            include_bytes!("../../assets/dashboard/themes/black but with important highlights.toml")
        }
        "broc" => include_bytes!("../../assets/dashboard/themes/broc.toml"),
        "cork" => include_bytes!("../../assets/dashboard/themes/cork.toml"),
        "ferra" => include_bytes!("../../assets/dashboard/themes/ferra.toml"),
        "forest" => include_bytes!("../../assets/dashboard/themes/forest.toml"),
        "lisbon" => include_bytes!("../../assets/dashboard/themes/lisbon.toml"),
        "midnight" => include_bytes!("../../assets/dashboard/themes/midnight.toml"),
        "oslo" => include_bytes!("../../assets/dashboard/themes/oslo.toml"),
        "plum" => include_bytes!("../../assets/dashboard/themes/plum.toml"),
        "portland" => include_bytes!("../../assets/dashboard/themes/portland.toml"),
        "sunset" => include_bytes!("../../assets/dashboard/themes/sunset.toml"),
        "tofino" => include_bytes!("../../assets/dashboard/themes/tofino.toml"),
        "vanimo" => include_bytes!("../../assets/dashboard/themes/vanimo.toml"),
        "vik" => include_bytes!("../../assets/dashboard/themes/vik.toml"),
        _ => return None,
    })
}

pub(super) fn theme_value<'a>(value: &'a toml::Value, path: &[&str], fallback: &'a str) -> &'a str {
    path.iter()
        .try_fold(value, |current, key| current.get(*key))
        .and_then(toml::Value::as_str)
        .unwrap_or(fallback)
}

#[cfg(test)]
mod tests {
    use std::{fs, path::PathBuf};

    use super::html_escape;
    use crate::server::middleware::{is_loopback_host, valid_key_shape, verify_api_key};
    use axum::http::{HeaderMap, HeaderValue};
    use serde::Deserialize;
    use sha2::{Digest, Sha256};

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
            serde_json::from_str(include_str!("../../assets/dashboard/manifest.json"))
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
}
