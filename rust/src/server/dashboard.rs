const DEFAULT_THEME: &str = "Cyber Red";
const MAX_THEME_NAME_BYTES: usize = 128;

use std::io::IsTerminal;

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
    "Discord",
    "Discord (80_ Saturation)",
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
    "Rose Pine",
    "Rose Pine Dawn",
    "Rose Pine Moon",
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
    let accounts = match db::AccountRepository::new(&state.database).list_all().await {
        Ok(accounts) => accounts,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let page_data = match db::DashboardRepository::new(&state.database)
        .load(period)
        .await
    {
        Ok(data) => data,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let theme_name = selected_theme(
        query
            .theme
            .as_deref()
            .unwrap_or(&state.server.dashboard_theme),
    );
    let show_disabled = query.show_disabled.as_deref() == Some("1");
    let health_snapshots = state
        .runtime
        .acquire()
        .await
        .ok()
        .map(|lease| {
            lease
                .generation()
                .inference()
                .router_handle()
                .health_snapshots()
        })
        .unwrap_or_default();
    let html = render_overview(
        &summary,
        OverviewPage {
            accounts: &accounts,
            page_data: &page_data,
            period,
            theme: theme_name,
            refresh_interval_s: state.server.dashboard_refresh_interval_s,
            show_disabled,
            health_snapshots: &health_snapshots,
        },
    );
    html_response(html)
}

pub(super) async fn accounts_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page_with_options(
        &state,
        "Accounts",
        "accounts",
        query.period,
        query.theme,
        query.show_disabled.as_deref() == Some("1"),
        ModelFilters::default(),
    )
    .await
}

pub(super) async fn models_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page_with_model_filters(
        &state,
        query.period,
        query.theme,
        ModelFilters {
            account: query.account,
            used: query.used,
            info_status: query.info_status,
            availability: query.availability,
            ..ModelFilters::default()
        },
    )
    .await
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
    let model_info = crate::operations::operator::show_model_info(&state.database, model_id)
        .await
        .ok()
        .flatten();
    let observations =
        crate::operations::operator::list_compact_model_observations(&state.database, model_id)
            .await
            .unwrap_or_default();
    let body = render_model_detail(model_id, model_info.as_ref(), &observations);
    let title_model_id = model_info
        .as_ref()
        .and_then(|info| info["detail"]["display_name"].as_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or(model_id);
    dashboard_page_with_body(
        &state,
        &format!("Model: {title_model_id}"),
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
    dashboard_data_page_with_options(
        &state,
        "Events",
        "events",
        query.period,
        query.theme,
        false,
        ModelFilters {
            event_type: query.event_type,
            ..ModelFilters::default()
        },
    )
    .await
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
    dashboard_data_page_with_options(
        &state,
        "Traces",
        "traces",
        query.period,
        query.theme,
        false,
        ModelFilters {
            trace_limit: query.limit,
            ..ModelFilters::default()
        },
    )
    .await
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

async fn observability_api(state: AppState, period: Option<String>, route: &str) -> Response {
    let period = match normalize_period(period.as_deref()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let stats = match db::DashboardRepository::new(&state.database)
        .observability_stats(period)
        .await
    {
        Ok(value) => value,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let key = match route {
        "transcoding" => "transcoding",
        "cache-observability" => "cache_observability",
        "canonical-request-segmentation" => "canonical_request_segmentation",
        "cache-stability" => "cache_stability",
        _ => "request_shaping",
    };
    json_response(
        StatusCode::OK,
        stats.get(key).cloned().unwrap_or(Value::Null),
    )
}

pub(super) async fn stats_transcoding(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "transcoding").await
}
pub(super) async fn stats_cache_observability(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "cache-observability").await
}
pub(super) async fn stats_request_segmentation(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "canonical-request-segmentation").await
}
pub(super) async fn stats_cache_stability(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "cache-stability").await
}
pub(super) async fn stats_request_shaping(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "request-shaping").await
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
}

pub(super) async fn timeseries_api(
    State(state): State<AppState>,
    Query(query): Query<TimeseriesQuery>,
) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let bucket = normalized_bucket(query.bucket.as_deref(), period);
    let rows = match db::DashboardRepository::new(&state.database)
        .timeseries_json(
            period.to_owned(),
            bucket.to_owned(),
            query.account,
            query.model,
        )
        .await
    {
        Ok(value) => value,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    json_response(StatusCode::OK, json!(rows))
}

fn normalized_bucket(value: Option<&str>, period: &str) -> &'static str {
    match value.unwrap_or("auto") {
        "day" => "day",
        "hour" => "hour",
        _ if period == "30d" => "day",
        _ => "hour",
    }
}

fn normalized_group_by(value: &str) -> &'static str {
    match value {
        "provider" => "provider",
        "model" => "model",
        "account" => "account",
        "provider_model" => "provider_model",
        _ => "provider_model",
    }
}

pub(super) async fn grouped_timeseries_api(
    State(state): State<AppState>,
    Query(query): Query<TimeseriesQuery>,
) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let _compat_metric = query.metric;
    let bucket = normalized_bucket(query.bucket.as_deref(), period);
    let group_by = normalized_group_by(query.group_by.as_deref().unwrap_or("provider_model"));
    let limit = query.limit.unwrap_or(12).clamp(1, 25);
    let (rows, from_rollups) = match db::DashboardRepository::new(&state.database)
        .grouped_timeseries_json(
            period.to_owned(),
            bucket.to_owned(),
            group_by.to_owned(),
            query.account,
            query.model,
        )
        .await
    {
        Ok(value) => value,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    if rows.is_empty() {
        return json_response(
            StatusCode::OK,
            json!({
                "bucket": bucket,
                "group_by": group_by,
                "metric": "requests",
                "limit": limit,
                "source": "empty",
                "degraded_reason": "rollup_empty",
                "buckets": [],
                "series": [],
                "points": [],
                "bucket_totals": []
            }),
        );
    }
    json_response(
        StatusCode::OK,
        grouped_timeseries_projection(&rows, bucket, group_by, limit, from_rollups),
    )
}

fn grouped_timeseries_projection(
    rows: &[Value],
    bucket: &str,
    group_by: &str,
    limit: usize,
    from_rollups: bool,
) -> Value {
    let mut totals = std::collections::BTreeMap::<String, i64>::new();
    for row in rows {
        let key = row["raw_series_key"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        *totals.entry(key).or_default() += row["request_count"].as_i64().unwrap_or(0);
    }
    let mut ranked = totals.into_iter().collect::<Vec<_>>();
    ranked.sort_by(|(left_key, left_count), (right_key, right_count)| {
        right_count
            .cmp(left_count)
            .then_with(|| left_key.cmp(right_key))
    });
    let selected = ranked
        .iter()
        .take(limit)
        .map(|(key, _)| key.clone())
        .collect::<std::collections::BTreeSet<_>>();
    let mut points = std::collections::BTreeMap::<(String, String), (Value, f64, f64)>::new();
    let mut buckets = std::collections::BTreeSet::new();
    for row in rows {
        let bucket_name = row["bucket"].as_str().unwrap_or_default().to_owned();
        buckets.insert(bucket_name.clone());
        let raw_key = row["raw_series_key"].as_str().unwrap_or_default();
        let is_other = !selected.contains(raw_key);
        let key = if is_other { "__other__" } else { raw_key }.to_owned();
        let point = points.entry((bucket_name.clone(), key.clone())).or_insert_with(|| {
            let is_other = key == "__other__";
            (json!({
                "bucket": bucket_name,
                "series_key": key,
                "label": if is_other { "Other" } else { row["raw_series_label"].as_str().unwrap_or_default() },
                "provider_id": if is_other { Value::Null } else { row["provider_id"].clone() },
                "model_id": if is_other { Value::Null } else { row["model_id"].clone() },
                "account_name": if is_other { Value::Null } else { json!("") },
                "is_other": is_other,
                "request_count": 0, "error_count": 0, "input_tokens": 0,
                "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": 0, "total_tokens": 0, "cost_microdollars": 0,
                "bytes_received": 0, "bytes_emitted": 0, "avg_latency_ms": 0.0,
                "avg_ttft_ms": 0.0
            }), 0.0, 0.0)
        });
        let value = &mut point.0;
        let count = row["request_count"].as_i64().unwrap_or(0);
        for field in [
            "request_count",
            "error_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_microdollars",
            "bytes_received",
            "bytes_emitted",
        ] {
            let next = value[field].as_i64().unwrap_or(0) + row[field].as_i64().unwrap_or(0);
            value[field] = json!(next);
        }
        point.1 += row["avg_latency_ms"].as_f64().unwrap_or(0.0) * count as f64;
        point.2 += row["avg_ttft_ms"].as_f64().unwrap_or(0.0) * count as f64;
    }
    let mut finished_points = points
        .into_values()
        .map(|(mut point, latency_sum, ttft_sum)| {
            let count = point["request_count"].as_i64().unwrap_or(0);
            point["avg_latency_ms"] = json!(if count > 0 {
                latency_sum / count as f64
            } else {
                0.0
            });
            point["avg_ttft_ms"] = json!(if count > 0 {
                ttft_sum / count as f64
            } else {
                0.0
            });
            point
        })
        .collect::<Vec<_>>();
    finished_points.sort_by(|left, right| {
        left["bucket"]
            .as_str()
            .cmp(&right["bucket"].as_str())
            .then_with(|| left["is_other"].as_bool().cmp(&right["is_other"].as_bool()))
            .then_with(|| left["label"].as_str().cmp(&right["label"].as_str()))
    });
    let mut series = std::collections::BTreeMap::<String, Value>::new();
    let mut bucket_totals = std::collections::BTreeMap::<String, Value>::new();
    for point in &finished_points {
        let key = point["series_key"].as_str().unwrap_or_default().to_owned();
        let is_other = point["is_other"].as_bool().unwrap_or(false);
        let entry = series.entry(key.clone()).or_insert_with(|| {
            let label = point["label"].clone();
            json!({"key":key,"label":label,"provider_id":point["provider_id"],"model_id":point["model_id"],"account_name":point["account_name"],"is_other":is_other,"total_requests":0,"error_count":0,"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":0,"cost_microdollars":0,"bytes_received":0,"bytes_emitted":0,"avg_latency_ms":0.0,"avg_ttft_ms":0.0})
        });
        let count = point["request_count"].as_i64().unwrap_or(0);
        entry["total_requests"] = json!(entry["total_requests"].as_i64().unwrap_or(0) + count);
        for field in [
            "error_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_microdollars",
            "bytes_received",
            "bytes_emitted",
        ] {
            entry[field] =
                json!(entry[field].as_i64().unwrap_or(0) + point[field].as_i64().unwrap_or(0));
        }
        entry["avg_latency_ms"] = json!(
            entry["avg_latency_ms"].as_f64().unwrap_or(0.0)
                + point["avg_latency_ms"].as_f64().unwrap_or(0.0) * count as f64
        );
        entry["avg_ttft_ms"] = json!(
            entry["avg_ttft_ms"].as_f64().unwrap_or(0.0)
                + point["avg_ttft_ms"].as_f64().unwrap_or(0.0) * count as f64
        );
        let bucket = point["bucket"].as_str().unwrap_or_default().to_owned();
        let total = bucket_totals.entry(bucket).or_insert_with(|| json!({"request_count":0,"error_count":0,"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":0,"cost_microdollars":0,"bytes_received":0,"bytes_emitted":0,"avg_latency_ms":0.0,"avg_ttft_ms":0.0}));
        total["request_count"] = json!(total["request_count"].as_i64().unwrap_or(0) + count);
        for field in [
            "error_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_microdollars",
            "bytes_received",
            "bytes_emitted",
        ] {
            total[field] =
                json!(total[field].as_i64().unwrap_or(0) + point[field].as_i64().unwrap_or(0));
        }
        total["avg_latency_ms"] = json!(
            total["avg_latency_ms"].as_f64().unwrap_or(0.0)
                + point["avg_latency_ms"].as_f64().unwrap_or(0.0) * count as f64
        );
        total["avg_ttft_ms"] = json!(
            total["avg_ttft_ms"].as_f64().unwrap_or(0.0)
                + point["avg_ttft_ms"].as_f64().unwrap_or(0.0) * count as f64
        );
    }
    for entry in series.values_mut() {
        let count = entry["total_requests"].as_i64().unwrap_or(0);
        if count > 0 {
            entry["avg_latency_ms"] =
                json!(entry["avg_latency_ms"].as_f64().unwrap_or(0.0) / count as f64);
            entry["avg_ttft_ms"] =
                json!(entry["avg_ttft_ms"].as_f64().unwrap_or(0.0) / count as f64);
        }
    }
    for total in bucket_totals.values_mut() {
        let count = total["request_count"].as_i64().unwrap_or(0);
        if count > 0 {
            total["avg_latency_ms"] =
                json!(total["avg_latency_ms"].as_f64().unwrap_or(0.0) / count as f64);
            total["avg_ttft_ms"] =
                json!(total["avg_ttft_ms"].as_f64().unwrap_or(0.0) / count as f64);
        }
    }
    let series = ranked_series_order(&ranked, &selected, series);
    let total_rows = bucket_totals
        .iter()
        .map(|(bucket, value)| {
            let mut value = value.clone();
            value["bucket"] = json!(bucket);
            value
        })
        .collect::<Vec<_>>();
    json!({"bucket":bucket,"group_by":group_by,"metric":"requests","limit":limit,"source":if from_rollups {"rollup"} else {"raw"},"degraded_reason":"none","buckets":buckets,"series":series,"points":finished_points,"bucket_totals":total_rows})
}

fn ordered_json_object(value: &Value, fields: &[&str]) -> String {
    let entries = fields
        .iter()
        .filter_map(|field| {
            value.get(*field).map(|field_value| {
                format!(
                    "{}:{}",
                    serde_json::to_string(field).unwrap_or_else(|_| "\"\"".to_owned()),
                    serde_json::to_string(field_value).unwrap_or_else(|_| "null".to_owned()),
                )
            })
        })
        .collect::<Vec<_>>();
    format!("{{{}}}", entries.join(","))
}

fn grouped_timeseries_json(value: &Value) -> String {
    const TOP: &[&str] = &[
        "bucket",
        "group_by",
        "metric",
        "limit",
        "series",
        "buckets",
        "bucket_totals",
        "points",
        "source",
        "degraded_reason",
    ];
    const SERIES: &[&str] = &[
        "key",
        "label",
        "provider_id",
        "model_id",
        "account_name",
        "is_other",
        "total_requests",
        "error_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_microdollars",
        "bytes_received",
        "bytes_emitted",
        "avg_latency_ms",
        "avg_ttft_ms",
    ];
    const BUCKET_TOTAL: &[&str] = &[
        "bucket",
        "request_count",
        "error_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_microdollars",
        "bytes_received",
        "bytes_emitted",
        "avg_latency_ms",
        "avg_ttft_ms",
    ];
    const POINT: &[&str] = &[
        "bucket",
        "series_key",
        "label",
        "provider_id",
        "model_id",
        "account_name",
        "is_other",
        "request_count",
        "error_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_microdollars",
        "bytes_received",
        "bytes_emitted",
        "avg_latency_ms",
        "avg_ttft_ms",
    ];
    let fields = TOP
        .iter()
        .filter_map(|field| {
            value.get(*field).map(|field_value| {
                let rendered = match *field {
                    "series" => field_value
                        .as_array()
                        .map(|items| {
                            format!(
                                "[{}]",
                                items
                                    .iter()
                                    .map(|item| ordered_json_object(item, SERIES))
                                    .collect::<Vec<_>>()
                                    .join(",")
                            )
                        })
                        .unwrap_or_else(|| "[]".to_owned()),
                    "bucket_totals" => field_value
                        .as_array()
                        .map(|items| {
                            format!(
                                "[{}]",
                                items
                                    .iter()
                                    .map(|item| ordered_json_object(item, BUCKET_TOTAL))
                                    .collect::<Vec<_>>()
                                    .join(",")
                            )
                        })
                        .unwrap_or_else(|| "[]".to_owned()),
                    "points" => field_value
                        .as_array()
                        .map(|items| {
                            format!(
                                "[{}]",
                                items
                                    .iter()
                                    .map(|item| ordered_json_object(item, POINT))
                                    .collect::<Vec<_>>()
                                    .join(",")
                            )
                        })
                        .unwrap_or_else(|| "[]".to_owned()),
                    _ => serde_json::to_string(field_value).unwrap_or_else(|_| "null".to_owned()),
                };
                format!(
                    "{}:{rendered}",
                    serde_json::to_string(field).unwrap_or_else(|_| "\"\"".to_owned()),
                )
            })
        })
        .collect::<Vec<_>>();
    format!("{{{}}}", fields.join(","))
}

fn escape_script_end_tags(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    let mut offset = 0;
    while offset < value.len() {
        let remaining = &value[offset..];
        if remaining
            .get(..8)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("</script"))
        {
            escaped.push_str("\\u003c");
            offset += 1;
            continue;
        }
        let Some(character) = remaining.chars().next() else {
            break;
        };
        escaped.push(character);
        offset += character.len_utf8();
    }
    escaped
}

fn ranked_series_order(
    ranked: &[(String, i64)],
    selected: &std::collections::BTreeSet<String>,
    mut series: std::collections::BTreeMap<String, Value>,
) -> Vec<Value> {
    let mut ordered = ranked
        .iter()
        .filter(|(key, _)| selected.contains(key))
        .filter_map(|(key, _)| series.remove(key))
        .collect::<Vec<_>>();
    if let Some(other) = series.remove("__other__") {
        ordered.push(other);
    }
    ordered
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
    dashboard_data_page_with_options(
        state,
        title,
        active_nav,
        period,
        theme,
        false,
        ModelFilters::default(),
    )
    .await
}

