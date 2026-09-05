//! Durable terminal convergence and retained ownership.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::watch;

use crate::{
    db::{Database, DatabaseError},
    routing::{ClaimError, SelectionClaim},
};

use super::{FinalizationIdentity, PostCommitInterruption};

const MAX_ERROR_DETAIL: usize = 512;
const DEFAULT_SUPERVISOR_CAPACITY: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum FinalizationOutcome {
    Completed,
    ClientError,
    UpstreamError,
    MidstreamError,
    ClientCancelled,
    Timeout,
    #[default]
    Interrupted,
}

impl FinalizationOutcome {
    fn request_status(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::ClientError => "client_error",
            Self::ClientCancelled => "cancelled",
            Self::UpstreamError | Self::MidstreamError | Self::Timeout | Self::Interrupted => {
                "error"
            }
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FinalizationData {
    pub outcome: FinalizationOutcome,
    pub status_code: Option<u16>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_microdollars: i64,
    pub latency_ms: i64,
    pub bytes_received: i64,
    pub bytes_emitted: i64,
    pub upstream_request_id: Option<String>,
    pub error_class: Option<String>,
    pub error_detail: Option<String>,
    pub release_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FinalizationResult {
    pub request_terminal: bool,
    pub request_transitioned: bool,
    pub attempt_terminal: bool,
    pub attempt_transitioned: bool,
    pub reservation_converged: bool,
    pub reservation_transitioned: bool,
    pub runtime_released: bool,
}

#[derive(Debug, Error)]
pub enum FinalizationError {
    #[error("durable finalization database operation failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("request terminal outcome conflicts with durable status {status:?}")]
    TerminalConflict { status: String },
    #[error("runtime ownership release failed: {0}")]
    Claim(#[from] ClaimError),
    #[error("finalization supervisor is at capacity")]
    Capacity,
    #[error("finalization worker exhausted bounded retries: {0}")]
    RetryExhausted(String),
}

#[derive(Debug, Clone)]
pub struct DurableFinalizer {
    database: Database,
}

impl DurableFinalizer {
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    pub async fn finalize_request(
        &self,
        identity: &FinalizationIdentity,
        data: FinalizationData,
        claim: Option<SelectionClaim>,
    ) -> Result<FinalizationResult, FinalizationError> {
        let durable = self.finalize_durable(identity, &data, true).await?;
        let runtime_released = release_claim(claim)?;
        Ok(FinalizationResult {
            runtime_released,
            ..durable
        })
    }

    pub async fn finalize_failed_attempt(
        &self,
        identity: &FinalizationIdentity,
        data: FinalizationData,
        claim: Option<SelectionClaim>,
    ) -> Result<FinalizationResult, FinalizationError> {
        let durable = self.finalize_durable(identity, &data, false).await?;
        let runtime_released = release_claim(claim)?;
        Ok(FinalizationResult {
            runtime_released,
            request_terminal: false,
            request_transitioned: false,
            ..durable
        })
    }

    pub async fn compensate_post_commit(
        &self,
        interruption: PostCommitInterruption,
    ) -> Result<FinalizationResult, FinalizationError> {
        let identity = interruption.identity;
        let claim = interruption.claim;
        self.finalize_failed_attempt(
            &identity,
            FinalizationData {
                outcome: FinalizationOutcome::Interrupted,
                error_class: Some("PublicationInterrupted".into()),
                release_reason: Some("post_commit_interrupted".into()),
                ..FinalizationData::default()
            },
            Some(claim),
        )
        .await
    }

    async fn finalize_durable(
        &self,
        identity: &FinalizationIdentity,
        data: &FinalizationData,
        terminalize_request: bool,
    ) -> Result<FinalizationResult, FinalizationError> {
        let identity = identity.clone();
        let data = data.clone();
        let target_status = data.outcome.request_status().to_owned();
        let detail = data.error_detail.as_deref().map(sanitize_detail);
        let result = self
            .database
            .with_transaction(move |connection| {
                let current: String = connection.query_row(
                    "SELECT status FROM requests WHERE id = ?1",
                    [identity.db_request_id],
                    |row| row.get(0),
                )?;
                let request_terminal = is_terminal_status(&current);
                let mut request_transitioned = false;
                if terminalize_request {
                    if request_terminal && current != target_status {
                        return Ok(TxnResult::Conflict(current));
                    }
                    if !request_terminal {
                        let changed = connection.execute(
                            "UPDATE requests SET status = ?1, completed_at = CURRENT_TIMESTAMP,
                             input_tokens = ?2, output_tokens = ?3, cost_microdollars = ?4,
                             status_code = ?5, error_class = ?6, error_detail = ?7,
                             upstream_request_id = ?8 WHERE id = ?9 AND status NOT IN
                             ('completed','client_error','cancelled','error','interrupted',
                              'failed','client_disconnected')",
                            tokio_rusqlite::rusqlite::params![
                                target_status,
                                data.input_tokens,
                                data.output_tokens,
                                data.cost_microdollars,
                                data.status_code.map(i64::from),
                                data.error_class.as_deref(),
                                detail,
                                data.upstream_request_id.as_deref(),
                                identity.db_request_id,
                            ],
                        )?;
                        request_transitioned = changed == 1;
                    }
                }
                let attempt_changed = connection.execute(
                    "UPDATE request_attempts SET completed_at = CURRENT_TIMESTAMP,
                     status_code = ?1, error_class = ?2, error_detail = ?3,
                     release_reason = ?4, bytes_received = ?5, bytes_emitted = ?6,
                     latency_ms = ?7, upstream_request_id = ?8
                     WHERE id = ?9 AND completed_at IS NULL",
                    tokio_rusqlite::rusqlite::params![
                        data.status_code.map(i64::from),
                        data.error_class.as_deref(),
                        detail,
                        data.release_reason.as_deref(),
                        data.bytes_received,
                        data.bytes_emitted,
                        data.latency_ms,
                        data.upstream_request_id.as_deref(),
                        identity.attempt_id,
                    ],
                )?;
                let reservation_changed = connection.execute(
                    "UPDATE reservations SET status = 'released', released_at = CURRENT_TIMESTAMP,
                     release_reason = ?1 WHERE id = ?2 AND status = 'active'",
                    tokio_rusqlite::rusqlite::params![
                        data.release_reason.as_deref().unwrap_or("finalized"),
                        identity.reservation_id,
                    ],
                )?;
                Ok(TxnResult::Success {
                    request_terminal: terminalize_request,
                    request_transitioned,
                    attempt_transitioned: attempt_changed == 1,
                    reservation_transitioned: reservation_changed == 1,
                })
            })
            .await?;
        match result {
            TxnResult::Conflict(status) => Err(FinalizationError::TerminalConflict { status }),
            TxnResult::Success {
                request_terminal,
                request_transitioned,
                attempt_transitioned,
                reservation_transitioned,
            } => Ok(FinalizationResult {
                request_terminal,
                request_transitioned,
                attempt_terminal: true,
                attempt_transitioned,
                reservation_converged: true,
                reservation_transitioned,
                runtime_released: false,
            }),
        }
    }
}

#[derive(Debug)]
enum TxnResult {
    Success {
        request_terminal: bool,
        request_transitioned: bool,
        attempt_transitioned: bool,
        reservation_transitioned: bool,
    },
    Conflict(String),
}

fn release_claim(claim: Option<SelectionClaim>) -> Result<bool, FinalizationError> {
    let Some(claim) = claim else { return Ok(false) };
    claim.release_quota_reservation()?;
    claim.release_active_claim()?;
    Ok(true)
}

fn is_terminal_status(status: &str) -> bool {
    matches!(
        status,
        "completed"
            | "client_error"
            | "cancelled"
            | "error"
            | "interrupted"
            | "failed"
            | "client_disconnected"
    )
}

fn sanitize_detail(value: &str) -> String {
    value
        .chars()
        .filter(|character| !character.is_control() || matches!(character, '\n' | '\t'))
        .take(MAX_ERROR_DETAIL)
        .collect()
}

#[derive(Debug, Clone)]
pub enum FinalizationCommand {
    Request {
        identity: FinalizationIdentity,
        data: FinalizationData,
        claim: Option<SelectionClaim>,
    },
    FailedAttempt {
        identity: FinalizationIdentity,
        data: FinalizationData,
        claim: Option<SelectionClaim>,
    },
}

impl FinalizationCommand {
    fn key(&self) -> (i64, i64) {
        match self {
            Self::Request { identity, .. } | Self::FailedAttempt { identity, .. } => {
                (identity.db_request_id, identity.attempt_id)
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SupervisorSnapshot {
    pub active_jobs: usize,
    pub capacity: usize,
}

#[derive(Debug, Clone)]
pub struct FinalizationHandle {
    receiver: watch::Receiver<Option<Result<FinalizationResult, String>>>,
}

impl FinalizationHandle {
    pub async fn wait(mut self) -> Result<FinalizationResult, FinalizationError> {
        loop {
            if let Some(result) = self.receiver.borrow().clone() {
                return result.map_err(FinalizationError::RetryExhausted);
            }
            self.receiver
                .changed()
                .await
                .map_err(|_| FinalizationError::RetryExhausted("worker stopped".into()))?;
        }
    }
}

#[derive(Debug)]
struct JobEntry {
    receiver: watch::Receiver<Option<Result<FinalizationResult, String>>>,
}

#[derive(Debug, Clone)]
pub struct FinalizationSupervisor {
    inner: Arc<SupervisorInner>,
}

#[derive(Debug)]
struct SupervisorInner {
    finalizer: DurableFinalizer,
    capacity: usize,
    jobs: Mutex<BTreeMap<(i64, i64), JobEntry>>,
}

impl FinalizationSupervisor {
    pub fn new(finalizer: DurableFinalizer) -> Self {
        Self::with_capacity(finalizer, DEFAULT_SUPERVISOR_CAPACITY)
    }

    pub fn with_capacity(finalizer: DurableFinalizer, capacity: usize) -> Self {
        Self {
            inner: Arc::new(SupervisorInner {
                finalizer,
                capacity: capacity.max(1),
                jobs: Mutex::new(BTreeMap::new()),
            }),
        }
    }

    pub fn register(
        &self,
        command: FinalizationCommand,
    ) -> Result<FinalizationHandle, FinalizationError> {
        let key = command.key();
        let (sender, receiver) = watch::channel(None);
        {
            let mut jobs = self.inner.jobs.lock().expect("finalization jobs lock");
            if let Some(existing) = jobs.get(&key) {
                return Ok(FinalizationHandle {
                    receiver: existing.receiver.clone(),
                });
            }
            if jobs.len() >= self.inner.capacity {
                return Err(FinalizationError::Capacity);
            }
            jobs.insert(
                key,
                JobEntry {
                    receiver: receiver.clone(),
                },
            );
        }
        let inner = Arc::clone(&self.inner);
        tokio::spawn(async move {
            let result = run_command(&inner.finalizer, command).await;
            let output = match result {
                Ok(value) => Ok(value),
                Err(error) => Err(error.to_string()),
            };
            let _ = sender.send(Some(output));
            inner
                .jobs
                .lock()
                .expect("finalization jobs lock")
                .remove(&key);
        });
        Ok(FinalizationHandle { receiver })
    }

    pub fn snapshot(&self) -> SupervisorSnapshot {
        SupervisorSnapshot {
            active_jobs: self
                .inner
                .jobs
                .lock()
                .expect("finalization jobs lock")
                .len(),
            capacity: self.inner.capacity,
        }
    }

    pub async fn drain(&self) {
        while self.snapshot().active_jobs != 0 {
            tokio::task::yield_now().await;
        }
    }

    pub async fn reconcile_once(&self) -> SupervisorSnapshot {
        tokio::task::yield_now().await;
        self.snapshot()
    }
}

async fn run_command(
    finalizer: &DurableFinalizer,
    command: FinalizationCommand,
) -> Result<FinalizationResult, FinalizationError> {
    let mut last = None;
    for _ in 0..3 {
        let result = match command.clone() {
            FinalizationCommand::Request {
                identity,
                data,
                claim,
            } => finalizer.finalize_request(&identity, data, claim).await,
            FinalizationCommand::FailedAttempt {
                identity,
                data,
                claim,
            } => {
                finalizer
                    .finalize_failed_attempt(&identity, data, claim)
                    .await
            }
        };
        match result {
            Ok(value) => return Ok(value),
            Err(error) => {
                last = Some(error.to_string());
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        }
    }
    Err(FinalizationError::RetryExhausted(
        last.unwrap_or_else(|| "unknown finalization failure".into()),
    ))
}
