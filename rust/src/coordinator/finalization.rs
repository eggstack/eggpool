//! Durable terminal convergence and retained ownership.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::watch;
use tokio_rusqlite::rusqlite::OptionalExtension;

use crate::{
    db::{Database, DatabaseError},
    routing::{ClaimError, SelectionClaim},
};

use super::{
    CoordinatorFaultInjector, CrashFaultPoint, FinalizationIdentity, PostCommitInterruption,
};

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
    pub cache_read_tokens: i64,
    pub cache_write_tokens: i64,
    pub reasoning_tokens: i64,
    pub thinking_characters: i64,
    pub cost_microdollars: i64,
    pub provider_cost_microdollars: Option<i64>,
    pub provider_cost_source: Option<String>,
    pub local_cost_microdollars: Option<i64>,
    pub local_cost_exactness: Option<String>,
    pub cache_counter_status: Option<String>,
    pub cached_input_tokens: Option<i64>,
    pub cache_read_input_tokens: Option<i64>,
    pub cache_creation_input_tokens: Option<i64>,
    pub cache_write_input_tokens: Option<i64>,
    pub cache_write_input_reported: Option<i64>,
    pub input_tokens_reported: Option<i64>,
    pub output_tokens_reported: Option<i64>,
    pub total_tokens_reported: Option<i64>,
    pub transcoded: bool,
    pub upstream_protocol: Option<String>,
    pub upstream_connect_ms: Option<i64>,
    pub upstream_read_ms: Option<i64>,
    pub coordinator_overhead_ms: Option<i64>,
    pub latency_ms: i64,
    pub first_byte_ms: Option<i64>,
    pub bytes_received: i64,
    pub bytes_emitted: i64,
    pub downstream_started: bool,
    pub upstream_request_id: Option<String>,
    pub error_class: Option<String>,
    pub error_detail: Option<String>,
    pub release_reason: Option<String>,
    pub retry_category: Option<String>,
    pub is_retry_outcome: bool,
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
    pub progress: FinalizationProgress,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Default)]
pub struct FinalizationProgress {
    pub durable_transition_checked: bool,
    pub durable_attempt_transitioned: bool,
    pub durable_reservation_converged: bool,
    pub runtime_cleanup_required: bool,
    pub quota_released: bool,
    pub active_count_released: bool,
    pub probe_released: bool,
    pub effect_progress: bool,
    pub completed: bool,
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
    #[error("durable finalization invariant failed for {entity} {id}: {reason}")]
    Invariant {
        entity: &'static str,
        id: i64,
        reason: String,
    },
    #[error("retained finalization command conflicts with an active command")]
    IncompatibleCommand,
    #[error("finalization worker exhausted bounded retries: {0}")]
    RetryExhausted(String),
    #[error("injected crash fault at {point:?}")]
    Injected { point: CrashFaultPoint },
}

#[derive(Debug, Clone)]
pub struct DurableFinalizer {
    database: Database,
    fault_injector: Option<CoordinatorFaultInjector>,
}

impl DurableFinalizer {
    pub fn new(database: Database) -> Self {
        Self {
            database,
            fault_injector: None,
        }
    }

    /// Attach a test-only crash fault injector. `None` (the default) keeps
    /// the normal finalization path unchanged.
    pub fn with_fault_injector(mut self, injector: CoordinatorFaultInjector) -> Self {
        self.fault_injector = Some(injector);
        self
    }

    fn fail_at(&self, point: CrashFaultPoint) -> Option<FinalizationError> {
        if self
            .fault_injector
            .as_ref()
            .is_some_and(|injector| injector.should_fail(point))
        {
            Some(FinalizationError::Injected { point })
        } else {
            None
        }
    }

    fn pause_at(&self, point: CrashFaultPoint) {
        if let Some(injector) = self.fault_injector.as_ref() {
            injector.pause_at(point);
        }
    }

