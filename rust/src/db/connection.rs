//! Serialized asynchronous SQLite access.

use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

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

/// Bounded, generation-selected retention work.  Each loop iteration uses a
/// separate SQLite transaction so maintenance never holds the write lock
/// while yielding for another batch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetentionCleanupPolicy {
    pub request_days: u64,
    pub event_days: u64,
    pub ping_days: u64,
    pub operational_event_days: u64,
    pub routing_decision_days: u64,
    pub rollup_days: u64,
    pub price_snapshot_days: u64,
    pub model_info_observation_days: u64,
    pub max_rows_per_batch: u32,
    pub max_batches: u32,
    pub max_tick_duration: Duration,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RetentionCleanupReport {
    pub batches_completed: u32,
    pub rows_changed: u64,
    pub budget_exhausted: bool,
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

/// Internal policy for opportunistic maintenance checkpoints (persistence
/// M001). The production SQLite automatic checkpoint threshold is unchanged
/// and remains the hard fallback that bounds worst-case WAL growth.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct CheckpointMaintenancePolicy {
    pub soft_wal_frames: u32,
}

impl CheckpointMaintenancePolicy {
    pub(crate) const DEFAULT_SOFT_WAL_FRAMES: u32 = 256;
    #[cfg(feature = "qualification-db-diagnostics")]
    pub(crate) const MAX_SOFT_WAL_FRAMES: u32 = 1000;

    pub(crate) fn effective() -> Self {
        #[cfg(feature = "qualification-db-diagnostics")]
        if let Some(soft) = qualification_checkpoint_soft_frames_override() {
            return Self {
                soft_wal_frames: soft,
            };
        }
        Self {
            soft_wal_frames: Self::DEFAULT_SOFT_WAL_FRAMES,
        }
    }
}

/// Bounded outcome of one opportunistic maintenance tick. Only scalar frame
/// counts cross this boundary; no SQL text, path, or request detail.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CheckpointMaintenanceOutcome {
    /// No durable transaction completed since the previous inspection, so no
    /// SQLite work was performed.
    NotDue,
    /// The database gate was already owned by foreground work; the optional
    /// tick deferred instead of queueing behind it.
    GateBusy,
    /// WAL state was inspected through SQLite and remains below the soft
    /// maintenance threshold.
    BelowThreshold { log_frames: u32 },
    /// A PASSIVE checkpoint ran on the existing worker and reported its
    /// post-checkpoint frame counts.
    Checkpointed {
        log_frames: u32,
        checkpointed_frames: u32,
    },
}

impl CheckpointMaintenanceOutcome {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::NotDue => "not_due",
            Self::GateBusy => "gate_busy",
            Self::BelowThreshold { .. } => "below_threshold",
            Self::Checkpointed { .. } => "checkpointed",
        }
    }
}

#[derive(Debug, Default)]
struct CheckpointMaintenanceStats {
    not_due: AtomicU64,
    gate_busy: AtomicU64,
    below_threshold: AtomicU64,
    checkpointed: AtomicU64,
    failures: AtomicU64,
    last_log_frames: AtomicU64,
    last_checkpointed_frames: AtomicU64,
}

#[cfg(feature = "qualification-db-diagnostics")]
impl CheckpointMaintenanceStats {
    fn snapshot(
        &self,
        soft_threshold_frames: u32,
    ) -> super::qualification::QualificationCheckpointMaintenance {
        super::qualification::QualificationCheckpointMaintenance {
            soft_threshold_frames,
            not_due: self.not_due.load(Ordering::Relaxed),
            gate_busy: self.gate_busy.load(Ordering::Relaxed),
            below_threshold: self.below_threshold.load(Ordering::Relaxed),
            checkpointed: self.checkpointed.load(Ordering::Relaxed),
            failures: self.failures.load(Ordering::Relaxed),
            last_log_frames: self.last_log_frames.load(Ordering::Relaxed),
            last_checkpointed_frames: self.last_checkpointed_frames.load(Ordering::Relaxed),
        }
    }
}

struct DatabaseInner {
    connection: AsyncConnection,
    gate: Arc<Semaphore>,
    closed: AtomicBool,
    physical_closed: AtomicBool,
    calls: AtomicU64,
    transactions: AtomicU64,
    config: DatabaseConfig,
    checkpoint_stats: CheckpointMaintenanceStats,
    #[cfg(feature = "qualification-db-diagnostics")]
    qualification: super::qualification::QualificationCollector,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TransactionKind {
    Publication,
    Finalization,
    Other,
}

impl TransactionKind {
    #[cfg(feature = "qualification-db-diagnostics")]
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Publication => "publication",
            Self::Finalization => "finalization",
            Self::Other => "other",
        }
    }
}

#[cfg(feature = "qualification-db-diagnostics")]
struct TransactionEnvelope<R> {
    outcome: TransactionCallResult<R>,
    phase: TransactionPhase,
}

#[cfg(feature = "qualification-db-diagnostics")]
enum TransactionCallResult<R> {
    Begin(SqliteError),
    Completed(Result<R, TransactionResult>),
}