async fn dashboard_data_page_with_options(
    state: &AppState,
    title: &str,
    active_nav: &str,
    period: Option<String>,
    theme: Option<String>,
    show_disabled: bool,
    model_filters: ModelFilters,
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
    let observability = if matches!(active_nav, "runtime" | "cache") {
        match db::DashboardRepository::new(&state.database)
            .observability_stats(period)
            .await
        {
            Ok(value) => value,
            Err(_) => return degraded("dashboard data unavailable"),
        }
    } else {
        Value::Null
    };
    let runtime_diagnostics = if active_nav == "runtime" {
        state
            .process
            .as_ref()
            .map(|process| process.diagnostics(&state.runtime))
    } else {
        None
    };
    let model_info = if active_nav == "models" {
        crate::operations::operator::list_model_info(&state.database, None)
            .await
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let health_snapshots = if active_nav == "accounts" {
        state
            .runtime
            .acquire()
            .await
            .ok()
            .map(|lease| {
                lease
                    .generation()
                    .inference()
                    .router_handle()
                    .health_snapshots()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let provider_priorities = if active_nav == "models" {
        state
            .runtime
            .acquire()
            .await
            .ok()
            .map(|lease| {
                lease
                    .generation()
                    .config()
                    .providers
                    .iter()
                    .map(|(provider_id, provider)| (provider_id.clone(), provider.routing_priority))
                    .collect::<std::collections::BTreeMap<_, _>>()
            })
            .unwrap_or_default()
    } else {
        std::collections::BTreeMap::new()
    };
    let timeseries_projection = if active_nav == "timeseries" {
        match db::DashboardRepository::new(&state.database)
            .grouped_timeseries_json(
                period.to_owned(),
                "hour".to_owned(),
                "provider_model".to_owned(),
                None,
                None,
            )
            .await
        {
            Ok((rows, _from_rollups)) if rows.is_empty() => Some(json!({
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
            })),
            Ok((rows, from_rollups)) => Some(grouped_timeseries_projection(
                &rows,
                "hour",
                "provider_model",
                12,
                from_rollups,
            )),
            Err(error) => {
                eprintln!("dashboard timeseries read failed: {error}");
                return degraded("dashboard data unavailable");
            }
        }
    } else {
        None
    };
    let routing_trace = if active_nav == "routing" {
        state
            .runtime
            .acquire()
            .await
            .ok()
            .map(|lease| {
                let config = &lease.generation().config().routing.trace;
                RoutingTraceSnapshot {
                    mode: config.mode.clone(),
                    sample_rate: config.sample_rate,
                    status: if config.mode == "off" {
                        "Off".to_owned()
                    } else {
                        "Unavailable".to_owned()
                    },
                    queue_capacity: if config.mode == "off" {
                        0
                    } else {
                        u64::from(config.queue_capacity)
                    },
                    ..RoutingTraceSnapshot::default()
                }
            })
            .unwrap_or_default()
    } else {
        RoutingTraceSnapshot::default()
    };
    let mut body = render_dashboard_page_body(
        title,
        active_nav,
        period,
        theme,
        &data,
        &summary,
        &observability,
        runtime_diagnostics.as_ref(),
        state.server.started_at.elapsed(),
        &model_info,
        show_disabled,
        &model_filters,
        &health_snapshots,
        &provider_priorities,
        timeseries_projection.as_ref(),
        &routing_trace,
    );
    if active_nav == "timeseries" {
        body = body.replace(
            "class=\"period-selector\"",
            "class=\"period-selector timeseries-period-selector\"",
        );
    }
    dashboard_page_with_body(
        state,
        title,
        active_nav,
        Some(period.to_owned()),
        Some(theme.to_owned()),
        body,
    )
}

async fn dashboard_data_page_with_model_filters(
    state: &AppState,
    period: Option<String>,
    theme: Option<String>,
    model_filters: ModelFilters,
) -> Response {
    dashboard_data_page_with_options(
        state,
        "Models",
        "models",
        period,
        theme,
        false,
        model_filters,
    )
    .await
}

pub(super) fn dashboard_header(title: &str, period: &str, theme: &str) -> String {
    format!(
        "<h2>{}</h2><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>",
        html_escape(title),
        period_options(period),
        html_escape(theme),
    )
}

fn dashboard_period_selector(period: &str, theme: &str) -> String {
    format!(
        "<form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>",
        period_options(period),
        html_escape(theme),
    )
}

pub(super) fn dashboard_empty(title: &str, message: &str) -> String {
    format!(
        "<section class=\"panel\"><div class=\"panel-header\"><h2>{}</h2></div><p class=\"empty\" role=\"status\">{}</p></section>",
        html_escape(title),
        html_escape(message),
    )
}

#[allow(clippy::too_many_arguments)]
pub(super) fn render_dashboard_page_body(
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

pub(super) fn render_accounts_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    show_disabled: bool,
    health_snapshots: &[crate::health::AccountHealthSnapshot],
) -> String {
    let detail_headers = [
        "Reserved",
        "Resv.",
        "5h rate",
        "7d rate",
        "30d rate",
        "BW received",
        "BW emitted",
        "Over budget",
        "Upstream backoff",
        "Backoff until",
        "Failures",
        "Auth fail",
        "Disabled",
        "Est. cost",
        "Cache R",
        "Cache W",
        "Reasoning",
        "Avg cost/req",
        "Avg cost/1k tok",
    ]
    .iter()
    .map(|label| format!("<th data-priority=\"3\">{label}</th>"))
    .collect::<String>();
    let disabled_count = data
        .accounts
        .iter()
        .filter(|account| !account.enabled)
        .count();
    if data
        .accounts
        .iter()
        .all(|account| !show_disabled && !account.enabled)
    {
        let empty_message = if disabled_count > 0 {
            format!(
                "No enabled accounts. {disabled_count} disabled account{} hidden — <a href=\"?show_disabled=1\">show them</a>.",
                if disabled_count == 1 { "" } else { "s" }
            )
        } else {
            "No accounts configured.".to_owned()
        };
        return format!(
            "<h2>Accounts</h2><form method=\"get\" class=\"period-selector account-filters\" data-period-selector aria-label=\"Account filters\"><label for=\"period\">Period: </label><select id=\"period\" name=\"period\" data-auto-submit=\"1\">{}</select><label for=\"show_disabled\">Disabled: </label><select id=\"show_disabled\" name=\"show_disabled\" data-auto-submit=\"1\"><option value=\"0\"{}>Hide disabled accounts</option><option value=\"1\"{}>Show disabled accounts</option></select><input type=\"hidden\" name=\"theme\" value=\"{}\"></form><section class=\"panel\"><p class=\"empty\">{empty_message}</p></section>",
            period_options(period),
            if show_disabled {
                ""
            } else {
                " selected=\"selected\""
            },
            if show_disabled {
                " selected=\"selected\""
            } else {
                ""
            },
            html_escape(theme)
        );
    }
    let rows = data
        .accounts
        .iter()
        .filter(|row| show_disabled || row.enabled)
        .map(|row| {
            let live_health = health_snapshots
                .iter()
                .find(|snapshot| snapshot.account_name == row.name);
            let health_state = live_health
                .map(|snapshot| snapshot.health_state.as_str())
                .or_else(|| {
                    data.pings
                        .iter()
                        .find(|ping| ping.account_name == row.name)
                        .map(|ping| {
                            if ping
                                .status_code
                                .is_some_and(|status| (200..300).contains(&status))
                            {
                                "healthy"
                            } else if ping.status_code.is_some() || ping.error.is_some() {
                                "unhealthy"
                            } else {
                                "unknown"
                            }
                        })
                })
                .unwrap_or("unknown");
            let exactness = exactness_badge(
                row.exact_count,
                row.derived_count,
                row.partial_count,
                row.estimated_count,
                row.unknown_count,
                row.provider_reported_count,
            );
            let tokens_per_second = if row.avg_latency_ms > 0.0 && row.requests > 0 {
                format!(
                    "{:.1} tok/s",
                    row.output_tokens as f64 * 1_000.0
                        / (row.avg_latency_ms * row.requests as f64)
                )
            } else {
                "0.0 tok/s".to_owned()
            };
            let authentication_failed = live_health.map_or("—", |snapshot| {
                if snapshot.health_state == "authentication_failed" {
                    "yes"
                } else {
                    "no"
                }
            });
            let operator_disabled = live_health.map_or("—", |snapshot| {
                if snapshot
                    .disabled_until
                    .is_some_and(|until| until > snapshot.last_check)
                {
                    "yes"
                } else {
                    "no"
                }
            });
            let auth_class = if authentication_failed == "—" {
                String::new()
            } else {
                format!(" class=\"{authentication_failed}\"")
            };
            let disabled_class = if operator_disabled == "—" {
                String::new()
            } else {
                format!(" class=\"{operator_disabled}\"")
            };
            let detail_cells = format!(
                "<td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td>",
                format_microdollars(row.reserved_microdollars),
                row.active_reservations,
                format_microdollars(row.utilization_5h),
                format_microdollars(row.utilization_7d),
                format_microdollars(row.utilization_30d),
                format_bytes(row.bytes_received),
                format_bytes(row.bytes_emitted),
                live_health.map_or(0, |snapshot| i64::from(snapshot.consecutive_failures)),
                auth_class,
                authentication_failed,
                disabled_class,
                operator_disabled,
                format_ratio_percent(Some(row.estimated_cost_fraction)),
                format_ratio_percent(row.cache_read_ratio),
                format_ratio_percent(row.cache_write_ratio),
                format_ratio_percent(row.reasoning_output_ratio),
                row.avg_cost_per_request
                    .map(format_microdollars)
                    .unwrap_or_else(|| "—".to_owned()),
                row.avg_cost_per_1k_tokens
                    .map(format_microdollars)
                    .unwrap_or_else(|| "—".to_owned()),
            );
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{}\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\" class=\"{}\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td>{}</tr>",
                html_escape(&row.name),
                html_escape(&row.provider_id),
                if row.enabled { "yes" } else { "no" },
                if row.enabled { "yes" } else { "no" },
                row.requests,
                format_microdollars(row.cost_microdollars),
                html_escape(health_state),
                html_escape(health_state),
                row.errors,
                format_tokens(row.input_tokens),
                format_tokens(row.output_tokens),
                format_tokens(row.input_tokens + row.output_tokens),
                format_latency(row.avg_latency_ms),
                tokens_per_second,
                exactness,
                detail_cells,
            )
        })
        .collect::<String>();
    let high_spend_rows = data
        .accounts
        .iter()
        .filter_map(|row| {
            let estimated_microdollars = row.cost_microdollars as f64 * row.estimated_cost_fraction;
            (estimated_microdollars >= 10_000_000.0).then(|| {
                format!(
                    "<li><code>{}</code>: ~${:.2} estimated ({}% of total)</li>",
                    html_escape(&row.name),
                    estimated_microdollars / 1_000_000.0,
                    (row.estimated_cost_fraction * 100.0).round() as i64
                )
            })
        })
        .collect::<String>();
    let pricing_warning = if high_spend_rows.is_empty() {
        String::new()
    } else {
        format!(
            "<div class=\"panel warn pricing-warning\"><strong>Pricing warning:</strong> the following accounts have substantial cost on estimated (non-exact) pricing in the selected period:<ul>{high_spend_rows}</ul></div>"
        )
    };
    format!(
        "<h2>Accounts</h2><form method=\"get\" class=\"period-selector account-filters\" data-period-selector aria-label=\"Account filters\"><label for=\"period\">Period: </label><select id=\"period\" name=\"period\" data-auto-submit=\"1\">{}</select><label for=\"show_disabled\">Disabled: </label><select id=\"show_disabled\" name=\"show_disabled\" data-auto-submit=\"1\"><option value=\"0\"{}>Hide disabled accounts</option><option value=\"1\"{}>Show disabled accounts</option></select><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>{pricing_warning}<section class=\"panel\"><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Account</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Enabled</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Health</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Input tokens</th><th data-priority=\"2\">Output tokens</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">TPS</th><th data-priority=\"2\">Exactness</th>{}</tr></thead><tbody>{rows}</tbody></table></div></section>",
        period_options(period),
        if show_disabled {
            ""
        } else {
            " selected=\"selected\""
        },
        if show_disabled {
            " selected=\"selected\""
        } else {
            ""
        },
        html_escape(theme),
        detail_headers
    )
}

fn exactness_badge(
    exact: i64,
    derived: i64,
    partial: i64,
    estimated: i64,
    unknown: i64,
    provider_reported: i64,
) -> String {
    let total = exact + derived + partial + estimated + unknown + provider_reported;
    if total == 0 {
        return "<span class=\"exactness-badge empty\">—</span>".to_owned();
    }
    let class = if estimated == total || unknown == total {
        "est-major"
    } else if estimated + unknown + partial > 0 {
        "partial-mix"
    } else {
        "derived"
    };
    let label = format!(
        "u:{provider_reported},e:{exact},d:{derived},p:{partial},~:{estimated},?:{unknown}"
    );
    format!(
        "<span class=\"exactness-badge {class}\" data-tooltip=\"{label}\" aria-label=\"{label}\">{label}</span>"
    )
}

pub(super) fn render_models_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    model_info: &[Value],
    filters: &ModelFilters,
    provider_priorities: &std::collections::BTreeMap<String, u32>,
) -> String {
    let option = |label: &str, value: &str, selected: Option<&str>| {
        format!(
            "<option value=\"{}\"{}>{}</option>",
            html_escape(value),
            if selected.map_or(value.is_empty(), |selected| selected == value) {
                " selected"
            } else {
                ""
            },
            html_escape(label),
        )
    };
    let account_options = std::iter::once(option("(any account)", "", filters.account.as_deref()))
        .chain(
            data.accounts
                .iter()
                .map(|account| option(&account.name, &account.name, filters.account.as_deref())),
        )
        .collect::<String>();
    let used_options = [("All", ""), ("Used", "used"), ("Unused", "unused")]
        .into_iter()
        .map(|(label, value)| option(label, value, filters.used.as_deref()))
        .collect::<String>();
    let info_options = [
        ("All", ""),
        ("Fresh", "fresh"),
        ("Partial", "partial"),
        ("Sparse", "sparse_new"),
        ("Stale", "stale"),
        ("Conflict", "conflicting"),
        ("Unmatched", "unmatched"),
    ]
    .into_iter()
    .map(|(label, value)| option(label, value, filters.info_status.as_deref()))
    .collect::<String>();
    let availability_options = [
        ("All", ""),
        ("Available", "available"),
        ("Unavailable", "unavailable"),
    ]
    .into_iter()
    .map(|(label, value)| option(label, value, filters.availability.as_deref()))
    .collect::<String>();
    let controls = format!(
        "<form method=\"get\" class=\"filter-form\"><label>Account: <select name=\"account\">{account_options}</select></label><label>Used: <select name=\"used\">{used_options}</select></label><label>Info: <select name=\"info_status\">{info_options}</select></label><label>Availability: <select name=\"availability\">{availability_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>",
        html_escape(period),
        html_escape(theme),
        period_options(period),
        html_escape(theme),
    );
    let models = data
        .models
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .filter(|row| match filters.used.as_deref() {
            Some("used") => row.requests > 0,
            Some("unused") => row.requests == 0,
            _ => true,
        })
        .filter(|row| match filters.availability.as_deref() {
            Some("available") => matches!(row.resolution_status.as_str(), "available" | "resolved"),
            Some("unavailable") => {
                matches!(row.resolution_status.as_str(), "unavailable" | "withdrawn")
            }
            _ => true,
        })
        .filter(|row| {
            filters.account.as_deref().is_none_or(str::is_empty)
                || data.requests.iter().any(|request| {
                    request.model_id == row.model_id
                        && filters.account.as_deref() == Some(request.account_name.as_str())
                })
        })
        .filter(|row| {
            let Some(status) = filters
                .info_status
                .as_deref()
                .filter(|status| !status.is_empty())
            else {
                return true;
            };
            model_info.iter().any(|info| {
                info["model_id"]
                    .as_str()
                    .is_some_and(|id| id.eq_ignore_ascii_case(&row.model_id))
                    && info["status"].as_str() == Some(status)
            })
        })
        .collect::<Vec<_>>();
    let model_info_warning = if model_info.is_empty() {
        "<p class=\"empty\" role=\"status\">Model info unavailable: service not attached. Check `app.state.model_info` and server logs.</p>"
    } else {
        ""
    };
    if models.is_empty() {
        let empty_message = if data
            .models
            .iter()
            .any(|row| row.model_id != "__deprecated__")
            && (filters
                .account
                .as_deref()
                .is_some_and(|value| !value.is_empty())
                || filters
                    .used
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
                || filters
                    .info_status
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
                || filters
                    .availability
                    .as_deref()
                    .is_some_and(|value| !value.is_empty()))
        {
            "No models match the selected filters."
        } else {
            "No models discovered from configured providers."
        };
        return format!(
            "<h2>Models</h2><p class=\"empty\" role=\"status\">Model info unavailable: service not attached. Check `app.state.model_info` and server logs.</p>{controls}<section class=\"panel\"><p class=\"empty\">{empty_message}</p></section>"
        );
    }
    let rows = models
        .iter()
        .map(|row| {
            let info = model_info
                .iter()
                .find(|info| info["model_id"].as_str().is_some_and(|id| id.eq_ignore_ascii_case(&row.model_id)));
            let info_pill = info.map_or_else(
                || "<span class=\"pill pill-unknown\" data-tooltip=\"No model info available\" aria-label=\"No model info available\">—</span>".to_owned(),
                |info| {
                    let status = info["status"].as_str().unwrap_or("unknown");
                    let summary = info["summary"].as_str().unwrap_or("");
                    format!("<span class=\"pill pill-{}\" data-tooltip=\"{}\">{}</span>", html_escape(status), html_escape(summary), html_escape(status))
                },
            );
            let (availability, availability_class) = match row.resolution_status.as_str() {
                "available" | "resolved" => ("available", "available"),
                "unavailable" | "withdrawn" => ("unavailable", "unavailable"),
                _ => ("configured", "configured"),
            };
            let availability_tooltip = if availability == "available" {
                " aria-label=\"Catalog entry with resolved protocol; can be routed.\" data-tooltip=\"Catalog entry with resolved protocol; can be routed.\""
            } else if availability == "unavailable" {
                " aria-label=\"Catalog entry is unavailable for routing.\" data-tooltip=\"Catalog entry is unavailable for routing.\""
            } else {
                ""
            };
            let exactness = exactness_badge(
                row.exact_count,
                row.derived_count,
                row.partial_count,
                row.estimated_count,
                row.unknown_count,
                row.provider_reported_count,
            );
            let tokens_per_second = if row.avg_latency_ms > 0.0 && row.requests > 0 {
                format!(
                    "{:.1} tok/s",
                    row.output_tokens as f64 * 1_000.0
                        / (row.avg_latency_ms * row.requests as f64)
                )
            } else {
                "—".to_owned()
            };
            let detail_cells = format!(
                "<td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td>",
                provider_priorities
                    .get(&row.provider_id)
                    .map_or_else(|| "—".to_owned(), |priority| priority.to_string()),
                format_ratio_percent(Some(row.estimated_cost_fraction)),
                format_ratio_percent(row.cache_read_ratio),
                format_ratio_percent(row.cache_write_ratio),
                format_ratio_percent(row.reasoning_output_ratio),
                row.avg_cost_per_request.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
                row.avg_cost_per_1k_tokens.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
            );
            let model_info_link_tooltip = info
                .and_then(|info| info["summary"].as_str())
                .filter(|summary| !summary.trim().is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("Open model info for {}", row.model_id));
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&model_info_link_tooltip),
                html_escape(&model_info_link_tooltip),
                html_escape(&row.model_id),
            );
            format!(
                "<tr data-model-id=\"{}\" data-model-info-key=\"{}\" data-provider-id=\"{}\"><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\"><span class=\"pill pill-{}\"{}>{}</span></td><td data-priority=\"1\">{}</td><td data-priority=\"2\"><span class=\"muted\">—</span></td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{:.1} ms</td><td data-priority=\"2\">{:.1} ms</td><td data-priority=\"2\">{}</td>{}</tr>",
                html_escape(&row.model_id),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                model_link,
                html_escape(&row.provider_id),
                availability_class,
                availability_tooltip,
                availability,
                info_pill,
                row.requests,
                format_microdollars(row.cost_microdollars),
                exactness,
                row.errors,
                format_tokens(row.input_tokens),
                format_tokens(row.output_tokens),
                format_tokens(row.input_tokens + row.output_tokens),
                row.avg_latency_ms,
                row.avg_ttft_ms,
                tokens_per_second,
                detail_cells,
            )
        })
        .collect::<String>();
    format!(
        "<h2>Models</h2>{model_info_warning}{controls}<section class=\"panel\"><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Model</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Avail.</th><th data-priority=\"1\">Info</th><th data-priority=\"2\">Benchmarks</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"1\">Exactness</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Input tokens</th><th data-priority=\"2\">Output tokens</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">Avg TTFT</th><th data-priority=\"2\">TPS</th>{}</tr></thead><tbody>{rows}</tbody></table></div></section>",
        "<th data-priority=\"3\">Priority</th><th data-priority=\"3\">Est. cost</th><th data-priority=\"3\">Cache R</th><th data-priority=\"3\">Cache W</th><th data-priority=\"3\">Reasoning</th><th data-priority=\"3\">Avg cost/req</th><th data-priority=\"3\">Avg cost/1k tok</th>"
    )
}

pub(super) fn render_model_detail(
    model_id: &str,
    model_info: Option<&Value>,
    observations: &[Value],
) -> String {
    let Some(info) = model_info else {
        return format!(
            "<h2>Model: {}</h2><p class=\"empty\">Model info not available.</p>",
            html_escape(model_id)
        );
    };
    let mut body = render_model_info_detail(model_id, info);
    body.push_str(&render_model_observations(observations));
    body
}

