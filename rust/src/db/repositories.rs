//! Typed repositories for the first Rust read-plane and compatibility writes.

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
                    params![account.name, account.api_key_env, account.enabled as i64, account.weight, account.provider_id],
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
                connection.query_row(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost_microdollars), 0), COALESCE(AVG(upstream_latency_ms), 0), COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_write_tokens), 0), COALESCE(SUM(reasoning_tokens), 0), COALESCE(SUM(CASE WHEN streamed = 1 THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN streamed = 0 THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'exact' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'derived' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'partial' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'unknown' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN cost_microdollars ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN cost_microdollars ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' AND reserved_microdollars IS NOT NULL AND cost_microdollars = reserved_microdollars AND local_cost_microdollars IS NOT NULL AND local_cost_microdollars > 0 AND local_cost_microdollars < cost_microdollars THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN exactness = 'estimated' AND reserved_microdollars IS NOT NULL AND cost_microdollars = reserved_microdollars AND local_cost_microdollars IS NOT NULL AND local_cost_microdollars > 0 AND local_cost_microdollars < cost_microdollars THEN cost_microdollars - local_cost_microdollars ELSE 0 END), 0), COALESCE(SUM(bytes_received), 0), COALESCE(SUM(bytes_emitted), 0), (SELECT COUNT(DISTINCT provider_id) FROM accounts), COALESCE(AVG(CASE WHEN streamed = 1 THEN first_byte_ms END), 0), CASE WHEN COALESCE(SUM(CASE WHEN status != 'pending' THEN upstream_latency_ms ELSE 0 END), 0) > 0 THEN CAST(SUM(CASE WHEN status != 'pending' THEN output_tokens ELSE 0 END) AS REAL) * 1000.0 / SUM(CASE WHEN status != 'pending' THEN upstream_latency_ms ELSE 0 END) ELSE 0 END, 0, 0 FROM requests WHERE started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour') WHEN '24h' THEN datetime('now', '-24 hours') WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days') ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')",
                    [period],
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