#[cfg(feature = "qualification-db-diagnostics")]
struct TransactionPhase {
    worker_queue_us: u64,
    begin_us: u64,
    body_us: u64,
    commit_us: Option<u64>,
    worker_finished_at: Instant,
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
                checkpoint_stats: CheckpointMaintenanceStats::default(),
                #[cfg(feature = "qualification-db-diagnostics")]
                qualification: super::qualification::QualificationCollector::new(),
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

    pub(crate) fn transaction_count(&self) -> u64 {
        self.inner.transactions.load(Ordering::Relaxed)
    }

    #[cfg(feature = "qualification-db-diagnostics")]
    pub fn qualification_snapshot(&self) -> crate::db::QualificationDbSnapshot {
        let mut snapshot = self.inner.qualification.snapshot();
        snapshot.checkpoint_maintenance = self
            .inner
            .checkpoint_stats
            .snapshot(CheckpointMaintenancePolicy::effective().soft_wal_frames);
        snapshot
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
        self.with_transaction_kind(TransactionKind::Other, operation)
            .await
    }

    pub(crate) async fn with_named_transaction<F, R>(
        &self,
        kind: TransactionKind,
        operation: F,
    ) -> Result<R, DatabaseError>
    where
        F: FnOnce(&mut SqliteConnection) -> Result<R, SqliteError> + Send + 'static,
        R: Send + 'static,
    {
        self.with_transaction_kind(kind, operation).await
    }

