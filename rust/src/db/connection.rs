//! Serialized asynchronous SQLite access.

use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};

use thiserror::Error;
use tokio::sync::Semaphore;
use tokio_rusqlite::{Connection as AsyncConnection, Error as AsyncSqliteError};

type SqliteConnection = tokio_rusqlite::rusqlite::Connection;
type SqliteError = tokio_rusqlite::rusqlite::Error;

#[derive(Debug, Clone)]
pub struct DatabaseConfig {
    pub path: String,
    pub busy_timeout_ms: u32,
    pub wal: bool,
    pub synchronous: String,
    pub read_only: bool,
    pub journal_size_limit: Option<u64>,
}

impl Default for DatabaseConfig {
    fn default() -> Self {
        Self {
            path: ":memory:".to_owned(),
            busy_timeout_ms: 5_000,
            wal: true,
            synchronous: "NORMAL".to_owned(),
            read_only: false,
            journal_size_limit: None,
        }
    }
}

impl From<&crate::config::DatabaseConfig> for DatabaseConfig {
    fn from(config: &crate::config::DatabaseConfig) -> Self {
        Self {
            path: config.path.clone(),
            busy_timeout_ms: config.busy_timeout_ms,
            wal: config.wal,
            synchronous: config.synchronous.clone(),
            read_only: false,
            journal_size_limit: config.journal_size_limit,
        }
    }
}

#[derive(Debug, Error)]
pub enum DatabaseError {
    #[error("database connection is closed")]
    Closed,
    #[error("database is read-only")]
    ReadOnly,
    #[error("SQLite busy/locked during {operation} after {busy_timeout_ms} ms: {source}")]
    Busy {
        operation: String,
        busy_timeout_ms: u32,
        #[source]
        source: Box<SqliteError>,
    },
    #[error("SQLite operation failed during {operation}: {source}")]
    Sqlite {
        operation: String,
        #[source]
        source: Box<SqliteError>,
    },
    #[error("SQLite transaction body failed and was rolled back: {source}")]
    Transaction {
        #[source]
        source: Box<SqliteError>,
    },
    #[error("SQLite ROLLBACK failed after a transaction error: {source}")]
    RollbackFailed {
        #[source]
        source: Box<SqliteError>,
        operation: Box<DatabaseError>,
    },
    #[error("SQLite COMMIT failed; transaction state was checked and rolled back: {source}")]
    CommitFailed {
        #[source]
        source: Box<SqliteError>,
        rollback_error: Option<Box<SqliteError>>,
    },
    #[error("database integrity check failed: {detail}")]
    Integrity { detail: String },
    #[error("migration checksum mismatch for {name}: expected {expected}, got {actual}")]
    MigrationChecksumMismatch {
        name: String,
        expected: String,
        actual: String,
    },
    #[error("database has unknown applied migration version {version}")]
    UnknownMigration { version: i64 },
    #[error("migration {version} has ledger name {actual:?}, expected {expected:?}")]
    MigrationNameMismatch {
        version: i64,
        expected: String,
        actual: String,
    },
    #[error("database schema requires migrations but is read-only")]
    ReadOnlyMigration,
    #[error("database worker semaphore closed")]
    WorkerClosed,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DatabaseStats {
    pub calls: u64,
    pub transactions: u64,
}

struct DatabaseInner {
    connection: AsyncConnection,
    gate: Arc<Semaphore>,
    closed: AtomicBool,
    physical_closed: AtomicBool,
    calls: AtomicU64,
    transactions: AtomicU64,
    config: DatabaseConfig,
}

/// One async SQLite connection with a single serialized operation gate.
#[derive(Clone)]
pub struct Database {
    inner: Arc<DatabaseInner>,
}

impl std::fmt::Debug for Database {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Database")
            .field("path", &self.inner.config.path)
            .field("read_only", &self.inner.config.read_only)
            .field("closed", &self.inner.closed.load(Ordering::Acquire))
            .finish()
    }
}