fn render_model_info_detail(model_id: &str, info: &Value) -> String {
    let detail = info.get("detail").unwrap_or(&Value::Null);
    let provenance = info.get("provenance").unwrap_or(&Value::Null);
    let conflicts = info.get("conflicts").unwrap_or(&Value::Null);
    let status = info["status"].as_str().unwrap_or("unknown");
    let sparse = info["sparse"].as_bool().unwrap_or(false);
    let summary = info["summary"].as_str().unwrap_or("");
    let status_display = match status {
        "sparse_new" => "sparse",
        "conflicting" => "conflict",
        "source_unavailable" => "source-unavailable",
        "manual_override" => "manual",
        other => other,
    };
    let mut status_pill_class = match status {
        "fresh" => "pill-fresh",
        "partial" | "manual_override" | "manual" => "pill-partial",
        "sparse_new" | "sparse" => "pill-sparse",
        "stale" | "withdrawn" => "pill-stale",
        "conflicting" | "conflict" => "pill-conflict",
        "unmatched" => "pill-unmatched",
        "source_unavailable" | "source-unavailable" => "pill-source-unavailable",
        _ => "pill-unknown",
    };
    if sparse && !matches!(status, "sparse" | "sparse_new") {
        status_pill_class = match status_pill_class {
            "pill-fresh" => "pill-fresh",
            other => other,
        };
    }
    let benchmarks = detail.get("benchmarks").and_then(Value::as_array);
    let benchmark_brief = benchmarks.map_or_else(String::new, |rows| {
        rows.iter()
            .take(4)
            .filter_map(|row| {
                let name = row["name"]
                    .as_str()
                    .or_else(|| row["benchmark"].as_str())?
                    .trim();
                if name.is_empty() {
                    return None;
                }
                let source = dashboard_benchmark_source(row["source"].as_str().unwrap_or(""));
                Some(format!(
                    "{source}: {} {}",
                    dashboard_benchmark_short_name(name),
                    dashboard_benchmark_result(row)
                ))
            })
            .collect::<Vec<_>>()
            .join(" · ")
    });
    let sources = dashboard_model_sources(provenance.get("sources"));
    let mut tooltip_parts = Vec::new();
    if !summary.trim().is_empty() {
        tooltip_parts.push(summary.to_owned());
    }
    if !sources.is_empty() {
        tooltip_parts.push(format!("Sources: {}", sources.join(", ")));
    }
    if let Some(refreshed) = info["last_refreshed_at"].as_str() {
        tooltip_parts.push(format!(
            "Last checked: {}",
            dashboard_iso_timestamp(refreshed)
        ));
    }
    let tooltip = if tooltip_parts.is_empty() {
        status.to_owned()
    } else {
        tooltip_parts.join(". ")
    };
    let mut status_label = status_display.to_owned();
    if sparse && !matches!(status, "sparse" | "sparse_new") {
        status_label.push_str(" (sparse)");
    }
    let status_pill = format!(
        "<span class=\"pill {status_pill_class}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</span>",
        html_escape(&tooltip),
        html_escape(&tooltip),
        html_escape(&status_label)
    );
    let display_name = detail["display_name"]
        .as_str()
        .filter(|name| !name.trim().is_empty())
        .unwrap_or(model_id);
    let first_seen = model_info_age(info["last_seen_at"].as_str(), false);
    let last_refreshed = model_info_age(info["last_refreshed_at"].as_str(), false);
    let next_refresh = model_info_age(info["next_refresh_at"].as_str(), true);
    let sparse_sub = if sparse { Some("Sparse") } else { None };
    let cards = format!(
        "<section class=\"cards\">{}{}{}{}</section>",
        model_info_metric_card(
            "Status",
            status_label.as_str(),
            sparse_sub,
            "Current freshness status of model-info metadata"
        ),
        model_info_metric_card(
            "Last seen",
            first_seen.as_str(),
            Some("Observation time"),
            "When this model was last observed by any source"
        ),
        model_info_metric_card(
            "Last refreshed",
            last_refreshed.as_str(),
            Some("Refresh time"),
            "When model-info metadata was last refreshed"
        ),
        model_info_metric_card(
            "Next refresh",
            next_refresh.as_str(),
            Some("Scheduled"),
            "When the next scheduled refresh will occur"
        ),
    );
    let summary_html = if summary.trim().is_empty() {
        "<em>No summary available.</em>".to_owned()
    } else {
        html_escape(summary)
    };
    let summary_panel = format!(
        "<section class=\"panel\"><h3>Summary</h3><p>{summary_html}</p>{}</section>",
        if benchmark_brief.is_empty() {
            String::new()
        } else {
            format!(
                "<p class=\"sub\"><strong>Benchmark snapshot:</strong> {}</p>",
                html_escape(&benchmark_brief)
            )
        }
    );
    let limits = detail.get("limits").unwrap_or(&Value::Null);
    let mut limit_parts = Vec::new();
    for (label, nested, legacy) in [
        ("Effective ctx", "effective_context", "context_tokens"),
        (
            "External ctx",
            "external_context",
            "context_window_external",
        ),
        ("Effective out", "effective_output", "max_output_tokens"),
        (
            "External out",
            "external_output",
            "max_output_tokens_external",
        ),
    ] {
        let value = limits.get(nested).or_else(|| detail.get(legacy));
        if let Some(count) = dashboard_positive_integer(value) {
            limit_parts.push(format!("{label}: {}", format_tokens(count)));
        }
    }
    let limits_html = if limit_parts.is_empty() {
        "—".to_owned()
    } else {
        limit_parts.join(" · ")
    };
    let modalities_html = dashboard_string_list(detail.get("modalities"));
    let tools_html = match detail.get("supports_tools").and_then(Value::as_bool) {
        Some(true) => "Yes",
        Some(false) => "No",
        None => "—",
    };
    let callability = format!(
        "<section class=\"panel\"><h3>Provider / Callability</h3><div class=\"table-scroll\"><table class=\"data\"><tbody><tr><th>Providers</th><td>{}</td></tr><tr><th>Sources</th><td>{}</td></tr><tr><th>Limits</th><td>{limits_html}</td></tr><tr><th>Modalities</th><td>{}</td></tr><tr><th>Tool support</th><td>{tools_html}</td></tr></tbody></table></div></section>",
        dashboard_code_list(detail.get("providers")),
        dashboard_code_list(provenance.get("sources")),
        modalities_html
    );
    let external_ids = detail
        .get("external_ids")
        .and_then(Value::as_object)
        .map(|items| {
            items
                .iter()
                .map(|(source, id)| {
                    format!(
                        "{}: <code>{}</code>",
                        html_escape(source),
                        html_escape(dashboard_json_text(id))
                    )
                })
                .collect::<Vec<_>>()
                .join("<br>")
        })
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "—".to_owned());
    let metadata = format!(
        "<section class=\"panel\"><h3>Metadata</h3><div class=\"table-scroll\"><table class=\"data\"><tbody><tr><th>Family</th><td>{}</td></tr><tr><th>License</th><td>{}</td></tr><tr><th>Release date</th><td>{}</td></tr><tr><th>External IDs</th><td>{external_ids}</td></tr></tbody></table></div></section>",
        dashboard_optional_text(detail.get("family")),
        dashboard_optional_text(detail.get("license")),
        dashboard_optional_text(detail.get("release_date"))
    );
    let benchmark_panel = dashboard_render_benchmarks(benchmarks);
    let huggingface = dashboard_render_huggingface(detail.get("huggingface_metadata"));
    let conflict_panel = dashboard_render_conflicts(conflicts);
    let reconciled = provenance
        .get("reconciled_at")
        .and_then(Value::as_str)
        .map(dashboard_iso_timestamp)
        .map(html_escape)
        .unwrap_or_else(|| "—".to_owned());
    let provenance_panel = format!(
        "<section class=\"panel\"><h3>Provenance</h3><div class=\"table-scroll\"><table class=\"data\"><tbody><tr><th>Sources</th><td>{}</td></tr><tr><th>Reconciled at</th><td>{reconciled}</td></tr></tbody></table></div></section>",
        dashboard_code_list(provenance.get("sources"))
    );
    format!(
        "<h2>{} <small>({})</small></h2><p>{status_pill}</p>{cards}{summary_panel}{callability}{metadata}{benchmark_panel}{huggingface}{conflict_panel}{provenance_panel}",
        html_escape(display_name),
        html_escape(model_id)
    )
}

fn model_info_metric_card(title: &str, metric: &str, sub: Option<&str>, tooltip: &str) -> String {
    let sub = sub
        .map(|value| format!("<p class=\"sub\">{}</p>", html_escape(value)))
        .unwrap_or_default();
    let metric = if metric.is_empty() {
        String::new()
    } else {
        format!("<p class=\"metric\">{}</p>", html_escape(metric))
    };
    format!(
        "<div class=\"card\" data-tooltip=\"{}\" data-tooltip-pos=\"bottom\" aria-label=\"{}\"><h3>{}</h3>{metric}{sub}</div>",
        html_escape(tooltip),
        html_escape(tooltip),
        html_escape(title)
    )
}

fn dashboard_json_text(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}

fn dashboard_optional_text(value: Option<&Value>) -> String {
    value
        .filter(|value| !value.is_null())
        .map(dashboard_json_text)
        .map(html_escape)
        .unwrap_or_else(|| "—".to_owned())
}

fn dashboard_iso_timestamp(value: &str) -> String {
    if value.len() == 19 && value.as_bytes().get(10) == Some(&b' ') {
        format!("{}T{}+00:00", &value[..10], &value[11..])
    } else {
        value.to_owned()
    }
}

fn dashboard_positive_integer(value: Option<&Value>) -> Option<i64> {
    let value = value?;
    if value.is_boolean() {
        return None;
    }
    let integer = value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|number| i64::try_from(number).ok()))
        .or_else(|| {
            value
                .as_f64()
                .filter(|number| number.is_finite() && number.fract() == 0.0)
                .map(|number| number as i64)
        })?;
    (integer > 0).then_some(integer)
}

fn dashboard_string_list(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| !item.is_null())
                .map(|item| html_escape(dashboard_json_text(item)))
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "—".to_owned())
}

fn dashboard_model_sources(value: Option<&Value>) -> Vec<String> {
    if let Some(items) = value.and_then(Value::as_array) {
        return items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
    }
    value
        .and_then(Value::as_object)
        .map(|items| {
            items
                .iter()
                .map(|(key, item)| {
                    item.get("source")
                        .and_then(Value::as_str)
                        .unwrap_or(key)
                        .to_owned()
                })
                .collect()
        })
        .unwrap_or_default()
}

fn dashboard_code_list(value: Option<&Value>) -> String {
    let sources = dashboard_model_sources(value);
    if sources.is_empty() {
        return "—".to_owned();
    }
    sources
        .iter()
        .map(|item| format!("<code>{}</code>", html_escape(item)))
        .collect::<Vec<_>>()
        .join(", ")
}

fn dashboard_benchmark_source(value: &str) -> String {
    match value {
        "artificial_analysis" => "AA".to_owned(),
        "openrouter" => "OpenRouter".to_owned(),
        "huggingface" => "Hugging Face".to_owned(),
        "" => "Unknown".to_owned(),
        other => other.to_owned(),
    }
}

fn dashboard_benchmark_short_name(name: &str) -> &str {
    name.strip_prefix("Artificial Analysis ")
        .or_else(|| name.strip_prefix("Design Arena: "))
        .unwrap_or(name)
}

fn dashboard_number(value: &Value) -> Option<String> {
    if value.is_boolean() {
        return None;
    }
    let number = value.as_f64()?;
    if !number.is_finite() {
        return None;
    }
    let text = format!("{number:.1}");
    Some(text.strip_suffix(".0").unwrap_or(&text).to_owned())
}

fn dashboard_benchmark_result(row: &Value) -> String {
    let mut parts = Vec::new();
    if let Some(score) = dashboard_number(&row["score"]) {
        parts.push(score);
    }
    if let Some(rank) = dashboard_positive_integer(Some(&row["rank"])) {
        parts.push(format!("#{rank}"));
    }
    if let Some(mut percentile) = dashboard_number(&row["percentile"]) {
        if let Some(value) = row["percentile"]
            .as_f64()
            .filter(|value| (0.0..=1.0).contains(value))
        {
            percentile = dashboard_number(&Value::from(value * 100.0)).unwrap_or(percentile);
        }
        parts.push(format!("{percentile}%ile"));
    }
    if let Some(version) = row["version"]
        .as_str()
        .filter(|value| !value.trim().is_empty())
    {
        parts.push(format!("v{version}"));
    }
    if parts.is_empty() {
        "—".to_owned()
    } else {
        parts.join(" · ")
    }
}

fn dashboard_render_benchmarks(benchmarks: Option<&Vec<Value>>) -> String {
    let Some(benchmarks) = benchmarks else {
        return String::new();
    };
    let rows = benchmarks.iter().filter_map(|row| {
        let name = row["name"].as_str().or_else(|| row["benchmark"].as_str())?.trim();
        if name.is_empty() { return None; }
        let result = dashboard_benchmark_result(row);
        let source = row["source"].as_str().unwrap_or("");
        let source_label = dashboard_benchmark_source(source);
        let observed = row["observed_at"].as_str().unwrap_or("");
        let observed_short = observed.chars().take(19).collect::<String>();
        let percentile = dashboard_number(&row["percentile"]).map(|mut value| {
            if let Some(raw) = row["percentile"]
                .as_f64()
                .filter(|number| (0.0..=1.0).contains(number))
            {
                value = dashboard_number(&Value::from(raw * 100.0)).unwrap_or(value);
            }
            format!("Percentile: {value}%")
        });
        let detail = [
            Some(name.to_owned()),
            dashboard_number(&row["score"]).map(|value| format!("Score: {value}")),
            dashboard_positive_integer(Some(&row["rank"]))
                .map(|value| format!("Rank: #{value}")),
            percentile,
            row["version"]
                .as_str()
                .filter(|value| !value.trim().is_empty())
                .map(|value| format!("Version: {value}")),
            row["notes"]
                .as_str()
                .or_else(|| row["caveat"].as_str())
                .filter(|value| !value.trim().is_empty())
                .map(str::to_owned),
            (!source.is_empty()).then(|| format!("Source: {source}")),
            (!observed.is_empty()).then(|| format!("Observed: {observed}")),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join("\n");
        let source_tooltip = if source.is_empty() { "Unknown source" } else { source };
        let observed_tooltip = if observed.is_empty() { "No observation timestamp" } else { observed };
        Some(format!("<tr><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td></tr>", html_escape(name), html_escape(name), html_escape(dashboard_benchmark_short_name(name)), html_escape(&detail), html_escape(&detail), html_escape(result), html_escape(source_tooltip), html_escape(source_tooltip), html_escape(source_label), html_escape(observed_tooltip), html_escape(observed_tooltip), html_escape(observed_short)))
    }).collect::<String>();
    if rows.is_empty() {
        String::new()
    } else {
        format!(
            "<section class=\"panel\"><h3>Benchmarks</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Benchmark</th><th>Result</th><th>Source</th><th>Observed</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
        )
    }
}

fn dashboard_render_huggingface(value: Option<&Value>) -> String {
    let Some(metadata) = value.and_then(Value::as_object) else {
        return String::new();
    };
    let mut rows = Vec::new();
    for key in [
        "downloads",
        "likes",
        "pipeline_tag",
        "library_name",
        "license",
    ] {
        if let Some(value) = metadata.get(key).filter(|value| !value.is_null()) {
            rows.push(format!(
                "<tr><th>{}</th><td>{}</td></tr>",
                html_escape(key),
                html_escape(dashboard_json_text(value))
            ));
        }
    }
    if let Some(tags) = metadata.get("tags").and_then(Value::as_array) {
        let values = tags
            .iter()
            .take(10)
            .filter(|value| !value.is_null())
            .map(|value| html_escape(dashboard_json_text(value)))
            .collect::<Vec<_>>();
        if !values.is_empty() {
            rows.push(format!(
                "<tr><th>Tags</th><td>{}</td></tr>",
                values.join(", ")
            ));
        }
    }
    if rows.is_empty() {
        String::new()
    } else {
        format!(
            "<section class=\"panel\"><h3>Hugging Face</h3><div class=\"table-scroll\"><table class=\"data\"><tbody>{}</tbody></table></div></section>",
            rows.join("")
        )
    }
}

fn dashboard_render_conflicts(value: &Value) -> String {
    let Some(conflicts) = value.as_object().filter(|value| !value.is_empty()) else {
        return String::new();
    };
    let rows = conflicts
        .iter()
        .map(|(field, value)| {
            if let Some(mapping) = value.as_object().filter(|mapping| !mapping.is_empty()) {
                let sources = mapping
                    .get("sources")
                    .and_then(Value::as_object)
                    .map(|items| {
                        items
                            .iter()
                            .map(|(key, value)| {
                                format!(
                                    "{}: {}",
                                    html_escape(key),
                                    html_escape(dashboard_json_text(value))
                                )
                            })
                            .collect::<Vec<_>>()
                            .join(", ")
                    })
                    .unwrap_or_default();
                format!(
                    "<tr><td>{}</td><td>{sources}</td><td>{}</td><td>{}</td></tr>",
                    html_escape(field),
                    mapping
                        .get("selected")
                        .map(dashboard_json_text)
                        .map(html_escape)
                        .unwrap_or_default(),
                    mapping
                        .get("reason")
                        .map(dashboard_json_text)
                        .map(html_escape)
                        .unwrap_or_default()
                )
            } else {
                format!(
                    "<tr><td>{}</td><td colspan='3'>{}</td></tr>",
                    html_escape(field),
                    html_escape(dashboard_json_text(value))
                )
            }
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Conflicts</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Field</th><th>Source values</th><th>Selected</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

fn render_model_observations(observations: &[Value]) -> String {
    if observations.is_empty() {
        return String::new();
    }
    let rows = observations.iter().map(|row| format!(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td><code>{}</code></td><td>{}</td></tr>",
        html_escape(row["source"].as_str().unwrap_or("")),
        html_escape(row["source_model_id"].as_str().unwrap_or("—")),
        row["provider_id"].as_str().map(|value| format!("<code>{}</code>", html_escape(value))).unwrap_or_else(|| "—".to_owned()),
        row["observed_at"]
            .as_str()
            .map(dashboard_iso_timestamp)
            .map(|value| html_escape(&value))
            .unwrap_or_else(|| "—".to_owned()),
        row["confidence"]
            .as_f64()
            .map(|_| row["confidence"].to_string())
            .unwrap_or_else(|| "—".to_owned()),
    )).collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Observations</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th>Source</th><th>Source model id</th><th>Provider</th><th>Observed</th><th>Confidence</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

fn model_info_age(timestamp: Option<&str>, reverse: bool) -> String {
    let Some(timestamp) = timestamp.and_then(parse_dashboard_timestamp) else {
        return "—".to_owned();
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64);
    let delta = if reverse {
        timestamp - now
    } else {
        now - timestamp
    };
    if delta < 0 {
        return "—".to_owned();
    }
    if delta == 0 {
        return "<1s".to_owned();
    }
    if delta < 60 {
        return format!("{delta}s");
    }
    let minutes = delta / 60;
    let seconds = delta % 60;
    if minutes < 60 {
        return format!("{minutes}m{seconds}s");
    }
    let hours = minutes / 60;
    let remaining_minutes = minutes % 60;
    if hours < 24 {
        return format!("{hours}h{remaining_minutes}m");
    }
    format!("{}d{}h", hours / 24, hours % 24)
}

fn parse_dashboard_timestamp(value: &str) -> Option<i64> {
    let value = value.trim();
    let date_time = value.get(..19)?;
    let year = date_time.get(0..4)?.parse::<i64>().ok()?;
    let month = date_time.get(5..7)?.parse::<i64>().ok()?;
    let day = date_time.get(8..10)?.parse::<i64>().ok()?;
    let hour = date_time.get(11..13)?.parse::<i64>().ok()?;
    let minute = date_time.get(14..16)?.parse::<i64>().ok()?;
    let second = date_time.get(17..19)?.parse::<i64>().ok()?;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    let y = year - i64::from(month <= 2);
    let era = if y >= 0 { y / 400 } else { (y - 399) / 400 };
    let year_of_era = y - era * 400;
    let shifted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let mut result =
        (era * 146097 + day_of_era - 719468) * 86400 + hour * 3600 + minute * 60 + second;
    let zone = value.get(19..).unwrap_or("");
    if let Some(offset) = zone.find(['+', '-']) {
        let sign = if zone.as_bytes()[offset] == b'+' {
            1
        } else {
            -1
        };
        let digits = zone.get(offset + 1..).unwrap_or("").replace(':', "");
        if digits.len() >= 4 {
            let hours = digits.get(..2)?.parse::<i64>().ok()?;
            let minutes = digits.get(2..4)?.parse::<i64>().ok()?;
            result -= sign * (hours * 3600 + minutes * 60);
        }
    }
    Some(result)
}

pub(super) fn render_latency_page(data: &db::DashboardData, period: &str, theme: &str) -> String {
    let header = format!(
        "<h2>Latency</h2>{}",
        dashboard_period_selector(period, theme)
    );
    if data.models.iter().all(|row| row.ttft_requests == 0) {
        return format!(
            "{header}<p class=\"empty\">No TTFT data for this period.</p><section class=\"panel\"><h3>Per-model breakdown</h3><p class=\"empty\">No model data for this period.</p></section>"
        );
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
            let percentiles = data
                .latency_percentiles
                .iter()
                .find(|row| row.provider_id == *provider && row.model_id.is_empty());
            let p50 = percentiles.map_or(0.0, |row| row.p50_ttft_ms);
            let p99 = percentiles.map_or(0.0, |row| row.p99_ttft_ms);
            format!(
            "<div class=\"card\" data-tooltip=\"Provider TTFT summary. The metric is average time to first token; the subtext shows P50, P99, and request count.\" data-tooltip-pos=\"bottom\" aria-label=\"Provider TTFT summary. The metric is average time to first token; the subtext shows P50, P99, and request count.\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">P50 {} · P99 {} · {} reqs</p></div>",
                html_escape(provider),
                format_latency(total / *requests as f64),
                format_latency(p50),
                format_latency(p99),
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
            let tooltip = format!("Open model info for {}", row.model_id);
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&tooltip),
                html_escape(&tooltip),
                html_escape(&row.model_id),
            );
            let percentiles = data
                .latency_percentiles
                .iter()
                .find(|item| item.provider_id == row.provider_id && item.model_id == row.model_id);
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">—</td></tr>",
                html_escape(&row.provider_id),
                row.ttft_requests,
                format_latency(row.avg_ttft_ms),
                format_latency(percentiles.map_or(0.0, |item| item.p50_ttft_ms)),
                format_latency(percentiles.map_or(0.0, |item| item.p99_ttft_ms)),
            )
        })
        .collect::<String>();
    format!(
        "{header}<section class=\"cards\">{cards}</section><section class=\"panel\"><h3>Per-model breakdown</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Model</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Avg TTFT</th><th data-priority=\"2\">P50 TTFT</th><th data-priority=\"2\">P99 TTFT</th><th data-priority=\"3\">Phases ms (c/r/o)</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(super) fn render_events_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    selected_type: &str,
) -> String {
    let visible_events = data
        .events
        .iter()
        .filter(|row| selected_type.is_empty() || row.event_type == selected_type)
        .collect::<Vec<_>>();
    let rows = if visible_events.is_empty() {
        "<p class=\"empty\">No events recorded.</p>".to_owned()
    } else {
        let rows = visible_events
            .iter()
            .map(|row| {
                format!(
                    "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\"><span class=\"event-tag {}\">{}</span></td><td data-priority=\"2\">{}</td></tr>",
                    html_escape(&row.created_at),
                    html_escape(&row.account_name),
                    sanitize_class_name(&row.event_type),
                    html_escape(&row.event_type),
                    html_escape(row.details.chars().take(200).collect::<String>()),
                )
            })
            .collect::<String>();
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">When</th><th data-priority=\"1\">Account</th><th data-priority=\"1\">Type</th><th data-priority=\"2\">Details</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let types = data
        .events
        .iter()
        .map(|row| row.event_type.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let type_options = types
        .into_iter()
        .map(|event_type| {
            format!(
                "<option value=\"{}\"{}>{}</option>",
                html_escape(event_type),
                if event_type == selected_type {
                    " selected"
                } else {
                    ""
                },
                html_escape(event_type)
            )
        })
        .collect::<String>();
    format!(
        "<h2>Events</h2><form method=\"get\" class=\"filter-form\"><label>Type: <select name=\"type\" data-auto-submit=\"1\"><option value=\"\" selected>(all types)</option>{type_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><noscript><button type=\"submit\">Apply</button></noscript></form>{}<section class=\"panel\">{rows}</section>",
        html_escape(period),
        html_escape(theme),
        dashboard_period_selector(period, theme)
    )
}

pub(super) fn render_timeseries_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    grouped: &Value,
) -> String {
    let grouped_json = escape_script_end_tags(&grouped_timeseries_json(grouped));
    let has_data = !grouped["points"].as_array().is_none_or(Vec::is_empty)
        && !grouped["buckets"].as_array().is_none_or(Vec::is_empty);
    let chart_display = if has_data {
        ""
    } else {
        " style=\"display: none;\""
    };
    let empty_display = if has_data {
        " style=\"display: none;\""
    } else {
        ""
    };
    let account_options = data
        .accounts
        .iter()
        .map(|row| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&row.name),
                html_escape(&row.name)
            )
        })
        .collect::<String>();
    let model_options = data
        .timeseries
        .iter()
        .map(|row| format!("{}/{}", row.model_id, row.provider_id))
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .map(|model| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&model),
                html_escape(&model)
            )
        })
        .collect::<String>();
    let mut by_bucket = std::collections::BTreeMap::<String, [i64; 7]>::new();
    for row in &data.timeseries {
        let totals = by_bucket.entry(row.bucket.clone()).or_default();
        totals[0] += row.requests;
        totals[1] += row.cost_microdollars;
        totals[2] += row.errors;
        totals[3] += row.total_tokens;
        totals[4] += row.input_tokens;
        totals[5] += row.output_tokens;
        totals[6] += row.bytes_received;
    }
    let aggregate_rows = by_bucket.iter().map(|(bucket, totals)| format!(
        "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
        html_escape(bucket), totals[0], format_microdollars(totals[1]), totals[2], format_tokens(totals[3]),
        format_tokens(totals[4]), format_tokens(totals[5]), format_bytes(totals[6]),
        format_bytes(data.timeseries.iter().filter(|row| row.bucket == *bucket).map(|row| row.bytes_emitted).sum::<i64>())
    )).collect::<String>();
    let aggregate_table = if aggregate_rows.is_empty() {
        "<p class=\"empty\">No requests in this window.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Bucket</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"3\">Input tokens</th><th data-priority=\"3\">Output tokens</th><th data-priority=\"3\">BW received</th><th data-priority=\"3\">BW emitted</th></tr></thead><tbody>{aggregate_rows}</tbody></table></div>"
        )
    };
    let usage_rows = data.timeseries.iter().map(|row| {
        let model_link = format!(
            "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"Open model info for {}\" aria-label=\"Open model info for {}\">{}</a>",
            query_component(&row.model_id), query_component(theme), html_escape(&row.model_id),
            html_escape(&row.provider_id), html_escape(&row.model_id), html_escape(&row.model_id),
            html_escape(&row.model_id), html_escape(&row.model_id));
        format!(
            "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
            html_escape(&row.bucket), html_escape(&row.series), html_escape(&row.provider_id),
            row.requests, format_microdollars(row.cost_microdollars), row.errors,
            format_tokens(row.total_tokens), format_latency(row.avg_latency_ms),
            format_tokens(row.input_tokens), format_tokens(row.output_tokens),
            format_tokens(row.cache_read_tokens), format_tokens(row.cache_write_tokens),
            format_tokens(row.reasoning_tokens), format_bytes(row.bytes_received),
            format_bytes(row.bytes_emitted), format_latency(row.avg_ttft_ms))
    }).collect::<String>();
    let usage_table = if usage_rows.is_empty() {
        "<p class=\"empty\">No requests in this window.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Bucket</th><th data-priority=\"1\">Series</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Model</th><th data-priority=\"1\">Requests</th><th data-priority=\"2\">Cost</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"3\">Input tokens</th><th data-priority=\"3\">Output tokens</th><th data-priority=\"3\">Cache read</th><th data-priority=\"3\">Cache write</th><th data-priority=\"3\">Reasoning</th><th data-priority=\"3\">BW received</th><th data-priority=\"3\">BW emitted</th><th data-priority=\"3\">Avg TTFT</th></tr></thead><tbody>{usage_rows}</tbody></table></div>"
        )
    };
    format!(
        "<h2>Timeseries (hour buckets, group by provider_model)</h2>{}<form method=\"get\" class=\"filter-form timeseries-controls\" data-timeseries-controls aria-label=\"Timeseries filters\"><label>Bucket: <select name=\"bucket\"><option value=\"auto\" selected>Auto (period-aware)</option><option value=\"hour\">Hour</option><option value=\"day\">Day</option></select></label><label>Group by: <select name=\"group_by\"><option value=\"provider_model\" selected>Provider / model</option><option value=\"provider\">Provider</option><option value=\"model\">Model</option><option value=\"account\">Account</option></select></label><label>Metric: <select name=\"metric\"><option value=\"tokens\" selected>Tokens</option><option value=\"requests\">Requests</option><option value=\"cost\">Cost</option><option value=\"errors\">Errors</option><option value=\"bytes\">Bandwidth</option></select></label><label>Limit: <select name=\"limit\"><option value=\"6\">Top 6</option><option value=\"8\">Top 8</option><option value=\"12\" selected>Top 12</option><option value=\"16\">Top 16</option><option value=\"20\">Top 20</option><option value=\"25\">Top 25</option></select></label><label>Account: <select name=\"account\"><option value=\"\" selected>(any account)</option>{account_options}</select></label><label>Model: <select name=\"model\"><option value=\"\" selected>(any model)</option>{model_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form><section class=\"panel timeseries-chart-panel\"><h3>Usage breakdown</h3><div class=\"chart-container\"{chart_display}><canvas class=\"grouped-timeseries-chart\" data-chart-id=\"grouped-timeseries-chart\" data-period=\"{}\" data-bucket=\"hour\" data-group-by=\"provider_model\" data-metric=\"tokens\" data-limit=\"12\" data-account=\"\" data-model=\"\"></canvas></div><p class=\"empty grouped-timeseries-empty\"{empty_display}>No requests in this window.</p><script type=\"application/json\" class=\"grouped-timeseries-data\" data-chart-id=\"grouped-timeseries-chart\">{grouped_json}</script></section><section class=\"panel\"><h3>Usage breakdown</h3>{usage_table}</section><section class=\"panel\"><h3>Aggregate per bucket</h3>{aggregate_table}</section>",
        dashboard_period_selector(period, theme),
        html_escape(period),
        html_escape(theme),
        html_escape(period),
    )
}