    async fn with_transaction_kind<F, R>(
        &self,
        kind: TransactionKind,
        operation: F,
    ) -> Result<R, DatabaseError>
    where
        F: FnOnce(&mut SqliteConnection) -> Result<R, SqliteError> + Send + 'static,
        R: Send + 'static,
    {
        #[cfg(not(feature = "qualification-db-diagnostics"))]
        let _ = kind;
        if self.inner.config.read_only {
            return Err(DatabaseError::ReadOnly);
        }
        #[cfg(feature = "qualification-db-diagnostics")]
        let started_at = Instant::now();
        #[cfg(feature = "qualification-db-diagnostics")]
        let gate_started_at = Instant::now();
        let permit = self.acquire_permit().await?;
        #[cfg(feature = "qualification-db-diagnostics")]
        let gate_wait_us = elapsed_us(gate_started_at);
        self.inner.calls.fetch_add(1, Ordering::Relaxed);
        self.inner.transactions.fetch_add(1, Ordering::Relaxed);
        let timeout = self.inner.config.busy_timeout_ms;
        #[cfg(feature = "qualification-db-diagnostics")]
        let call_started_at = Instant::now();
        let result = self
            .inner
            .connection
            .call(move |connection| {
                #[cfg(feature = "qualification-db-diagnostics")]
                let worker_started_at = Instant::now();
                #[cfg(feature = "qualification-db-diagnostics")]
                let worker_queue_us = elapsed_us(call_started_at);
                #[cfg(feature = "qualification-db-diagnostics")]
                let begin_started_at = Instant::now();
                let begin_result = connection.execute_batch("BEGIN IMMEDIATE");
                #[cfg(feature = "qualification-db-diagnostics")]
                let begin_us = elapsed_us(begin_started_at);
                #[cfg(not(feature = "qualification-db-diagnostics"))]
                begin_result?;
                #[cfg(feature = "qualification-db-diagnostics")]
                if let Err(source) = begin_result {
                    #[cfg(feature = "qualification-db-diagnostics")]
                    let phase = TransactionPhase {
                        worker_queue_us,
                        begin_us,
                        body_us: 0,
                        commit_us: None,
                        worker_finished_at: Instant::now(),
                    };
                    #[cfg(feature = "qualification-db-diagnostics")]
                    return Ok(TransactionEnvelope {
                        outcome: TransactionCallResult::Begin(source),
                        phase,
                    });
                }

                #[cfg(feature = "qualification-db-diagnostics")]
                let body_started_at = Instant::now();
                let body_result = operation(connection);
                #[cfg(feature = "qualification-db-diagnostics")]
                let body_us = elapsed_us(body_started_at);
                #[cfg(feature = "qualification-db-diagnostics")]
                let mut commit_us = None;
                let transaction_result = match body_result {
                    Ok(value) => {
                        #[cfg(feature = "qualification-db-diagnostics")]
                        let commit_started_at = Instant::now();
                        let commit_result = connection.execute_batch("COMMIT");
                        #[cfg(feature = "qualification-db-diagnostics")]
                        {
                            commit_us = Some(elapsed_us(commit_started_at));
                        }
                        match commit_result {
                            Ok(()) => Ok(value),
                            Err(commit) => {
                                let rollback_error = connection.execute_batch("ROLLBACK").err();
                                Err(TransactionResult::Commit {
                                    commit,
                                    rollback_error,
                                })
                            }
                        }
                    }
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
                #[cfg(feature = "qualification-db-diagnostics")]
                let phase = TransactionPhase {
                    worker_queue_us,
                    begin_us,
                    body_us,
                    commit_us,
                    worker_finished_at: Instant::now(),
                };
                #[cfg(feature = "qualification-db-diagnostics")]
                {
                    let _ = worker_started_at;
                    Ok(TransactionEnvelope {
                        outcome: TransactionCallResult::Completed(transaction_result),
                        phase,
                    })
                }
                #[cfg(not(feature = "qualification-db-diagnostics"))]
                {
                    Ok(transaction_result)
                }
            })
            .await;
        drop(permit);
        #[cfg(feature = "qualification-db-diagnostics")]
        let total_us = elapsed_us(started_at);
        match result {
            Err(AsyncSqliteError::ConnectionClosed) => Err(DatabaseError::Closed),
            Err(AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source)) => {
                #[cfg(feature = "qualification-db-diagnostics")]
                self.inner
                    .qualification
                    .record(super::qualification::QualificationRecordInput {
                        kind,
                        gate_wait_us,
                        worker_queue_us: 0,
                        begin_us: 0,
                        body_us: 0,
                        commit_us: None,
                        worker_return_us: 0,
                        total_us,
                        success: false,
                    });
                Err(map_sqlite("transaction", timeout, source))
            }
            Err(_) => {
                #[cfg(feature = "qualification-db-diagnostics")]
                self.inner
                    .qualification
                    .record(super::qualification::QualificationRecordInput {
                        kind,
                        gate_wait_us,
                        worker_queue_us: 0,
                        begin_us: 0,
                        body_us: 0,
                        commit_us: None,
                        worker_return_us: 0,
                        total_us,
                        success: false,
                    });
                Err(DatabaseError::WorkerClosed)
            }
            #[cfg(feature = "qualification-db-diagnostics")]
            Ok(envelope) => {
                let success = matches!(&envelope.outcome, TransactionCallResult::Completed(Ok(_)));
                self.inner
                    .qualification
                    .record(super::qualification::QualificationRecordInput {
                        kind,
                        gate_wait_us,
                        worker_queue_us: envelope.phase.worker_queue_us,
                        begin_us: envelope.phase.begin_us,
                        body_us: envelope.phase.body_us,
                        commit_us: envelope.phase.commit_us,
                        worker_return_us: elapsed_us(envelope.phase.worker_finished_at),
                        total_us,
                        success,
                    });
                match envelope.outcome {
                    TransactionCallResult::Begin(source) => {
                        Err(map_sqlite("transaction", timeout, source))
                    }
                    TransactionCallResult::Completed(Ok(value)) => Ok(value),
                    TransactionCallResult::Completed(Err(TransactionResult::Body {
                        operation,
                    })) => Err(map_sqlite("transaction body", timeout, operation)),
                    TransactionCallResult::Completed(Err(TransactionResult::Rollback {
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
                    TransactionCallResult::Completed(Err(TransactionResult::Commit {
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
                }
            }
            #[cfg(not(feature = "qualification-db-diagnostics"))]
            Ok(Ok(value)) => Ok(value),
            #[cfg(not(feature = "qualification-db-diagnostics"))]
            Ok(Err(TransactionResult::Body { operation })) => {
                Err(map_sqlite("transaction body", timeout, operation))
            }
            #[cfg(not(feature = "qualification-db-diagnostics"))]
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
            #[cfg(not(feature = "qualification-db-diagnostics"))]
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
        }
    }

    /// Begin a transaction whose lifetime is controlled by the caller.
    ///
    /// The connection gate remains held until `commit` or `rollback`.  This
    /// is intentionally a small primitive for the runtime reload acceptance
    /// window: candidate work happens before the transaction, and the caller
    /// can commit the staged runtime pointer while SQLite is still
    /// uncommitted.  Ordinary repository writes should continue to use
    /// `with_transaction`.
    pub async fn begin_transaction(&self) -> Result<DatabaseTransaction, DatabaseError> {
        if self.inner.config.read_only {
            return Err(DatabaseError::ReadOnly);
        }
        let permit = self.acquire_permit().await?;
        self.inner.calls.fetch_add(1, Ordering::Relaxed);
        self.inner.transactions.fetch_add(1, Ordering::Relaxed);
        let timeout = self.inner.config.busy_timeout_ms;
        if let Err(error) = self
            .inner
            .connection
            .clone()
            .call(|connection| connection.execute_batch("BEGIN IMMEDIATE"))
            .await
        {
            drop(permit);
            return Err(match error {
                AsyncSqliteError::ConnectionClosed => DatabaseError::Closed,
                AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source) => {
                    map_sqlite("begin transaction", timeout, source)
                }
                _ => DatabaseError::WorkerClosed,
            });
        }
        Ok(DatabaseTransaction {
            database: self.clone(),
            permit: Some(permit),
            finished: false,
        })
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

    /// Run a passive WAL checkpoint for the process-owned maintenance task.
    /// SQLite returns checkpoint counters; the supervisor intentionally keeps
    /// only the success/failure category and does not retain database detail.
    pub async fn checkpoint(&self) -> Result<(), DatabaseError> {
        self.call(|connection| {
            run_passive_checkpoint(connection)?;
            Ok(())
        })
        .await
    }

    /// Opportunistic maintenance checkpoint for the process-owned task
    /// (persistence M001). The tick never queues behind foreground work: when
    /// no durable transaction completed since `observed_transactions` was last
    /// updated, no SQLite work runs; when the gate is already owned, the tick
    /// defers. WAL state is observed through SQLite itself
    /// (`PRAGMA wal_checkpoint(NOOP)`), and PASSIVE work runs only once the
    /// soft threshold is due. The unchanged SQLite automatic checkpoint
    /// threshold remains the hard fallback.
    pub(crate) async fn checkpoint_maintenance(
        &self,
        policy: CheckpointMaintenancePolicy,
        observed_transactions: &AtomicU64,
    ) -> Result<CheckpointMaintenanceOutcome, DatabaseError> {
        let current = self.inner.transactions.load(Ordering::Relaxed);
        if current == observed_transactions.load(Ordering::Relaxed) {
            self.inner
                .checkpoint_stats
                .not_due
                .fetch_add(1, Ordering::Relaxed);
            return Ok(CheckpointMaintenanceOutcome::NotDue);
        }
        if self.inner.closed.load(Ordering::Acquire) {
            self.record_maintenance_failure();
            return Err(DatabaseError::Closed);
        }
        let permit = match self.inner.gate.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(tokio::sync::TryAcquireError::NoPermits) => {
                self.inner
                    .checkpoint_stats
                    .gate_busy
                    .fetch_add(1, Ordering::Relaxed);
                return Ok(CheckpointMaintenanceOutcome::GateBusy);
            }
            Err(tokio::sync::TryAcquireError::Closed) => {
                self.record_maintenance_failure();
                return Err(DatabaseError::WorkerClosed);
            }
        };
        self.inner.calls.fetch_add(1, Ordering::Relaxed);
        let soft = policy.soft_wal_frames;
        let inspected = self
            .inner
            .connection
            .call(move |connection| {
                let progress = query_wal_progress(connection)?;
                if progress.busy {
                    Ok(MaintenanceInspection::Deferred)
                } else if u64::from(progress.log_frames) >= u64::from(soft) {
                    let after = run_passive_checkpoint(connection)?;
                    Ok(MaintenanceInspection::Checkpointed(after))
                } else {
                    Ok(MaintenanceInspection::BelowThreshold(progress))
                }
            })
            .await;
        drop(permit);
        let inspection = inspected.map_err(|error| {
            self.record_maintenance_failure();
            match error {
                AsyncSqliteError::ConnectionClosed => DatabaseError::Closed,
                AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source) => {
                    map_sqlite(
                        "checkpoint maintenance",
                        self.inner.config.busy_timeout_ms,
                        source,
                    )
                }
                _ => DatabaseError::WorkerClosed,
            }
        })?;
        if matches!(inspection, MaintenanceInspection::Deferred) {
            self.inner
                .checkpoint_stats
                .gate_busy
                .fetch_add(1, Ordering::Relaxed);
            return Ok(CheckpointMaintenanceOutcome::GateBusy);
        }
        let progress = match inspection {
            MaintenanceInspection::BelowThreshold(progress)
            | MaintenanceInspection::Checkpointed(progress) => progress,
            MaintenanceInspection::Deferred => unreachable!("deferred inspection returned above"),
        };
        observed_transactions.store(current, Ordering::Relaxed);
        self.inner
            .checkpoint_stats
            .last_log_frames
            .store(u64::from(progress.log_frames), Ordering::Relaxed);
        self.inner
            .checkpoint_stats
            .last_checkpointed_frames
            .store(u64::from(progress.checkpointed_frames), Ordering::Relaxed);
        if matches!(inspection, MaintenanceInspection::BelowThreshold(_)) {
            self.inner
                .checkpoint_stats
                .below_threshold
                .fetch_add(1, Ordering::Relaxed);
            Ok(CheckpointMaintenanceOutcome::BelowThreshold {
                log_frames: progress.log_frames,
            })
        } else {
            self.inner
                .checkpoint_stats
                .checkpointed
                .fetch_add(1, Ordering::Relaxed);
            Ok(CheckpointMaintenanceOutcome::Checkpointed {
                log_frames: progress.log_frames,
                checkpointed_frames: progress.checkpointed_frames,
            })
        }
    }

    fn record_maintenance_failure(&self) {
        self.inner
            .checkpoint_stats
            .failures
            .fetch_add(1, Ordering::Relaxed);
    }

    /// Rebuild the database through SQLite's dedicated maintenance command.
    /// The serialized gate prevents this from racing another operation, while
    /// SQLite's configured busy timeout keeps contention bounded.
    pub async fn vacuum(&self) -> Result<(), DatabaseError> {
        self.call(|connection| {
            connection.execute_batch("VACUUM")?;
            Ok(())
        })
        .await
    }

    /// Create a consistent online-backup snapshot without copying a live WAL
    /// file.  The destination is opened only inside the SQLite worker thread,
    /// so the source connection remains under the same serialized gate as all
    /// other database operations.
    pub async fn backup_to(&self, destination: std::path::PathBuf) -> Result<(), DatabaseError> {
        self.call(move |source| {
            let mut target = tokio_rusqlite::rusqlite::Connection::open(destination)?;
            let backup = tokio_rusqlite::rusqlite::backup::Backup::new(source, &mut target)?;
            backup.run_to_completion(32, std::time::Duration::from_millis(10), None)?;
            Ok(())
        })
        .await
    }

    /// Delete only terminal/historical rows under a bounded maintenance
    /// budget. Pending requests and active reservations are never selected.
    pub async fn cleanup_retention(
        &self,
        policy: RetentionCleanupPolicy,
    ) -> Result<RetentionCleanupReport, DatabaseError> {
        let row_limit = i64::from(policy.max_rows_per_batch.max(1));
        let batch_limit = policy.max_batches.max(1);
        let started = Instant::now();
        let mut report = RetentionCleanupReport::default();

        while report.batches_completed < batch_limit
            && started.elapsed() < policy.max_tick_duration.max(Duration::from_millis(1))
        {
            let changed = self
                .with_transaction(move |connection| {
                    let mut changed = 0_u64;
                    changed += delete_old(
                        connection,
                        "reservations",
                        "id",
                        "request_id IN (SELECT id FROM requests WHERE status != 'pending' AND started_at < datetime('now', ?1))",
                        policy.request_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "requests",
                        "id",
                        "status != 'pending' AND started_at < datetime('now', ?1)",
                        policy.request_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "account_events",
                        "id",
                        "created_at < datetime('now', ?1)",
                        policy.event_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "provider_pings",
                        "id",
                        "probed_at < datetime('now', ?1)",
                        policy.ping_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "operational_events",
                        "id",
                        "occurred_at < datetime('now', ?1)",
                        policy.operational_event_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "routing_decisions",
                        "id",
                        "decision_made_at < datetime('now', ?1)",
                        policy.routing_decision_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "usage_rollups",
                        "rowid",
                        "bucket_start < datetime('now', ?1)",
                        policy.rollup_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "model_price_snapshots",
                        "id",
                        "captured_at < datetime('now', ?1)",
                        policy.price_snapshot_days,
                        row_limit,
                    )?;
                    changed += delete_old(
                        connection,
                        "model_info_observations",
                        "id",
                        "observed_at < datetime('now', ?1)",
                        policy.model_info_observation_days,
                        row_limit,
                    )?;
                    Ok(changed)
                })
                .await?;
            report.rows_changed = report.rows_changed.saturating_add(changed);
            report.batches_completed = report.batches_completed.saturating_add(1);
            if changed == 0 {
                break;
            }
            tokio::task::yield_now().await;
        }
        report.budget_exhausted = report.batches_completed >= batch_limit
            || started.elapsed() >= policy.max_tick_duration.max(Duration::from_millis(1));
        Ok(report)
    }

    async fn configure(&self) -> Result<(), DatabaseError> {
        #[cfg(feature = "qualification-db-diagnostics")]
        validate_qualification_checkpoint_overrides()?;
        let config = self.inner.config.clone();
        #[cfg(feature = "qualification-db-diagnostics")]
        let wal_autocheckpoint_override = qualification_wal_autocheckpoint_override()?;
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
            #[cfg(feature = "qualification-db-diagnostics")]
            {
                if let Some(pages) = wal_autocheckpoint_override {
                    connection.pragma_update(None, "wal_autocheckpoint", pages)?;
                }
                let effective = super::qualification::QualificationEffectivePragmas {
                    journal_mode: connection
                        .query_row("PRAGMA journal_mode", [], |row| row.get(0))?,
                    synchronous: synchronous_name(connection.query_row(
                        "PRAGMA synchronous",
                        [],
                        |row| row.get::<_, i64>(0),
                    )?),
                    page_size: connection
                        .query_row("PRAGMA page_size", [], |row| row.get::<_, i64>(0))?
                        as u64,
                    wal_autocheckpoint_pages: connection.query_row(
                        "PRAGMA wal_autocheckpoint",
                        [],
                        |row| row.get::<_, i64>(0),
                    )? as u64,
                };
                Ok(Some(effective))
            }
            #[cfg(not(feature = "qualification-db-diagnostics"))]
            Ok(None::<()>)
        })
        .await
        .map(|effective| {
            #[cfg(feature = "qualification-db-diagnostics")]
            if let Some(effective) = effective {
                self.inner.qualification.set_effective(effective);
            }
            #[cfg(not(feature = "qualification-db-diagnostics"))]
            let _ = effective;
        })
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

#[cfg(feature = "qualification-db-diagnostics")]
fn qualification_wal_autocheckpoint_override() -> Result<Option<u32>, DatabaseError> {
    let Some(value) = std::env::var_os("EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES") else {
        return Ok(None);
    };
    let value = value.to_str().ok_or_else(|| DatabaseError::Integrity {
        detail: "EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES must be ASCII digits".to_owned(),
    })?;
    parse_qualification_wal_autocheckpoint(value).map(Some)
}

#[cfg(feature = "qualification-db-diagnostics")]
fn parse_qualification_wal_autocheckpoint(value: &str) -> Result<u32, DatabaseError> {
    let pages = value.parse::<u32>().map_err(|_| DatabaseError::Integrity {
        detail: "EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES must be an integer in 0..=100000"
            .to_owned(),
    })?;
    if pages <= 100_000 {
        Ok(pages)
    } else {
        Err(DatabaseError::Integrity {
            detail:
                "EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES must be an integer in 0..=100000"
                    .to_owned(),
        })
    }
}

#[cfg(feature = "qualification-db-diagnostics")]
fn qualification_checkpoint_soft_frames_override() -> Option<u32> {
    let value = std::env::var_os("EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES")?;
    let value = value.to_str().unwrap_or("");
    parse_qualification_checkpoint_soft_frames(value).ok()
}

#[cfg(feature = "qualification-db-diagnostics")]
fn parse_qualification_checkpoint_soft_frames(value: &str) -> Result<u32, DatabaseError> {
    let frames = value.parse::<u32>().map_err(|_| DatabaseError::Integrity {
        detail: "EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES must be an integer in 1..=1000"
            .to_owned(),
    })?;
    if (1..=CheckpointMaintenancePolicy::MAX_SOFT_WAL_FRAMES).contains(&frames) {
        Ok(frames)
    } else {
        Err(DatabaseError::Integrity {
            detail: "EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES must be an integer in 1..=1000"
                .to_owned(),
        })
    }
}

/// Fail fast at startup when a qualification-only checkpoint override is
/// present but outside its bounded range. Ordinary builds never consult these
/// variables.
#[cfg(feature = "qualification-db-diagnostics")]
fn validate_qualification_checkpoint_overrides() -> Result<(), DatabaseError> {
    if let Some(raw) = std::env::var_os("EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES") {
        let raw = raw.to_str().ok_or_else(|| DatabaseError::Integrity {
            detail: "EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES must be ASCII digits".to_owned(),
        })?;
        parse_qualification_checkpoint_soft_frames(raw)?;
    }
    crate::task_supervisor::validate_qualification_checkpoint_interval()?;
    Ok(())
}

#[cfg(feature = "qualification-db-diagnostics")]
fn synchronous_name(value: i64) -> String {
    match value {
        0 => "OFF",
        1 => "NORMAL",
        2 => "FULL",
        3 => "EXTRA",
        _ => "UNKNOWN",
    }
    .to_owned()
}

#[cfg(feature = "qualification-db-diagnostics")]
fn elapsed_us(started_at: Instant) -> u64 {
    started_at.elapsed().as_micros().min(u128::from(u64::MAX)) as u64
}

fn delete_old(
    connection: &mut SqliteConnection,
    table: &str,
    id_column: &str,
    predicate: &str,
    retain_days: u64,
    limit: i64,
) -> Result<u64, SqliteError> {
    let modifier = format!("-{} days", retain_days.max(1));
    let sql = format!(
        "DELETE FROM {table} WHERE {id_column} IN (SELECT {id_column} FROM {table} WHERE {predicate} ORDER BY {id_column} LIMIT ?2)"
    );
    let changed = connection.execute(&sql, tokio_rusqlite::rusqlite::params![modifier, limit])?;
    Ok(changed as u64)
}

/// A caller-controlled SQLite transaction.  It is deliberately not a general
/// transaction abstraction: it exists to keep the runtime acceptance gate and
/// the durable config-derived state under one explicit owner.
pub struct DatabaseTransaction {
    database: Database,
    permit: Option<tokio::sync::OwnedSemaphorePermit>,
    finished: bool,
}

impl std::fmt::Debug for DatabaseTransaction {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DatabaseTransaction")
            .field("finished", &self.finished)
            .finish_non_exhaustive()
    }
}

impl DatabaseTransaction {
    pub async fn call<F, R>(&self, operation: F) -> Result<R, DatabaseError>
    where
        F: FnOnce(&mut SqliteConnection) -> Result<R, SqliteError> + Send + 'static,
        R: Send + 'static,
    {
        if self.finished {
            return Err(DatabaseError::Closed);
        }
        self.database
            .inner
            .connection
            .clone()
            .call(operation)
            .await
            .map_err(|error| match error {
                AsyncSqliteError::ConnectionClosed => DatabaseError::Closed,
                AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source) => {
                    map_sqlite(
                        "transaction operation",
                        self.database.inner.config.busy_timeout_ms,
                        source,
                    )
                }
                _ => DatabaseError::WorkerClosed,
            })
    }

    pub async fn commit(mut self) -> Result<(), DatabaseError> {
        if self.finished {
            return Err(DatabaseError::Closed);
        }
        let result = self
            .database
            .inner
            .connection
            .clone()
            .call(|connection| connection.execute_batch("COMMIT"))
            .await;
        match result {
            Ok(()) => {
                self.finished = true;
                self.permit.take();
                Ok(())
            }
            Err(error) => {
                let commit = match error {
                    AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source) => {
                        source
                    }
                    AsyncSqliteError::ConnectionClosed => {
                        return Err(DatabaseError::Closed);
                    }
                    _ => return Err(DatabaseError::WorkerClosed),
                };
                let rollback_error =
                    self.database
                        .inner
                        .connection
                        .clone()
                        .call(|connection| connection.execute_batch("ROLLBACK"))
                        .await
                        .err()
                        .and_then(|error| match error {
                            AsyncSqliteError::Close((_, source))
                            | AsyncSqliteError::Error(source) => Some(source),
                            _ => None,
                        });
                self.finished = true;
                self.permit.take();
                Err(DatabaseError::CommitFailed {
                    source: Box::new(commit),
                    rollback_error: rollback_error.map(Box::new),
                })
            }
        }
    }

    pub async fn rollback(mut self) -> Result<(), DatabaseError> {
        if self.finished {
            return Ok(());
        }
        let result = self
            .database
            .inner
            .connection
            .clone()
            .call(|connection| connection.execute_batch("ROLLBACK"))
            .await;
        self.finished = true;
        self.permit.take();
        match result {
            Ok(()) => Ok(()),
            Err(AsyncSqliteError::ConnectionClosed) => Err(DatabaseError::Closed),
            Err(AsyncSqliteError::Close((_, source)) | AsyncSqliteError::Error(source)) => {
                Err(map_sqlite(
                    "rollback",
                    self.database.inner.config.busy_timeout_ms,
                    source,
                ))
            }
            Err(_) => Err(DatabaseError::WorkerClosed),
        }
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

/// Bounded WAL frame observation shared by the maintenance tick and the
/// compatibility checkpoint path. Only scalar counters cross this boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct WalCheckpointProgress {
    busy: bool,
    log_frames: u32,
    checkpointed_frames: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MaintenanceInspection {
    Deferred,
    BelowThreshold(WalCheckpointProgress),
    Checkpointed(WalCheckpointProgress),
}

/// Observe WAL frame counts through SQLite without performing checkpoint
/// work. A busy report means another checkpoint owner is active; the caller
/// treats that as a deferral signal rather than an error.
fn query_wal_progress(
    connection: &mut SqliteConnection,
) -> Result<WalCheckpointProgress, SqliteError> {
    let (busy, log, checkpointed): (i64, i64, i64) =
        connection.query_row("PRAGMA wal_checkpoint(NOOP)", [], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?;
    Ok(WalCheckpointProgress {
        busy: busy != 0,
        log_frames: u32::try_from(log.max(0)).unwrap_or(u32::MAX),
        checkpointed_frames: u32::try_from(checkpointed.max(0)).unwrap_or(u32::MAX),
    })
}

fn run_passive_checkpoint(
    connection: &mut SqliteConnection,
) -> Result<WalCheckpointProgress, SqliteError> {
    let (_, log, checkpointed): (i64, i64, i64) =
        connection.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?;
    Ok(WalCheckpointProgress {
        busy: false,
        log_frames: u32::try_from(log.max(0)).unwrap_or(u32::MAX),
        checkpointed_frames: u32::try_from(checkpointed.max(0)).unwrap_or(u32::MAX),
    })
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

#[cfg(all(test, feature = "qualification-db-diagnostics"))]
mod qualification_tests {
    use super::*;

    #[test]
    fn wal_autocheckpoint_override_accepts_only_bounded_integers() {
        assert_eq!(parse_qualification_wal_autocheckpoint("0").unwrap(), 0);
        assert_eq!(
            parse_qualification_wal_autocheckpoint("100000").unwrap(),
            100000
        );
        for value in ["", "-1", "100001", "1.5", "secret"] {
            assert!(
                parse_qualification_wal_autocheckpoint(value).is_err(),
                "{value}"
            );
        }
    }

    #[test]
    fn checkpoint_soft_frames_override_accepts_only_bounded_values() {
        assert_eq!(parse_qualification_checkpoint_soft_frames("1").unwrap(), 1);
        assert_eq!(
            parse_qualification_checkpoint_soft_frames("1000").unwrap(),
            1000
        );
        for value in ["", "0", "1001", "1.5", "secret", "-4"] {
            assert!(
                parse_qualification_checkpoint_soft_frames(value).is_err(),
                "{value}"
            );
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn transaction_records_capture_success_and_rollback_phases() {
        let database = Database::open(DatabaseConfig::default())
            .await
            .expect("database opens");
        database
            .with_transaction(|connection| {
                connection.execute_batch("CREATE TABLE diagnostic_test (id INTEGER)")
            })
            .await
            .expect("transaction commits");
        database
            .with_transaction(|_connection| Err::<(), _>(SqliteError::InvalidQuery))
            .await
            .expect_err("transaction rolls back");
        let snapshot = database.qualification_snapshot();
        assert_eq!(snapshot.effective.journal_mode, "memory");
        assert_eq!(snapshot.records.len(), 2);
        assert!(snapshot.records[0].success);
        assert!(snapshot.records[0].commit_us.is_some());
        assert!(!snapshot.records[1].success);
        assert_eq!(snapshot.records[1].commit_us, None);
        database.close().await.expect("database closes");
    }
}

#[cfg(test)]
mod maintenance_tests {
    use super::*;
    use std::sync::atomic::AtomicU64;

    #[tokio::test(flavor = "current_thread")]
    async fn idle_tick_reports_not_due_without_sqlite_work() {
        let database = Database::open(DatabaseConfig::default())
            .await
            .expect("database opens");
        let observed = AtomicU64::new(database.transaction_count());
        let calls_before = database.stats().calls;
        let outcome = database
            .checkpoint_maintenance(CheckpointMaintenancePolicy::effective(), &observed)
            .await
            .expect("idle maintenance succeeds");
        assert_eq!(outcome, CheckpointMaintenanceOutcome::NotDue);
        assert_eq!(outcome.as_str(), "not_due");
        assert_eq!(
            database.stats().calls,
            calls_before,
            "idle tick must not touch SQLite"
        );
        database.close().await.expect("database closes");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn write_then_inspect_reports_below_threshold_and_advances_observed() {
        let database = Database::open(DatabaseConfig::default())
            .await
            .expect("database opens");
        database
            .with_transaction(|connection| {
                connection.execute_batch("CREATE TABLE maintenance_probe (id INTEGER)")
            })
            .await
            .expect("transaction commits");
        let observed = AtomicU64::new(0);
        let outcome = database
            .checkpoint_maintenance(CheckpointMaintenancePolicy::effective(), &observed)
            .await
            .expect("maintenance succeeds");
        assert!(
            matches!(outcome, CheckpointMaintenanceOutcome::BelowThreshold { .. }),
            "unexpected outcome: {outcome:?}"
        );
        assert_eq!(outcome.as_str(), "below_threshold");
        assert_eq!(observed.load(Ordering::Relaxed), 1);
        let outcome = database
            .checkpoint_maintenance(CheckpointMaintenancePolicy::effective(), &observed)
            .await
            .expect("second maintenance succeeds");
        assert_eq!(outcome, CheckpointMaintenanceOutcome::NotDue);
        database.close().await.expect("database closes");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn busy_gate_defers_without_queueing_or_advancing_observed() {
        let database = Database::open(DatabaseConfig::default())
            .await
            .expect("database opens");
        database
            .with_transaction(|connection| {
                connection.execute_batch("CREATE TABLE maintenance_busy (id INTEGER)")
            })
            .await
            .expect("transaction commits");
        // Hold the single gate through an explicit transaction. The optional
        // tick must defer instead of queueing behind this holder.
        let holder = database
            .begin_transaction()
            .await
            .expect("gate holder begins");
        let observed = AtomicU64::new(0);
        let outcome = database
            .checkpoint_maintenance(CheckpointMaintenancePolicy::effective(), &observed)
            .await
            .expect("busy maintenance defers");
        assert_eq!(outcome, CheckpointMaintenanceOutcome::GateBusy);
        assert_eq!(outcome.as_str(), "gate_busy");
        assert_eq!(
            observed.load(Ordering::Relaxed),
            0,
            "deferred tick must not advance the observed watermark"
        );
        holder.rollback().await.expect("holder rolls back");
        let outcome = database
            .checkpoint_maintenance(CheckpointMaintenancePolicy::effective(), &observed)
            .await
            .expect("maintenance succeeds after release");
        assert!(
            matches!(outcome, CheckpointMaintenanceOutcome::BelowThreshold { .. }),
            "unexpected outcome: {outcome:?}"
        );
        database.close().await.expect("database closes");
    }

    fn unique_temp_path(tag: &str) -> std::path::PathBuf {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let id = COUNTER.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "eggpool-checkpoint-maintenance-{tag}-{}-{id}.db",
            std::process::id()
        ))
    }

    fn remove_temp_database(path: &std::path::Path) {
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let mut candidate = path.as_os_str().to_owned();
            candidate.push(suffix);
            let _ = std::fs::remove_file(std::path::Path::new(&candidate));
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn file_database_reports_wal_and_unchanged_autocheckpoint_ceiling() {
        let path = unique_temp_path("ceiling");
        let database = Database::open(DatabaseConfig {
            path: path.to_string_lossy().into_owned(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("database opens");
        let journal_mode: String = database
            .call(|connection| connection.query_row("PRAGMA journal_mode", [], |row| row.get(0)))
            .await
            .expect("journal mode reads");
        assert_eq!(journal_mode.to_ascii_lowercase(), "wal");
        let autocheckpoint: i64 = database
            .call(|connection| {
                connection.query_row("PRAGMA wal_autocheckpoint", [], |row| row.get(0))
            })
            .await
            .expect("autocheckpoint reads");
        assert_eq!(
            autocheckpoint, 1000,
            "production automatic checkpoint safety ceiling must remain unchanged"
        );
        database
            .checkpoint()
            .await
            .expect("compatibility checkpoint still runs");
        database.close().await.expect("database closes");
        remove_temp_database(&path);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn low_soft_threshold_triggers_bounded_passive_checkpoint() {
        let path = unique_temp_path("trigger");
        let database = Database::open(DatabaseConfig {
            path: path.to_string_lossy().into_owned(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("database opens");
        database
            .with_transaction(|connection| {
                connection.execute_batch(
                    "CREATE TABLE maintenance_wal (id INTEGER PRIMARY KEY, body TEXT); \
                     INSERT INTO maintenance_wal (body) VALUES ('alpha'), ('beta'), ('gamma')",
                )
            })
            .await
            .expect("transaction commits");
        let observed = AtomicU64::new(0);
        let policy = CheckpointMaintenancePolicy { soft_wal_frames: 1 };
        let outcome = database
            .checkpoint_maintenance(policy, &observed)
            .await
            .expect("maintenance succeeds");
        let (log_frames, checkpointed_frames) = match outcome {
            CheckpointMaintenanceOutcome::Checkpointed {
                log_frames,
                checkpointed_frames,
            } => (log_frames, checkpointed_frames),
            CheckpointMaintenanceOutcome::BelowThreshold { log_frames } => {
                panic!("expected a due threshold with written WAL frames, saw {log_frames}")
            }
            other => panic!("unexpected outcome: {other:?}"),
        };
        assert_eq!(outcome.as_str(), "checkpointed");
        assert!(
            checkpointed_frames <= log_frames.max(checkpointed_frames),
            "frame scalars must stay bounded: {log_frames}/{checkpointed_frames}"
        );
        assert_eq!(observed.load(Ordering::Relaxed), 1);
        database.close().await.expect("database closes");
        remove_temp_database(&path);
    }
}
