use super::*;

pub(in crate::server) async fn summary(
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

pub(super) async fn observability_api(
    state: AppState,
    period: Option<String>,
    route: &str,
) -> Response {
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

pub(in crate::server) async fn stats_transcoding(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "transcoding").await
}
pub(in crate::server) async fn stats_cache_observability(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "cache-observability").await
}
pub(in crate::server) async fn stats_request_segmentation(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "canonical-request-segmentation").await
}
pub(in crate::server) async fn stats_cache_stability(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "cache-stability").await
}
pub(in crate::server) async fn stats_request_shaping(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    observability_api(state, query.period, "request-shaping").await
}

pub(in crate::server) async fn timeseries_api(
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

pub(super) fn normalized_bucket(value: Option<&str>, period: &str) -> &'static str {
    match value.unwrap_or("auto") {
        "day" => "day",
        "hour" => "hour",
        _ if period == "30d" => "day",
        _ => "hour",
    }
}

pub(super) fn normalized_group_by(value: &str) -> &'static str {
    match value {
        "provider" => "provider",
        "model" => "model",
        "account" => "account",
        "provider_model" => "provider_model",
        _ => "provider_model",
    }
}

pub(in crate::server) async fn grouped_timeseries_api(
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

pub(super) fn grouped_timeseries_projection(
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

pub(super) fn ordered_json_object(value: &Value, fields: &[&str]) -> String {
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

pub(super) fn grouped_timeseries_json(value: &Value) -> String {
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

pub(super) fn escape_script_end_tags(value: &str) -> String {
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

pub(super) fn ranked_series_order(
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