pub(super) fn render_bandwidth_page(data: &db::DashboardData, period: &str, theme: &str) -> String {
    let account_options = data
        .accounts
        .iter()
        .map(|account| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&account.name),
                html_escape(&account.name)
            )
        })
        .collect::<String>();
    format!(
        "<h2>Bandwidth</h2><form method=\"get\" class=\"filter-form\"><label>Account: <select name=\"account\" data-auto-submit=\"1\"><option value=\"\" selected>(all accounts)</option>{account_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"bucket\" value=\"hour\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><noscript><button type=\"submit\">Apply</button></noscript></form>{}<section class=\"cards\"><div class=\"card\" data-tooltip=\"Total bytes received from clients by EggPool in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Total bytes received from clients by EggPool in the selected period.\"><h3>Total received</h3><p class=\"metric\">{}</p><p class=\"sub\">client → proxy</p></div><div class=\"card\" data-tooltip=\"Total bytes emitted by EggPool toward clients in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Total bytes emitted by EggPool toward clients in the selected period.\"><h3>Total emitted</h3><p class=\"metric\">{}</p><p class=\"sub\">upstream → proxy</p></div></section><section class=\"panel\"><h3>Bandwidth activity (last 180 days)</h3>{}</section>",
        html_escape(period),
        html_escape(theme),
        dashboard_period_selector(period, theme),
        format_bytes(data.cache.total_bytes_received),
        format_bytes(data.cache.total_bytes_emitted),
        render_bandwidth_heatmap(&data.token_activity, theme),
    )
}

pub(super) fn render_pings_page(data: &db::DashboardData, period: &str, theme: &str) -> String {
    if data.pings.is_empty() {
        return format!(
            "<h2>Provider Pings</h2>{}<p class=\"empty\">No ping data yet. Data appears after the first catalog refresh.</p><section class=\"panel\"><h3>Recent pings</h3><p class=\"empty\">No pings recorded yet.</p></section>",
            dashboard_period_selector(period, theme)
        );
    }
    let mut provider_totals: Vec<(&str, f64, i64, i64)> = Vec::new();
    for row in &data.pings {
        let latency = row.latency_ms.map_or(0.0, |value| value as f64);
        let success = i64::from(
            row.status_code
                .is_some_and(|code| (200..300).contains(&code)),
        );
        if let Some((_, total, count, successes)) = provider_totals
            .iter_mut()
            .find(|(provider, _, _, _)| *provider == row.provider_id)
        {
            if row.latency_ms.is_some() {
                *total += latency;
                *count += 1;
            }
            *successes += success;
        } else {
            provider_totals.push((
                &row.provider_id,
                latency,
                i64::from(row.latency_ms.is_some()),
                success,
            ));
        }
    }
    provider_totals.sort_by(|left, right| left.0.cmp(right.0));
    let cards = provider_totals
        .iter()
        .map(|(provider, total, count, successes)| {
            let ping_count = data.pings.iter().filter(|row| row.provider_id == *provider).count();
            let success_rate = if ping_count == 0 {
                0.0
            } else {
                *successes as f64 * 100.0 / ping_count as f64
            };
            let status = if success_rate >= 90.0 {
                "healthy"
            } else {
                "degraded"
            };
            format!(
                "<div class=\"card\" data-tooltip=\"Provider ping latency summary. The metric is average ping latency; the subtext shows health status, success rate, and last seen model count.\" data-tooltip-pos=\"bottom\" aria-label=\"Provider ping latency summary. The metric is average ping latency; the subtext shows health status, success rate, and last seen model count.\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\"><span class=\"{}\">{}</span> &middot; {success_rate:.1}% success &middot; {} models</p></div>",
                html_escape(provider),
                format_latency(if *count == 0 { 0.0 } else { total / *count as f64 }),
                status,
                status,
                data.pings
                    .iter()
                    .find(|row| row.provider_id == *provider)
                    .map_or(0, |row| row.model_count),
            )
        })
        .collect::<String>();
    let rows = data
        .pings
        .iter()
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.provider_id),
                html_escape(&row.probed_at),
                format_latency(row.latency_ms.unwrap_or_default() as f64),
                row.status_code
                    .map_or_else(|| "—".to_owned(), |v| v.to_string()),
                html_escape(&row.account_name),
                row.model_count,
                html_escape(row.error.as_deref().unwrap_or_default()),
            )
        })
        .collect::<String>();
    format!(
        "<h2>Provider Pings</h2>{}<section class=\"cards\">{cards}</section><section class=\"panel\"><h3>Recent pings</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Time</th><th data-priority=\"1\">Latency</th><th data-priority=\"1\">Status</th><th data-priority=\"2\">Account</th><th data-priority=\"2\">Models</th><th data-priority=\"3\">Error</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
        dashboard_period_selector(period, theme)
    )
}

pub(super) fn render_reliability_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
) -> String {
    let attempts: i64 = data.retries.iter().map(|row| row.attempts).sum();
    let failures: i64 = data.retries.iter().map(|row| row.failures).sum();
    let successes: i64 = data.retries.iter().map(|row| row.successes).sum();
    let retry_rows = data
        .retries
        .iter()
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
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
    let first_attempt_rate = if attempts > 0 {
        successes as f64 * 100.0 / attempts as f64
    } else {
        0.0
    };
    let retry_rate = if attempts > 0 {
        retry_attempts as f64 * 100.0 / attempts as f64
    } else {
        0.0
    };
    let average_attempt_latency = if attempts > 0 {
        data.retries
            .iter()
            .map(|row| row.avg_latency_ms * row.attempts as f64)
            .sum::<f64>()
            / attempts as f64
    } else {
        0.0
    };
    let operational_summary = if data.operational_summary.is_empty() {
        "<p class=\"empty\">No operational events in this window.</p>".to_owned()
    } else {
        let rows = data.operational_summary.iter().map(|row| format!(
            "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
            html_escape(&row.event_type), row.event_count, html_escape(&row.last_seen),
            row.interrupted_requests, row.released_reservations)).collect::<String>();
        format!(
            "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Event type</th><th data-priority=\"1\">Count</th><th data-priority=\"2\">Last seen</th><th data-priority=\"2\">Interrupted</th><th data-priority=\"3\">Released</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let recent_operational_events = if data.recent_operational_events.is_empty() {
        "<p class=\"empty\">No recent operational events.</p>".to_owned()
    } else {
        let rows = data.recent_operational_events.iter().map(|row| format!(
            "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td></tr>",
            html_escape(&row.occurred_at), html_escape(&row.event_type), html_escape(row.details.chars().take(200).collect::<String>()))).collect::<String>();
        format!(
            "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">When</th><th data-priority=\"1\">Type</th><th data-priority=\"2\">Details</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let attempts_chart = format!(
        "<div class=\"chart-wrap\" style=\"height: 280px;\"><canvas id=\"reliability-attempts-by-provider\"></canvas></div><script type=\"application/json\" class=\"static-chart-data\" data-chart-id=\"reliability-attempts-by-provider\">{{\"type\":\"bar\",\"labels\":[\"Success\",\"Retry\",\"Failed\"],\"datasets\":[{{\"label\":\"Attempts\",\"data\":[{successes},{retry_attempts},{failures}],\"backgroundColor\":[\"rgba(75, 192, 120, 0.7)\",\"rgba(255, 159, 64, 0.7)\",\"rgba(255, 99, 132, 0.7)\"]}}],\"options\":{{\"responsive\":true,\"maintainAspectRatio\":false,\"plugins\":{{\"legend\":{{\"display\":false}}}},\"scales\":{{\"y\":{{\"beginAtZero\":true,\"title\":{{\"display\":true,\"text\":\"Count\"}}}}}}}}}}</script>"
    );
    let distribution = if retry_rows.is_empty() {
        "<p class=\"empty\">No attempt data for this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Category</th><th data-priority=\"1\">Attempts</th><th data-priority=\"2\">Retry outcomes</th><th data-priority=\"2\">Successes</th><th data-priority=\"2\">Failures</th><th data-priority=\"3\">Avg attempt latency</th></tr></thead><tbody>{retry_rows}</tbody></table></div>"
        )
    };
    format!(
        "<h2>Reliability</h2>{}<section class=\"cards\"><div class=\"card\" data-tooltip=\"Total upstream attempts in the selected period, including retries.\" data-tooltip-pos=\"bottom\" aria-label=\"Total upstream attempts in the selected period, including retries.\"><h3>Total attempts</h3><p class=\"metric\">{attempts}</p><p class=\"sub\">{period}</p></div><div class=\"card\" data-tooltip=\"Attempts that completed successfully. The subtext highlights the first-attempt success rate.\" data-tooltip-pos=\"bottom\" aria-label=\"Attempts that completed successfully. The subtext highlights the first-attempt success rate.\"><h3>Success attempts</h3><p class=\"metric\">{successes}</p><p class=\"sub\">first-attempt success rate {first_attempt_rate:.1}%</p></div><div class=\"card\" data-tooltip=\"Attempts that were retries rather than initial tries.\" data-tooltip-pos=\"bottom\" aria-label=\"Attempts that were retries rather than initial tries.\"><h3>Retry attempts</h3><p class=\"metric\">{retry_attempts}</p><p class=\"sub\">retry rate {retry_rate:.1}%</p></div><div class=\"card\" data-tooltip=\"Attempts that ended in failure. The subtext shows average attempt latency.\" data-tooltip-pos=\"bottom\" aria-label=\"Attempts that ended in failure. The subtext shows average attempt latency.\"><h3>Failed attempts</h3><p class=\"metric\">{failures}</p><p class=\"sub\">avg attempt latency {average_attempt_latency:.1} ms</p></div></section><section class=\"panel\"><h3>Attempts by provider (aggregated)</h3>{attempts_chart}</section><section class=\"cards system-health\"><div class=\"card\" data-tooltip=\"Requests still in progress. Subtext shows the oldest pending age.\" data-tooltip-pos=\"bottom\" aria-label=\"Requests still in progress. Subtext shows the oldest pending age.\"><h3>Pending requests</h3><p class=\"metric\">{}</p><p class=\"sub\">oldest — · stale 0</p></div><div class=\"card\" data-tooltip=\"Active quota or spend reservations for in-flight work.\" data-tooltip-pos=\"bottom\" aria-label=\"Active quota or spend reservations for in-flight work.\"><h3>Active reservations</h3><p class=\"metric\">{}</p><p class=\"sub\">reserved {} · oldest —</p></div><div class=\"card\" data-tooltip=\"Explanation of the pending-request snapshot and stale threshold used by the reliability view.\" data-tooltip-pos=\"bottom\" aria-label=\"Explanation of the pending-request snapshot and stale threshold used by the reliability view.\"><h3>Pending window</h3><p class=\"sub\">stale &amp;gt; 15 minutes are flagged for cleanup</p><p class=\"sub\">snapshot is instantaneous; reload to refresh</p></div></section><section class=\"panel\"><h3>Retry distribution</h3>{distribution}</section><section class=\"panel\"><h3>Operational events (summary)</h3>{operational_summary}</section><section class=\"panel\"><h3>Operational events (recent)</h3>{recent_operational_events}</section>",
        dashboard_period_selector(period, theme),
        data.pending_requests,
        data.active_reservations,
        format_microdollars(data.active_reserved_microdollars),
    )
}

pub(super) fn render_routing_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    trace: &RoutingTraceSnapshot,
) -> String {
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
            let tooltip = format!("Open model info for {}", row.model_id);
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&tooltip),
                html_escape(&tooltip),
                html_escape(&row.model_id),
            );
            format!(
            "<tr><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{:.2}</td><td data-priority=\"2\">{:.2}</td><td data-priority=\"2\">{:.2}</td><td data-priority=\"3\">{:.3}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.provider_id),
                row.decisions,
                row.avg_eligible,
                row.avg_scored,
                row.avg_excluded,
                row.avg_score,
                row.distinct_accounts,
            )
        })
        .collect::<String>();
    let distribution = if rows.is_empty() {
        "<p class=\"empty\">No routing decisions in this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Model</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Decisions</th><th data-priority=\"2\">Avg eligible</th><th data-priority=\"2\">Avg scored</th><th data-priority=\"2\">Avg excluded</th><th data-priority=\"3\">Avg score</th><th data-priority=\"3\">Distinct accounts</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let mut selected_by_account = std::collections::BTreeMap::<&str, i64>::new();
    for row in &data.routing_selection {
        *selected_by_account.entry(&row.account_name).or_default() += row.selection_count;
    }
    let mut account_selections = selected_by_account.into_iter().collect::<Vec<_>>();
    account_selections
        .sort_by(|left, right| left.1.cmp(&right.1).then_with(|| right.0.cmp(left.0)));
    let selection_total = account_selections
        .iter()
        .map(|(_, count)| count)
        .sum::<i64>();
    let selection_skew = if let (Some((least_name, least)), Some((most_name, most))) =
        (account_selections.first(), account_selections.last())
    {
        if selection_total > 0 {
            let ratio = if *least == 0 {
                0.0
            } else {
                *most as f64 / *least as f64
            };
            let warning = ratio > 3.0 && selection_total > 10;
            format!(
                "<div class=\"card{}\" data-tooltip=\"Selection skew\" data-tooltip-pos=\"bottom\" aria-label=\"Selection skew\"><h3>Selection skew</h3><p class=\"metric\">{ratio:.1}x</p><p class=\"sub\">max/min ratio ({} / {})</p><p class=\"sub\">{} selections across {} accounts</p></div>",
                if warning { " warning" } else { "" },
                html_escape(most_name),
                html_escape(least_name),
                selection_total,
                account_selections.len(),
            )
        } else {
            String::new()
        }
    } else {
        String::new()
    };
    let selection_rows = data
        .routing_selection
        .iter()
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{:.2}</td><td data-priority=\"3\">{:.3}</td><td data-priority=\"3\">{:.2}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.account_name),
                html_escape(&row.provider_id),
                row.selection_count,
                row.last_selected_score.map_or_else(|| "—".to_owned(), |v| format!("{v:.3}")),
                row.last_selected_tier.map_or_else(|| "—".to_owned(), |v| v.to_string()),
                row.avg_selected_tier,
                row.avg_selected_score,
                row.avg_eligible_count,
                html_escape(row.last_selected_at.chars().take(19).collect::<String>()),
            )
        })
        .collect::<String>();
    let selection_table = if selection_rows.is_empty() {
        "<p class=\"empty\">No selection data in this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Account</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Selections</th><th data-priority=\"2\">Last score</th><th data-priority=\"2\">Last tier</th><th data-priority=\"3\">Avg tier</th><th data-priority=\"3\">Avg score</th><th data-priority=\"3\">Avg eligible</th><th data-priority=\"3\">Last selected</th></tr></thead><tbody>{selection_rows}</tbody></table></div>"
        )
    };
    let trace_status = format!(
        "<div class=\"card\" data-tooltip=\"Trace status\" data-tooltip-pos=\"bottom\" aria-label=\"Trace status\"><h3>Trace status</h3><p class=\"metric\">{}</p><p class=\"sub\">{} accepted, {} written</p></div>",
        html_escape(&trace.status),
        trace.accepted,
        trace.written,
    );
    let queue_warning =
        trace.queue_capacity > 0 && trace.queue_depth as f64 / trace.queue_capacity as f64 > 0.8;
    let trace_panel = format!(
        "<section class=\"panel\"><h3>Routing trace observability</h3><p class=\"sub\">Diagnostic traces are written by a background writer off the request path. Queue overload or writer failure never delays dispatch.</p><section class=\"cards\"><div class=\"card\" data-tooltip=\"Trace mode\" data-tooltip-pos=\"bottom\" aria-label=\"Trace mode\"><h3>Trace mode</h3><p class=\"metric\">{}</p><p class=\"sub\">sample rate {:.1}%</p></div>{trace_status}<div class=\"card{}\" data-tooltip=\"Dropped traces\" data-tooltip-pos=\"bottom\" aria-label=\"Dropped traces\"><h3>Dropped traces</h3><p class=\"metric\">{}</p><p class=\"sub\">across all drop reasons</p></div><div class=\"card{}\" data-tooltip=\"Queue depth\" data-tooltip-pos=\"bottom\" aria-label=\"Queue depth\"><h3>Queue depth</h3><p class=\"metric\">{}/{}</p><p class=\"sub\">current / capacity</p></div></section></section>",
        html_escape(&trace.mode),
        trace.sample_rate * 100.0,
        if trace.dropped > 0 { " warning" } else { "" },
        trace.dropped,
        if queue_warning { " warning" } else { "" },
        trace.queue_depth,
        trace.queue_capacity,
    );
    format!(
        "<h2>Routing</h2>{}<section class=\"cards\"><div class=\"card\" data-tooltip=\"Total routing decisions recorded in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Total routing decisions recorded in the selected period.\"><h3>Routing decisions</h3><p class=\"metric\">{decisions}</p><p class=\"sub\">in selected period</p></div><div class=\"card\" data-tooltip=\"Average number of accounts that remained eligible for each routing decision.\" data-tooltip-pos=\"bottom\" aria-label=\"Average number of accounts that remained eligible for each routing decision.\"><h3>Avg eligible / decision</h3><p class=\"metric\">{avg_eligible:.2}</p><p class=\"sub\">candidate accounts per decision</p></div><div class=\"card\" data-tooltip=\"Count of different accounts chosen across routing decisions in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Count of different accounts chosen across routing decisions in the selected period.\"><h3>Distinct selected accounts</h3><p class=\"metric\">{distinct}</p><p class=\"sub\">across all (model, provider) groups</p></div>{selection_skew}</section>{trace_panel}<section class=\"panel\"><h3>Exclusion taxonomy</h3><p class=\"empty\">No exclusion data in this period.</p></section><section class=\"panel\"><h3>Routing distribution</h3>{distribution}</section><section class=\"panel\"><h3>Account selection breakdown</h3>{selection_table}</section><section class=\"panel\"><h3>Account exclusions</h3><p class=\"empty\">No exclusion data in this period.</p></section>",
        dashboard_period_selector(period, theme),
    )
}