    pub async fn finalize_request(
        &self,
        identity: &FinalizationIdentity,
        data: FinalizationData,
        claim: Option<SelectionClaim>,
    ) -> Result<FinalizationResult, FinalizationError> {
        self.pause_at(CrashFaultPoint::DurableFinalizerWriteBefore);
        if let Some(error) = self.fail_at(CrashFaultPoint::DurableFinalizerWriteBefore) {
            return Err(error);
        }
        let durable = self.finalize_durable(identity, &data, true).await?;
        self.pause_at(CrashFaultPoint::DurableFinalizerWriteAfter);
        if let Some(error) = self.fail_at(CrashFaultPoint::DurableFinalizerWriteAfter) {
            return Err(error);
        }
        let runtime_cleanup_required = claim.is_some();
        self.pause_at(CrashFaultPoint::RuntimeComponentReleaseBefore);
        if let Some(error) = self.fail_at(CrashFaultPoint::RuntimeComponentReleaseBefore) {
            return Err(error);
        }
        let runtime_released = release_claim(claim.as_ref())?;
        self.pause_at(CrashFaultPoint::RuntimeComponentReleaseAfter);
        if let Some(error) = self.fail_at(CrashFaultPoint::RuntimeComponentReleaseAfter) {
            return Err(error);
        }
        let durable_converged = durable_converged(&durable.progress);
        Ok(FinalizationResult {
            runtime_released,
            progress: FinalizationProgress {
                runtime_cleanup_required,
                quota_released: runtime_released,
                active_count_released: runtime_released,
                probe_released: runtime_released,
                completed: durable_converged && (!runtime_cleanup_required || runtime_released),
                ..durable.progress
            },
            ..durable
        })
    }