impl Database {
    pub async fn open(config: DatabaseConfig) -> Result<Self, DatabaseError> {
        validate_config(&config)?;
        let connection = if config.read_only && config.path != ":memory:" {
            let uri = format!("file:{}?mode=ro", percent_encode_path(&config.path));
            AsyncConnection::open_with_flags(
                uri,
                tokio_rusqlite::rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
                    | tokio_rusqlite::rusqlite::OpenFlags::SQLITE_OPEN_URI,
            )
            .await
        } else if config.path == ":memory:" {
            AsyncConnection::open_in_memory().await
        } else {
            AsyncConnection::open(&config.path).await
        }
        .map_err(|source| map_sqlite("open", config.busy_timeout_ms, source))?;

        let database = Self {
            inner: Arc::new(DatabaseInner {
                connection,
                gate: Arc::new(Semaphore::new(1)),
                closed: AtomicBool::new(false),
                physical_closed: AtomicBool::new(false),
                calls: AtomicU64::new(0),
                transactions: AtomicU64::new(0),
                config,
            }),
        };
        if let Err(error) = database.configure().await {
            let _ = database.close().await;
            return Err(error);
        }
        Ok(database)
    }

    pub fn config(&self) -> &DatabaseConfig {
        &self.inner.config
    }

    pub fn stats(&self) -> DatabaseStats {
        DatabaseStats {
            calls: self.inner.calls.load(Ordering::Relaxed),
            transactions: self.inner.transactions.load(Ordering::Relaxed),
        }
    }

    pub async fn close(&self) -> Result<(), DatabaseError> {
        let permit = self
            .inner
            .gate
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| DatabaseError::WorkerClosed)?;
        self.inner.closed.store(true, Ordering::Release);
        if self.inner.physical_closed.swap(true, Ordering::AcqRel) {
            drop(permit);
            return Ok(());
        }
        let result = self
            .inner
            .connection
            .clone()
            .close()
            .await
            .map_err(|error| match error {
                AsyncSqliteError::Close((_, source)) => {
                    map_sqlite("close", self.inner.config.busy_timeout_ms, source)
                }
                AsyncSqliteError::ConnectionClosed => DatabaseError::Closed,
                AsyncSqliteError::Error(source) => {
                    map_sqlite("close", self.inner.config.busy_timeout_ms, source)
                }
                _ => DatabaseError::WorkerClosed,
            });
        drop(permit);
        result
    }

    pub async fn call<F, R>(&self, operation: F) -> Result<R, DatabaseError>
    where
        F: FnOnce(&mut SqliteConnection) -> Result<R, SqliteError> + Send + 'static,
        R: Send + 'static,
    {
        let permit = self.acquire_permit().await?;
        self.inner.calls.fetch_add(1, Ordering::Relaxed);
        let result = self.inner.connection.call(operation).await;
        drop(permit);
        result.map_err(|error| match error {
            AsyncSqliteError::ConnectionClosed => DatabaseError::Closed,
            AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source) => {
                map_sqlite("call", self.inner.config.busy_timeout_ms, source)
            }
            _ => DatabaseError::WorkerClosed,
        })
    }

    pub async fn with_transaction<F, R>(&self, operation: F) -> Result<R, DatabaseError>
    where
        F: FnOnce(&mut SqliteConnection) -> Result<R, SqliteError> + Send + 'static,
        R: Send + 'static,
    {
        if self.inner.config.read_only {
            return Err(DatabaseError::ReadOnly);
        }
        let permit = self.acquire_permit().await?;
        self.inner.calls.fetch_add(1, Ordering::Relaxed);
        self.inner.transactions.fetch_add(1, Ordering::Relaxed);
        let timeout = self.inner.config.busy_timeout_ms;
        let result =
            self.inner
                .connection
                .call(
                    move |connection| -> Result<Result<R, TransactionResult>, SqliteError> {
                        connection.execute_batch("BEGIN IMMEDIATE")?;
                        let result = match operation(connection) {
                            Ok(value) => connection
                                .execute_batch("COMMIT")
                                .map(|()| value)
                                .map_err(|commit| {
                                    let rollback_error = connection.execute_batch("ROLLBACK").err();
                                    TransactionResult::Commit {
                                        commit,
                                        rollback_error,
                                    }
                                }),
                            Err(operation_error) => match connection.execute_batch("ROLLBACK") {
                                Ok(()) => Err(TransactionResult::Body {
                                    operation: operation_error,
                                }),
                                Err(rollback) => Err(TransactionResult::Rollback {
                                    operation: operation_error,
                                    rollback,
                                }),
                            },
                        };
                        Ok(result)
                    },
                )
                .await;
        drop(permit);
        match result {
            Err(AsyncSqliteError::ConnectionClosed) => Err(DatabaseError::Closed),
            Err(AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source)) => {
                Err(map_sqlite("transaction", timeout, source))
            }
            Err(_) => Err(DatabaseError::WorkerClosed),
            Ok(Err(TransactionResult::Body { operation })) => {
                Err(map_sqlite("transaction body", timeout, operation))
            }
            Ok(Err(TransactionResult::Rollback {
                operation,
                rollback,
            })) => {
                self.inner.closed.store(true, Ordering::Release);
                let error = DatabaseError::RollbackFailed {
                    source: Box::new(rollback),
                    operation: Box::new(DatabaseError::Transaction {
                        source: Box::new(operation),
                    }),
                };
                let _ = self.close().await;
                Err(error)
            }
            Ok(Err(TransactionResult::Commit {
                commit,
                rollback_error,
            })) => {
                let rollback_failed = rollback_error.is_some();
                let error = DatabaseError::CommitFailed {
                    source: Box::new(commit),
                    rollback_error: rollback_error.map(Box::new),
                };
                if rollback_failed {
                    self.inner.closed.store(true, Ordering::Release);
                    let _ = self.close().await;
                }
                Err(error)
            }
            Ok(Ok(value)) => Ok(value),
        }
    }

    pub async fn quick_check(&self) -> Result<(), DatabaseError> {
        let checks = self
            .call(|connection| {
                let mut statement = connection.prepare("PRAGMA quick_check")?;
                statement
                    .query_map([], |row| row.get(0))?
                    .collect::<Result<Vec<String>, _>>()
            })
            .await?;
        if checks.len() == 1 && checks[0].eq_ignore_ascii_case("ok") {
            Ok(())
        } else {
            Err(DatabaseError::Integrity {
                detail: "PRAGMA quick_check did not return ok".to_owned(),
            })
        }
    }

    async fn configure(&self) -> Result<(), DatabaseError> {
        let config = self.inner.config.clone();
        self.call(move |connection| {
            connection.execute_batch("PRAGMA foreign_keys = ON")?;
            connection.pragma_update(None, "busy_timeout", config.busy_timeout_ms)?;
            if !config.read_only && config.wal {
                connection.pragma_update(None, "journal_mode", "WAL")?;
            }
            connection.pragma_update(None, "synchronous", config.synchronous.as_str())?;
            if let Some(limit) = config.journal_size_limit {
                connection.pragma_update(None, "journal_size_limit", limit)?;
            }
            Ok(())
        })
        .await
    }

    async fn acquire_permit(&self) -> Result<tokio::sync::OwnedSemaphorePermit, DatabaseError> {
        if self.inner.closed.load(Ordering::Acquire) {
            return Err(DatabaseError::Closed);
        }
        self.inner
            .gate
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| DatabaseError::WorkerClosed)
    }
}