pub(super) fn render_traces_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    limit: usize,
) -> String {
    if data.requests.is_empty() {
        return format!(
            "<h2>Traces</h2><p class=\"sub\">Auth-gated; does not include error_detail or client_ip; for incident debugging only.</p><form method=\"get\" class=\"filter-form\"><label class=\"trace-limit\">Limit: <span class=\"number-stepper\" data-stepper-for=\"limit\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"dec\" aria-label=\"Decrease limit\">−</button><input type=\"number\" name=\"limit\" id=\"limit\" value=\"{}\" min=\"10\" max=\"500\" data-stepper-input=\"1\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"inc\" aria-label=\"Increase limit\">+</button></span></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form>{}<section class=\"panel\"><p class=\"empty\">No recent requests.</p></section>",
            limit,
            html_escape(period),
            html_escape(theme),
            dashboard_period_selector(period, theme)
        );
    }
    let rows = data
        .requests
        .iter()
        .take(limit)
        .map(|row| {
            let status = row.status_code.map_or_else(
                || row.status.clone(),
                |code| format!("{} ({code})", row.status),
            );
            let latency = row
                .latency_ms
                .filter(|value| *value > 0.0)
                .map_or_else(|| "—".to_owned(), format_latency);
            let model_tooltip = format!("Open model info for {}", row.model_id);
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&model_tooltip),
                html_escape(&model_tooltip),
                html_escape(&row.model_id),
            );
            let request_id = row
                .proxy_request_id
                .as_deref()
                .filter(|id| !id.is_empty())
                .map_or_else(|| "—".to_owned(), |id| id.chars().take(8).collect());
            let has_thinking = row.reasoning_tokens > 0 || row.thinking_characters > 0;
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\" class=\"{}\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.started_at),
                html_escape(&row.account_name),
                html_escape(&status),
                latency,
                html_escape(&row.provider_id),
                html_escape(&row.protocol),
                html_escape(row.error_class.as_deref().unwrap_or("—")),
                format_tokens(row.input_tokens),
                format_tokens(row.output_tokens),
                if has_thinking { "yes" } else { "no" },
                if has_thinking { format_tokens(row.reasoning_tokens) } else { "—".to_owned() },
                html_escape(&request_id),
            )
        })
        .collect::<String>();
    format!(
        "<h2>Traces</h2><p class=\"sub\">Auth-gated; does not include error_detail or client_ip; for incident debugging only.</p><form method=\"get\" class=\"filter-form\"><label class=\"trace-limit\">Limit: <span class=\"number-stepper\" data-stepper-for=\"limit\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"dec\" aria-label=\"Decrease limit\">−</button><input type=\"number\" name=\"limit\" id=\"limit\" value=\"{}\" min=\"10\" max=\"500\" data-stepper-input=\"1\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"inc\" aria-label=\"Increase limit\">+</button></span></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form>{}<section class=\"panel\"><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Time</th><th data-priority=\"1\">Account</th><th data-priority=\"1\">Model</th><th data-priority=\"1\">Status</th><th data-priority=\"1\">Latency</th><th data-priority=\"2\">Provider</th><th data-priority=\"2\">Protocol</th><th data-priority=\"2\">Error class</th><th data-priority=\"2\">In</th><th data-priority=\"2\">Out</th><th data-priority=\"3\">Thinking</th><th data-priority=\"3\">ID</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
        limit,
        html_escape(period),
        html_escape(theme),
        dashboard_period_selector(period, theme)
    )
}

pub(super) fn render_runtime_page(
    data: &db::DashboardData,
    summary: &db::DashboardSummary,
    observability: &Value,
    diagnostics: Option<&crate::runtime_lifecycle::RuntimeDiagnosticsSnapshot>,
    runtime_uptime: std::time::Duration,
) -> String {
    let diag = diagnostics
        .map(serde_json::to_value)
        .and_then(Result::ok)
        .unwrap_or(Value::Null);
    let metric = |path: &[&str]| -> String {
        path.iter()
            .fold(&diag, |value, key| &value[*key])
            .as_i64()
            .map(|n| n.to_string())
            .unwrap_or_else(|| "—".to_owned())
    };
    let state = |path: &[&str]| -> String {
        path.iter()
            .fold(&diag, |value, key| &value[*key])
            .as_str()
            .map(html_escape)
            .unwrap_or_else(|| "not collected".to_owned())
    };
    let tasks = diagnostics
        .map(|snapshot| {
            snapshot
                .tasks
                .iter()
                .take(64)
                .map(|task| {
                    format!(
                        "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{}\">{}</td><td data-priority=\"2\">—</td><td data-priority=\"2\">—</td><td data-priority=\"2\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"4\">—/—</td><td data-priority=\"4\">—</td></tr>",
                        html_escape(&task.name),
                        if task.running {
                            "yes"
                        } else if task.enabled {
                            ""
                        } else {
                            "no"
                        },
                        if task.running { "running" } else { "stopped" },
                    )
                })
                .collect::<String>()
        })
        .unwrap_or_default();
    let transcoded = observability["transcoding"]["transcoded_count"]
        .as_i64()
        .map(|n| n.to_string())
        .unwrap_or_else(|| "—".to_owned());
    let parent_process_id = parent_process_id()
        .map(|pid| pid.to_string())
        .unwrap_or_else(|| "—".to_owned());
    let daemon_hint = if std::io::stdin().is_terminal() {
        "no"
    } else {
        "yes"
    };
    let process_sub = format!("PPID {parent_process_id} · daemon {daemon_hint}");
    let host_platform = host_platform_label();
    let server_cards = format!(
        "<section class=\"cards\">{}{}{}</section>",
        runtime_metric_card("Server PID", &std::process::id().to_string(), &process_sub),
        runtime_metric_card(
            "Uptime",
            &format_runtime_age(runtime_uptime),
            "uptime since start"
        ),
        runtime_metric_card("Python", "not applicable", &host_platform),
    );
    let load_summary = load_average_summary();
    let memory_cards = format!(
        "<section class=\"cards\">{}{}{}{}{}</section>",
        runtime_metric_card("RSS memory", "not collected", "resident set size"),
        runtime_metric_card("Open FDs", "not collected", "file descriptors"),
        runtime_metric_card(
            "Active threads",
            "not collected",
            "threading.active_count()",
        ),
        runtime_metric_card("Load average", "not collected", &load_summary),
        runtime_metric_card(
            "Dispatch overhead",
            "not collected",
            "last 0 / 100 attempts"
        ),
    );
    let task_rows = if tasks.is_empty() {
        "<p class=\"empty\">No background tasks registered.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Task</th><th data-priority=\"1\">Status</th><th data-priority=\"2\">Restarts</th><th data-priority=\"2\">Max restarts</th><th data-priority=\"2\">Interval</th><th data-priority=\"3\">Next run</th><th data-priority=\"3\">Done</th><th data-priority=\"4\">Success/Fail</th><th data-priority=\"4\">Last error</th></tr></thead><tbody>{}</tbody></table></div>",
            tasks
        )
    };
    let database_cards = format!(
        "<section class=\"cards\">{}{}{}{}</section>",
        runtime_metric_card(
            "Database",
            "not collected",
            "file path and size unavailable"
        ),
        runtime_metric_card("WAL", "not collected", "WAL mode unavailable"),
        runtime_metric_card("Sync", "not collected", "synchronous mode unavailable"),
        runtime_metric_card("Stats DB", "shared", "single serialized SQLite owner"),
    );
    let routing_cards = format!(
        "<section class=\"cards\">{}{}{}{}</section>",
        runtime_metric_card(
            "Pending requests",
            &data.pending_requests.to_string(),
            "oldest age not collected"
        ),
        runtime_metric_card(
            "Active reservations",
            &data.active_reservations.to_string(),
            "reserved amount not collected"
        ),
        runtime_metric_card("In-flight requests", "not collected", "active upstream"),
        runtime_metric_card("Active backoffs", "not collected", "account backoff rows"),
    );
    let network_cards = format!(
        "<section class=\"cards\">{}{}{}</section>",
        runtime_metric_card("Outbound builds", "not collected", "client lifecycle"),
        runtime_metric_card(
            "Outbound requests",
            "not collected",
            "request count unavailable"
        ),
        runtime_metric_card(
            "Provider clients",
            "not collected",
            "pool snapshot unavailable"
        ),
    );
    let reload_cards = format!(
        "<section class=\"cards\">{}{}{}</section>",
        runtime_metric_card(
            "Reload outcomes",
            &metric(&["counters", "reload_attempts"]),
            "attempts observed"
        ),
        runtime_metric_card(
            "Reload failures",
            &metric(&["counters", "reload_failures"]),
            "failures observed"
        ),
        runtime_metric_card(
            "Reload phase",
            &state(&["reload", "phase"]),
            "runtime diagnostic snapshot"
        ),
    );
    let transcoding_panel = format!(
        "<section class=\"panel\"><h3>Transcoding (24h)</h3><section class=\"cards\">{}{}{}</section><p class=\"empty-state\">Loss warnings are not collected for this period.</p></section>",
        runtime_metric_card(
            "Total requests",
            &summary.total_requests.to_string(),
            "in period"
        ),
        runtime_metric_card(
            "Native",
            &summary
                .total_requests
                .saturating_sub(
                    observability["transcoding"]["transcoded_count"]
                        .as_i64()
                        .unwrap_or(0)
                )
                .to_string(),
            "no transcoding"
        ),
        runtime_metric_card("Transcoded", &transcoded, "cross-protocol"),
    );
    let runtime_snapshot = format!(
        "<section class=\"panel\"><h3>Dispatch spans</h3><p class=\"empty\">Dispatch span details are not collected.</p><p class=\"status\">Metrics received/flushed/dropped: {}/{}/{}</p></section>",
        metric(&["metrics", "total_received"]),
        metric(&["metrics", "total_flushed"]),
        metric(&["metrics", "total_dropped"]),
    );
    format!(
        "<h2>Runtime</h2><p class=\"sub\">Process-level diagnostics for the running EggPool instance.</p>{server_cards}{memory_cards}<section class=\"panel\"><h3>Background tasks</h3>{task_rows}</section>{database_cards}{routing_cards}{network_cards}{transcoding_panel}{runtime_snapshot}{reload_cards}<section class=\"panel\"><h3>Health states</h3><p class=\"empty\">No health state data.</p></section>",
    )
}

fn runtime_metric_card(title: &str, metric: &str, sub: &str) -> String {
    let tooltip = match title {
        "Server PID" => "Process identity of the running supervisor (PPID and daemon mode).",
        "Uptime" => "Elapsed time since the current EggPool process started.",
        "Python" => "Python runtime version and platform for the running process.",
        "RSS memory" => "Resident memory currently held by the EggPool process.",
        "Open FDs" => "Open file descriptors currently held by the process.",
        "Active threads" => "Current number of active Python threads in the process.",
        "Load average" => {
            "Host load average (1m primary, subtext shows normalized load or 5m/15m)."
        }
        "Dispatch overhead" => {
            "EggPool-local time spent before each upstream dispatch attempt begins."
        }
        "Database" => "Primary SQLite database path and on-disk size.",
        "WAL" => "SQLite write-ahead log size and whether WAL mode is active.",
        "Sync" => "SQLite synchronous mode and whether the primary DB connection is live.",
        "Stats DB" => "Whether stats use a separate SQLite connection.",
        "Pending requests" => "Requests still in progress. Subtext shows the oldest pending age.",
        "Active reservations" => "Active quota or spend reservations for in-flight work.",
        "In-flight requests" => "Requests currently active against upstream providers.",
        "Active backoffs" => {
            "Persisted account backoff rows currently suppressing or delaying eligible accounts."
        }
        "Outbound builds" => {
            "How many times the shared outbound client manager has built a client."
        }
        "Outbound requests" => "Requests via the shared outbound client. Subtext shows errors.",
        "Provider clients" => {
            "How many per-provider HTTP clients were built in the provider client pool."
        }
        "Provider cache hit rate" => {
            "Protocol-aware cache hit rate: cache_read_tokens / cache_eligible_input_tokens. For OpenAI-compatible providers the denominator is total billed prompt tokens; for Anthropic it is fresh input + cache read + cache creation. Cache writes/creation are warmup, not hits."
        }
        "Cache write/warmup rate" => {
            "Cache write (creation) tokens as a share of eligible input. These populate cache entries and are not cache hits."
        }
        _ => title,
    };
    let tooltip = html_escape(tooltip);
    format!(
        "<div class=\"card\" data-tooltip=\"{tooltip}\" data-tooltip-pos=\"bottom\" aria-label=\"{tooltip}\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">{}</p></div>",
        html_escape(title),
        html_escape(metric),
        html_escape(sub),
    )
}

fn format_runtime_age(elapsed: std::time::Duration) -> String {
    let seconds = elapsed.as_secs();
    if seconds < 1 {
        "<1s".to_owned()
    } else if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3_600 {
        format!("{}m{}s", seconds / 60, seconds % 60)
    } else if seconds < 86_400 {
        format!("{}h{}m", seconds / 3_600, (seconds % 3_600) / 60)
    } else {
        format!("{}d{}h", seconds / 86_400, (seconds % 86_400) / 3_600)
    }
}

#[cfg(unix)]
fn parent_process_id() -> Option<u32> {
    Some(std::os::unix::process::parent_id())
}

#[cfg(not(unix))]
fn parent_process_id() -> Option<u32> {
    None
}

fn host_platform_label() -> String {
    let platform = match std::env::consts::OS {
        "macos" => "macOS",
        "linux" => "Linux",
        "windows" => "Windows",
        other => other,
    };
    format!("{platform}-{}", std::env::consts::ARCH)
}

fn load_average_summary() -> String {
    #[cfg(target_os = "linux")]
    if let Ok(loadavg) = std::fs::read_to_string("/proc/loadavg") {
        if let Some(load) = loadavg
            .split_whitespace()
            .next()
            .and_then(|value| value.parse::<f64>().ok())
        {
            if let Ok(cpu_count) = std::thread::available_parallelism() {
                return format!(
                    "{:.2}/core · {} CPUs",
                    load / cpu_count.get() as f64,
                    cpu_count.get()
                );
            }
        }
    }

    "load average unavailable".to_owned()
}

