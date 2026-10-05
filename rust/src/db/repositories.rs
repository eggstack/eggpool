//! Typed repositories for the first Rust read-plane and compatibility writes.

use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use tokio_rusqlite::rusqlite::{OptionalExtension, params};

use super::{Database, DatabaseError};

#[derive(Debug, Clone, PartialEq)]
pub struct Account {
    pub id: i64,
    pub name: String,
    pub api_key_env: String,
    pub enabled: bool,
    pub weight: f64,
    pub provider_id: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AccountConfig {
    pub name: String,
    pub api_key_env: String,
    pub enabled: bool,
    pub weight: f64,
    pub provider_id: String,
}

impl AccountConfig {
    pub fn new(name: impl Into<String>, api_key_env: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            api_key_env: api_key_env.into(),
            enabled: true,
            weight: 1.0,
            provider_id: "opencode-go".to_owned(),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Model {
    pub model_id: String,
    pub display_name: Option<String>,
    pub protocol: String,
    pub provider_id: String,
    pub resolution_status: String,
}

/// Raw durable global catalog identity. JSON remains advisory and is parsed
/// by the catalog boundary, not by the SQL row mapper.
#[derive(Debug, Clone, PartialEq)]
pub struct CatalogModel {
    pub model_id: String,
    pub display_name: Option<String>,
    pub protocol: String,
    pub capabilities: String,
    pub source_metadata: String,
    pub protocol_source: Option<String>,
    pub first_seen_at: String,
    pub last_seen_at: String,
    pub resolution_status: String,
    pub provider_id: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ProviderModelMetadata {
    pub model_id: String,
    pub provider_id: String,
    pub display_name: Option<String>,
    pub protocol: Option<String>,
    pub capabilities: String,
    pub source_metadata: String,
    pub protocol_source: Option<String>,
    pub first_seen_at: String,
    pub last_seen_at: String,
    pub resolution_status: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AccountModelSupport {
    pub account_id: i64,
    pub model_id: String,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CatalogRefreshState {
    pub account_id: i64,
    pub provider_id: String,
    pub last_successful_refresh_at: String,
    pub last_outcome: String,
    pub model_count: i64,
}

#[derive(Debug, Clone)]
pub struct CatalogModelWrite {
    pub model_id: String,
    pub display_name: Option<String>,
    pub protocol: String,
    pub capabilities: Value,
    pub source_metadata: Value,
    pub first_seen_at: i64,
    pub last_seen_at: i64,
    pub protocol_source: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ProviderModelWrite {
    pub model_id: String,
    pub provider_id: String,
    pub display_name: Option<String>,
    pub protocol: Option<String>,
    pub capabilities: Value,
    pub source_metadata: Value,
    pub protocol_source: Option<String>,
    pub first_seen_at: i64,
    pub last_seen_at: i64,
    pub resolution_status: String,
}

#[derive(Debug, Clone)]
pub struct CatalogRefreshWrite {
    pub account_id: i64,
    pub provider_id: String,
    pub refreshed_at: i64,
    pub outcome: String,
    pub model_count: i64,
}

#[derive(Debug, Clone)]
pub struct CatalogPingWrite {
    pub provider_id: String,
    pub account_name: String,
    pub latency_ms: i64,
    pub status_code: Option<i64>,
    pub error: Option<String>,
    pub model_count: i64,
}

#[derive(Debug, Clone, Default)]
pub struct CatalogPersistenceBatch {
    pub models: Vec<CatalogModelWrite>,
    pub provider_models: Vec<ProviderModelWrite>,
    pub support: BTreeSet<(i64, String)>,
    pub refresh: Vec<CatalogRefreshWrite>,
    pub pings: Vec<CatalogPingWrite>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    pub id: i64,
    pub proxy_request_id: Option<String>,
    pub account_id: i64,
    pub provider_id: String,
    pub model_id: String,
    pub protocol: String,
    pub streamed: bool,
    pub status: String,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_microdollars: i64,
    pub started_at: String,
    pub completed_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Ping {
    pub provider_id: String,
    pub account_name: String,
    pub probed_at: String,
    pub latency_ms: Option<i64>,
    pub status_code: Option<i64>,
    pub error: Option<String>,
    pub model_count: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct UsageSummary {
    pub total_requests: i64,
    pub error_requests: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_microdollars: i64,
    pub streamed_requests: i64,
    pub avg_latency_ms: f64,
}

/// The stable, read-only summary shape used by the first dashboard API slice.
/// Values intentionally mirror Python's `/api/stats/summary` fields; future
/// dashboard pages can extend this boundary without reaching into SQL.
#[derive(Debug, Clone, PartialEq)]
pub struct DashboardSummary {
    pub total_requests: i64,
    pub successful_requests: i64,
    pub error_requests: i64,
    pub total_input_tokens: i64,
    pub total_output_tokens: i64,
    pub total_cost_microdollars: i64,
    pub avg_latency_ms: f64,
    pub total_cache_read_tokens: i64,
    pub total_cache_write_tokens: i64,
    pub total_reasoning_tokens: i64,
    pub streamed_requests: i64,
    pub non_streamed_requests: i64,
    pub exact_count: i64,
    pub derived_count: i64,
    pub partial_count: i64,
    pub estimated_count: i64,
    pub unknown_count: i64,
    pub provider_reported_count: i64,
    pub provider_reported_cost_microdollars: i64,
    pub estimated_cost_sum_microdollars: i64,
    pub reservation_fallback_rows: i64,
    pub reservation_fallback_excess_microdollars: i64,
    pub total_bytes_received: i64,
    pub total_bytes_emitted: i64,
    pub total_providers: i64,
    pub avg_ttft_ms: f64,
    pub tokens_per_second: f64,
    pub p50_ttft_ms: f64,
    pub p99_ttft_ms: f64,
}

/// Bounded, read-only rows used by the dashboard pages.  These deliberately
/// contain display-safe aggregates and identifiers only; request bodies,
/// credentials, and raw provider payloads never cross this boundary.
#[derive(Debug, Clone, PartialEq)]
pub struct DashboardAccountRow {
    pub name: String,
    pub provider_id: String,
    pub enabled: bool,
    pub requests: i64,
    pub errors: i64,
    pub cost_microdollars: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub exact_count: i64,
    pub derived_count: i64,
    pub partial_count: i64,
    pub estimated_count: i64,
    pub unknown_count: i64,
    pub provider_reported_count: i64,
    pub avg_latency_ms: f64,
    pub reserved_microdollars: i64,
    pub active_reservations: i64,
    pub bytes_received: i64,
    pub bytes_emitted: i64,
    pub estimated_cost_fraction: f64,
    pub cache_read_ratio: Option<f64>,
    pub cache_write_ratio: Option<f64>,
    pub reasoning_output_ratio: Option<f64>,
    pub avg_cost_per_request: Option<i64>,
    pub avg_cost_per_1k_tokens: Option<i64>,
    pub utilization_5h: i64,
    pub utilization_7d: i64,
    pub utilization_30d: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardModelRow {
    pub model_id: String,
    pub provider_id: String,
    pub resolution_status: String,
    pub requests: i64,
    pub errors: i64,
    pub cost_microdollars: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub avg_latency_ms: f64,
    pub ttft_requests: i64,
    pub avg_ttft_ms: f64,
    pub exact_count: i64,
    pub derived_count: i64,
    pub partial_count: i64,
    pub estimated_count: i64,
    pub unknown_count: i64,
    pub provider_reported_count: i64,
    pub estimated_cost_fraction: f64,
    pub cache_read_ratio: Option<f64>,
    pub cache_write_ratio: Option<f64>,
    pub reasoning_output_ratio: Option<f64>,
    pub avg_cost_per_request: Option<i64>,
    pub avg_cost_per_1k_tokens: Option<i64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardLatencyPercentileRow {
    pub provider_id: String,
    /// Empty means provider-level aggregate; otherwise this is the model ID.
    pub model_id: String,
    pub request_count: i64,
    pub p50_ttft_ms: f64,
    pub p99_ttft_ms: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardRequestRow {
    pub started_at: String,
    pub account_name: String,
    pub provider_id: String,
    pub model_id: String,
    pub status: String,
    pub status_code: Option<i64>,
    pub latency_ms: Option<f64>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub error_class: Option<String>,
    pub error_message: Option<String>,
    pub protocol: String,
    pub proxy_request_id: Option<String>,
    pub reasoning_tokens: i64,
    pub thinking_characters: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardEventRow {
    pub created_at: String,
    pub account_name: String,
    pub event_type: String,
    pub details: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardOperationalSummaryRow {
    pub event_type: String,
    pub event_count: i64,
    pub last_seen: String,
    pub interrupted_requests: i64,
    pub released_reservations: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardOperationalEventRow {
    pub occurred_at: String,
    pub event_type: String,
    pub details: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardRetryRow {
    pub category: String,
    pub attempts: i64,
    pub retry_outcomes: i64,
    pub successes: i64,
    pub failures: i64,
    pub avg_latency_ms: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardRoutingRow {
    pub model_id: String,
    pub provider_id: String,
    pub decisions: i64,
    pub avg_eligible: f64,
    pub avg_scored: f64,
    pub avg_excluded: f64,
    pub avg_score: f64,
    pub distinct_accounts: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardRoutingSelectionRow {
    pub account_name: String,
    pub provider_id: String,
    pub selection_count: i64,
    pub avg_selected_tier: f64,
    pub avg_selected_score: f64,
    pub avg_eligible_count: f64,
    pub last_selected_score: Option<f64>,
    pub last_selected_tier: Option<i64>,
    pub last_selected_at: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardTimeseriesRow {
    pub bucket: String,
    pub series: String,
    pub provider_id: String,
    pub model_id: String,
    pub requests: i64,
    pub cost_microdollars: i64,
    pub errors: i64,
    pub total_tokens: i64,
    pub avg_latency_ms: f64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_tokens: i64,
    pub cache_write_tokens: i64,
    pub reasoning_tokens: i64,
    pub bytes_received: i64,
    pub bytes_emitted: i64,
    pub avg_ttft_ms: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardIpRow {
    pub client_ip: String,
    pub requests: i64,
    pub cost_microdollars: i64,
    pub avg_latency_ms: f64,
    pub errors: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub unique_models: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DashboardTokenActivityRow {
    pub day: String,
    pub total_tokens: i64,
    pub requests: i64,
    pub bytes_received: i64,
    pub bytes_emitted: i64,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct DashboardCacheSummary {
    pub rows_with_read: i64,
    pub rows_with_write: i64,
    pub rows_with_reasoning: i64,
    pub total_bytes_received: i64,
    pub total_bytes_emitted: i64,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct DashboardData {
    pub pending_requests: i64,
    pub active_reservations: i64,
    pub active_reserved_microdollars: i64,
    pub finalizer_cleaned_24h: i64,
    pub crash_recovery_24h: i64,
    pub accounts: Vec<DashboardAccountRow>,
    pub models: Vec<DashboardModelRow>,
    pub latency_percentiles: Vec<DashboardLatencyPercentileRow>,
    pub requests: Vec<DashboardRequestRow>,
    pub events: Vec<DashboardEventRow>,
    pub operational_summary: Vec<DashboardOperationalSummaryRow>,
    pub recent_operational_events: Vec<DashboardOperationalEventRow>,
    pub pings: Vec<Ping>,
    pub retries: Vec<DashboardRetryRow>,
    pub routing: Vec<DashboardRoutingRow>,
    pub routing_selection: Vec<DashboardRoutingSelectionRow>,
    pub timeseries: Vec<DashboardTimeseriesRow>,
    pub ip_stats: Vec<DashboardIpRow>,
    pub token_activity: Vec<DashboardTokenActivityRow>,
    pub cache: DashboardCacheSummary,
}

/// Query every bounded dashboard slice from the canonical schema.  Keeping
/// the page data in one repository makes the Rust read plane auditable and
/// prevents page handlers from drifting into ad-hoc SQL.
#[derive(Debug, Clone)]
pub struct DashboardRepository {
    database: Database,
}

/// Normalize one optional dashboard filter.
///
/// The filter forms submit their `(any …)` option as an empty value, which
/// deserializes to `Some("")`. The SQL guards are written as
/// `?N IS NULL OR column = ?N`, so a blank value skips the `IS NULL`
/// short-circuit and matches nothing: the page reports "no data" for a filter
/// the operator never set. Treat blank as absent, the way the models page
/// already does.
fn dashboard_filter(value: Option<String>) -> Option<String> {
    value.filter(|value| !value.trim().is_empty())
}

fn dashboard_sql(sql: &str) -> String {
    // Rust's continuation-string syntax removes newlines and indentation.
    // Keep the query text readable while restoring token boundaries before
    // handing it to SQLite.
    let query = sql
        .replace("SELECT", "SELECT ")
        .replace("UNION ALL", " UNION ALL ");
    [
        "LEFT JOIN",
        "FROM",
        "WHERE",
        "AND",
        "WHEN",
        "ELSE",
        "END",
        "GROUP BY",
        "ORDER BY",
        "LIMIT",
    ]
    .into_iter()
    .fold(query, |query, keyword| {
        query.replace(keyword, &format!(" {keyword}"))
    })
}

fn cache_status_breakdown(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    period: &str,
    dimension: &str,
) -> Result<Value, tokio_rusqlite::rusqlite::Error> {
    let sql = format!(
        "SELECT {dimension}, COALESCE(cache_counter_status,'not_reported'), COUNT(*), \
         COALESCE(SUM(CASE WHEN cache_counter_status='reported' THEN COALESCE(cached_input_tokens,0) ELSE 0 END),0) \
         FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour') \
         WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days') \
         ELSE datetime('now','-24 hours') END AND started_at < datetime('now') AND status!='pending' \
         GROUP BY {dimension}, cache_counter_status ORDER BY COUNT(*) DESC, {dimension} LIMIT 100"
    );
    let mut statement = connection.prepare(&sql)?;
    let mut output = serde_json::Map::new();
    for row in statement.query_map([period], |row| {
        Ok((
            row.get::<_, Option<String>>(0)?
                .unwrap_or_else(|| "unknown".to_owned()),
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })? {
        let (key, raw_status, count, cached) = row?;
        let status = match raw_status.as_str() {
            "reported" | "not_reported" | "unknown_format" => raw_status,
            _ => "unknown_format".to_owned(),
        };
        let key = key.chars().take(160).collect::<String>();
        let entry = output.entry(key).or_insert_with(|| {
            serde_json::json!({
                "reported": 0, "not_reported": 0, "unknown_format": 0,
                "total_requests": 0, "total_cached_input_tokens": 0
            })
        });
        entry[&status] =
            serde_json::json!(entry[&status].as_i64().unwrap_or(0).saturating_add(count));
        entry["total_requests"] = serde_json::json!(
            entry["total_requests"]
                .as_i64()
                .unwrap_or(0)
                .saturating_add(count)
        );
        entry["total_cached_input_tokens"] = serde_json::json!(
            entry["total_cached_input_tokens"]
                .as_i64()
                .unwrap_or(0)
                .saturating_add(cached)
        );
    }
    Ok(Value::Object(output))
}

impl DashboardRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    /// Return the compatibility API's bounded, bucketed request series.
    /// All filters are bound values and the bucket expression comes from a
    /// closed enum at the server boundary.
    pub async fn timeseries_json(
        &self,
        period: String,
        bucket: String,
        account: Option<String>,
        model: Option<String>,
    ) -> Result<Vec<Value>, DatabaseError> {
        let account = dashboard_filter(account);
        let model = dashboard_filter(model);
        self.database
            .call(move |connection| {
                let format = if bucket == "day" {
                    "%Y-%m-%d 00:00:00"
                } else {
                    "%Y-%m-%d %H:00:00"
                };
                let sql = dashboard_sql(&format!(
                    "SELECT strftime('{format}', r.started_at), COUNT(*),\
                    COALESCE(SUM(r.input_tokens),0), COALESCE(SUM(r.output_tokens),0),\
                    COALESCE(SUM(r.input_tokens),0)+COALESCE(SUM(r.output_tokens),0),\
                    COALESCE(SUM(r.cost_microdollars),0),\
                    COALESCE(SUM(CASE WHEN r.status='error' THEN 1 ELSE 0 END),0),\
                    COALESCE(SUM(r.bytes_received),0), COALESCE(SUM(r.bytes_emitted),0),\
                    COALESCE(AVG(CASE WHEN r.streamed=1 THEN r.first_byte_ms END),0)\
                    FROM requests r JOIN accounts a ON a.id=r.account_id\
                    WHERE r.started_at >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour')\
                    WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days')\
                    ELSE datetime('now','-24 hours') END AND r.started_at < datetime('now')\
                    AND (?2 IS NULL OR a.name=?2)\
                    AND (?3 IS NULL OR r.model_id=?3 OR r.original_model_id=?3)\
                    GROUP BY 1 ORDER BY 1 LIMIT 2048"
                ));
                let mut statement = connection.prepare(&sql)?;
                statement
                    .query_map(params![period, account, model], |row| {
                        Ok(serde_json::json!({
                            "bucket": row.get::<_, String>(0)?,
                            "request_count": row.get::<_, i64>(1)?,
                            "input_tokens": row.get::<_, i64>(2)?,
                            "output_tokens": row.get::<_, i64>(3)?,
                            "total_tokens": row.get::<_, i64>(4)?,
                            "cost_microdollars": row.get::<_, i64>(5)?,
                            "error_count": row.get::<_, i64>(6)?,
                            "bytes_received": row.get::<_, i64>(7)?,
                            "bytes_emitted": row.get::<_, i64>(8)?,
                            "avg_ttft_ms": row.get::<_, f64>(9)?,
                        }))
                    })?
                    .collect()
            })
            .await
    }

    /// Return raw rows for the server's bounded top-N grouped timeseries
    /// compatibility projection. Identifiers and SQL expressions are chosen
    /// only from the server's validated grouping enum.
    pub async fn grouped_timeseries_json(
        &self,
        period: String,
        bucket: String,
        group_by: String,
        account: Option<String>,
        model: Option<String>,
    ) -> Result<(Vec<Value>, bool), DatabaseError> {
        let account = dashboard_filter(account);
        let model = dashboard_filter(model);
        self.database
            .call(move |connection| {
                let format = if bucket == "day" {
                    "%Y-%m-%d 00:00:00"
                } else {
                    "%Y-%m-%d %H:00:00"
                };
                let (key, label) = match group_by.as_str() {
                    "provider" => ("r.provider_id", "r.provider_id"),
                    "model" => ("r.model_id", "r.model_id"),
                    "account" => ("CAST(r.account_id AS TEXT)", "CAST(r.account_id AS TEXT)"),
                    _ => ("r.provider_id || '/' || r.model_id", "r.provider_id || ' / ' || r.model_id"),
                };
                let provider = if matches!(group_by.as_str(), "provider" | "provider_model") {
                    "r.provider_id"
                } else {
                    "''"
                };
                let model_value = if matches!(group_by.as_str(), "model" | "provider_model") {
                    "r.model_id"
                } else {
                    "''"
                };
                let rollup_sql = dashboard_sql(&format!(
                    "SELECT strftime('{format}',r.bucket_start), {key}, {label}, {provider},\
                    {model_value}, '', SUM(r.request_count), SUM(r.error_count),\
                    SUM(r.input_tokens), SUM(r.output_tokens), SUM(r.cache_read_tokens),\
                    SUM(r.cache_write_tokens), SUM(r.reasoning_tokens),\
                    SUM(r.input_tokens)+SUM(r.output_tokens), SUM(r.cost_microdollars),\
                    SUM(r.bytes_received), SUM(r.bytes_emitted),\
                    CASE WHEN SUM(r.request_count)>0 THEN CAST(SUM(r.latency_ms_sum) AS REAL)/SUM(r.request_count) ELSE 0 END,\
                    CASE WHEN SUM(r.first_byte_ms_count)>0 THEN CAST(SUM(r.first_byte_ms_sum) AS REAL)/SUM(r.first_byte_ms_count) ELSE 0 END\
                    FROM usage_rollups r WHERE r.bucket_start >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour')\
                    WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days')\
                    ELSE datetime('now','-24 hours') END AND r.bucket_start < datetime('now')\
                    AND r.bucket_size_s=(SELECT MAX(bucket_size_s) FROM usage_rollups WHERE bucket_start >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour')\
                    WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days')\
                    ELSE datetime('now','-24 hours') END AND bucket_start < datetime('now'))\
                    AND (?2 IS NULL OR r.account_id=(SELECT id FROM accounts WHERE name=?2))\
                    AND (?3 IS NULL OR r.model_id=?3) GROUP BY 1,2,3,4,5 ORDER BY 1,2 LIMIT 5000"
                ));
                let mut rollup_statement = connection.prepare(&rollup_sql)?;
                let rollup_rows = rollup_statement
                    .query_map(params![period, account, model], |row| {
                        Ok(serde_json::json!({
                            "bucket": row.get::<_, String>(0)?,
                            "raw_series_key": row.get::<_, String>(1)?,
                            "raw_series_label": row.get::<_, String>(2)?,
                            "provider_id": row.get::<_, String>(3)?,
                            "model_id": row.get::<_, String>(4)?,
                            "account_name": "",
                            "request_count": row.get::<_, i64>(6)?,
                            "error_count": row.get::<_, i64>(7)?,
                            "input_tokens": row.get::<_, i64>(8)?,
                            "output_tokens": row.get::<_, i64>(9)?,
                            "cache_read_tokens": row.get::<_, i64>(10)?,
                            "cache_write_tokens": row.get::<_, i64>(11)?,
                            "reasoning_tokens": row.get::<_, i64>(12)?,
                            "total_tokens": row.get::<_, i64>(13)?,
                            "cost_microdollars": row.get::<_, i64>(14)?,
                            "bytes_received": row.get::<_, i64>(15)?,
                            "bytes_emitted": row.get::<_, i64>(16)?,
                            "avg_latency_ms": row.get::<_, f64>(17)?,
                            "avg_ttft_ms": row.get::<_, f64>(18)?,
                        }))
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                if !rollup_rows.is_empty() {
                    return Ok((rollup_rows, true));
                }
                let sql = dashboard_sql(&format!(
                    "SELECT strftime('{format}',r.started_at), {key}, {label}, r.provider_id,\
                    COALESCE(r.original_model_id,r.model_id), a.name, COUNT(*),\
                    COALESCE(SUM(CASE WHEN r.status='error' THEN 1 ELSE 0 END),0),\
                    COALESCE(SUM(r.input_tokens),0), COALESCE(SUM(r.output_tokens),0),\
                    COALESCE(SUM(r.cache_read_tokens),0), COALESCE(SUM(r.cache_write_tokens),0),\
                    COALESCE(SUM(r.reasoning_tokens),0),\
                    COALESCE(SUM(r.input_tokens),0)+COALESCE(SUM(r.output_tokens),0),\
                    COALESCE(SUM(r.cost_microdollars),0), COALESCE(SUM(r.bytes_received),0),\
                    COALESCE(SUM(r.bytes_emitted),0), COALESCE(AVG(r.upstream_latency_ms),0),\
                    COALESCE(AVG(CASE WHEN r.streamed=1 THEN r.first_byte_ms END),0)\
                    FROM requests r JOIN accounts a ON a.id=r.account_id\
                    WHERE r.started_at >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour')\
                    WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days')\
                    ELSE datetime('now','-24 hours') END AND r.started_at < datetime('now')\
                    AND (?2 IS NULL OR a.name=?2)\
                    AND (?3 IS NULL OR r.model_id=?3 OR r.original_model_id=?3)\
                    GROUP BY 1,2,3,4,5,6 ORDER BY 1,2 LIMIT 5000"
                ));
                let mut statement = connection.prepare(&sql)?;
                statement
                    .query_map(params![period, account, model], |row| {
                        Ok(serde_json::json!({
                            "bucket": row.get::<_, String>(0)?,
                            "raw_series_key": row.get::<_, String>(1)?,
                            "raw_series_label": row.get::<_, String>(2)?,
                            "provider_id": row.get::<_, Option<String>>(3)?,
                            "model_id": row.get::<_, Option<String>>(4)?,
                            "account_name": row.get::<_, Option<String>>(5)?,
                            "request_count": row.get::<_, i64>(6)?,
                            "error_count": row.get::<_, i64>(7)?,
                            "input_tokens": row.get::<_, i64>(8)?,
                            "output_tokens": row.get::<_, i64>(9)?,
                            "cache_read_tokens": row.get::<_, i64>(10)?,
                            "cache_write_tokens": row.get::<_, i64>(11)?,
                            "reasoning_tokens": row.get::<_, i64>(12)?,
                            "total_tokens": row.get::<_, i64>(13)?,
                            "cost_microdollars": row.get::<_, i64>(14)?,
                            "bytes_received": row.get::<_, i64>(15)?,
                            "bytes_emitted": row.get::<_, i64>(16)?,
                            "avg_latency_ms": row.get::<_, f64>(17)?,
                            "avg_ttft_ms": row.get::<_, f64>(18)?,
                        }))
                    })?
                    .collect::<Result<Vec<_>, _>>()
                    .map(|rows| (rows, false))
            })
            .await
    }

    /// Bounded compatibility projections for the historical runtime/cache
    /// stats endpoints. Raw request content and hashes are never selected.
    pub async fn observability_stats(&self, period: &str) -> Result<Value, DatabaseError> {
        let period = period.to_owned();
        self.database.call(move |connection| {
            let sql = "SELECT COUNT(*), COALESCE(SUM(transcoded),0), COALESCE(SUM(CASE WHEN cache_counter_status='reported' THEN cache_read_tokens ELSE 0 END),0), COALESCE(SUM(CASE WHEN cache_counter_status='reported' THEN cache_write_tokens ELSE 0 END),0), COALESCE(SUM(CASE WHEN cache_counter_status='reported' THEN COALESCE(cached_input_tokens,0) ELSE 0 END),0), COALESCE(SUM(cache_counter_status='reported'),0), COALESCE(SUM(cache_counter_status='not_reported' OR cache_counter_status IS NULL),0), COALESCE(SUM(CASE WHEN cache_counter_status='unknown_format' OR (cache_counter_status IS NOT NULL AND cache_counter_status NOT IN ('reported','not_reported','unknown_format')) THEN 1 ELSE 0 END),0), COALESCE(SUM(segmentation_status='segmented'),0), COALESCE(SUM(segmentation_status='not_collected'),0), COALESCE(SUM(segmentation_status='parse_failure'),0), COALESCE(SUM(segmentation_status='empty_request'),0), COALESCE(SUM(CASE WHEN stable_prefix_bytes>0 THEN 1 ELSE 0 END),0), COALESCE(SUM(COALESCE(stable_prefix_estimated_tokens,0)),0), COALESCE(SUM(COALESCE(semi_stable_estimated_tokens,0)),0), COALESCE(SUM(COALESCE(volatile_estimated_tokens,0)),0), COALESCE(SUM(COALESCE(stable_prefix_bytes,0)),0), COALESCE(SUM(COALESCE(semi_stable_bytes,0)),0), COALESCE(SUM(COALESCE(volatile_bytes,0)),0), COALESCE(SUM(compression_status='observed'),0), COALESCE(SUM(compression_candidate_count),0), COALESCE(SUM(compression_eligible_candidate_count),0), COALESCE(SUM(compression_suppressed_candidate_count),0), COALESCE(SUM(CASE WHEN volatile_bytes>0 THEN 1 ELSE 0 END),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_counter_status='reported' AND transcoded=0),0), COALESCE(SUM(cache_read_tokens>0),0) FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour') WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days') ELSE datetime('now','-24 hours') END AND started_at < datetime('now') AND status!='pending'";
            let values = connection.query_row(sql, [&period], |row| (0..28).map(|index| row.get::<_, i64>(index)).collect::<Result<Vec<_>, _>>())?;
            let total=values[0]; let transcoded=values[1]; let read=values[2]; let write=values[3]; let cached=values[4];
            let reported=values[5]; let not_reported=values[6]; let unknown=values[7];
            let known=reported+not_reported+unknown;
            let ratio=|numerator:i64, denominator:i64| if denominator==0 { Value::Null } else { serde_json::json!(numerator as f64/denominator as f64) };
            let per_account_status = cache_status_breakdown(connection, &period, "CAST(account_id AS TEXT)")?;
            let per_model_cache_status = cache_status_breakdown(connection, &period, "model_id")?;
            let per_protocol_status = cache_status_breakdown(connection, &period, "COALESCE(provider_id,'unknown') || '->' || COALESCE(upstream_protocol,'unknown')")?;
            let cache=serde_json::json!({"by_status":{"not_reported":not_reported,"reported":reported,"unknown_format":unknown},"cache_benefited_request_rate":ratio(values[27],total),"cache_benefited_requests":values[27],"cache_counter_coverage_rate":ratio(reported,total),"cache_counter_not_reported_requests":not_reported,"cache_counter_reported_requests":reported,"cache_counter_unknown_requests":unknown,"cache_eligible_input_tokens":read+write+cached,"cache_eligible_requests":reported,"cache_hit_ratio_known_only":ratio(read,read+write),"cache_read_tokens_canonical":read,"cache_write_rate":ratio(write,read+write),"cache_write_tokens_canonical":write,"inconsistent_cache_counter_rows":0,"input_tokens_total":values[24],"output_tokens_total":values[25],"per_account_status":per_account_status,"per_model_status":per_model_cache_status,"per_protocol_status":per_protocol_status,"provider_cache_hit_rate":Value::Null,"requests_total":total,"total_cache_creation_input_tokens":write,"total_cache_read_input_tokens":read,"total_cache_write_input_tokens":write,"total_cached_input_tokens":cached,"total_requests":total,"transcoded_requests":transcoded});
            let segmentation_statuses = ["segmented", "not_collected", "empty_request", "parse_failure"];
            let empty_segmentation = || serde_json::json!({"segmented":0,"not_collected":0,"empty_request":0,"parse_failure":0});
            let mut per_model = serde_json::Map::new();
            let mut model_query = connection.prepare("SELECT model_id, COALESCE(segmentation_status,'empty_request'), COUNT(*), COALESCE(SUM(COALESCE(stable_prefix_estimated_tokens,0)),0), COALESCE(SUM(COALESCE(volatile_estimated_tokens,0)),0) FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour') WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days') ELSE datetime('now','-24 hours') END AND started_at < datetime('now') AND status!='pending' GROUP BY model_id, segmentation_status ORDER BY COUNT(*) DESC, model_id LIMIT 100")?;
            for row in model_query.query_map([&period], |row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,i64>(2)?,row.get::<_,i64>(3)?,row.get::<_,i64>(4)?)))? {
                let (model,status,count,stable,volatile)=row?;
                let status=if segmentation_statuses.contains(&status.as_str()) {status} else {"parse_failure".to_owned()};
                let entry=per_model.entry(model.chars().take(128).collect::<String>()).or_insert_with(||serde_json::json!({"segmented":0,"not_collected":0,"empty_request":0,"parse_failure":0,"total_requests":0,"stable_prefix_estimated_tokens":0,"volatile_estimated_tokens":0}));
                entry[status]=serde_json::json!(entry[&status].as_i64().unwrap_or(0).saturating_add(count));
                entry["total_requests"]=serde_json::json!(entry["total_requests"].as_i64().unwrap_or(0).saturating_add(count));
                entry["stable_prefix_estimated_tokens"]=serde_json::json!(entry["stable_prefix_estimated_tokens"].as_i64().unwrap_or(0).saturating_add(stable));
                entry["volatile_estimated_tokens"]=serde_json::json!(entry["volatile_estimated_tokens"].as_i64().unwrap_or(0).saturating_add(volatile));
            }
            drop(model_query);
            let mut per_provider = serde_json::Map::new();
            let mut provider_query = connection.prepare("SELECT COALESCE(provider_id,'unknown'), COALESCE(upstream_protocol,'unknown'), COALESCE(segmentation_status,'empty_request'), COUNT(*) FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now','-1 hour') WHEN '7d' THEN datetime('now','-7 days') WHEN '30d' THEN datetime('now','-30 days') ELSE datetime('now','-24 hours') END AND started_at < datetime('now') AND status!='pending' GROUP BY provider_id, upstream_protocol, segmentation_status ORDER BY COUNT(*) DESC, provider_id, upstream_protocol LIMIT 100")?;
            for row in provider_query.query_map([&period], |row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,String>(2)?,row.get::<_,i64>(3)?)))? {
                let (provider,protocol,status,count)=row?;
                let status=if segmentation_statuses.contains(&status.as_str()) {status} else {"parse_failure".to_owned()};
                let key=format!("{}->{}",provider.chars().take(128).collect::<String>(),protocol.chars().take(32).collect::<String>());
                let entry=per_provider.entry(key).or_insert_with(empty_segmentation);
                entry[status]=serde_json::json!(entry[&status].as_i64().unwrap_or(0).saturating_add(count));
            }
            let segmentation=serde_json::json!({"total_requests":total,"by_status":{"segmented":values[8],"not_collected":values[9],"parse_failure":values[10],"empty_request":values[11]},"compressible_candidate_requests":values[23],"protected_requests":values[12],"byte_totals":{"all":values[16]+values[17]+values[18],"semi_stable":values[17],"stable_prefix":values[16],"volatile":values[18]},"token_totals":{"all":values[13]+values[14]+values[15],"semi_stable":values[14],"stable_prefix":values[13],"volatile":values[15]},"per_model_status":per_model,"per_provider_status":per_provider});
            let transcoding=serde_json::json!({"native_count":total.saturating_sub(transcoded),"per_direction":{},"top_loss_warnings":[],"total":total,"transcoded_count":transcoded});
            let stability=serde_json::json!({"notes":"Cache-stability tracking is per-request and in-memory on TranscodeContext.cache_boundary_tracker; durable summary counts are reported-only.","transcoded_request_count":transcoded});
            let shaping=serde_json::json!({"cache":{"cache_counter_known_rows":known,"cache_counter_reported_rate":ratio(reported,total),"cache_counter_reported_rows":reported,"cache_read_tokens":read,"cache_write_tokens":write,"cached_input_tokens":cached,"native_cache_observed_requests":values[26]},"guardrails":{"routing_uses_cache_metrics":false,"routing_uses_stable_prefix_hash":false},"mode":{"routing":"reporting_only"},"period":period,"segmentation":{"compressible_candidate_requests":values[23],"protected_requests":values[12],"requests_empty_request":values[11],"requests_not_collected":values[9],"requests_parse_failure":values[10],"requests_segmented":values[8]}});
            Ok(serde_json::json!({"transcoding":transcoding,"cache_observability":cache,"canonical_request_segmentation":segmentation,"cache_stability":stability,"request_shaping":shaping,"compression_summary":{"observed_requests":values[19],"candidate_count":values[20],"eligible_candidate_count":values[21],"suppressed_candidate_count":values[22]}}))
        }).await
    }

    pub async fn load(&self, period: &str) -> Result<DashboardData, DatabaseError> {
        let period = period.to_owned();
        self.database
            .call(move |connection| {
                let mut accounts = connection.prepare(&dashboard_sql(
                    "SELECT a.name, a.provider_id, a.enabled,\
                     COUNT(r.id),\
                     COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(r.cost_microdollars), 0),\
                     COALESCE(SUM(r.input_tokens), 0),\
                     COALESCE(SUM(r.output_tokens), 0),\
                     COALESCE(SUM(CASE WHEN r.exactness='exact' THEN 1 ELSE 0 END),0),\
                     COALESCE(SUM(CASE WHEN r.exactness='derived' THEN 1 ELSE 0 END),0),\
                     COALESCE(SUM(CASE WHEN r.exactness='partial' THEN 1 ELSE 0 END),0),\
                     COALESCE(SUM(CASE WHEN r.exactness='estimated' THEN 1 ELSE 0 END),0),\
                     COALESCE(SUM(CASE WHEN r.exactness='unknown' THEN 1 ELSE 0 END),0),\
                     COALESCE(SUM(CASE WHEN r.exactness='provider_reported' THEN 1 ELSE 0 END),0),\
                     COALESCE(AVG(r.upstream_latency_ms), 0),\
                     COALESCE((SELECT SUM(res.reserved_microdollars) FROM reservations res WHERE res.account_id = a.id AND res.status = 'active' AND res.released_at IS NULL), 0),\
                     (SELECT COUNT(*) FROM reservations res WHERE res.account_id = a.id AND res.status = 'active' AND res.released_at IS NULL),\
                     COALESCE(SUM(r.bytes_received), 0), COALESCE(SUM(r.bytes_emitted), 0),\
                     CASE WHEN COUNT(r.id) > 0 THEN CAST(COALESCE(SUM(CASE WHEN r.exactness = 'estimated' THEN 1 ELSE 0 END), 0) AS REAL) / COUNT(r.id) ELSE 0 END,\
                     CASE WHEN SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) > 0 THEN CAST(SUM(COALESCE(r.cache_read_tokens,0)) AS REAL) / SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) END,\
                     CASE WHEN SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) > 0 THEN CAST(SUM(COALESCE(r.cache_write_tokens,0)) AS REAL) / SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) END,\
                     CASE WHEN SUM(COALESCE(r.output_tokens,0)) > 0 THEN CAST(SUM(COALESCE(r.reasoning_tokens,0)) AS REAL) / SUM(COALESCE(r.output_tokens,0)) END,\
                     CASE WHEN COUNT(r.id) > 0 THEN CAST(CAST(SUM(COALESCE(r.cost_microdollars,0)) AS REAL) / COUNT(r.id) AS INTEGER) END,\
                     CASE WHEN SUM(COALESCE(r.input_tokens,0) + COALESCE(r.output_tokens,0)) > 0 THEN CAST(CAST(SUM(COALESCE(r.cost_microdollars,0)) AS REAL) * 1000.0 / SUM(COALESCE(r.input_tokens,0) + COALESCE(r.output_tokens,0)) AS INTEGER) END,\
                     COALESCE((SELECT SUM(w.cost_microdollars) / 5 FROM requests w WHERE w.account_id = a.id AND w.status != 'pending' AND w.started_at >= datetime('now', '-5 hours')), 0),\
                     COALESCE((SELECT SUM(w.cost_microdollars) / 168 FROM requests w WHERE w.account_id = a.id AND w.status != 'pending' AND w.started_at >= datetime('now', '-7 days')), 0),\
                     COALESCE((SELECT SUM(w.cost_microdollars) / 720 FROM requests w WHERE w.account_id = a.id AND w.status != 'pending' AND w.started_at >= datetime('now', '-30 days')), 0)\
                     FROM accounts a\
                     LEFT JOIN requests r ON r.account_id = a.id\
                       AND r.started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                         WHEN '7d' THEN datetime('now', '-7 days')\
                         WHEN '30d' THEN datetime('now', '-30 days')\
                         ELSE datetime('now', '-24 hours') END\
                       AND r.started_at < datetime('now')\
                     GROUP BY a.id, a.name, a.provider_id, a.enabled ORDER BY a.id",
                ))?;
                let accounts = accounts
                    .query_map([&period], |row| {
                        Ok(DashboardAccountRow {
                            name: row.get(0)?,
                            provider_id: row.get(1)?,
                            enabled: row.get::<_, i64>(2)? != 0,
                            requests: row.get(3)?,
                            errors: row.get(4)?,
                            cost_microdollars: row.get(5)?,
                            input_tokens: row.get(6)?,
                            output_tokens: row.get(7)?,
                            exact_count: row.get(8)?,
                            derived_count: row.get(9)?,
                            partial_count: row.get(10)?,
                            estimated_count: row.get(11)?,
                            unknown_count: row.get(12)?,
                            provider_reported_count: row.get(13)?,
                            avg_latency_ms: row.get(14)?,
                            reserved_microdollars: row.get(15)?,
                            active_reservations: row.get(16)?,
                            bytes_received: row.get(17)?,
                            bytes_emitted: row.get(18)?,
                            estimated_cost_fraction: row.get(19)?,
                            cache_read_ratio: row.get(20)?,
                            cache_write_ratio: row.get(21)?,
                            reasoning_output_ratio: row.get(22)?,
                            avg_cost_per_request: row.get(23)?,
                            avg_cost_per_1k_tokens: row.get(24)?,
                            utilization_5h: row.get(25)?,
                            utilization_7d: row.get(26)?,
                            utilization_30d: row.get(27)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut models = connection.prepare(&dashboard_sql(
                    "SELECT m.model_id, m.provider_id, m.resolution_status,\
                     COUNT(r.id),\
                     COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(r.cost_microdollars), 0),\
                     COALESCE(SUM(r.input_tokens), 0),\
                     COALESCE(SUM(r.output_tokens), 0),\
                     COALESCE(AVG(r.upstream_latency_ms), 0),\
                     COALESCE(SUM(CASE WHEN r.streamed = 1 THEN 1 ELSE 0 END), 0),\
                     COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)\
                     ,COALESCE(SUM(CASE WHEN r.exactness='exact' THEN 1 ELSE 0 END),0)\
                     ,COALESCE(SUM(CASE WHEN r.exactness='derived' THEN 1 ELSE 0 END),0)\
                     ,COALESCE(SUM(CASE WHEN r.exactness='partial' THEN 1 ELSE 0 END),0)\
                     ,COALESCE(SUM(CASE WHEN r.exactness='estimated' THEN 1 ELSE 0 END),0)\
                     ,COALESCE(SUM(CASE WHEN r.exactness='unknown' THEN 1 ELSE 0 END),0)\
                     ,COALESCE(SUM(CASE WHEN r.exactness='provider_reported' THEN 1 ELSE 0 END),0)\
                     ,CASE WHEN COUNT(r.id) > 0 THEN CAST(COALESCE(SUM(CASE WHEN r.exactness='estimated' THEN 1 ELSE 0 END), 0) AS REAL) / COUNT(r.id) ELSE 0 END\
                     ,CASE WHEN SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) > 0 THEN CAST(SUM(COALESCE(r.cache_read_tokens,0)) AS REAL) / SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) END\
                     ,CASE WHEN SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) > 0 THEN CAST(SUM(COALESCE(r.cache_write_tokens,0)) AS REAL) / SUM(COALESCE(r.input_tokens,0) + COALESCE(r.cache_read_tokens,0) + COALESCE(r.cache_write_tokens,0)) END\
                     ,CASE WHEN SUM(COALESCE(r.output_tokens,0)) > 0 THEN CAST(SUM(COALESCE(r.reasoning_tokens,0)) AS REAL) / SUM(COALESCE(r.output_tokens,0)) END\
                     ,CASE WHEN COUNT(r.id) > 0 THEN CAST(CAST(SUM(COALESCE(r.cost_microdollars,0)) AS REAL) / COUNT(r.id) AS INTEGER) END\
                     ,CASE WHEN SUM(COALESCE(r.input_tokens,0) + COALESCE(r.output_tokens,0)) > 0 THEN CAST(CAST(SUM(COALESCE(r.cost_microdollars,0)) AS REAL) * 1000.0 / SUM(COALESCE(r.input_tokens,0) + COALESCE(r.output_tokens,0)) AS INTEGER) END\
                     FROM models m\
                     LEFT JOIN requests r ON r.model_id = m.model_id\
                       AND r.started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                         WHEN '7d' THEN datetime('now', '-7 days')\
                         WHEN '30d' THEN datetime('now', '-30 days')\
                         ELSE datetime('now', '-24 hours') END\
                       AND r.started_at < datetime('now')\
                     GROUP BY m.model_id, m.provider_id, m.resolution_status\
                     ORDER BY m.model_id, m.provider_id",
                ))?;
                let models = models
                    .query_map([&period], |row| {
                        Ok(DashboardModelRow {
                            model_id: row.get(0)?,
                            provider_id: row.get(1)?,
                            resolution_status: row.get(2)?,
                            requests: row.get(3)?,
                            errors: row.get(4)?,
                            cost_microdollars: row.get(5)?,
                            input_tokens: row.get(6)?,
                            output_tokens: row.get(7)?,
                            avg_latency_ms: row.get(8)?,
                            ttft_requests: row.get(9)?,
                            avg_ttft_ms: row.get(10)?,
                            exact_count: row.get(11)?,
                            derived_count: row.get(12)?,
                            partial_count: row.get(13)?,
                            estimated_count: row.get(14)?,
                            unknown_count: row.get(15)?,
                            provider_reported_count: row.get(16)?,
                            estimated_cost_fraction: row.get(17)?,
                            cache_read_ratio: row.get(18)?,
                            cache_write_ratio: row.get(19)?,
                            reasoning_output_ratio: row.get(20)?,
                            avg_cost_per_request: row.get(21)?,
                            avg_cost_per_1k_tokens: row.get(22)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut latency_percentiles = connection.prepare(&dashboard_sql(
                    "WITH provider_ranked AS (\
                       SELECT provider_id, first_byte_ms,\
                         ROW_NUMBER() OVER (PARTITION BY provider_id ORDER BY first_byte_ms) AS rn,\
                         COUNT(*) OVER (PARTITION BY provider_id) AS n\
                       FROM requests WHERE streamed = 1 AND first_byte_ms IS NOT NULL\
                         AND started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                           WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                           ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')\
                     ), model_ranked AS (\
                       SELECT provider_id, model_id, first_byte_ms,\
                         ROW_NUMBER() OVER (PARTITION BY provider_id, model_id ORDER BY first_byte_ms) AS rn,\
                         COUNT(*) OVER (PARTITION BY provider_id, model_id) AS n\
                       FROM requests WHERE streamed = 1 AND first_byte_ms IS NOT NULL\
                         AND started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                           WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                           ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')\
                     )\
                     SELECT provider_id, '', COUNT(*),\
                       AVG(CASE WHEN rn IN ((n + 1) / 2, (n + 2) / 2) THEN first_byte_ms END),\
                       MAX(CASE WHEN rn = (99 * n + 99) / 100 THEN first_byte_ms END)\
                     FROM provider_ranked GROUP BY provider_id\
                     UNION ALL\
                     SELECT provider_id, model_id, COUNT(*),\
                       AVG(CASE WHEN rn IN ((n + 1) / 2, (n + 2) / 2) THEN first_byte_ms END),\
                       MAX(CASE WHEN rn = (99 * n + 99) / 100 THEN first_byte_ms END)\
                     FROM model_ranked GROUP BY provider_id, model_id\
                     ORDER BY 1, 2 LIMIT 400",
                ))?;
                let latency_percentiles = latency_percentiles
                    .query_map([&period], |row| {
                        Ok(DashboardLatencyPercentileRow {
                            provider_id: row.get(0)?,
                            model_id: row.get(1)?,
                            request_count: row.get(2)?,
                            p50_ttft_ms: row.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                            p99_ttft_ms: row.get::<_, Option<f64>>(4)?.unwrap_or(0.0),
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut requests = connection.prepare(&dashboard_sql(
                    "SELECT r.started_at, COALESCE(a.name, ''), COALESCE(r.provider_id, a.provider_id),\
                     r.model_id, r.status, r.status_code, r.upstream_latency_ms,\
                     COALESCE(r.input_tokens, 0), COALESCE(r.output_tokens, 0),\
                     r.error_class, r.error_message, COALESCE(r.protocol, 'openai'),\
                     r.proxy_request_id, COALESCE(r.reasoning_tokens, 0),\
                     COALESCE(r.thinking_characters, 0)\
                     FROM requests r LEFT JOIN accounts a ON a.id = r.account_id\
                     WHERE r.started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days')\
                       WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                       AND r.started_at < datetime('now')\
                     ORDER BY r.started_at DESC, r.id DESC LIMIT 500",
                ))?;
                let requests = requests
                    .query_map([&period], |row| {
                        Ok(DashboardRequestRow {
                            started_at: row.get(0)?,
                            account_name: row.get(1)?,
                            provider_id: row.get(2)?,
                            model_id: row.get(3)?,
                            status: row.get(4)?,
                            status_code: row.get(5)?,
                            latency_ms: row.get(6)?,
                            input_tokens: row.get(7)?,
                            output_tokens: row.get(8)?,
                            error_class: row.get(9)?,
                            error_message: row.get(10)?,
                            protocol: row.get(11)?,
                            proxy_request_id: row.get(12)?,
                            reasoning_tokens: row.get(13)?,
                            thinking_characters: row.get(14)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut events = connection.prepare(&dashboard_sql(
                    "SELECT e.created_at, COALESCE(a.name, ''), e.event_type, e.details\
                     FROM account_events e LEFT JOIN accounts a ON a.id = e.account_id\
                     ORDER BY e.created_at DESC, e.id DESC LIMIT 100",
                ))?;
                let events = events
                    .query_map([], |row| {
                        Ok(DashboardEventRow {
                            created_at: row.get(0)?,
                            account_name: row.get(1)?,
                            event_type: row.get(2)?,
                            details: row.get(3)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut operational_summary = connection.prepare(&dashboard_sql(
                    "SELECT event_type, COUNT(*), MAX(occurred_at),\
                     COALESCE(SUM(CAST(json_extract(CASE WHEN json_valid(details_json) THEN details_json ELSE '{}' END, '$.interrupted_requests') AS INTEGER)), 0),\
                     COALESCE(SUM(CAST(json_extract(CASE WHEN json_valid(details_json) THEN details_json ELSE '{}' END, '$.released_reservations') AS INTEGER)), 0)\
                     FROM operational_events WHERE occurred_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                     GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 25",
                ))?;
                let operational_summary = operational_summary
                    .query_map([&period], |row| {
                        Ok(DashboardOperationalSummaryRow {
                            event_type: row.get(0)?, event_count: row.get(1)?, last_seen: row.get(2)?,
                            interrupted_requests: row.get(3)?, released_reservations: row.get(4)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                let mut recent_operational_events = connection.prepare(&dashboard_sql(
                    "SELECT occurred_at, event_type, details_json FROM operational_events\
                     WHERE occurred_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                     ORDER BY occurred_at DESC, id DESC LIMIT 25",
                ))?;
                let recent_operational_events = recent_operational_events
                    .query_map([&period], |row| {
                        Ok(DashboardOperationalEventRow { occurred_at: row.get(0)?, event_type: row.get(1)?, details: row.get(2)? })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut pings = connection.prepare(&dashboard_sql(
                    "SELECT provider_id, account_name, probed_at, latency_ms, status_code, error, model_count\
                     FROM provider_pings WHERE probed_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days')\
                       WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                     ORDER BY probed_at DESC, id DESC LIMIT 100",
                ))?;
                let pings = pings
                    .query_map([&period], ping_from_row)?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut retries = connection.prepare(&dashboard_sql(
                    "SELECT COALESCE(retry_category, 'unknown'), COUNT(*),\
                     COALESCE(SUM(is_retry_outcome), 0),\
                     COALESCE(SUM(CASE WHEN status_code >= 200 AND status_code < 300 THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300) THEN 1 ELSE 0 END), 0),\
                     COALESCE(AVG(latency_ms), 0)\
                     FROM request_attempts\
                     WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days')\
                       WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                     GROUP BY COALESCE(retry_category, 'unknown')\
                     ORDER BY CASE COALESCE(retry_category, 'unknown')\
                       WHEN 'initial' THEN 0 WHEN 'success' THEN 1\
                       WHEN 'provider_error' THEN 2 WHEN 'failover' THEN 3 ELSE 4 END",
                ))?;
                let retries = retries
                    .query_map([&period], |row| {
                        Ok(DashboardRetryRow {
                            category: row.get(0)?,
                            attempts: row.get(1)?,
                            retry_outcomes: row.get(2)?,
                            successes: row.get(3)?,
                            failures: row.get(4)?,
                            avg_latency_ms: row.get(5)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut routing = connection.prepare(&dashboard_sql(
                    "SELECT model_id, COALESCE(provider_id, ''), COUNT(*),\
                     COALESCE(AVG(eligible_count), 0), COALESCE(AVG(scored_count), 0),\
                     COALESCE(AVG(attempted_excluded_count), 0), COALESCE(AVG(selected_score), 0),\
                     COUNT(DISTINCT selected_account_name)\
                     FROM routing_decisions\
                     WHERE decision_made_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days')\
                       WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                     GROUP BY model_id, provider_id ORDER BY model_id, provider_id",
                ))?;
                let routing = routing
                    .query_map([&period], |row| {
                        Ok(DashboardRoutingRow {
                            model_id: row.get(0)?,
                            provider_id: row.get(1)?,
                            decisions: row.get(2)?,
                            avg_eligible: row.get(3)?,
                            avg_scored: row.get(4)?,
                            avg_excluded: row.get(5)?,
                            avg_score: row.get(6)?,
                            distinct_accounts: row.get(7)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut routing_selection = connection.prepare(&dashboard_sql(
                    "SELECT selected_account_name, COALESCE(provider_id, ''), COUNT(*),\
                     COALESCE(AVG(selected_tier), 0), COALESCE(AVG(selected_score), 0),\
                     COALESCE(AVG(eligible_count), 0),\
                     (SELECT latest.selected_score FROM routing_decisions latest\
                       WHERE latest.selected_account_name = routing_decisions.selected_account_name\
                         AND COALESCE(latest.provider_id, '') = COALESCE(routing_decisions.provider_id, '')\
                         AND latest.decision_made_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                           WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                           ELSE datetime('now', '-24 hours') END\
                       ORDER BY latest.decision_made_at DESC, latest.id DESC LIMIT 1),\
                     (SELECT latest.selected_tier FROM routing_decisions latest\
                       WHERE latest.selected_account_name = routing_decisions.selected_account_name\
                         AND COALESCE(latest.provider_id, '') = COALESCE(routing_decisions.provider_id, '')\
                         AND latest.decision_made_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                           WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                           ELSE datetime('now', '-24 hours') END\
                       ORDER BY latest.decision_made_at DESC, latest.id DESC LIMIT 1),\
                     MAX(decision_made_at)\
                     FROM routing_decisions\
                     WHERE decision_made_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                       AND selected_account_name IS NOT NULL\
                     GROUP BY selected_account_name, provider_id\
                     ORDER BY COUNT(*) DESC, selected_account_name, provider_id LIMIT 100",
                ))?;
                let routing_selection = routing_selection
                    .query_map([&period], |row| {
                        Ok(DashboardRoutingSelectionRow {
                            account_name: row.get(0)?,
                            provider_id: row.get(1)?,
                            selection_count: row.get(2)?,
                            avg_selected_tier: row.get(3)?,
                            avg_selected_score: row.get(4)?,
                            avg_eligible_count: row.get(5)?,
                            last_selected_score: row.get(6)?,
                            last_selected_tier: row.get(7)?,
                            last_selected_at: row.get(8)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut timeseries = connection.prepare(&dashboard_sql(
                    "SELECT strftime('%Y-%m-%d %H:00:00', r.started_at),\
                     r.provider_id || ' / ' || r.model_id, r.provider_id, r.model_id, COUNT(*),\
                     COALESCE(SUM(r.cost_microdollars), 0),\
                     COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(r.input_tokens + r.output_tokens), 0),\
                     COALESCE(AVG(r.upstream_latency_ms), 0),\
                     COALESCE(SUM(r.input_tokens), 0), COALESCE(SUM(r.output_tokens), 0),\
                     COALESCE(SUM(r.cache_read_tokens), 0), COALESCE(SUM(r.cache_write_tokens), 0),\
                     COALESCE(SUM(r.reasoning_tokens), 0), COALESCE(SUM(r.bytes_received), 0),\
                     COALESCE(SUM(r.bytes_emitted), 0), COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)\
                     FROM requests r\
                     WHERE r.started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days')\
                       WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END\
                       AND r.started_at < datetime('now')\
                     GROUP BY 1, 2, 3, 4 ORDER BY 1 ASC, 2 LIMIT 200",
                ))?;
                let timeseries = timeseries
                    .query_map([&period], |row| {
                        Ok(DashboardTimeseriesRow {
                            bucket: row.get(0)?,
                            series: row.get(1)?,
                            provider_id: row.get(2)?,
                            model_id: row.get(3)?,
                            requests: row.get(4)?,
                            cost_microdollars: row.get(5)?,
                            errors: row.get(6)?,
                            total_tokens: row.get(7)?,
                            avg_latency_ms: row.get(8)?,
                            input_tokens: row.get(9)?,
                            output_tokens: row.get(10)?,
                            cache_read_tokens: row.get(11)?,
                            cache_write_tokens: row.get(12)?,
                            reasoning_tokens: row.get(13)?,
                            bytes_received: row.get(14)?,
                            bytes_emitted: row.get(15)?,
                            avg_ttft_ms: row.get(16)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut ip_stats = connection.prepare(&dashboard_sql(
                    "SELECT COALESCE(NULLIF(client_ip, ''), 'unknown'), COUNT(*),\
                     COALESCE(SUM(cost_microdollars), 0), COALESCE(AVG(upstream_latency_ms), 0),\
                     COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),\
                     COUNT(DISTINCT model_id)\
                     FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')\
                     GROUP BY COALESCE(NULLIF(client_ip, ''), 'unknown')\
                     ORDER BY COUNT(*) DESC, COALESCE(NULLIF(client_ip, ''), 'unknown') LIMIT 10",
                ))?;
                let ip_stats = ip_stats
                    .query_map([&period], |row| {
                        Ok(DashboardIpRow {
                            client_ip: row.get(0)?,
                            requests: row.get(1)?,
                            cost_microdollars: row.get(2)?,
                            avg_latency_ms: row.get(3)?,
                            errors: row.get(4)?,
                            input_tokens: row.get(5)?,
                            output_tokens: row.get(6)?,
                            unique_models: row.get(7)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let mut token_activity = connection.prepare(
                    "SELECT date(bucket_start), COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0), COALESCE(SUM(request_count), 0), COALESCE(SUM(bytes_received), 0), COALESCE(SUM(bytes_emitted), 0)\
                     FROM usage_rollups WHERE bucket_start >= datetime('now', '-180 days')\
                     GROUP BY date(bucket_start) ORDER BY date(bucket_start) LIMIT 180",
                )?;
                let token_activity = token_activity
                    .query_map([], |row| {
                        Ok(DashboardTokenActivityRow {
                            day: row.get(0)?,
                            total_tokens: row.get(1)?,
                            requests: row.get(2)?,
                            bytes_received: row.get(3)?,
                            bytes_emitted: row.get(4)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                let cache = connection.query_row(
                    &dashboard_sql("SELECT\
                     COALESCE(SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN cache_write_tokens > 0 THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN reasoning_tokens > 0 THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(bytes_received), 0), COALESCE(SUM(bytes_emitted), 0)\
                     FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour')\
                       WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days')\
                       ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')"),
                    [&period],
                    |row| {
                        Ok(DashboardCacheSummary {
                            rows_with_read: row.get(0)?,
                            rows_with_write: row.get(1)?,
                            rows_with_reasoning: row.get(2)?,
                            total_bytes_received: row.get(3)?,
                            total_bytes_emitted: row.get(4)?,
                        })
                    },
                )?;

                let pending_requests = connection.query_row(
                    "SELECT COUNT(*) FROM requests WHERE status = 'pending'",
                    [],
                    |row| row.get(0),
                )?;
                let (active_reservations, active_reserved_microdollars) = connection.query_row(
                    "SELECT COUNT(*), COALESCE(SUM(reserved_microdollars), 0) FROM reservations WHERE status = 'active' AND released_at IS NULL",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )?;
                let finalizer_cleaned_24h = connection.query_row(
                    "SELECT COALESCE(SUM(CAST(json_extract(details_json, '$.leaked_requests') AS INTEGER)), 0) FROM operational_events WHERE event_type = 'stale_request_finalizer' AND occurred_at >= datetime('now', '-24 hours')",
                    [],
                    |row| row.get(0),
                )?;
                let crash_recovery_24h = connection.query_row(
                    "SELECT COUNT(*) FROM operational_events WHERE event_type = 'crash_recovery' AND occurred_at >= datetime('now', '-24 hours')",
                    [],
                    |row| row.get(0),
                )?;

                Ok(DashboardData {
                    pending_requests,
                    active_reservations,
                    active_reserved_microdollars,
                    finalizer_cleaned_24h,
                    crash_recovery_24h,
                    accounts,
                    models,
                    latency_percentiles,
                    requests,
                    events,
                    operational_summary,
                    recent_operational_events,
                    pings,
                    retries,
                    routing,
                    routing_selection,
                    timeseries,
                    ip_stats,
                    token_activity,
                    cache,
                })
            })
            .await
    }
}

#[derive(Debug, Clone)]
pub struct AccountRepository {
    database: Database,
}

impl AccountRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn get_by_name(&self, name: &str) -> Result<Option<Account>, DatabaseError> {
        let name = name.to_owned();
        self.database
            .call(move |connection| {
                connection
                    .query_row(
                        "SELECT id, name, api_key_env, enabled, weight, provider_id\n\
                 FROM accounts WHERE name = ?1",
                        [name],
                        account_from_row,
                    )
                    .optional()
            })
            .await
    }

    pub async fn list_enabled(&self) -> Result<Vec<Account>, DatabaseError> {
        self.database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT id, name, api_key_env, enabled, weight, provider_id\n\
                 FROM accounts WHERE enabled = 1 ORDER BY id",
                )?;
                statement.query_map([], account_from_row)?.collect()
            })
            .await
    }

    /// Return every durable account in stable id order, including disabled rows.
    pub async fn list_all(&self) -> Result<Vec<Account>, DatabaseError> {
        self.database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT id, name, api_key_env, enabled, weight, provider_id\n\
                     FROM accounts ORDER BY id",
                )?;
                statement.query_map([], account_from_row)?.collect()
            })
            .await
    }

    pub async fn sync_from_config(
        &self,
        accounts: Vec<AccountConfig>,
    ) -> Result<Vec<(String, i64)>, DatabaseError> {
        self.database.with_transaction(move |connection| {
            for account in &accounts {
                connection.execute(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id)\n\
                     VALUES (?1, ?2, ?3, ?4, ?5)\n\
                     ON CONFLICT(name) DO UPDATE SET api_key_env = excluded.api_key_env,\n\
                       enabled = excluded.enabled, weight = excluded.weight,\n\
                       provider_id = excluded.provider_id",
                    params![
                        account.name,
                        account.api_key_env,
                        account.enabled as i64,
                        account.weight,
                        account.provider_id,
                    ],
                )?;
            }
            if accounts.is_empty() {
                connection.execute("UPDATE accounts SET enabled = 0 WHERE enabled = 1", [])?;
            } else {
                let placeholders = (1..=accounts.len()).map(|index| format!("?{index}")).collect::<Vec<_>>().join(", ");
                let names = accounts.iter().map(|account| account.name.as_str()).collect::<Vec<_>>();
                connection.execute(
                    &format!("UPDATE accounts SET enabled = 0 WHERE enabled = 1 AND name NOT IN ({placeholders})"),
                    params_from_names(&names),
                )?;
            }
            let ids: Vec<i64> = accounts.iter().map(|account| {
                connection.query_row("SELECT id FROM accounts WHERE name = ?1", [&account.name], |row| row.get(0))
            }).collect::<Result<_, _>>()?;
            Ok(accounts.iter().zip(ids).map(|(account, id)| (account.name.clone(), id)).collect())
        }).await
    }
}

#[derive(Debug, Clone)]
pub struct ModelRepository {
    database: Database,
}

impl ModelRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn list(&self, provider_id: Option<&str>) -> Result<Vec<Model>, DatabaseError> {
        let provider_id = provider_id.map(str::to_owned);
        self.database
            .call(move |connection| {
                let mut statement = connection.prepare(
                    "SELECT model_id, display_name, protocol, provider_id, resolution_status\n\
                 FROM models\n\
                 WHERE (?1 IS NULL OR provider_id = ?1)\n\
                 ORDER BY model_id, provider_id",
                )?;
                statement
                    .query_map([provider_id], model_from_row)?
                    .collect()
            })
            .await
    }

    pub async fn get(
        &self,
        model_id: &str,
        provider_id: &str,
    ) -> Result<Option<Model>, DatabaseError> {
        let model_id = model_id.to_owned();
        let provider_id = provider_id.to_owned();
        self.database
            .call(move |connection| {
                connection
                    .query_row(
                        "SELECT model_id, display_name, protocol, provider_id, resolution_status\n\
                 FROM models WHERE model_id = ?1 AND provider_id = ?2",
                        params![model_id, provider_id],
                        model_from_row,
                    )
                    .optional()
            })
            .await
    }
}

/// Read-plane access to the existing catalog tables. No migration is owned by
/// this repository; all SQL targets the canonical schema-54 tables.
#[derive(Debug, Clone)]
pub struct CatalogRepository {
    database: Database,
}

impl CatalogRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn list_models(&self) -> Result<Vec<CatalogModel>, DatabaseError> {
        self.database.call(|connection| {
            let mut statement = connection.prepare(
                "SELECT model_id, display_name, protocol, capabilities, source_metadata,\n\
                        protocol_source, CAST(first_seen_at AS TEXT), CAST(last_seen_at AS TEXT), resolution_status, provider_id\n\
                 FROM models ORDER BY model_id",
            )?;
            statement.query_map([], catalog_model_from_row)?.collect()
        }).await
    }

    pub async fn list_provider_models(&self) -> Result<Vec<ProviderModelMetadata>, DatabaseError> {
        self.database.call(|connection| {
            let mut statement = connection.prepare(
                    "SELECT model_id, provider_id, display_name, protocol, capabilities, source_metadata,\n\
                        protocol_source, CAST(first_seen_at AS TEXT), CAST(last_seen_at AS TEXT), resolution_status\n\
                 FROM provider_model_metadata ORDER BY model_id, provider_id",
            )?;
            statement.query_map([], provider_model_from_row)?.collect()
        }).await
    }

    pub async fn list_account_model_support(
        &self,
    ) -> Result<Vec<AccountModelSupport>, DatabaseError> {
        self.database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT account_id, model_id, enabled FROM account_models\n\
                 ORDER BY account_id, model_id",
                )?;
                statement
                    .query_map([], account_model_support_from_row)?
                    .collect()
            })
            .await
    }

    pub async fn list_refresh_state(&self) -> Result<Vec<CatalogRefreshState>, DatabaseError> {
        self.database.call(|connection| {
            let mut statement = connection.prepare(
                "SELECT account_id, provider_id, CAST(last_successful_refresh_at AS TEXT), last_outcome, model_count\n\
                 FROM catalog_refresh_state ORDER BY account_id",
            )?;
            statement.query_map([], refresh_state_from_row)?.collect()
        }).await
    }

    /// Apply one catalog refresh's semantic state in a single schema-54
    /// transaction. The caller supplies the already-hydrated durable rows so
    /// semantic comparison remains outside the transaction while all writes
    /// stay behind the typed catalog repository boundary.
    pub async fn apply_persistence_batch(
        &self,
        existing_models: Vec<CatalogModel>,
        existing_provider_models: Vec<ProviderModelMetadata>,
        existing_support: Vec<AccountModelSupport>,
        batch: CatalogPersistenceBatch,
    ) -> Result<(), DatabaseError> {
        self.database.with_transaction(move |connection| {
            let desired_model_ids: BTreeSet<String> = batch
                .models
                .iter()
                .map(|row| row.model_id.clone())
                .collect();
            let desired_provider_keys: BTreeSet<(String, String)> = batch
                .provider_models
                .iter()
                .map(|row| (row.model_id.clone(), row.provider_id.clone()))
                .collect();
            let existing_model_ids: BTreeSet<String> = existing_models
                .iter()
                .map(|row| row.model_id.clone())
                .filter(|id| id != "__deprecated__")
                .collect();
            let existing_provider_keys: BTreeSet<(String, String)> = existing_provider_models
                .iter()
                .map(|row| (row.model_id.clone(), row.provider_id.clone()))
                .collect();
            let existing_model_map: BTreeMap<_, _> = existing_models
                .into_iter()
                .map(|row| (row.model_id.clone(), row))
                .collect();
            for row in &batch.models {
                let capabilities = canonical_json(&row.capabilities);
                let source_metadata = canonical_json(&row.source_metadata);
                match existing_model_map.get(&row.model_id) {
                    None => {
                        connection.execute(
                            "INSERT INTO models (model_id, display_name, protocol, capabilities, source_metadata, first_seen_at, last_seen_at, protocol_source, resolution_status) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'resolved')",
                            params![row.model_id, row.display_name, row.protocol, capabilities, source_metadata, timestamp(row.first_seen_at), timestamp(row.last_seen_at), row.protocol_source.as_deref().filter(|value| *value != "unresolved")],
                        )?;
                    }
                    Some(old)
                        if (
                            old.display_name.as_ref(),
                            old.protocol.as_str(),
                            canonical_stored(&old.capabilities),
                            canonical_stored(&old.source_metadata),
                            old.protocol_source.as_ref(),
                            old.resolution_status.as_str(),
                        ) != (
                            row.display_name.as_ref(),
                            row.protocol.as_str(),
                            capabilities.clone(),
                            source_metadata.clone(),
                            row.protocol_source.as_ref(),
                            "resolved",
                        ) =>
                    {
                        connection.execute(
                            "UPDATE models SET display_name = ?1, protocol = ?2, capabilities = ?3, source_metadata = ?4, last_seen_at = ?5, protocol_source = ?6, resolution_status = 'resolved' WHERE model_id = ?7",
                            params![row.display_name, row.protocol, capabilities, source_metadata, timestamp(row.last_seen_at), row.protocol_source.as_deref().filter(|value| *value != "unresolved"), row.model_id],
                        )?;
                    }
                    _ => {}
                }
            }
            let existing_provider_map: BTreeMap<_, _> = existing_provider_models
                .into_iter()
                .map(|row| ((row.model_id.clone(), row.provider_id.clone()), row))
                .collect();
            for row in &batch.provider_models {
                let capabilities = canonical_json(&row.capabilities);
                let source_metadata = canonical_json(&row.source_metadata);
                if let Some(old) = existing_provider_map
                    .get(&(row.model_id.clone(), row.provider_id.clone()))
                {
                    let old_key = (
                        old.display_name.as_ref(),
                        old.protocol.as_ref(),
                        canonical_stored(&old.capabilities),
                        canonical_stored(&old.source_metadata),
                        old.protocol_source.as_ref(),
                        old.resolution_status.as_str(),
                    );
                    let new_key = (
                        row.display_name.as_ref(),
                        row.protocol.as_ref(),
                        capabilities.clone(),
                        source_metadata.clone(),
                        row.protocol_source.as_ref(),
                        row.resolution_status.as_str(),
                    );
                    if old_key != new_key {
                        connection.execute(
                            "UPDATE provider_model_metadata SET display_name = ?1, protocol = ?2, capabilities = ?3, source_metadata = ?4, protocol_source = ?5, last_seen_at = ?6, resolution_status = ?7 WHERE model_id = ?8 AND provider_id = ?9",
                            params![row.display_name, row.protocol, capabilities, source_metadata, row.protocol_source, timestamp(row.last_seen_at), row.resolution_status, row.model_id, row.provider_id],
                        )?;
                    }
                } else {
                    connection.execute(
                        "INSERT INTO provider_model_metadata (model_id, provider_id, display_name, protocol, capabilities, source_metadata, protocol_source, first_seen_at, last_seen_at, resolution_status) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                        params![row.model_id, row.provider_id, row.display_name, row.protocol, capabilities, source_metadata, row.protocol_source, timestamp(row.first_seen_at), timestamp(row.last_seen_at), row.resolution_status],
                    )?;
                }
            }
            let old_support: BTreeSet<(i64, String)> = existing_support
                .into_iter()
                .filter(|row| row.enabled)
                .map(|row| (row.account_id, row.model_id))
                .collect();
            for (account_id, model_id) in batch.support.difference(&old_support) {
                connection.execute(
                    "INSERT INTO account_models (account_id, model_id, enabled) VALUES (?1, ?2, 1) ON CONFLICT(account_id, model_id) DO UPDATE SET enabled = 1",
                    params![account_id, model_id],
                )?;
            }
            for (account_id, model_id) in old_support.difference(&batch.support) {
                connection.execute(
                    "UPDATE account_models SET enabled = 0 WHERE account_id = ?1 AND model_id = ?2 AND enabled = 1",
                    params![account_id, model_id],
                )?;
            }
            for refresh in &batch.refresh {
                connection.execute(
                    "INSERT INTO catalog_refresh_state (account_id, provider_id, last_successful_refresh_at, last_outcome, model_count) VALUES (?1, ?2, ?3, ?4, ?5) ON CONFLICT(account_id) DO UPDATE SET provider_id = excluded.provider_id, last_successful_refresh_at = excluded.last_successful_refresh_at, last_outcome = excluded.last_outcome, model_count = excluded.model_count",
                    params![refresh.account_id, refresh.provider_id, timestamp(refresh.refreshed_at), refresh.outcome, refresh.model_count],
                )?;
            }
            for ping in &batch.pings {
                connection.execute(
                    "INSERT INTO provider_pings (provider_id, account_name, latency_ms, status_code, error, model_count) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![ping.provider_id, ping.account_name, ping.latency_ms, ping.status_code, ping.error, ping.model_count],
                )?;
            }
            let stale_models = existing_model_ids
                .difference(&desired_model_ids)
                .cloned()
                .collect::<Vec<_>>();
            if !stale_models.is_empty() {
                connection.execute(
                    "INSERT OR IGNORE INTO models (model_id, display_name, protocol, resolution_status, provider_id) VALUES ('__deprecated__', 'Deprecated models', 'openai', 'resolved', 'opencode-go')",
                    [],
                )?;
                for model_id in stale_models {
                    connection.execute(
                        "UPDATE requests SET original_model_id = model_id, model_id = '__deprecated__' WHERE model_id = ?1",
                        [&model_id],
                    )?;
                    connection.execute(
                        "UPDATE reservations SET original_model_id = model_id, model_id = '__deprecated__' WHERE model_id = ?1",
                        [&model_id],
                    )?;
                    connection.execute("DELETE FROM account_models WHERE model_id = ?1", [&model_id])?;
                    connection.execute(
                        "DELETE FROM provider_model_metadata WHERE model_id = ?1",
                        [&model_id],
                    )?;
                    connection.execute("DELETE FROM models WHERE model_id = ?1", [&model_id])?;
                }
            }
            for (model_id, provider_id) in existing_provider_keys.difference(&desired_provider_keys) {
                connection.execute(
                    "DELETE FROM provider_model_metadata WHERE model_id = ?1 AND provider_id = ?2",
                    params![model_id, provider_id],
                )?;
            }
            Ok(())
        }).await
    }
}

fn canonical_json(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "{}".into())
}

fn canonical_stored(value: &str) -> String {
    serde_json::from_str::<Value>(value)
        .map_or_else(|_| value.to_owned(), |value| canonical_json(&value))
}

fn timestamp(value: i64) -> String {
    value.to_string()
}

#[derive(Debug, Clone)]
pub struct RequestRepository {
    database: Database,
}

impl RequestRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn get_by_id(&self, id: i64) -> Result<Option<Request>, DatabaseError> {
        self.database
            .call(move |connection| {
                connection
                    .query_row(&request_sql("WHERE id = ?1"), [id], request_from_row)
                    .optional()
            })
            .await
    }

    pub async fn list_recent(&self, limit: u32) -> Result<Vec<Request>, DatabaseError> {
        let limit = i64::from(limit.min(1_000));
        self.database
            .call(move |connection| {
                let mut statement = connection.prepare(&format!(
                    "{} ORDER BY started_at DESC, id DESC LIMIT ?1",
                    request_sql("")
                ))?;
                statement.query_map([limit], request_from_row)?.collect()
            })
            .await
    }

    pub async fn create_pending(
        &self,
        account_id: i64,
        model_id: String,
        protocol: String,
        provider_id: String,
        proxy_request_id: String,
        streamed: bool,
    ) -> Result<i64, DatabaseError> {
        self.database.with_transaction(move |connection| {
            connection.execute(
                "INSERT INTO requests\n\
                 (account_id, model_id, status, protocol, streamed, proxy_request_id, provider_id)\n\
                 VALUES (?1, ?2, 'pending', ?3, ?4, ?5, ?6)",
                params![account_id, model_id, protocol, streamed as i64, proxy_request_id, provider_id],
            )?;
            Ok(connection.last_insert_rowid())
        }).await
    }

    pub async fn complete(
        &self,
        id: i64,
        status: String,
        input_tokens: i64,
        output_tokens: i64,
        cost_microdollars: i64,
    ) -> Result<bool, DatabaseError> {
        self.database
            .with_transaction(move |connection| {
                let changed = connection.execute(
                    "UPDATE requests SET status = ?1, completed_at = CURRENT_TIMESTAMP,\n\
                 input_tokens = ?2, output_tokens = ?3, cost_microdollars = ?4\n\
                 WHERE id = ?5 AND status = 'pending'",
                    params![status, input_tokens, output_tokens, cost_microdollars, id],
                )?;
                Ok(changed == 1)
            })
            .await
    }
}

#[derive(Debug, Clone)]
pub struct PingRepository {
    database: Database,
}

impl PingRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn recent(
        &self,
        provider_id: Option<&str>,
        limit: u32,
    ) -> Result<Vec<Ping>, DatabaseError> {
        let provider_id = provider_id.map(str::to_owned);
        let limit = i64::from(limit.min(1_000));
        self.database.call(move |connection| {
            let mut statement = connection.prepare(
                "SELECT provider_id, account_name, probed_at, latency_ms, status_code, error, model_count\n\
                 FROM provider_pings WHERE (?1 IS NULL OR provider_id = ?1)\n\
                 ORDER BY probed_at DESC, id DESC LIMIT ?2",
            )?;
            statement.query_map(params![provider_id, limit], ping_from_row)?.collect()
        }).await
    }

    /// Return the latest ping per `(provider_id, account_name)` pair.
    /// One row per configured account at most, so the result stays bounded
    /// without loading ping history. Raw `error` text stays in the row and
    /// must be classified to a bounded category before display.
    pub async fn latest_grouped(&self) -> Result<BTreeMap<(String, String), Ping>, DatabaseError> {
        let rows: Vec<Ping> = self
            .database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT provider_id, account_name, probed_at, latency_ms, status_code, error, model_count\n\
                     FROM provider_pings WHERE id IN\n\
                     (SELECT MAX(id) FROM provider_pings GROUP BY provider_id, account_name)",
                )?;
                statement.query_map([], ping_from_row)?.collect()
            })
            .await?;
        Ok(rows
            .into_iter()
            .map(|ping| ((ping.provider_id.clone(), ping.account_name.clone()), ping))
            .collect())
    }

    pub async fn record(
        &self,
        provider_id: String,
        account_name: String,
        latency_ms: Option<i64>,
        status_code: Option<i64>,
        error: Option<String>,
        model_count: i64,
    ) -> Result<i64, DatabaseError> {
        self.database
            .with_transaction(move |connection| {
                connection.execute(
                    "INSERT INTO provider_pings\n\
                 (provider_id, account_name, latency_ms, status_code, error, model_count)\n\
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        provider_id,
                        account_name,
                        latency_ms,
                        status_code,
                        error,
                        model_count
                    ],
                )?;
                Ok(connection.last_insert_rowid())
            })
            .await
    }
}

#[derive(Debug, Clone)]
pub struct UsageRollupRepository {
    database: Database,
}

/// One account's persisted 5h/7d/30d usage values for routing.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct UsageWindowSnapshot {
    pub cost_5h: i64,
    pub cost_7d: i64,
    pub cost_30d: i64,
    pub request_count_5h: i64,
    pub request_count_7d: i64,
    pub request_count_30d: i64,
    pub token_count_5h: i64,
    pub token_count_7d: i64,
    pub token_count_30d: i64,
}

/// Read-only usage window aggregation used by quota hydration.
#[derive(Debug, Clone)]
pub struct UsageWindowRepository {
    database: Database,
}

impl UsageWindowRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    /// Fetch every account's three horizons in one bounded SQL read.
    pub async fn get_all_usage_windows(
        &self,
        now_iso: &str,
    ) -> Result<BTreeMap<i64, UsageWindowSnapshot>, DatabaseError> {
        let now_iso = now_iso.to_owned();
        self.database
            .call(move |connection| {
                let mut statement = connection.prepare(
                    "SELECT account_id,\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?1, '-5 hours') THEN CAST(cost_microdollars AS REAL) ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?2, '-7 days') THEN CAST(cost_microdollars AS REAL) ELSE 0 END), 0),\
                     COALESCE(SUM(CAST(cost_microdollars AS REAL)), 0),\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?3, '-5 hours') THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?4, '-7 days') THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?5, '-30 days') THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?6, '-5 hours') THEN CAST(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0) AS REAL) ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN started_at >= datetime(?7, '-7 days') THEN CAST(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0) AS REAL) ELSE 0 END), 0),\
                     COALESCE(SUM(CAST(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0) AS REAL)), 0)\
                     FROM requests \
                     WHERE status != 'pending' \
                       AND started_at >= datetime(?8, '-30 days')\
                     GROUP BY account_id",
                )?;
                let rows = statement.query_map(
                    params![
                        &now_iso, &now_iso, &now_iso, &now_iso,
                        &now_iso, &now_iso, &now_iso, &now_iso,
                    ],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            UsageWindowSnapshot {
                                cost_5h: clamp_sqlite_aggregate(row.get::<_, f64>(1)?),
                                cost_7d: clamp_sqlite_aggregate(row.get::<_, f64>(2)?),
                                cost_30d: clamp_sqlite_aggregate(row.get::<_, f64>(3)?),
                                request_count_5h: clamp_sqlite_aggregate(row.get::<_, f64>(4)?),
                                request_count_7d: clamp_sqlite_aggregate(row.get::<_, f64>(5)?),
                                request_count_30d: clamp_sqlite_aggregate(row.get::<_, f64>(6)?),
                                token_count_5h: clamp_sqlite_aggregate(row.get::<_, f64>(7)?),
                                token_count_7d: clamp_sqlite_aggregate(row.get::<_, f64>(8)?),
                                token_count_30d: clamp_sqlite_aggregate(row.get::<_, f64>(9)?),
                            },
                        ))
                    },
                )?;
                rows.collect()
            })
            .await
    }
}

fn clamp_sqlite_aggregate(value: f64) -> i64 {
    if !value.is_finite() || value <= 0.0 {
        0
    } else {
        value.min(i64::MAX as f64) as i64
    }
}

impl UsageRollupRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn summary(&self, start: &str, end: &str) -> Result<UsageSummary, DatabaseError> {
        let start = start.to_owned();
        let end = end.to_owned();
        self.database
            .call(move |connection| {
                connection.query_row(
                    "SELECT COALESCE(SUM(request_count), 0), COALESCE(SUM(error_count), 0),\n\
                 COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),\n\
                 COALESCE(SUM(cost_microdollars), 0),\n\
                 COALESCE(SUM(CASE WHEN streamed = 1 THEN request_count ELSE 0 END), 0),\n\
                 CASE WHEN COALESCE(SUM(request_count), 0) > 0\n\
                   THEN CAST(SUM(latency_ms_sum) AS REAL) / SUM(request_count) ELSE 0 END\n\
                 FROM usage_rollups WHERE bucket_start >= ?1 AND bucket_start < ?2",
                    params![start, end],
                    |row| {
                        Ok(UsageSummary {
                            total_requests: row.get(0)?,
                            error_requests: row.get(1)?,
                            input_tokens: row.get(2)?,
                            output_tokens: row.get(3)?,
                            cost_microdollars: row.get(4)?,
                            streamed_requests: row.get(5)?,
                            avg_latency_ms: row.get(6)?,
                        })
                    },
                )
            })
            .await
    }

    pub async fn dashboard_summary(
        &self,
        start: &str,
        end: &str,
    ) -> Result<DashboardSummary, DatabaseError> {
        let start = start.to_owned();
        let end = end.to_owned();
        self.database
            .call(move |connection| {
                connection.query_row(
                    "SELECT COUNT(*),\
                     COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),\
                     COALESCE(SUM(cost_microdollars), 0), COALESCE(AVG(upstream_latency_ms), 0),\
                     COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_write_tokens), 0),\
                     COALESCE(SUM(reasoning_tokens), 0),\
                     COALESCE(SUM(CASE WHEN streamed = 1 THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN streamed = 0 THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'exact' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'derived' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'partial' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'unknown' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN cost_microdollars ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN cost_microdollars ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'estimated'\
                       AND reserved_microdollars IS NOT NULL\
                       AND cost_microdollars = reserved_microdollars\
                       AND local_cost_microdollars IS NOT NULL\
                       AND local_cost_microdollars > 0\
                       AND local_cost_microdollars < cost_microdollars THEN 1 ELSE 0 END), 0),\
                     COALESCE(SUM(CASE WHEN exactness = 'estimated'\
                       AND reserved_microdollars IS NOT NULL\
                       AND cost_microdollars = reserved_microdollars\
                       AND local_cost_microdollars IS NOT NULL\
                       AND local_cost_microdollars > 0\
                       AND local_cost_microdollars < cost_microdollars\
                       THEN cost_microdollars - local_cost_microdollars ELSE 0 END), 0),\
                     COALESCE(SUM(bytes_received), 0), COALESCE(SUM(bytes_emitted), 0),\
                     (SELECT COUNT(DISTINCT provider_id) FROM accounts),\
                     COALESCE(AVG(CASE WHEN streamed = 1 THEN first_byte_ms END), 0),\
                     CASE WHEN COALESCE(SUM(CASE WHEN status != 'pending' THEN upstream_latency_ms ELSE 0 END), 0) > 0\
                       THEN CAST(SUM(CASE WHEN status != 'pending' THEN output_tokens ELSE 0 END) AS REAL) * 1000.0\
                         / SUM(CASE WHEN status != 'pending' THEN upstream_latency_ms ELSE 0 END)\
                       ELSE 0 END, 0, 0\
                     FROM requests\n\
                     WHERE started_at >= CASE ?1\n\
                       WHEN '1h' THEN datetime('now', '-1 hour')\n\
                       WHEN '24h' THEN datetime('now', '-24 hours')\n\
                       WHEN '7d' THEN datetime('now', '-7 days')\n\
                       WHEN '30d' THEN datetime('now', '-30 days')\n\
                       ELSE ?1 END\n\
                       AND started_at < CASE WHEN ?2 = 'now' THEN datetime('now') ELSE ?2 END",
                    params![start, end],
                    |row| {
                        let total_requests: i64 = row.get(0)?;
                        let error_requests: i64 = row.get(2)?;
                        let input_tokens: i64 = row.get(3)?;
                        let output_tokens: i64 = row.get(4)?;
                        let cache_read: i64 = row.get(7)?;
                        let cache_write: i64 = row.get(8)?;
                        Ok(DashboardSummary {
                            total_requests,
                            successful_requests: row.get(1)?,
                            error_requests,
                            total_input_tokens: input_tokens,
                            total_output_tokens: output_tokens,
                            total_cost_microdollars: row.get(5)?,
                            avg_latency_ms: row.get(6)?,
                            total_cache_read_tokens: cache_read,
                            total_cache_write_tokens: cache_write,
                            total_reasoning_tokens: row.get(9)?,
                            streamed_requests: row.get(10)?,
                            non_streamed_requests: row.get(11)?,
                            exact_count: row.get(12)?,
                            derived_count: row.get(13)?,
                            partial_count: row.get(14)?,
                            estimated_count: row.get(15)?,
                            unknown_count: row.get(16)?,
                            provider_reported_count: row.get(17)?,
                            provider_reported_cost_microdollars: row.get(18)?,
                            estimated_cost_sum_microdollars: row.get(19)?,
                            reservation_fallback_rows: row.get(20)?,
                            reservation_fallback_excess_microdollars: row.get(21)?,
                            total_bytes_received: row.get(22)?,
                            total_bytes_emitted: row.get(23)?,
                            total_providers: row.get(24)?,
                            avg_ttft_ms: row.get(25)?,
                            tokens_per_second: row.get(26)?,
                            p50_ttft_ms: row.get(27)?,
                            p99_ttft_ms: row.get(28)?,
                        })
                    },
                )
            })
            .await
    }

    /// Read the compact request summary without depending on optional,
    /// newer observability columns. The full compatibility shape is returned
    /// with zeroes for dimensions not part of the F004 repository contract.
    pub async fn dashboard_summary_basic(
        &self,
        period: &str,
    ) -> Result<DashboardSummary, DatabaseError> {
        let period = period.to_owned();
        self.database
            .call(move |connection| {
                let ttft_predicate = "streamed = 1 AND first_byte_ms IS NOT NULL AND started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour') WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days') ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')";
                let ttft_count: i64 = connection.query_row(
                    &format!("SELECT COUNT(*) FROM requests WHERE {ttft_predicate}"),
                    [&period],
                    |row| row.get(0),
                )?;
                let (p50_ttft_ms, p99_ttft_ms) = if ttft_count >= 50_000 {
                    let (lower_ttft, upper_ttft, p99_ttft): (
                        Option<f64>,
                        Option<f64>,
                        Option<f64>,
                    ) = connection.query_row(
                        "WITH histogram AS (SELECT first_byte_ms, COUNT(*) AS frequency FROM requests WHERE streamed = 1 AND first_byte_ms IS NOT NULL AND started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour') WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days') ELSE datetime('now', '-24 hours') END AND started_at < datetime('now') GROUP BY first_byte_ms), ranked AS (SELECT first_byte_ms, frequency, SUM(frequency) OVER (ORDER BY first_byte_ms ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_count, SUM(frequency) OVER () AS total_count FROM histogram) SELECT MAX(CASE WHEN cumulative_count >= (total_count + 1) / 2 AND cumulative_count - frequency < (total_count + 1) / 2 THEN first_byte_ms END), MAX(CASE WHEN cumulative_count >= (total_count + 2) / 2 AND cumulative_count - frequency < (total_count + 2) / 2 THEN first_byte_ms END), MAX(CASE WHEN cumulative_count >= (99 * total_count + 99) / 100 AND cumulative_count - frequency < (99 * total_count + 99) / 100 THEN first_byte_ms END) FROM ranked",
                        [&period],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )?;
                    (
                        lower_ttft
                            .zip(upper_ttft)
                            .map_or(0.0, |(lower, upper)| f64::midpoint(lower, upper)),
                        p99_ttft.unwrap_or(0.0),
                    )
                } else if ttft_count == 0 {
                    (0.0, 0.0)
                } else {
                    let at = |offset: i64| -> Result<f64, tokio_rusqlite::rusqlite::Error> {
                        connection.query_row(
                            &format!("SELECT first_byte_ms FROM requests WHERE {ttft_predicate} ORDER BY first_byte_ms LIMIT 1 OFFSET ?2"),
                            params![&period, offset],
                            |row| row.get(0),
                        )
                    };
                    let lower = at((ttft_count - 1) / 2)?;
                    let upper = at(ttft_count / 2)?;
                    let p99_offset = (99 * ttft_count + 99) / 100 - 1;
                    (f64::midpoint(lower, upper), at(p99_offset)?)
                };
                connection.query_row(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost_microdollars), 0), COALESCE(AVG(upstream_latency_ms), 0), COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_write_tokens), 0), COALESCE(SUM(reasoning_tokens), 0), COALESCE(SUM(CASE WHEN streamed = 1 THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN streamed = 0 THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'exact' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'derived' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'partial' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'unknown' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN cost_microdollars ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN cost_microdollars ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' AND reserved_microdollars IS NOT NULL AND cost_microdollars = reserved_microdollars AND local_cost_microdollars IS NOT NULL AND local_cost_microdollars > 0 AND local_cost_microdollars < cost_microdollars THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' AND reserved_microdollars IS NOT NULL AND cost_microdollars = reserved_microdollars AND local_cost_microdollars IS NOT NULL AND local_cost_microdollars > 0 AND local_cost_microdollars < cost_microdollars THEN cost_microdollars - local_cost_microdollars ELSE 0 END), 0), COALESCE(SUM(bytes_received), 0), COALESCE(SUM(bytes_emitted), 0), (SELECT COUNT(DISTINCT provider_id) FROM accounts), COALESCE(AVG(CASE WHEN streamed = 1 THEN first_byte_ms END), 0), CASE WHEN COALESCE(SUM(CASE WHEN status != 'pending' THEN upstream_latency_ms ELSE 0 END), 0) > 0 THEN CAST(SUM(CASE WHEN status != 'pending' THEN output_tokens ELSE 0 END) AS REAL) * 1000.0 / SUM(CASE WHEN status != 'pending' THEN upstream_latency_ms ELSE 0 END) ELSE 0 END, ?2, ?3 FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour') WHEN '24h' THEN datetime('now', '-24 hours') WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days') ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')",
                    params![period, p50_ttft_ms, p99_ttft_ms],
                    |row| {
                        Ok(DashboardSummary {
                            total_requests: row.get(0)?,
                            successful_requests: row.get(1)?,
                            error_requests: row.get(2)?,
                            total_input_tokens: row.get(3)?,
                            total_output_tokens: row.get(4)?,
                            total_cost_microdollars: row.get(5)?,
                            avg_latency_ms: row.get(6)?,
                            total_cache_read_tokens: row.get(7)?,
                            total_cache_write_tokens: row.get(8)?,
                            total_reasoning_tokens: row.get(9)?,
                            streamed_requests: row.get(10)?,
                            non_streamed_requests: row.get(11)?,
                            exact_count: row.get(12)?,
                            derived_count: row.get(13)?,
                            partial_count: row.get(14)?,
                            estimated_count: row.get(15)?,
                            unknown_count: row.get(16)?,
                            provider_reported_count: row.get(17)?,
                            provider_reported_cost_microdollars: row.get(18)?,
                            estimated_cost_sum_microdollars: row.get(19)?,
                            reservation_fallback_rows: row.get(20)?,
                            reservation_fallback_excess_microdollars: row.get(21)?,
                            total_bytes_received: row.get(22)?,
                            total_bytes_emitted: row.get(23)?,
                            total_providers: row.get(24)?,
                            avg_ttft_ms: row.get(25)?,
                            tokens_per_second: row.get(26)?,
                            p50_ttft_ms: row.get(27)?,
                            p99_ttft_ms: row.get(28)?,
                        })
                    },
                )
            })
            .await
    }
}

fn account_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<Account> {
    Ok(Account {
        id: row.get(0)?,
        name: row.get(1)?,
        api_key_env: row.get(2)?,
        enabled: row.get::<_, i64>(3)? != 0,
        weight: row.get(4)?,
        provider_id: row.get(5)?,
    })
}

fn model_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<Model> {
    Ok(Model {
        model_id: row.get(0)?,
        display_name: row.get(1)?,
        protocol: row.get(2)?,
        provider_id: row.get(3)?,
        resolution_status: row.get(4)?,
    })
}

fn catalog_model_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<CatalogModel> {
    Ok(CatalogModel {
        model_id: row.get(0)?,
        display_name: row.get(1)?,
        protocol: row.get(2)?,
        capabilities: row.get(3)?,
        source_metadata: row.get(4)?,
        protocol_source: row.get(5)?,
        first_seen_at: row.get(6)?,
        last_seen_at: row.get(7)?,
        resolution_status: row.get(8)?,
        provider_id: row.get(9)?,
    })
}

fn provider_model_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<ProviderModelMetadata> {
    Ok(ProviderModelMetadata {
        model_id: row.get(0)?,
        provider_id: row.get(1)?,
        display_name: row.get(2)?,
        protocol: row.get(3)?,
        capabilities: row.get(4)?,
        source_metadata: row.get(5)?,
        protocol_source: row.get(6)?,
        first_seen_at: row.get(7)?,
        last_seen_at: row.get(8)?,
        resolution_status: row.get(9)?,
    })
}

fn account_model_support_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<AccountModelSupport> {
    Ok(AccountModelSupport {
        account_id: row.get(0)?,
        model_id: row.get(1)?,
        enabled: row.get::<_, i64>(2)? != 0,
    })
}

fn refresh_state_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<CatalogRefreshState> {
    Ok(CatalogRefreshState {
        account_id: row.get(0)?,
        provider_id: row.get(1)?,
        last_successful_refresh_at: row.get(2)?,
        last_outcome: row.get(3)?,
        model_count: row.get(4)?,
    })
}

fn request_sql(condition: &str) -> String {
    format!(
        "SELECT id, proxy_request_id, account_id, provider_id, model_id, protocol,\n\
             streamed, status, input_tokens, output_tokens, cost_microdollars, started_at, completed_at\n\
             FROM requests {condition}"
    )
}

fn request_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<Request> {
    Ok(Request {
        id: row.get(0)?,
        proxy_request_id: row.get(1)?,
        account_id: row.get(2)?,
        provider_id: row.get(3)?,
        model_id: row.get(4)?,
        protocol: row.get(5)?,
        streamed: row.get::<_, i64>(6)? != 0,
        status: row.get(7)?,
        input_tokens: row.get(8)?,
        output_tokens: row.get(9)?,
        cost_microdollars: row.get(10)?,
        started_at: row.get(11)?,
        completed_at: row.get(12)?,
    })
}

fn ping_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<Ping> {
    Ok(Ping {
        provider_id: row.get(0)?,
        account_name: row.get(1)?,
        probed_at: row.get(2)?,
        latency_ms: row.get(3)?,
        status_code: row.get(4)?,
        error: row.get(5)?,
        model_count: row.get(6)?,
    })
}

fn params_from_names<'a>(names: &'a [&'a str]) -> impl tokio_rusqlite::rusqlite::Params + 'a {
    tokio_rusqlite::rusqlite::params_from_iter(names.iter().copied())
}