    pub async fn finalize_failed_attempt(
        &self,
        identity: &FinalizationIdentity,
        data: FinalizationData,
        claim: Option<SelectionClaim>,
    ) -> Result<FinalizationResult, FinalizationError> {
        self.pause_at(CrashFaultPoint::FailedAttemptTerminalizationBefore);
        if let Some(error) = self.fail_at(CrashFaultPoint::FailedAttemptTerminalizationBefore) {
            return Err(error);
        }
        self.pause_at(CrashFaultPoint::DurableFinalizerWriteBefore);
        if let Some(error) = self.fail_at(CrashFaultPoint::DurableFinalizerWriteBefore) {
            return Err(error);
        }
        let durable = self.finalize_durable(identity, &data, false).await?;
        self.pause_at(CrashFaultPoint::DurableFinalizerWriteAfter);
        if let Some(error) = self.fail_at(CrashFaultPoint::DurableFinalizerWriteAfter) {
            return Err(error);
        }
        self.pause_at(CrashFaultPoint::FailedAttemptTerminalizationAfter);
        if let Some(error) = self.fail_at(CrashFaultPoint::FailedAttemptTerminalizationAfter) {
            return Err(error);
        }
        let runtime_cleanup_required = claim.is_some();
        self.pause_at(CrashFaultPoint::RuntimeComponentReleaseBefore);
        if let Some(error) = self.fail_at(CrashFaultPoint::RuntimeComponentReleaseBefore) {
            return Err(error);
        }
        let runtime_released = release_claim(claim.as_ref())?;
        self.pause_at(CrashFaultPoint::RuntimeComponentReleaseAfter);
        if let Some(error) = self.fail_at(CrashFaultPoint::RuntimeComponentReleaseAfter) {
            return Err(error);
        }
        let durable_converged = durable_converged(&durable.progress);
        Ok(FinalizationResult {
            runtime_released,
            progress: FinalizationProgress {
                runtime_cleanup_required,
                quota_released: runtime_released,
                active_count_released: runtime_released,
                probe_released: runtime_released,
                completed: durable_converged && (!runtime_cleanup_required || runtime_released),
                ..durable.progress
            },
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
        let result = self.database.with_transaction(move |connection| {
            let request = connection
                .query_row(
                    "SELECT account_id, model_id, provider_id, protocol, streamed, status
                     FROM requests WHERE id = ?1",
                    [identity.db_request_id],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, String>(2)?,
                            row.get::<_, String>(3)?,
                            row.get::<_, i64>(4)?,
                            row.get::<_, String>(5)?,
                        ))
                    },
                )
                .optional()?;
            let Some((account_id, model_id, provider_id, protocol, _streamed, current)) = request
            else {
                return Ok(TxnResult::Invariant {
                    entity: "request",
                    id: identity.db_request_id,
                    reason: "row is missing".into(),
                });
            };
            let request_identity_matches = model_id == identity.model_id
                && protocol == identity.client_protocol
                && (!terminalize_request
                    || (account_id == identity.account_id
                        && provider_id == identity.provider_id));
            if !request_identity_matches {
                return Ok(TxnResult::Invariant {
                    entity: "request",
                    id: identity.db_request_id,
                    reason: if terminalize_request {
                        "identity relationship does not match"
                    } else {
                        "request model/protocol relationship does not match"
                    }
                    .into(),
                });
            }
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
                         upstream_request_id = ?8, last_attempt_id = ?9,
                         cache_read_tokens = ?11, cache_write_tokens = ?12,
                         reasoning_tokens = ?13, thinking_characters = ?14,
                         upstream_latency_ms = ?15, first_byte_ms = ?16,
                         bytes_received = ?17, bytes_emitted = ?18,
                         upstream_connect_ms = ?19, upstream_read_ms = ?20,
                         coordinator_overhead_ms = ?21,
                         cache_counter_status = ?22, cached_input_tokens = ?23,
                         cache_read_input_tokens = ?24,
                         cache_creation_input_tokens = ?25,
                         cache_write_input_tokens = ?26,
                         cache_write_input_reported = ?27,
                         input_tokens_reported = ?28, output_tokens_reported = ?29,
                         total_tokens_reported = ?30, transcoded = ?31,
                         provider_cost_microdollars = ?32,
                         provider_cost_source = ?33,
                         local_cost_microdollars = ?34,
                         local_cost_exactness = ?35,
                         upstream_protocol = ?36 WHERE id = ?10
                         AND status NOT IN ('completed','client_error','cancelled','error',
                         'interrupted','failed','client_disconnected')",
                        tokio_rusqlite::rusqlite::params![
                            target_status,
                            data.input_tokens,
                            data.output_tokens,
                            data.cost_microdollars,
                            data.status_code.map(i64::from),
                            data.error_class.as_deref(),
                            detail,
                            data.upstream_request_id.as_deref(),
                            identity.attempt_id,
                            identity.db_request_id,
                            data.cache_read_tokens,
                            data.cache_write_tokens,
                            data.reasoning_tokens,
                            data.thinking_characters,
                            data.latency_ms,
                            data.first_byte_ms,
                            data.bytes_received,
                            data.bytes_emitted,
                            data.upstream_connect_ms,
                            data.upstream_read_ms,
                            data.coordinator_overhead_ms,
                            // The column is NOT NULL: render the Python safe
                            // default when no normalized usage exists.
                            data.cache_counter_status.as_deref().unwrap_or("not_reported"),
                            data.cached_input_tokens,
                            data.cache_read_input_tokens,
                            data.cache_creation_input_tokens,
                            data.cache_write_input_tokens,
                            data.cache_write_input_reported,
                            data.input_tokens_reported,
                            data.output_tokens_reported,
                            data.total_tokens_reported,
                            i64::from(data.transcoded),
                            data.provider_cost_microdollars,
                            data.provider_cost_source.as_deref(),
                            data.local_cost_microdollars,
                            data.local_cost_exactness.as_deref(),
                            data.upstream_protocol.as_deref(),
                        ],
                    )?;
                    request_transitioned = changed == 1;
                    if !request_transitioned {
                        let observed: Option<String> = connection
                            .query_row(
                                "SELECT status FROM requests WHERE id = ?1",
                                [identity.db_request_id],
                                |row| row.get(0),
                            )
                            .optional()?;
                        if observed.as_deref() != Some(target_status.as_str()) {
                            return Ok(TxnResult::Invariant {
                                entity: "request",
                                id: identity.db_request_id,
                                reason: "zero-row terminal transition did not converge".into(),
                            });
                        }
                    }
                }
            } else if current != "pending" {
                return Ok(TxnResult::Invariant {
                    entity: "request",
                    id: identity.db_request_id,
                    reason: "failed attempt parent is not pending".into(),
                });
            }

            let attempt = connection
                .query_row(
                    "SELECT request_id, account_id, provider_id, model_id, protocol,
                            completed_at, status_code, error_class
                     FROM request_attempts WHERE id = ?1",
                    [identity.attempt_id],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, String>(2)?,
                            row.get::<_, String>(3)?,
                            row.get::<_, String>(4)?,
                            row.get::<_, Option<String>>(5)?,
                            row.get::<_, Option<i64>>(6)?,
                            row.get::<_, Option<String>>(7)?,
                        ))
                    },
                )
                .optional()?;
            let Some((attempt_request, attempt_account, attempt_provider, attempt_model, attempt_protocol, completed_at, existing_status, existing_error)) = attempt else {
                return Ok(TxnResult::Invariant { entity: "attempt", id: identity.attempt_id, reason: "row is missing".into() });
            };
            if attempt_request != identity.db_request_id
                || attempt_account != identity.account_id
                || attempt_provider != identity.provider_id
                || attempt_model != identity.model_id
                || attempt_protocol != identity.upstream_protocol
            {
                return Ok(TxnResult::Invariant { entity: "attempt", id: identity.attempt_id, reason: "identity relationship does not match".into() });
            }
            if completed_at.is_some()
                && (data
                    .status_code
                    .map(i64::from)
                    .is_some_and(|value| Some(value) != existing_status)
                    || data
                        .error_class
                        .as_deref()
                        .is_some_and(|value| Some(value) != existing_error.as_deref()))
            {
                return Ok(TxnResult::Invariant {
                    entity: "attempt",
                    id: identity.attempt_id,
                    reason: "terminal facts conflict".into(),
                });
            }
            let attempt_changed = connection.execute(
                "UPDATE request_attempts SET completed_at = CURRENT_TIMESTAMP,
                 status_code = ?1, error_class = ?2, error_detail = ?3,
                 release_reason = ?4, bytes_received = ?5, bytes_emitted = ?6,
                 latency_ms = ?7, upstream_request_id = ?8,
                 retry_category = ?10, is_retry_outcome = ?11
                 WHERE id = ?9 AND completed_at IS NULL",
                tokio_rusqlite::rusqlite::params![
                    data.status_code.map(i64::from), data.error_class.as_deref(), detail,
                    data.release_reason.as_deref(), data.bytes_received, data.bytes_emitted,
                    data.latency_ms, data.upstream_request_id.as_deref(), identity.attempt_id,
                    data.retry_category.as_deref(), i64::from(data.is_retry_outcome),
                ],
            )?;
            if attempt_changed == 0 {
                let still_terminal: Option<String> = connection
                    .query_row("SELECT completed_at FROM request_attempts WHERE id = ?1", [identity.attempt_id], |row| row.get(0))
                    .optional()?;
                if still_terminal.is_none() {
                    return Ok(TxnResult::Invariant { entity: "attempt", id: identity.attempt_id, reason: "zero-row attempt transition did not converge".into() });
                }
            }

            let reservation = connection
                .query_row(
                    "SELECT request_id, account_id, model_id, status FROM reservations WHERE id = ?1",
                    [identity.reservation_id],
                    |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, String>(2)?, row.get::<_, String>(3)?)),
                )
                .optional()?;
            let Some((reservation_request, reservation_account, reservation_model, reservation_status)) = reservation else {
                return Ok(TxnResult::Invariant { entity: "reservation", id: identity.reservation_id, reason: "row is missing".into() });
            };
            if reservation_request != identity.db_request_id
                || reservation_account != identity.account_id
                || reservation_model != identity.model_id
            {
                return Ok(TxnResult::Invariant { entity: "reservation", id: identity.reservation_id, reason: "identity relationship does not match".into() });
            }
            let reservation_changed = if reservation_status == "active" {
                connection.execute(
                    "UPDATE reservations SET status = 'released', released_at = CURRENT_TIMESTAMP,
                     release_reason = ?1 WHERE id = ?2 AND status = 'active'",
                    tokio_rusqlite::rusqlite::params![data.release_reason.as_deref().unwrap_or("finalized"), identity.reservation_id],
                )?
            } else if reservation_status == "released" || reservation_status == "expired" {
                0
            } else {
                return Ok(TxnResult::Invariant { entity: "reservation", id: identity.reservation_id, reason: "unknown terminal status".into() });
            };
            if reservation_changed == 0 && reservation_status == "active" {
                let observed: Option<String> = connection.query_row("SELECT status FROM reservations WHERE id = ?1", [identity.reservation_id], |row| row.get(0)).optional()?;
                if !matches!(observed.as_deref(), Some("released" | "expired")) {
                    return Ok(TxnResult::Invariant { entity: "reservation", id: identity.reservation_id, reason: "zero-row reservation transition did not converge".into() });
                }
            }
            Ok(TxnResult::Success {
                request_terminal: terminalize_request,
                request_transitioned,
                attempt_transitioned: attempt_changed == 1,
                reservation_transitioned: reservation_changed == 1,
            })
        }).await?;
        match result {
            TxnResult::Conflict(status) => Err(FinalizationError::TerminalConflict { status }),
            TxnResult::Invariant { entity, id, reason } => {
                Err(FinalizationError::Invariant { entity, id, reason })
            }
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
                progress: FinalizationProgress {
                    durable_transition_checked: true,
                    durable_attempt_transitioned: true,
                    durable_reservation_converged: true,
                    effect_progress: true,
                    ..FinalizationProgress::default()
                },
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
    Invariant {
        entity: &'static str,
        id: i64,
        reason: String,
    },
}