pub(super) fn render_cache_page(
    data: &db::DashboardData,
    stats: &Value,
    period: &str,
    theme: &str,
) -> String {
    let num = |path: &[&str]| {
        path.iter()
            .fold(stats, |v, k| &v[*k])
            .as_i64()
            .map(|n| n.to_string())
            .unwrap_or_else(|| "—".to_owned())
    };
    let rate = stats["cache_observability"]["cache_counter_coverage_rate"]
        .as_f64()
        .map(|v| format!("{:.1}%", v * 100.0))
        .unwrap_or_else(|| "—".to_owned());
    let hit_rate = stats["cache_observability"]["cache_hit_ratio_known_only"]
        .as_f64()
        .map(|v| format!("{:.1}%", v * 100.0))
        .unwrap_or_else(|| "—".to_owned());
    let write_rate = stats["cache_observability"]["cache_write_rate"]
        .as_f64()
        .map(|v| format!("{:.1}%", v * 100.0))
        .unwrap_or_else(|| "—".to_owned());
    let cache_sub = format!(
        "{} provider-reported rows · {} classified rows",
        num(&["request_shaping", "cache", "cache_counter_reported_rows"]),
        num(&["request_shaping", "cache", "cache_counter_known_rows"]),
    );
    let stability_notes = stats["cache_stability"]["notes"]
        .as_str()
        .unwrap_or("Boundary detail lives in per-request traces.");
    let segmentation_totals = [
        ("Total finalized requests", &["total_requests"][..]),
        (
            "Stable prefix tokens",
            &["token_totals", "stable_prefix"][..],
        ),
        ("Semi-stable tokens", &["token_totals", "semi_stable"][..]),
        ("Volatile suffix tokens", &["token_totals", "volatile"][..]),
        ("Stable prefix bytes", &["byte_totals", "stable_prefix"][..]),
        ("Semi-stable bytes", &["byte_totals", "semi_stable"][..]),
        ("Volatile suffix bytes", &["byte_totals", "volatile"][..]),
    ]
    .iter()
    .map(|(label, path)| {
        let value = path
            .iter()
            .fold(&stats["canonical_request_segmentation"], |value, key| {
                &value[*key]
            })
            .as_i64()
            .unwrap_or(0);
        format!("<tr><td>{label}</td><td class=\"num\">{value}</td></tr>")
    })
    .collect::<String>();
    let values = [
        // Rust has no configured request-compression path; structural
        // segmentation is observational and never rewrites the request.
        ("Request changes", "no changes".to_owned()),
        ("Provider cache counter coverage", rate.clone()),
        (
            "Rows with cache reads",
            data.cache.rows_with_read.to_string(),
        ),
        (
            "Rows with cache writes",
            data.cache.rows_with_write.to_string(),
        ),
        (
            "Rows without cache counters",
            num(&["cache_observability", "cache_counter_not_reported_requests"]),
        ),
        (
            "Unknown counter format",
            num(&["cache_observability", "cache_counter_unknown_requests"]),
        ),
        (
            "Cache read tokens",
            num(&["cache_observability", "cache_read_tokens_canonical"]),
        ),
        (
            "Cache write tokens",
            num(&["cache_observability", "cache_write_tokens_canonical"]),
        ),
        ("Provider cache hit rate", "not collected".to_owned()),
        (
            "Cache write/warmup rate",
            stats["cache_observability"]["cache_write_rate"]
                .as_f64()
                .map(|v| format!("{:.1}%", v * 100.0))
                .unwrap_or_else(|| "not collected".to_owned()),
        ),
        (
            "Transcoded requests",
            num(&["transcoding", "transcoded_count"]),
        ),
        (
            "Segmented",
            num(&["canonical_request_segmentation", "by_status", "segmented"]),
        ),
        (
            "Not collected",
            num(&[
                "canonical_request_segmentation",
                "by_status",
                "not_collected",
            ]),
        ),
        (
            "Empty request",
            num(&[
                "canonical_request_segmentation",
                "by_status",
                "empty_request",
            ]),
        ),
        (
            "Parse failure",
            num(&[
                "canonical_request_segmentation",
                "by_status",
                "parse_failure",
            ]),
        ),
        (
            "With protected prefix",
            num(&["canonical_request_segmentation", "protected_requests"]),
        ),
        (
            "With volatile suffix",
            num(&[
                "canonical_request_segmentation",
                "compressible_candidate_requests",
            ]),
        ),
        ("Mode", "reporting_only".to_owned()),
        ("Cache metrics", "no".to_owned()),
        ("Compression metrics", "no".to_owned()),
        ("Stable-prefix hash", "no".to_owned()),
        ("Compression policy", "no".to_owned()),
    ];
    let cards = [
        runtime_metric_card("Request changes", "no changes", "disabled by config"),
        runtime_metric_card("Provider cache counters", &rate, &cache_sub),
        // No compressor executes in this runtime, so there can be no
        // compression fallback or compression-policy warning to report.
        runtime_metric_card(
            "Safety guardrail",
            "Clean",
            "0 fallbacks · 0 policy warnings",
        ),
        runtime_metric_card(
            "Routing isolation",
            "Isolated",
            "mode reporting_only · cache/compression stay out of scorer",
        ),
    ]
    .concat();
    let reporting_cards = [
        runtime_metric_card(
            "Rows with cache counters",
            &num(&["cache_observability", "cache_counter_reported_requests"]),
            "upstream returned cache fields",
        ),
        runtime_metric_card(
            "Rows without cache counters",
            &num(&["cache_observability", "cache_counter_not_reported_requests"]),
            "payload clean, no cache keys",
        ),
        runtime_metric_card(
            "Unrecognized payload shape",
            &num(&["cache_observability", "cache_counter_unknown_requests"]),
            "parse failure or unrecognized",
        ),
        runtime_metric_card(
            "Provider cache hit rate",
            &hit_rate,
            &format!(
                "read {} / eligible {} · write/warmup {} · {} reported",
                num(&["cache_observability", "cache_read_tokens_canonical"]),
                num(&["cache_observability", "cache_eligible_input_tokens"]),
                write_rate,
                rate
            ),
        ),
        runtime_metric_card(
            "Cache write/warmup rate",
            &write_rate,
            &format!(
                "warmup, not hits · eligible {}",
                num(&["cache_observability", "cache_eligible_requests"])
            ),
        ),
    ]
    .concat();
    let reporting_table = format!(
        "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Metric</th><th data-priority=\"2\">Value</th></tr></thead><tbody><tr><td>Total finalized requests</td><td class=\"num\">{}</td></tr><tr><td>Input tokens (all requests)</td><td class=\"num\">{}</td></tr><tr><td>Output tokens (all requests)</td><td class=\"num\">{}</td></tr><tr><td>Read tokens (canonical)</td><td class=\"num\">{}</td></tr><tr><td>Write tokens (canonical)</td><td class=\"num\">{}</td></tr><tr><td>Eligible input tokens (denominator)</td><td class=\"num\">{}</td></tr><tr><td>Provider cache hit rate</td><td class=\"num\">{}</td></tr><tr><td>Cache write/warmup rate</td><td class=\"num\">{}</td></tr><tr><td>Coverage (cache counters reported)</td><td class=\"num\">{}</td></tr><tr><td>Anthropic cache read</td><td class=\"num\">{}</td></tr><tr><td>Anthropic cache creation</td><td class=\"num\">{}</td></tr></tbody></table></div>",
        num(&["cache_observability", "total_requests"]),
        num(&["cache_observability", "input_tokens_total"]),
        num(&["cache_observability", "output_tokens_total"]),
        num(&["cache_observability", "cache_read_tokens_canonical"]),
        num(&["cache_observability", "cache_write_tokens_canonical"]),
        num(&["cache_observability", "cache_eligible_input_tokens"]),
        hit_rate,
        write_rate,
        rate,
        num(&["cache_observability", "total_cache_read_input_tokens"]),
        num(&["cache_observability", "total_cache_creation_input_tokens"]),
    );
    let card_slice = |items: &[(&str, String)]| {
        items
            .iter()
            .map(|(name, value)| {
                let sub = match *name {
                    "Segmented" => "produced a normal result",
                    "Not collected" => "segmentation intentionally skipped",
                    "Empty request" => "segmentation ran but found no content",
                    "Parse failure" => "non-mapping payload or unknown",
                    "With protected prefix" => "stable_prefix_bytes > 0",
                    "With volatile suffix" => "volatile_bytes > 0",
                    "Mode" => "cache/compression in routing",
                    "Cache metrics"
                    | "Compression metrics"
                    | "Stable-prefix hash"
                    | "Compression policy" => "in scorer inputs",
                    _ => "persisted scalar observation",
                };
                runtime_metric_card(name, value, sub)
            })
            .collect::<String>()
    };
    let advanced = format!(
        "<section class=\"panel\"><h3>Native cache preservation ({})</h3><p class=\"sub\">Native cache annotations are tracked per request during transcoding. The durable summary below confirms the tracker is wired and counts transcoded requests in window; per-boundary detail is in the request trace.</p><section class=\"cards\">{}</section><p class=\"sub\">{}</p></section><section class=\"panel\"><h3>Request segmentation ({})</h3><p class=\"sub\">Structural segmentation shows how much traffic was segmented, intentionally skipped, or had no segmentable content without mutating requests.</p><section class=\"cards\">{}</section><div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Metric</th><th data-priority=\"2\">Value</th></tr></thead><tbody>{}</tbody></table></div></section><section class=\"panel\"><h3>Routing isolation</h3><p class=\"sub\">Cache and compression metrics are reporting-only. The <code>QuotaFairScorer</code> does NOT consume cache, compression, stable-prefix-hash, or compression-policy fields. Same-provider account scoring stays load-based. These flags are hardcoded; they reflect how the router is built, not the current request stream.</p><section class=\"cards\">{}</section><p class=\"sub\">Scorer inputs (allowed): <code>health, quota, active_requests, model_eligibility</code></p></section>",
        html_escape(period),
        runtime_metric_card(
            "Transcoded requests",
            &num(&["cache_stability", "transcoded_request_count"]),
            "boundary tracker active"
        ),
        html_escape(stability_notes),
        html_escape(period),
        card_slice(&values[11..17]),
        segmentation_totals,
        card_slice(&values[17..]),
    );
    format!(
        "<h2>Cache</h2><p class=\"sub\">Cache reporting, request shaping, and safety guardrails.</p>{}<div id=\"cache-summary\"><section class=\"panel\"><h3>Request shaping ({})</h3><p class=\"sub\">Operator summary for request changes, provider cache counter coverage, safety guardrails, and routing isolation. Routing stays load-based and reporting-only metrics never enter the scorer.</p><section class=\"cards\">{cards}</section></section></div><div id=\"cache-reporting\"><section class=\"panel\"><h3>Provider cache counters ({})</h3><p class=\"sub\">Provider-reported cache counters from upstream payloads. Missing cache fields mean the upstream did not surface them. They are not cache misses and do not prove the upstream is uncached. EggPool never disables provider-side caching.</p><section class=\"cards\">{reporting_cards}</section>{reporting_table}</section></div><details class=\"advanced-details\" id=\"advanced-diagnostics\"><summary>Show advanced diagnostics</summary><div class=\"advanced-body\">{}</div></details>",
        dashboard_period_selector(period, theme),
        html_escape(period),
        html_escape(period),
        advanced,
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

fn civil_date_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let shifted = days_since_epoch + 719_468;
    let era = if shifted >= 0 {
        shifted / 146_097
    } else {
        (shifted - 146_096) / 146_097
    };
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

fn parse_theme_rgb(color: &str) -> Option<(f64, f64, f64)> {
    let color = color.strip_prefix('#')?;
    let color = if color.len() == 8 {
        &color[..6]
    } else if color.len() == 6 {
        color
    } else {
        return None;
    };
    Some((
        u8::from_str_radix(&color[0..2], 16).ok()? as f64 / 255.0,
        u8::from_str_radix(&color[2..4], 16).ok()? as f64 / 255.0,
        u8::from_str_radix(&color[4..6], 16).ok()? as f64 / 255.0,
    ))
}

fn theme_lightness(color: &str) -> Option<f64> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    Some((red.max(green).max(blue) + red.min(green).min(blue)) / 2.0)
}

fn mix_theme_colors(base: &str, target: &str, ratio: f64) -> Option<String> {
    let channels = |color: &str| -> Option<(i32, i32, i32)> {
        let color = color.strip_prefix('#')?;
        let color = if color.len() == 8 { &color[..6] } else { color };
        if color.len() != 6 {
            return None;
        }
        Some((
            i32::from_str_radix(&color[0..2], 16).ok()?,
            i32::from_str_radix(&color[2..4], 16).ok()?,
            i32::from_str_radix(&color[4..6], 16).ok()?,
        ))
    };
    let (base_red, base_green, base_blue) = channels(base)?;
    let (target_red, target_green, target_blue) = channels(target)?;
    let channel = |base: i32, target: i32| (base as f64 + (target - base) as f64 * ratio) as u8;
    Some(format!(
        "#{:02x}{:02x}{:02x}",
        channel(base_red, target_red),
        channel(base_green, target_green),
        channel(base_blue, target_blue),
    ))
}

fn adjust_theme_lightness(color: &str, factor: f64) -> Option<String> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    let max = red.max(green).max(blue);
    let min = red.min(green).min(blue);
    let lightness = (max + min) / 2.0;
    let delta = max - min;
    if delta == 0.0 {
        let channel = (lightness * factor).clamp(0.0, 1.0);
        return Some(format!(
            "#{:02x}{:02x}{:02x}",
            (channel * 255.0) as u8,
            (channel * 255.0) as u8,
            (channel * 255.0) as u8,
        ));
    }

    let saturation = if lightness <= 0.5 {
        delta / (max + min)
    } else {
        delta / (2.0 - max - min)
    };
    let hue = if max == red {
        ((green - blue) / delta + if green < blue { 6.0 } else { 0.0 }) / 6.0
    } else if max == green {
        ((blue - red) / delta + 2.0) / 6.0
    } else {
        ((red - green) / delta + 4.0) / 6.0
    };
    let lightness = (lightness * factor).clamp(0.0, 1.0);
    let q = if lightness < 0.5 {
        lightness * (1.0 + saturation)
    } else {
        lightness + saturation - lightness * saturation
    };
    let p = 2.0 * lightness - q;
    let hue_to_rgb = |mut hue: f64| {
        if hue < 0.0 {
            hue += 1.0;
        } else if hue > 1.0 {
            hue -= 1.0;
        }
        if hue < 1.0 / 6.0 {
            p + (q - p) * 6.0 * hue
        } else if hue < 0.5 {
            q
        } else if hue < 2.0 / 3.0 {
            p + (q - p) * (2.0 / 3.0 - hue) * 6.0
        } else {
            p
        }
    };
    Some(format!(
        "#{:02x}{:02x}{:02x}",
        (hue_to_rgb(hue + 1.0 / 3.0) * 255.0) as u8,
        (hue_to_rgb(hue) * 255.0) as u8,
        (hue_to_rgb(hue - 1.0 / 3.0) * 255.0) as u8,
    ))
}

fn theme_heatmap_colors(name: &str) -> [String; 5] {
    let Some(bytes) = theme_bytes(name) else {
        return [
            "#ebedf0".to_owned(),
            "#9be9a8".to_owned(),
            "#40c463".to_owned(),
            "#30a14e".to_owned(),
            "#216e39".to_owned(),
        ];
    };
    let value = std::str::from_utf8(bytes)
        .unwrap_or("")
        .parse::<toml::Value>()
        .unwrap_or_else(|_| toml::Value::Table(Default::default()));
    let background = theme_value(&value, &["general", "background"], "#1e1e2e");
    let primary = theme_value(&value, &["text", "primary"], "#cdd6f4");
    let success = theme_value(&value, &["text", "success"], "#a6e3a1");
    let page_background = if parse_theme_rgb(background)
        .is_some_and(|(r, g, b)| (r.max(g).max(b) + r.min(g).min(b)) / 2.0 < 0.5)
    {
        theme_value(&value, &["buffer", "background"], background)
    } else {
        background
    };
    let mix = |base: &str, target: &str, ratio: f64| {
        let (Some((r1, g1, b1)), Some((r2, g2, b2))) =
            (parse_theme_rgb(base), parse_theme_rgb(target))
        else {
            return base.to_owned();
        };
        format!(
            "#{:02x}{:02x}{:02x}",
            (r1 * 255.0 + (r2 - r1) * 255.0 * ratio) as u8,
            (g1 * 255.0 + (g2 - g1) * 255.0 * ratio) as u8,
            (b1 * 255.0 + (b2 - b1) * 255.0 * ratio) as u8,
        )
    };
    let adjust = |color: &str, factor: f64| {
        let Some((r, g, b)) = parse_theme_rgb(color) else {
            return color.to_owned();
        };
        let max = r.max(g).max(b);
        let min = r.min(g).min(b);
        let mut lightness = (max + min) / 2.0;
        let delta = max - min;
        if delta == 0.0 {
            lightness = (lightness * factor).clamp(0.0, 1.0);
            let channel = (lightness * 255.0) as u8;
            return format!("#{channel:02x}{channel:02x}{channel:02x}");
        }
        let saturation = if lightness > 0.5 {
            delta / (2.0 - max - min)
        } else {
            delta / (max + min)
        };
        let hue = if max == r {
            (g - b) / delta + if g < b { 6.0 } else { 0.0 }
        } else if max == g {
            (b - r) / delta + 2.0
        } else {
            (r - g) / delta + 4.0
        } / 6.0;
        lightness = (lightness * factor).clamp(0.0, 1.0);
        let q = if lightness < 0.5 {
            lightness * (1.0 + saturation)
        } else {
            lightness + saturation - lightness * saturation
        };
        let p = 2.0 * lightness - q;
        let hue_channel = |mut t: f64| {
            if t < 0.0 {
                t += 1.0;
            }
            if t > 1.0 {
                t -= 1.0;
            }
            if t < 1.0 / 6.0 {
                p + (q - p) * 6.0 * t
            } else if t < 1.0 / 2.0 {
                q
            } else if t < 2.0 / 3.0 {
                p + (q - p) * (2.0 / 3.0 - t) * 6.0
            } else {
                p
            }
        };
        format!(
            "#{:02x}{:02x}{:02x}",
            (hue_channel(hue + 1.0 / 3.0) * 255.0) as u8,
            (hue_channel(hue) * 255.0) as u8,
            (hue_channel(hue - 1.0 / 3.0) * 255.0) as u8,
        )
    };
    [
        mix(page_background, primary, 0.06),
        mix(page_background, success, 0.35),
        success.to_owned(),
        adjust(success, 0.7),
        adjust(success, 0.45),
    ]
}

fn render_token_heatmap(
    rows: &[crate::db::repositories::DashboardTokenActivityRow],
    theme: &str,
) -> String {
    if rows.is_empty() {
        return "<p class=\"empty\">No activity data available.</p>".to_owned();
    }
    let values = rows
        .iter()
        .map(|row| (row.day.as_str(), (row.total_tokens, row.requests)))
        .collect::<std::collections::BTreeMap<_, _>>();
    let epoch_days = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
        / 86_400;
    let start_day = epoch_days - 179;
    let weekday_sunday = (start_day + 4).rem_euclid(7);
    let grid_start = start_day - weekday_sunday;
    let weeks = (epoch_days - grid_start) / 7 + 1;
    let step = 16_i64;
    let cell = 13_i64;
    let left = 36_i64;
    let top = 20_i64;
    let svg_width = left + weeks * step + 10;
    let svg_height = top + 7 * step + 10;
    let max_value = (0..180)
        .map(|offset| {
            let (year, month, day) = civil_date_from_days(start_day + offset);
            let key = format!("{year:04}-{month:02}-{day:02}");
            values.get(key.as_str()).map_or(0, |value| value.0)
        })
        .max()
        .unwrap_or(0)
        .max(1);
    let colors = theme_heatmap_colors(theme);
    let day_labels = [(1, "Mon"), (3, "Wed"), (5, "Fri")]
        .into_iter()
        .map(|(day, label)| {
            format!(
                "<text x=\"0\" y=\"{}\" class=\"heatmap-label\" text-anchor=\"start\" dominant-baseline=\"central\">{label}</text>",
                top + day * step + cell / 2
            )
        })
        .collect::<String>();
    let month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let mut month_labels = String::new();
    let mut prior_month = 0;
    for week in 0..weeks {
        let (_, month, _) = civil_date_from_days(grid_start + week * 7);
        if month != prior_month {
            prior_month = month;
            month_labels.push_str(&format!(
                "<text x=\"{}\" y=\"10\" class=\"heatmap-label\" text-anchor=\"start\">{}</text>",
                left + week * step,
                month_names[(month - 1) as usize]
            ));
        }
    }
    let mut cells = format!("{day_labels}{month_labels}");
    let mut hitboxes = String::new();
    for week in 0..weeks {
        for day_of_week in 0..7 {
            let day_number = grid_start + week * 7 + day_of_week;
            if day_number < start_day || day_number > epoch_days {
                hitboxes.push_str("<div class=\"heatmap-hitbox\"></div>");
                continue;
            }
            let (year, month, day) = civil_date_from_days(day_number);
            let key = format!("{year:04}-{month:02}-{day:02}");
            let (token_count, request_count) = values.get(key.as_str()).copied().unwrap_or((0, 0));
            let ratio = token_count as f64 / max_value as f64;
            let level = if token_count == 0 {
                0
            } else if ratio < 0.25 {
                1
            } else if ratio < 0.5 {
                2
            } else if ratio < 0.75 {
                3
            } else {
                4
            };
            let color = &colors[level];
            let x = left + week * step;
            let y = top + day_of_week * step;
            let weekday = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                [(day_number + 4).rem_euclid(7) as usize];
            let tooltip = format!(
                "{weekday}, {} {day} {year}\n{} tokens · {request_count} request{}",
                month_names[(month - 1) as usize],
                format_tokens(token_count),
                if request_count == 1 { "" } else { "s" }
            );
            let title = format!("{key}: {}", tooltip.replace('\n', " — "));
            cells.push_str(&format!(
                "<rect x=\"{x}\" y=\"{y}\" width=\"{cell}\" height=\"{cell}\" rx=\"2\" fill=\"{color}\" class=\"heatmap-cell\" pointer-events=\"none\"><title>{}</title></rect>",
                html_escape(title)
            ));
            let tooltip = html_escape(tooltip);
            hitboxes.push_str(&format!(
                "<div class=\"heatmap-hitbox\" data-tooltip=\"{tooltip}\" aria-label=\"{tooltip}\"></div>"
            ));
        }
    }
    format!(
        "<div class=\"heatmap\"><svg width=\"{svg_width}\" height=\"{svg_height}\" viewBox=\"0 0 {svg_width} {svg_height}\" role=\"img\" aria-label=\"Token activity (last 180 days)\">{cells}</svg><div class=\"heatmap-overlay\" style=\"--heatmap-weeks: {weeks}\" aria-hidden=\"true\">{hitboxes}</div></div>"
    )
}

