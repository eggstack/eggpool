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