enum TransactionResult {
    Body {
        operation: SqliteError,
    },
    Rollback {
        operation: SqliteError,
        rollback: SqliteError,
    },
    Commit {
        commit: SqliteError,
        rollback_error: Option<SqliteError>,
    },
}

fn validate_config(config: &DatabaseConfig) -> Result<(), DatabaseError> {
    if config.synchronous != "OFF"
        && config.synchronous != "NORMAL"
        && config.synchronous != "FULL"
        && config.synchronous != "EXTRA"
    {
        return Err(DatabaseError::Integrity {
            detail: "synchronous must be OFF, NORMAL, FULL, or EXTRA".to_owned(),
        });
    }
    Ok(())
}

fn map_sqlite(operation: &str, busy_timeout_ms: u32, source: SqliteError) -> DatabaseError {
    let is_busy = matches!(
        source,
        SqliteError::SqliteFailure(ref failure, _) if matches!(failure.extended_code & 0xff, 5 | 6)
    );
    if is_busy {
        DatabaseError::Busy {
            operation: operation.to_owned(),
            busy_timeout_ms,
            source: Box::new(source),
        }
    } else {
        DatabaseError::Sqlite {
            operation: operation.to_owned(),
            source: Box::new(source),
        }
    }
}

fn percent_encode_path(path: &str) -> String {
    path.bytes()
        .flat_map(|byte| match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b'.' | b'_' | b'-' => {
                vec![byte as char]
            }
            other => format!("%{other:02X}").chars().collect(),
        })
        .collect()
}