fn render_bandwidth_heatmap(
    rows: &[crate::db::repositories::DashboardTokenActivityRow],
    theme: &str,
) -> String {
    if rows.is_empty() {
        return "<p class=\"empty\">No activity data available.</p>".to_owned();
    }
    let values = rows
        .iter()
        .map(|row| {
            (
                row.day.as_str(),
                (row.bytes_received, row.bytes_emitted, row.requests),
            )
        })
        .collect::<std::collections::BTreeMap<_, _>>();
    let today = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
        / 86_400;
    let start = today - 179;
    let grid_start = start - (start + 4).rem_euclid(7);
    let weeks = (today - grid_start) / 7 + 1;
    let step = 16_i64;
    let cell = 13_i64;
    let left = 36_i64;
    let top = 20_i64;
    let width = left + weeks * step + 10;
    let height = top + 7 * step + 10;
    let max_value = (0..180)
        .map(|offset| {
            let (year, month, day) = civil_date_from_days(start + offset);
            let key = format!("{year:04}-{month:02}-{day:02}");
            values
                .get(key.as_str())
                .map_or(0, |(received, emitted, _)| {
                    received.saturating_add(*emitted)
                })
        })
        .max()
        .unwrap_or(0)
        .max(1);
    let colors = theme_heatmap_colors(theme);
    let mut cells = String::new();
    for (day, label) in [(1, "Mon"), (3, "Wed"), (5, "Fri")] {
        cells.push_str(&format!(
            "<text x=\"0\" y=\"{}\" class=\"heatmap-label\" text-anchor=\"start\" dominant-baseline=\"central\">{label}</text>",
            top + day * step + cell / 2
        ));
    }
    let months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let mut last_month = 0;
    for week in 0..weeks {
        let (_, month, _) = civil_date_from_days(grid_start + week * 7);
        if month != last_month {
            last_month = month;
            cells.push_str(&format!(
                "<text x=\"{}\" y=\"10\" class=\"heatmap-label\" text-anchor=\"start\">{}</text>",
                left + week * step,
                months[(month - 1) as usize]
            ));
        }
    }
    let mut hitboxes = String::new();
    for week in 0..weeks {
        for weekday_offset in 0..7 {
            let day_number = grid_start + week * 7 + weekday_offset;
            if day_number < start || day_number > today {
                hitboxes.push_str("<div class=\"heatmap-hitbox\"></div>");
                continue;
            }
            let (year, month, day) = civil_date_from_days(day_number);
            let key = format!("{year:04}-{month:02}-{day:02}");
            let (received, emitted, requests) =
                values.get(key.as_str()).copied().unwrap_or((0, 0, 0));
            let total = received.saturating_add(emitted);
            let ratio = total as f64 / max_value as f64;
            let level = if total == 0 {
                0
            } else if ratio < 0.25 {
                1
            } else if ratio < 0.5 {
                2
            } else if ratio < 0.75 {
                3
            } else {
                4
            };
            let x = left + week * step;
            let y = top + weekday_offset * step;
            let pretty_day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                [(day_number + 4).rem_euclid(7) as usize];
            let tooltip = format!(
                "{pretty_day}, {} {day} {year}\n{} in · {} out · {} request{}",
                months[(month - 1) as usize],
                format_bytes(received),
                format_bytes(emitted),
                requests,
                if requests == 1 { "" } else { "s" }
            );
            let title = format!("{key}: {}", tooltip.replace('\n', " — "));
            cells.push_str(&format!(
                "<rect x=\"{x}\" y=\"{y}\" width=\"{cell}\" height=\"{cell}\" rx=\"2\" fill=\"{}\" class=\"heatmap-cell\" pointer-events=\"none\"><title>{}</title></rect>",
                colors[level],
                html_escape(title)
            ));
            let tooltip = html_escape(tooltip);
            hitboxes.push_str(&format!(
                "<div class=\"heatmap-hitbox\" data-tooltip=\"{tooltip}\" aria-label=\"{tooltip}\"></div>"
            ));
        }
    }
    format!(
        "<div class=\"heatmap\"><svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"Bandwidth activity (last 180 days)\">{cells}</svg><div class=\"heatmap-overlay\" style=\"--heatmap-weeks: {weeks}\" aria-hidden=\"true\">{hitboxes}</div></div>"
    )
}

fn format_ratio_percent(value: Option<f64>) -> String {
    value
        .filter(|ratio| ratio.is_finite())
        .map(|ratio| format!("{:.1}%", ratio * 100.0))
        .unwrap_or_else(|| "—".to_owned())
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
    let include_chart_js =
        body_requires_chart_runtime(&body) || matches!(active_nav, "reliability" | "routing");
    let shell_period = match active_nav {
        "runtime" => "runtime",
        "traces" => "recent",
        _ => period,
    };
    html_response(render_dashboard_layout(
        title,
        active_nav,
        shell_period,
        theme,
        15,
        body,
        include_chart_js,
    ))
}

fn body_requires_chart_runtime(body: &str) -> bool {
    [
        "data-chart-endpoint",
        "grouped-timeseries-chart",
        "static-chart-data",
        "id=\"timeseries-chart\"",
    ]
    .iter()
    .any(|hook| body.contains(hook))
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
        let class = if *key == active_nav { "active" } else { "" };
        format!(
            "<a class=\"{}\" href=\"{}?{}\">{}</a>",
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
    let refresh_script = if matches!(active_nav, "overview" | "runtime" | "cache") {
        auto_refresh_script(refresh_interval_s)
    } else {
        String::new()
    };
    let navigation_markup = format!(
        "<div class=\"topnav-menu\" id=\"topnav-menu\">{}<form method=\"get\" class=\"theme-selector\" data-tooltip=\"Switch dashboard theme\" data-tooltip-pos=\"bottom\" aria-label=\"Switch dashboard theme\"><select name=\"theme\" onchange=\"this.form.submit()\">{}</select><input type=\"hidden\" name=\"period\" value=\"{}\"></form></div>",
        navigation,
        theme_options,
        html_escape(period)
    );
    format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>{}</title>\n<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\">\n<link rel=\"preload\" href=\"/static/dashboard.css\" as=\"style\">\n<link rel=\"stylesheet\" href=\"/static/dashboard.css\">\n<link rel=\"stylesheet\" href=\"/static/theme.css?theme={}\">\n{}\n</head>\n<body>\n<svg class=\"egg-background\" viewBox=\"0 0 256 256\" preserveAspectRatio=\"xMidYMid meet\" aria-hidden=\"true\" focusable=\"false\"><path class=\"shape\" d=\"M128 30\n           C82 30 55 88 57 145\n           C59 202 89 231 128 231\n           C167 231 197 202 199 145\n           C201 88 174 30 128 30 Z\" /><path class=\"thin\" d=\"M86 132 H112 L126 111 L144 158 L159 132 H174\" /><circle class=\"shape\" cx=\"85\" cy=\"132\" r=\"5\" /><circle class=\"shape\" cx=\"174\" cy=\"132\" r=\"5\" /></svg>\n<header class=\"topbar\"><button class=\"topnav-burger\" type=\"button\" aria-label=\"Open page menu\" aria-expanded=\"false\" aria-controls=\"topnav-menu\"><svg class=\"topnav-burger-icon\" viewBox=\"0 0 24 24\" width=\"24\" height=\"24\" aria-hidden=\"true\" focusable=\"false\"><rect class=\"bar bar-1\" x=\"0\" y=\"0\" width=\"24\" height=\"2\" rx=\"1\"/><rect class=\"bar bar-2\" x=\"0\" y=\"11\" width=\"24\" height=\"2\" rx=\"1\"/><rect class=\"bar bar-3\" x=\"0\" y=\"22\" width=\"24\" height=\"2\" rx=\"1\"/></svg></button><h1><a href=\"/?{}\">EggPool</a></h1><nav class=\"topnav\">{}<button type=\"button\" class=\"topnav-refresh\" data-tooltip=\"Reload this page\" aria-label=\"Reload this page\" onclick=\"window.location.reload()\">↻</button></nav></header>\n<main id=\"dashboard-content\">\n{}\n</main>\n<footer><small>Period: <span class=\"period-label\">{}</span> &middot; auto-refresh {}s &middot; <span id=\"dashboard-updated\">ready</span></small></footer>\n{}<script defer src=\"/static/dashboard.js\"></script>{}\n</body>\n</html>",
        html_escape(title),
        query_component(theme),
        chart_preload,
        query,
        navigation_markup,
        body,
        html_escape(period),
        refresh_interval_s,
        refresh_script,
        chart_script
    )
}

fn auto_refresh_script(refresh_interval_s: u64) -> String {
    let interval_ms = refresh_interval_s.max(1).saturating_mul(1000);
    format!(
        r#"<script>
(() => {{
  const intervalMs = {interval_ms};
  const content = document.getElementById("dashboard-content");
  const updated = document.getElementById("dashboard-updated");
  if (!content || !updated || !window.DOMParser) {{
    return;
  }}
  const refresh = async () => {{
    try {{
      const response = await fetch(window.location.href, {{
        cache: "no-store",
        headers: {{"x-dashboard-refresh": "1"}},
      }});
      if (!response.ok) {{
        return;
      }}
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const next = doc.getElementById("dashboard-content");
      if (next) {{
        if (window.Chart && typeof window.Chart.getChart === "function") {{
          content.querySelectorAll("canvas").forEach((canvas) => {{
            const chart = window.Chart.getChart(canvas);
            if (chart) {{
              chart.destroy();
            }}
          }});
        }}
        const replacement = document.importNode(next, true);
        content.replaceChildren(...replacement.childNodes);
        updated.textContent = new Date().toLocaleTimeString();
        if (window.EggPoolDashboard) {{
          const dash = window.EggPoolDashboard;
          if (typeof dash.bootstrap === "function") {{
            dash.bootstrap();
          }} else {{
            if (typeof dash.initGroupedTimeseriesCharts === "function") {{
              dash.initGroupedTimeseriesCharts();
            }}
            if (typeof dash.reinitTimeseriesChart === "function") {{
              dash.reinitTimeseriesChart();
            }}
            if (typeof dash.initChartLoadingShells === "function") {{
              dash.initChartLoadingShells();
            }}
          }}
        }}
      }}
    }} catch (_err) {{
      updated.textContent = "stale";
    }}
  }};
  window.setInterval(refresh, intervalMs);
}})();
</script>"#
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

fn sanitize_class_name(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '_' | '-') {
                character
            } else {
                '_'
            }
        })
        .collect()
}