fn release_claim(claim: Option<&SelectionClaim>) -> Result<bool, FinalizationError> {
    let Some(claim) = claim else { return Ok(false) };
    claim.release_quota_reservation()?;
    claim.release_active_claim()?;
    Ok(true)
}

fn durable_converged(progress: &FinalizationProgress) -> bool {
    progress.durable_transition_checked
        && progress.durable_attempt_transitioned
        && progress.durable_reservation_converged
        && progress.effect_progress
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
    compatibility: CommandCompatibility,
}

#[derive(Debug, Clone)]
pub struct FinalizationSupervisor {
    inner: Arc<SupervisorInner>,
}

#[derive(Debug)]
struct SupervisorInner {
    finalizer: DurableFinalizer,
    capacity: usize,
    retry_delay: Duration,
    jobs: Mutex<BTreeMap<(i64, i64), JobEntry>>,
    fault_injector: Option<CoordinatorFaultInjector>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct CommandCompatibility {
    proxy_request_id: String,
    attempt_number: i64,
    account_id: i64,
    account_name: String,
    provider_id: String,
    model_id: String,
    upstream_model_id: String,
    client_protocol: String,
    upstream_protocol: String,
    reservation_id: i64,
    request_terminal: bool,
    outcome: FinalizationOutcome,
    status_code: Option<u16>,
    error_class: Option<String>,
    release_reason: Option<String>,
    input_tokens: i64,
    output_tokens: i64,
    cache_read_tokens: i64,
    cache_write_tokens: i64,
    reasoning_tokens: i64,
    thinking_characters: i64,
    cost_microdollars: i64,
    provider_cost_microdollars: Option<i64>,
    provider_cost_source: Option<String>,
    local_cost_microdollars: Option<i64>,
    local_cost_exactness: Option<String>,
    cache_counter_status: Option<String>,
    cached_input_tokens: Option<i64>,
    cache_read_input_tokens: Option<i64>,
    cache_creation_input_tokens: Option<i64>,
    cache_write_input_tokens: Option<i64>,
    cache_write_input_reported: Option<i64>,
    input_tokens_reported: Option<i64>,
    output_tokens_reported: Option<i64>,
    total_tokens_reported: Option<i64>,
    transcoded: bool,
    data_upstream_protocol: Option<String>,
    upstream_connect_ms: Option<i64>,
    upstream_read_ms: Option<i64>,
    coordinator_overhead_ms: Option<i64>,
    bytes_received: i64,
    bytes_emitted: i64,
    latency_ms: i64,
    first_byte_ms: Option<i64>,
    downstream_started: bool,
    retry_category: Option<String>,
    is_retry_outcome: bool,
    upstream_request_id: Option<String>,
}

impl FinalizationCommand {
    fn compatibility(&self) -> CommandCompatibility {
        let (identity, request_terminal, data) = match self {
            Self::Request { identity, data, .. } => (identity, true, data),
            Self::FailedAttempt { identity, data, .. } => (identity, false, data),
        };
        CommandCompatibility {
            proxy_request_id: identity.proxy_request_id.clone(),
            attempt_number: identity.attempt_number,
            account_id: identity.account_id,
            account_name: identity.account_name.clone(),
            provider_id: identity.provider_id.clone(),
            model_id: identity.model_id.clone(),
            upstream_model_id: identity.upstream_model_id.clone(),
            client_protocol: identity.client_protocol.clone(),
            upstream_protocol: identity.upstream_protocol.clone(),
            reservation_id: identity.reservation_id,
            request_terminal,
            outcome: data.outcome,
            status_code: data.status_code,
            error_class: data.error_class.clone(),
            release_reason: data.release_reason.clone(),
            input_tokens: data.input_tokens,
            output_tokens: data.output_tokens,
            cache_read_tokens: data.cache_read_tokens,
            cache_write_tokens: data.cache_write_tokens,
            reasoning_tokens: data.reasoning_tokens,
            thinking_characters: data.thinking_characters,
            cost_microdollars: data.cost_microdollars,
            provider_cost_microdollars: data.provider_cost_microdollars,
            provider_cost_source: data.provider_cost_source.clone(),
            local_cost_microdollars: data.local_cost_microdollars,
            local_cost_exactness: data.local_cost_exactness.clone(),
            cache_counter_status: data.cache_counter_status.clone(),
            cached_input_tokens: data.cached_input_tokens,
            cache_read_input_tokens: data.cache_read_input_tokens,
            cache_creation_input_tokens: data.cache_creation_input_tokens,
            cache_write_input_tokens: data.cache_write_input_tokens,
            cache_write_input_reported: data.cache_write_input_reported,
            input_tokens_reported: data.input_tokens_reported,
            output_tokens_reported: data.output_tokens_reported,
            total_tokens_reported: data.total_tokens_reported,
            transcoded: data.transcoded,
            data_upstream_protocol: data.upstream_protocol.clone(),
            upstream_connect_ms: data.upstream_connect_ms,
            upstream_read_ms: data.upstream_read_ms,
            coordinator_overhead_ms: data.coordinator_overhead_ms,
            bytes_received: data.bytes_received,
            bytes_emitted: data.bytes_emitted,
            latency_ms: data.latency_ms,
            first_byte_ms: data.first_byte_ms,
            downstream_started: data.downstream_started,
            retry_category: data.retry_category.clone(),
            is_retry_outcome: data.is_retry_outcome,
            upstream_request_id: data.upstream_request_id.clone(),
        }
    }
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
                retry_delay: Duration::from_millis(1),
                jobs: Mutex::new(BTreeMap::new()),
                fault_injector: None,
            }),
        }
    }

    pub fn with_retry_delay(mut self, retry_delay: Duration) -> Self {
        Arc::get_mut(&mut self.inner)
            .expect("retry delay configured before supervisor sharing")
            .retry_delay = retry_delay;
        self
    }

    /// Attach a test-only crash fault injector for terminal-job
    /// registration and completion boundaries. Must be called before the
    /// supervisor is shared.
    pub fn with_fault_injector(mut self, injector: CoordinatorFaultInjector) -> Self {
        Arc::get_mut(&mut self.inner)
            .expect("fault injector configured before supervisor sharing")
            .fault_injector = Some(injector);
        self
    }

    pub fn register(
        &self,
        command: FinalizationCommand,
    ) -> Result<FinalizationHandle, FinalizationError> {
        if self.inner.fault_injector.as_ref().is_some_and(|injector| {
            injector.should_fail(CrashFaultPoint::TerminalJobRegistrationBefore)
        }) {
            return Err(FinalizationError::Injected {
                point: CrashFaultPoint::TerminalJobRegistrationBefore,
            });
        }
        if let Some(injector) = self.inner.fault_injector.as_ref() {
            injector.pause_at(CrashFaultPoint::TerminalJobRegistrationBefore);
        }
        let key = command.key();
        let compatibility = command.compatibility();
        let (sender, receiver) = watch::channel(None);
        {
            let mut jobs = self.inner.jobs.lock().expect("finalization jobs lock");
            if let Some(existing) = jobs.get(&key) {
                if existing.compatibility != compatibility {
                    return Err(FinalizationError::IncompatibleCommand);
                }
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
                    compatibility,
                },
            );
        }
        if self.inner.fault_injector.as_ref().is_some_and(|injector| {
            injector.should_fail(CrashFaultPoint::TerminalJobRegistrationAfter)
        }) {
            self.inner
                .jobs
                .lock()
                .expect("finalization jobs lock")
                .remove(&key);
            return Err(FinalizationError::Injected {
                point: CrashFaultPoint::TerminalJobRegistrationAfter,
            });
        }
        if let Some(injector) = self.inner.fault_injector.as_ref() {
            injector.pause_at(CrashFaultPoint::TerminalJobRegistrationAfter);
        }
        let inner = Arc::clone(&self.inner);
        tokio::spawn(async move {
            if let Some(injector) = inner.fault_injector.as_ref() {
                injector.pause_at(CrashFaultPoint::TerminalJobCompletionBefore);
                if injector.should_fail(CrashFaultPoint::TerminalJobCompletionBefore) {
                    let _ = sender.send(Some(Err(format!(
                        "injected crash fault at {:?}",
                        CrashFaultPoint::TerminalJobCompletionBefore
                    ))));
                    inner
                        .jobs
                        .lock()
                        .expect("finalization jobs lock")
                        .remove(&key);
                    return;
                }
            }
            let result = run_command(&inner.finalizer, command, inner.retry_delay).await;
            if let Some(injector) = inner.fault_injector.as_ref() {
                injector.pause_at(CrashFaultPoint::TerminalJobCompletionAfter);
                if injector.should_fail(CrashFaultPoint::TerminalJobCompletionAfter) {
                    let _ = sender.send(Some(Err(format!(
                        "injected crash fault at {:?}",
                        CrashFaultPoint::TerminalJobCompletionAfter
                    ))));
                    inner
                        .jobs
                        .lock()
                        .expect("finalization jobs lock")
                        .remove(&key);
                    return;
                }
            }
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

    /// Return whether two coordinator handles share one retained-finalization
    /// boundary.  The generation factory uses this to make the finite and
    /// streaming paths observably share one supervisor.
    pub fn same_as(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.inner, &other.inner)
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
    retry_delay: Duration,
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
                tokio::time::sleep(retry_delay).await;
            }
        }
    }
    Err(FinalizationError::RetryExhausted(
        last.unwrap_or_else(|| "unknown finalization failure".into()),
    ))
}