fn overview_metric_card(label: &str, value: impl std::fmt::Display, subtext: &str) -> String {
    let tooltip = match label {
        "Requests" => Some(
            "Total proxied requests in the selected period. The subtext splits them into successful and error requests.",
        ),
        "Error rate" => Some("Fraction of requests in the selected period that ended in an error."),
        "Total cost" => Some(
            "Total recorded request cost in the selected period. When the upstream provider reports a cost (e.g. OpenCode Go's usage.cost field), that value takes precedence over locally computed rates; otherwise eggpool falls back to per-token rates from the catalog. Reservation-derived estimates are advisory and never inflate the totals when more trustworthy data is available.",
        ),
        "Utilization imbalance" => Some(
            "Coefficient of variation across active accounts. Higher values mean load is concentrated unevenly.",
        ),
        "Total tokens" => Some(
            "Input, output, cache-read, and cache-write tokens recorded for the selected period. This total includes provider cache counters and can exceed fresh input/output volume on cache-heavy workloads.",
        ),
        "Fresh tokens" => Some(
            "Input plus output tokens recorded for the selected period, excluding cache-read and cache-write counters.",
        ),
        "Request shaping" => Some("Request shaping"),
        "Cache reads" => Some(
            "Provider-reported prompt-cache read tokens. The subtext shows the bounded read share cache_read / (input + cache_read + cache_write) and the cache write volume.",
        ),
        "Provider cache hit rate" => Some(
            "Protocol-aware cache hit rate: cache_read_tokens / cache_eligible_input_tokens. For OpenAI-compatible providers the denominator is total billed prompt tokens; for Anthropic it is fresh input + cache read + cache creation. Cache writes/creation are warmup, not hits.",
        ),
        "Cache write/warmup rate" => Some(
            "Cache write (creation) tokens as a share of eligible input. These populate cache entries and are not cache hits.",
        ),
        "Reasoning tokens" => {
            Some("Tokens reported by upstreams as reasoning or extended-thinking output.")
        }
        "Throughput" => Some(
            "Aggregate token throughput across requests, computed from total tokens divided by total latency.",
        ),
        "Streaming" => {
            Some("How many requests used streaming responses versus non-streaming responses.")
        }
        "Exactness" => Some(
            "Count of requests whose cost was exact. The subtext also shows derived, estimated, and unknown-cost rows.",
        ),
        "Bandwidth received" => {
            Some("Total bytes received from clients by EggPool in the selected period.")
        }
        "Bandwidth emitted" => {
            Some("Total bytes emitted by EggPool toward clients in the selected period.")
        }
        "Avg TTFT (streamed)" => {
            Some("Average time to first token for streamed requests, with P50 and P99 shown below.")
        }
        "Pending requests" => {
            Some("Requests still in progress. Subtext shows the oldest pending age.")
        }
        "Active reservations" => Some("Active quota or spend reservations for in-flight work."),
        "Finalizer (24h)" => Some(
            "Reliability cleanup activity over the last 24 hours, including stale request cleanup, timeout cases, and crash recovery runs.",
        ),
        "Retry rate" => Some(
            "Share of upstream attempts that required another try instead of succeeding or failing terminally on the first attempt.",
        ),
        "First-attempt success" => Some("Share of attempts that completed without any retry."),
        _ => None,
    };
    let tooltip_attrs = tooltip.map_or_else(String::new, |text| {
        let text = html_escape(text);
        format!(" aria-label=\"{text}\" data-tooltip=\"{text}\" data-tooltip-pos=\"bottom\"")
    });
    format!(
        "<div class=\"card\"{tooltip_attrs}><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">{}</p></div>",
        html_escape(label),
        html_escape(value),
        html_escape(subtext),
    )
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

pub(super) fn render_overview(summary: &db::DashboardSummary, page: OverviewPage<'_>) -> String {
    let OverviewPage {
        accounts,
        page_data,
        period,
        theme,
        refresh_interval_s,
        show_disabled,
        health_snapshots,
    } = page;
    let total = summary.total_requests;
    let errors = summary.error_requests;
    let error_rate = if total == 0 {
        0.0
    } else {
        errors as f64 / total as f64 * 100.0
    };
    let fresh_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        fresh_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let cost_subtext = if summary.provider_reported_count > 0 {
        format!(
            "in {} · out {} · total {} · {} provider-billed",
            format_tokens(summary.total_input_tokens),
            format_tokens(summary.total_output_tokens),
            format_tokens(fresh_tokens),
            summary.provider_reported_count
        )
    } else {
        format!(
            "in {} · out {} · total {}",
            format_tokens(summary.total_input_tokens),
            format_tokens(summary.total_output_tokens),
            format_tokens(fresh_tokens),
        )
    };
    let disabled_count = accounts.iter().filter(|account| !account.enabled).count();
    let account_rows = accounts
        .iter()
        .filter(|account| show_disabled || account.enabled)
        .map(|account| {
            let row = page_data.accounts.iter().find(|row| row.name == account.name);
            let live_health = health_snapshots
                .iter()
                .find(|snapshot| snapshot.account_name == account.name);
            let health = live_health
                .map(|snapshot| snapshot.health_state.as_str())
                .or_else(|| {
                    page_data
                        .pings
                        .iter()
                        .find(|ping| ping.account_name == account.name)
                        .map(|ping| {
                            if ping.status_code.is_some_and(|status| (200..300).contains(&status)) {
                                "healthy"
                            } else if ping.status_code.is_some() || ping.error.is_some() {
                                "unhealthy"
                            } else {
                                "unknown"
                            }
                        })
                })
                .unwrap_or("unknown");
            let exactness = row.map_or_else(String::new, |row| {
                exactness_badge(
                    row.exact_count,
                    row.derived_count,
                    row.partial_count,
                    row.estimated_count,
                    row.unknown_count,
                    row.provider_reported_count,
                )
            });
            let requests = row.map_or(0, |row| row.requests);
            let errors = row.map_or(0, |row| row.errors);
            let input_tokens = row.map_or(0, |row| row.input_tokens);
            let output_tokens = row.map_or(0, |row| row.output_tokens);
            let latency = row.map_or(0.0, |row| row.avg_latency_ms);
            let tps = if latency > 0.0 && requests > 0 {
                format!("{:.1} tok/s", output_tokens as f64 * 1_000.0 / (latency * requests as f64))
            } else {
                "0.0 tok/s".to_owned()
            };
            let authentication_failed = live_health.map_or("—", |snapshot| {
                if snapshot.health_state == "authentication_failed" {
                    "yes"
                } else {
                    "no"
                }
            });
            let operator_disabled = live_health.map_or("—", |snapshot| {
                if snapshot
                    .disabled_until
                    .is_some_and(|until| until > snapshot.last_check)
                {
                    "yes"
                } else {
                    "no"
                }
            });
            let auth_class = if authentication_failed == "—" {
                String::new()
            } else {
                format!(" class=\"{authentication_failed}\"")
            };
            let disabled_class = if operator_disabled == "—" {
                String::new()
            } else {
                format!(" class=\"{operator_disabled}\"")
            };
            let detail_cells = row.map_or_else(
                || "<td data-priority=\"3\">—</td>".repeat(19),
                |row| {
                    format!(
                        "<td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td>",
                        format_microdollars(row.reserved_microdollars),
                        row.active_reservations,
                        format_microdollars(row.utilization_5h),
                        format_microdollars(row.utilization_7d),
                        format_microdollars(row.utilization_30d),
                        format_bytes(row.bytes_received),
                        format_bytes(row.bytes_emitted),
                        live_health.map_or(0, |snapshot| snapshot.consecutive_failures),
                        auth_class, authentication_failed,
                        disabled_class, operator_disabled,
                        format_ratio_percent(Some(row.estimated_cost_fraction)),
                        format_ratio_percent(row.cache_read_ratio),
                        format_ratio_percent(row.cache_write_ratio),
                        format_ratio_percent(row.reasoning_output_ratio),
                        row.avg_cost_per_request.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
                        row.avg_cost_per_1k_tokens.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
                    )
                },
            );
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{}\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\" class=\"{}\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td>{}</tr>",
                html_escape(&account.name), html_escape(&account.provider_id),
                if account.enabled { "yes" } else { "no" }, if account.enabled { "yes" } else { "no" },
                requests, row.map_or_else(|| "$0.00".to_owned(), |row| format_microdollars(row.cost_microdollars)),
                sanitize_class_name(health), html_escape(health), errors, format_tokens(input_tokens),
                format_tokens(output_tokens), format_tokens(input_tokens + output_tokens), format_latency(latency), tps,
                exactness, detail_cells,
            )
        })
        .collect::<String>();
    let toggle_label = if show_disabled {
        "Hide disabled".to_owned()
    } else if disabled_count > 0 {
        format!("Show {disabled_count} disabled")
    } else {
        "Show disabled".to_owned()
    };
    let toggle_value = if show_disabled { "0" } else { "1" };
    let account_href = format!(
        "?period={}&amp;theme={}&amp;show_disabled={toggle_value}",
        query_component(period),
        query_component(theme)
    );
    let account_table = if account_rows.is_empty() {
        "<p class=\"empty\">No accounts configured.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Account</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Enabled</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Health</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Input tokens</th><th data-priority=\"2\">Output tokens</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">TPS</th><th data-priority=\"2\">Exactness</th><th data-priority=\"3\">Reserved</th><th data-priority=\"3\">Resv.</th><th data-priority=\"3\">5h rate</th><th data-priority=\"3\">7d rate</th><th data-priority=\"3\">30d rate</th><th data-priority=\"3\">BW received</th><th data-priority=\"3\">BW emitted</th><th data-priority=\"3\">Over budget</th><th data-priority=\"3\">Upstream backoff</th><th data-priority=\"3\">Backoff until</th><th data-priority=\"3\">Failures</th><th data-priority=\"3\">Auth fail</th><th data-priority=\"3\">Disabled</th><th data-priority=\"3\">Est. cost</th><th data-priority=\"3\">Cache R</th><th data-priority=\"3\">Cache W</th><th data-priority=\"3\">Reasoning</th><th data-priority=\"3\">Avg cost/req</th><th data-priority=\"3\">Avg cost/1k tok</th></tr></thead><tbody>{account_rows}</tbody></table></div>"
        )
    };
    let mut glance_models = page_data
        .models
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .collect::<Vec<_>>();
    glance_models.sort_by(|left, right| {
        right
            .requests
            .cmp(&left.requests)
            .then_with(|| right.cost_microdollars.cmp(&left.cost_microdollars))
            .then_with(|| left.model_id.cmp(&right.model_id))
            .then_with(|| left.provider_id.cmp(&right.provider_id))
    });
    let model_rows = glance_models
        .into_iter()
        .take(10)
        .map(|row| {
            let tooltip = format!("Open model info for {}", row.model_id);
            format!(
                "<tr><td data-priority=\"1\"><a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a></td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&tooltip),
                html_escape(&tooltip),
                html_escape(&row.model_id),
                row.requests,
                format_microdollars(row.cost_microdollars),
                html_escape(&row.provider_id),
                row.errors,
                format_latency(row.avg_latency_ms),
                format_tokens(row.input_tokens + row.output_tokens),
            )
        })
        .collect::<String>();
    let event_rows = page_data
        .events
        .iter()
        .take(10)
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\"><span class=\"event-tag {}\">{}</span></td><td data-priority=\"2\">{}</td></tr>",
                html_escape(&row.created_at),
                html_escape(&row.account_name),
                sanitize_class_name(&row.event_type),
                html_escape(&row.event_type),
                html_escape(row.details.chars().take(120).collect::<String>()),
            )
        })
        .collect::<String>();
    let overview_glance = format!(
        "<section class=\"overview-grid\"><div class=\"panel\"><h3>Top models</h3>{}</div><div class=\"panel\"><h3>Recent events</h3>{}</div></section>",
        if model_rows.is_empty() {
            "<p class=\"empty\">No model activity in this period.</p>".to_owned()
        } else {
            format!(
                "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Model</th><th data-priority=\"1\">Reqs</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Provider</th><th data-priority=\"2\">Errs</th><th data-priority=\"2\">Latency</th><th data-priority=\"3\">Total tokens</th></tr></thead><tbody>{model_rows}</tbody></table></div>"
            )
        },
        if event_rows.is_empty() {
            "<p class=\"empty\">No recent events.</p>".to_owned()
        } else {
            format!(
                "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">When</th><th data-priority=\"1\">Account</th><th data-priority=\"1\">Type</th><th data-priority=\"2\">Details</th></tr></thead><tbody>{event_rows}</tbody></table></div>"
            )
        },
    );
    let ip_rows = page_data
        .ip_stats
        .iter()
        .take(10)
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.client_ip), row.requests,
                format_microdollars(row.cost_microdollars), format_latency(row.avg_latency_ms),
                row.errors, format_tokens(row.input_tokens), format_tokens(row.output_tokens),
                format_tokens(row.input_tokens + row.output_tokens), row.unique_models,
            )
        })
        .collect::<String>();
    let ip_panel = if ip_rows.is_empty() {
        String::new()
    } else {
        format!(
            "<section class=\"panel\"><h3>Request breakdown by IP</h3><div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">IP Address</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">Errors</th><th data-priority=\"3\">Input tokens</th><th data-priority=\"3\">Output tokens</th><th data-priority=\"3\">Total tokens</th><th data-priority=\"3\">Models</th></tr></thead><tbody>{ip_rows}</tbody></table></div></section>"
        )
    };
    let retry_attempts = page_data
        .retries
        .iter()
        .map(|row| row.attempts)
        .sum::<i64>();
    let retry_outcomes = page_data
        .retries
        .iter()
        .map(|row| row.retry_outcomes)
        .sum::<i64>();
    let success_attempts = page_data
        .retries
        .iter()
        .map(|row| row.successes)
        .sum::<i64>();
    let first_attempt_success = if retry_attempts > 0 {
        format!(
            "{:.1}%",
            success_attempts as f64 / retry_attempts as f64 * 100.0
        )
    } else {
        "0.0%".to_owned()
    };
    let pending_subtext = if page_data.pending_requests == 0 {
        "oldest — · stale 0"
    } else {
        "oldest — · stale count unavailable"
    };
    let active_accounts = accounts
        .iter()
        .filter(|account| account.enabled)
        .filter_map(|account| {
            page_data
                .accounts
                .iter()
                .find(|row| row.name == account.name)
                .filter(|row| row.requests > 0)
                .map(|row| row.cost_microdollars as f64)
        })
        .collect::<Vec<_>>();
    let utilization_imbalance = if active_accounts.len() < 2 {
        "0.00%".to_owned()
    } else {
        let mean = active_accounts.iter().sum::<f64>() / active_accounts.len() as f64;
        if mean == 0.0 {
            "0.00%".to_owned()
        } else {
            let variance = active_accounts
                .iter()
                .map(|cost| (cost - mean).powi(2))
                .sum::<f64>()
                / active_accounts.len() as f64;
            format!("{:.2}%", variance.sqrt() / mean * 100.0)
        }
    };
    let cache_hit_rate = "—";
    let cards_second = format!(
        "<section class=\"cards system-health\">{}{}{}{}{}</section>",
        overview_metric_card(
            "Pending requests",
            page_data.pending_requests,
            pending_subtext
        ),
        overview_metric_card(
            "Active reservations",
            page_data.active_reservations,
            &format!(
                "reserved {}",
                format_microdollars(page_data.active_reserved_microdollars)
            )
        ),
        overview_metric_card(
            "Finalizer (24h)",
            page_data.finalizer_cleaned_24h,
            &format!("cleaned · {} recovery", page_data.crash_recovery_24h)
        ),
        overview_metric_card(
            "Retry rate",
            if retry_attempts > 0 {
                format!(
                    "{:.1}%",
                    retry_outcomes as f64 / retry_attempts as f64 * 100.0
                )
            } else {
                "0.0%".to_owned()
            },
            &format!("of {retry_attempts} attempts"),
        ),
        overview_metric_card(
            "First-attempt success",
            first_attempt_success,
            "no retry needed"
        ),
    );
    let cards_third = format!(
        "<section class=\"cards\">{}{}{}{}{}{}{}{}</section>",
        overview_metric_card(
            "Total tokens",
            format_tokens(accounted_tokens),
            &format!(
                "fresh {} · cache read {} · cache write {}",
                format_tokens(fresh_tokens),
                format_tokens(summary.total_cache_read_tokens),
                format_tokens(summary.total_cache_write_tokens)
            )
        ),
        overview_metric_card("Request shaping", "—", "request shaping state unavailable"),
        overview_metric_card(
            "Fresh tokens",
            format_tokens(fresh_tokens),
            &format!(
                "in {} · out {}",
                format_tokens(summary.total_input_tokens),
                format_tokens(summary.total_output_tokens)
            )
        ),
        overview_metric_card(
            "Provider cache hit rate",
            cache_hit_rate,
            "legacy summary estimate"
        ),
        overview_metric_card(
            "Reasoning tokens",
            format_tokens(summary.total_reasoning_tokens),
            "extended thinking"
        ),
        overview_metric_card(
            "Throughput",
            format!("{:.1} tok/s", summary.tokens_per_second),
            "aggregate Σtokens / Σlatency"
        ),
        overview_metric_card(
            "Streaming",
            summary.streamed_requests,
            &format!("streamed · {} non-streamed", summary.non_streamed_requests)
        ),
        overview_metric_card(
            "Exactness",
            summary.exact_count,
            &format!(
                "exact · {} derived · {} upstream · {} est · {} unk",
                summary.derived_count,
                summary.provider_reported_count,
                summary.estimated_count,
                summary.unknown_count
            )
        ),
    );
    let cards_fourth = format!(
        "<section class=\"cards\">{}{}{}</section>",
        overview_metric_card(
            "Bandwidth received",
            format_bytes(summary.total_bytes_received),
            "client → proxy"
        ),
        overview_metric_card(
            "Bandwidth emitted",
            format_bytes(summary.total_bytes_emitted),
            "upstream → proxy"
        ),
        overview_metric_card(
            "Avg TTFT (streamed)",
            format_latency(summary.avg_ttft_ms),
            &format!(
                "P50 {} · P99 {}",
                format_latency(summary.p50_ttft_ms),
                format_latency(summary.p99_ttft_ms)
            )
        ),
    );
    let token_activity = format!(
        "<section class=\"panel\"><h3>Token activity (last 180 days)</h3>{}</section>",
        render_token_heatmap(&page_data.token_activity, theme)
    );
    let operational_panels = if summary.total_requests > 0 {
        let mut ping_groups = std::collections::BTreeMap::<String, Vec<&db::Ping>>::new();
        for ping in &page_data.pings {
            ping_groups
                .entry(ping.provider_id.clone())
                .or_default()
                .push(ping);
        }
        let ping_rows = ping_groups
            .iter()
            .map(|(provider, observations)| {
                let successes = observations
                    .iter()
                    .filter(|ping| ping.status_code.is_some_and(|status| (200..300).contains(&status)))
                    .count();
                let success_rate = successes as f64 / observations.len() as f64 * 100.0;
                let avg_latency = observations
                    .iter()
                    .filter_map(|ping| ping.latency_ms)
                    .map(|latency| latency as f64)
                    .sum::<f64>()
                    / observations
                        .iter()
                        .filter(|ping| ping.latency_ms.is_some())
                        .count()
                        .max(1) as f64;
                let latest = observations[0];
                let status = if success_rate >= 90.0 { "healthy" } else { "degraded" };
                format!(
                    "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{status}\">{status}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{success_rate:.1}%</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
                    html_escape(provider),
                    format_latency(avg_latency),
                    latest.model_count,
                    html_escape(&latest.probed_at),
                )
            })
            .collect::<String>();
        let provider_health = if ping_rows.is_empty() {
            String::new()
        } else {
            format!(
                "<section class=\"panel\"><h3>Provider health</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Status</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">Success rate</th><th data-priority=\"3\">Models</th><th data-priority=\"3\">Last ping</th></tr></thead><tbody>{ping_rows}</tbody></table></div></section>"
            )
        };
        let warning_panel = if summary.reservation_fallback_rows > 0 {
            format!(
                "<section class=\"panel warn reservation-fallback-warning\"><h3>Warnings</h3><p role=\"status\">{} reservation cost fallback rows require review.</p></section>",
                summary.reservation_fallback_rows
            )
        } else {
            String::new()
        };
        format!("{provider_health}{warning_panel}")
    } else {
        String::new()
    };
    let body = format!(
        "{}<section class=\"cards\"><div class=\"card\" aria-label=\"Total proxied requests in the selected period. The subtext splits them into successful and error requests.\" data-tooltip=\"Total proxied requests in the selected period. The subtext splits them into successful and error requests.\" data-tooltip-pos=\"bottom\"><h3>Requests</h3><p class=\"metric\">{}</p><p class=\"sub\">Success {} · Errors {}</p></div><div class=\"card\" aria-label=\"Fraction of requests in the selected period that ended in an error.\" data-tooltip=\"Fraction of requests in the selected period that ended in an error.\" data-tooltip-pos=\"bottom\"><h3>Error rate</h3><p class=\"metric\">{:.2}%</p><p class=\"sub\">avg latency {:.1} ms</p></div><div class=\"card\" aria-label=\"Total recorded request cost in the selected period. When the upstream provider reports a cost (e.g. OpenCode Go's usage.cost field), that value takes precedence over locally computed rates; otherwise eggpool falls back to per-token rates from the catalog. Reservation-derived estimates are advisory and never inflate the totals when more trustworthy data is available.\" data-tooltip=\"Total recorded request cost in the selected period. When the upstream provider reports a cost (e.g. OpenCode Go's usage.cost field), that value takes precedence over locally computed rates; otherwise eggpool falls back to per-token rates from the catalog. Reservation-derived estimates are advisory and never inflate the totals when more trustworthy data is available.\" data-tooltip-pos=\"bottom\"><h3>Total cost</h3><p class=\"metric\">${:.2}</p><p class=\"sub\">{}</p></div><div class=\"card\" aria-label=\"Coefficient of variation across active accounts. Higher values mean load is concentrated unevenly.\" data-tooltip=\"Coefficient of variation across active accounts. Higher values mean load is concentrated unevenly.\" data-tooltip-pos=\"bottom\"><h3>Utilization imbalance</h3><p class=\"metric\">{}</p><p class=\"sub\">CV across active accounts</p></div></section><section class=\"panel\"><div class=\"panel-header\"><h3>Account breakdown<span class=\"panel-header-chip\">{} enabled</span></h3><a class=\"show-disabled-toggle\" href=\"{}\" aria-pressed=\"{}\"><span class=\"disabled-toggle-icon\" aria-hidden=\"true\">&#x25BE;</span>{}</a></div>{}</section><section class=\"panel\"><h3>Request timeseries</h3><div class=\"chart-loading-shell\" data-chart-endpoint=\"/api/timeseries?period={}&amp;bucket=hour\" data-chart-canvas=\"timeseries-chart\" data-chart-state=\"loading\" style=\"height: 300px;\"><span class=\"chart-loading-spinner\" aria-hidden=\"true\"></span><span>Loading chart data…</span></div><script type=\"application/json\" class=\"chart-loading-shell-data\" data-chart-canvas=\"timeseries-chart\">{{}}</script><noscript><div class=\"chart-wrap\" style=\"height: 300px;\"><canvas id=\"timeseries-chart\" data-period=\"{}\"></canvas></div><script type=\"application/json\" id=\"timeseries-initial-data\" data-period=\"{}\">[]</script></noscript></section>",
        dashboard_header("Overview", period, theme),
        total,
        summary.successful_requests,
        errors,
        error_rate,
        summary.avg_latency_ms,
        summary.total_cost_microdollars as f64 / 1_000_000.0,
        cost_subtext,
        utilization_imbalance,
        accounts.iter().filter(|account| account.enabled).count(),
        account_href,
        show_disabled,
        toggle_label,
        account_table,
        html_escape(period),
        html_escape(period),
        html_escape(period)
    );
    let body = body.replacen(
        "<section class=\"cards\">",
        &format!("{cards_second}<section class=\"cards\">"),
        1,
    );
    let body = body.replacen(
        "<section class=\"panel\"><div class=\"panel-header\"><h3>Account breakdown",
        &format!(
            "{cards_third}{cards_fourth}<section class=\"panel\"><div class=\"panel-header\"><h3>Account breakdown"
        ),
        1,
    );
    let body = body.replace(
        "</noscript></section>",
        &format!(
            "</noscript></section>{overview_glance}{ip_panel}{token_activity}{operational_panels}"
        ),
    );
    render_dashboard_layout(
        "Overview",
        "overview",
        period,
        theme,
        refresh_interval_s,
        body.clone(),
        body_requires_chart_runtime(&body),
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
    let Some(bytes) = theme_bytes(name) else {
        return String::new();
    };
    let value = std::str::from_utf8(bytes)
        .unwrap_or("")
        .parse::<toml::Value>()
        .unwrap_or_else(|_| toml::Value::Table(Default::default()));
    let get = |path: &[&str], fallback: &'static str| theme_value(&value, path, fallback);
    let general_background = get(&["general", "background"], "#1e1e2e");
    let text_primary = get(&["text", "primary"], "#cdd6f4");
    let text_secondary = get(&["text", "secondary"], "#a6adc8");
    let text_success = get(&["text", "success"], "#a6e3a1");
    let text_error = get(&["text", "error"], "#f38ba8");
    let buffer_background = get(&["buffer", "background"], "#1e1e2e");
    let buffer_title = get(&["buffer", "background_title_bar"], "#181825");
    let buffer_url = get(&["buffer", "url"], "#89b4fa");
    let buffer_action = get(&["buffer", "action"], "#fab387");
    let page_background = if theme_lightness(general_background).is_some_and(|value| value < 0.5) {
        buffer_background.to_owned()
    } else {
        general_background.to_owned()
    };
    let info = buffer_url;
    let warning = buffer_action;
    let primary_button = get(&["buttons", "primary", "background_selected"], "#313244");
    let primary_button = if primary_button.is_empty() {
        get(&["buffer", "background_title_bar"], "#181825")
    } else {
        primary_button
    };
    let muted = {
        let topic = get(&["buffer", "topic"], "#7f849c");
        if topic == text_primary {
            text_secondary
        } else {
            topic
        }
    };
    let page_border = get(&["general", "border"], "#45475a");
    let card_background = buffer_background;
    let button_primary_background = {
        let selected = get(&["buttons", "primary", "background_selected"], "");
        if !selected.is_empty() {
            selected
        } else {
            let background = get(&["buttons", "primary", "background"], "");
            if background.is_empty() {
                general_background
            } else {
                background
            }
        }
    };
    let values = [
        ("--page-bg", page_background.to_owned()),
        ("--page-text", text_primary.to_owned()),
        ("--page-border", page_border.to_owned()),
        ("--topbar-bg", general_background.to_owned()),
        ("--topbar-text", text_primary.to_owned()),
        ("--topbar-border", page_border.to_owned()),
        ("--nav-text", text_secondary.to_owned()),
        (
            "--nav-hover-bg",
            get(&["buffer", "highlight"], "#45475a").to_owned(),
        ),
        ("--nav-active-bg", primary_button.to_owned()),
        ("--nav-active-text", text_primary.to_owned()),
        ("--card-bg", card_background.to_owned()),
        ("--card-border", page_border.to_owned()),
        ("--table-header-bg", buffer_title.to_owned()),
        ("--table-header-text", text_secondary.to_owned()),
        (
            "--table-border",
            get(&["general", "horizontal_rule"], "#313244").to_owned(),
        ),
        ("--text-muted", muted.to_owned()),
        ("--text-secondary", text_secondary.to_owned()),
        ("--color-success", text_success.to_owned()),
        ("--color-error", text_error.to_owned()),
        ("--color-warning", warning.to_owned()),
        ("--color-info", info.to_owned()),
        ("--button-primary-bg", button_primary_background.to_owned()),
        ("--button-primary-text", text_primary.to_owned()),
        (
            "--chip-bg",
            mix_theme_colors(&page_background, text_primary, 0.06)
                .unwrap_or_else(|| "#313244".to_owned()),
        ),
        (
            "--chip-border",
            mix_theme_colors(&page_background, text_primary, 0.14)
                .unwrap_or_else(|| "#45475a".to_owned()),
        ),
        ("--button-bg", card_background.to_owned()),
        (
            "--button-border",
            mix_theme_colors(card_background, text_primary, 0.18)
                .unwrap_or_else(|| page_border.to_owned()),
        ),
        (
            "--button-bg-hover",
            mix_theme_colors(card_background, text_primary, 0.08)
                .unwrap_or_else(|| card_background.to_owned()),
        ),
        (
            "--button-bg-active",
            mix_theme_colors(card_background, info, 0.20)
                .unwrap_or_else(|| card_background.to_owned()),
        ),
        ("--link-color", info.to_owned()),
        (
            "--link-color-hover",
            adjust_theme_lightness(info, 0.85).unwrap_or_else(|| info.to_owned()),
        ),
        ("--accent-color", info.to_owned()),
        (
            "--tag-default-bg",
            mix_theme_colors(&page_background, info, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-default-text", info.to_owned()),
        (
            "--tag-success-bg",
            mix_theme_colors(&page_background, text_success, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-success-text", text_success.to_owned()),
        (
            "--tag-warning-bg",
            mix_theme_colors(&page_background, warning, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-warning-text", warning.to_owned()),
        (
            "--tag-error-bg",
            mix_theme_colors(&page_background, text_error, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-error-text", text_error.to_owned()),
        (
            "--heatmap-0",
            mix_theme_colors(&page_background, text_primary, 0.06)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        (
            "--heatmap-1",
            mix_theme_colors(&page_background, text_success, 0.35)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--heatmap-2", text_success.to_owned()),
        (
            "--heatmap-3",
            adjust_theme_lightness(text_success, 0.7).unwrap_or_else(|| text_success.to_owned()),
        ),
        (
            "--heatmap-4",
            adjust_theme_lightness(text_success, 0.45).unwrap_or_else(|| text_success.to_owned()),
        ),
        ("--heatmap-label-text", muted.to_owned()),
    ];
    let declarations = values
        .iter()
        .map(|(property, color)| format!("  {property}: {color};"))
        .collect::<Vec<_>>()
        .join("\n");
    format!(":root {{\n{declarations}\n}}")
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
        assert!(html.contains("pill-fresh"));
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
        assert!(
            html.contains("data-chart-endpoint=\"/api/timeseries?period=24h&amp;bucket=hour\"")
        );
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
