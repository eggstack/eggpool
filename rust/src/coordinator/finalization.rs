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

/// Placeholder substituted for removed credential material.
///
/// The marker never contains any part of the value it replaces, so a
/// redacted detail cannot leak a truncated prefix of the removed secret.
const REDACTED: &str = "[redacted]";

/// Shortest standalone credential that follows a scheme word such as
/// `Bearer` and is treated as credential material. The bound keeps ordinary
/// prose such as "bearer token missing" intact.
const MIN_STANDALONE_CREDENTIAL_LENGTH: usize = 16;

/// Shortest opaque run that is treated as a high-entropy credential.
const MIN_OPAQUE_TOKEN_LENGTH: usize = 32;

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
    #[error("finalization already completed; terminal ownership was consumed")]
    AlreadyTransitioned,
    #[error("injected crash fault at {point:?}")]
    Injected { point: CrashFaultPoint },
}

impl FinalizationError {
    /// Whether re-running the same command can never succeed.
    ///
    /// These are deterministic local outcomes, not transient database or
    /// ownership contention, so the bounded retry loop must surface them
    /// directly instead of collapsing them into `RetryExhausted`.
    pub fn is_permanent(&self) -> bool {
        matches!(
            self,
            Self::TerminalConflict { .. }
                | Self::Capacity
                | Self::Invariant { .. }
                | Self::IncompatibleCommand
                | Self::AlreadyTransitioned
        )
    }
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
        let durable = self
            .finalize_durable(identity, &data, true)
            .await
            .map_err(|error| release_claim_after_durable_error(claim.as_ref(), error))?;
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
        let durable = self
            .finalize_durable(identity, &data, false)
            .await
            .map_err(|error| release_claim_after_durable_error(claim.as_ref(), error))?;
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
        let result = self.database.with_named_transaction(
            crate::db::TransactionKind::Finalization,
            move |connection| {
            let request = connection
                .query_row(
                    "SELECT account_id, model_id, provider_id, protocol, streamed, status,
                            user_id, credential_id, auth_epoch, principal_kind, request_kind, parent_proxy_request_id
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
                            row.get::<_, Option<String>>(6)?,
                            row.get::<_, Option<String>>(7)?,
                            row.get::<_, Option<i64>>(8)?,
                            row.get::<_, String>(9)?,
                            row.get::<_, String>(10)?,
                            row.get::<_, Option<String>>(11)?,
                        ))
                    },
                )
                .optional()?;
            let Some((
                account_id,
                model_id,
                provider_id,
                protocol,
                _streamed,
                current,
                user_id,
                credential_id,
                auth_epoch,
                principal_kind,
                request_kind,
                parent_proxy_request_id,
            )) = request
            else {
                return Ok(TxnResult::Invariant {
                    entity: "request",
                    id: identity.db_request_id,
                    reason: "row is missing".into(),
                });
            };
            let request_identity_matches = model_id == identity.model_id
                && protocol == identity.client_protocol
                && user_id == identity.user_id
                && credential_id == identity.credential_id
                && auth_epoch == identity.auth_epoch
                && principal_kind == identity.principal_kind.as_str()
                && request_kind == identity.request_kind.as_str()
                && parent_proxy_request_id == identity.parent_proxy_request_id
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
            crate::user_budget::record_attempt_and_maybe_settle(
                connection,
                crate::user_budget::BudgetAttemptSettlement {
                    request_id: identity.db_request_id,
                    attempt_id: identity.attempt_id,
                    attempt_transitioned: attempt_changed == 1,
                    terminalize_request,
                    input_tokens: data.input_tokens,
                    output_tokens: data.output_tokens,
                    cost_microdollars: data.cost_microdollars,
                    provider_reported_microdollars: data.provider_cost_microdollars,
                    local_estimated_microdollars: data.local_cost_microdollars,
                    tokens_known: data.input_tokens_reported.is_some()
                        || data.output_tokens_reported.is_some()
                        || data.total_tokens_reported.is_some()
                        || data.input_tokens > 0
                        || data.output_tokens > 0,
                    cost_known: data.provider_cost_microdollars.is_some()
                        || data.local_cost_microdollars.is_some()
                        || data.cost_microdollars > 0,
                },
            )?;
            // Instance-global settlement folds the same attempt facts and is
            // a no-op when the request never took an instance hold, so
            // rule-free and unattributed requests keep their existing shape.
            crate::instance_budget::record_attempt_and_maybe_settle(
                connection,
                crate::user_budget::BudgetAttemptSettlement {
                    request_id: identity.db_request_id,
                    attempt_id: identity.attempt_id,
                    attempt_transitioned: attempt_changed == 1,
                    terminalize_request,
                    input_tokens: data.input_tokens,
                    output_tokens: data.output_tokens,
                    cost_microdollars: data.cost_microdollars,
                    provider_reported_microdollars: data.provider_cost_microdollars,
                    local_estimated_microdollars: data.local_cost_microdollars,
                    tokens_known: data.input_tokens_reported.is_some()
                        || data.output_tokens_reported.is_some()
                        || data.total_tokens_reported.is_some()
                        || data.input_tokens > 0
                        || data.output_tokens > 0,
                    cost_known: data.provider_cost_microdollars.is_some()
                        || data.local_cost_microdollars.is_some()
                        || data.cost_microdollars > 0,
                },
            )?;
            Ok(TxnResult::Success {
                request_terminal: terminalize_request,
                request_transitioned,
                attempt_transitioned: attempt_changed == 1,
                reservation_transitioned: reservation_changed == 1,
            })
            },
        ).await?;
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

/// Compensate ownership when the durable terminal write fails.
///
/// `SelectionClaim` has no `Drop` side effects, so an error propagated past
/// this point would strand the account's active-request count — and any
/// converted quota reservation — for the life of the process, permanently
/// shrinking the concurrency slots routing can select. Release before
/// propagating; the caller keeps the original failure.
fn release_claim_after_durable_error(
    claim: Option<&SelectionClaim>,
    error: FinalizationError,
) -> FinalizationError {
    if let Err(release_error) = release_claim(claim) {
        tracing::error!(
            "durable finalization failed and its selection claim could not be released: \
             {release_error}"
        );
    }
    error
}

pub(crate) fn release_claim(claim: Option<&SelectionClaim>) -> Result<bool, FinalizationError> {
    let Some(claim) = claim else { return Ok(false) };
    // Active-count ownership is released first, matching `release_all` and
    // the post-commit compensation path. The active decrement is what gates
    // routing, so a fallible quota release must never be able to skip it.
    claim.release_active_claim()?;
    claim.release_quota_reservation()?;
    Ok(true)
}

/// Compensate ownership for a command the supervisor will never run.
///
/// `register` takes the command by value, so a refusal used to drop its claim
/// on the floor. `SelectionClaim` has no `Drop` side effects, so the
/// account's active-request count — and any converted quota reservation —
/// would stay consumed for the life of the process, permanently shrinking the
/// concurrency slots routing can select. Compensate exactly as terminal
/// convergence does, then surface the original refusal.
fn refuse_command(mut command: FinalizationCommand, error: FinalizationError) -> FinalizationError {
    if let Err(release_error) = release_claim(command.take_claim().as_ref()) {
        // Ownership compensation itself failed; the refusal must not be
        // reported as a clean capacity/incompatibility outcome.
        tracing::error!(
            "refused finalization command could not release its selection claim: {release_error}"
        );
    }
    error
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

/// Bound a terminal detail and remove credential-shaped material from it.
///
/// The persisted diagnostic column is operator-facing, so ordinary prose,
/// URLs, and model identifiers must survive substantially intact. The rule is
/// therefore conservative and deterministic:
///
/// - labeled credential fields (`authorization`, `x-api-key`, `api_key`,
///   `password`, `token`, …) lose their value, in the plain, the quoted, and
///   the JSON spelling of the field;
/// - known vendor token prefixes (`sk-`, `ghp_`, `xoxb-`, `AKIA`, `AIza`, …)
///   lose the whole token, whatever the case of the prefix;
/// - a standalone `Bearer`/`Basic`/`Token`/`Digest` credential and a JSON web
///   token lose the credential, not the scheme word;
/// - a URL authority that carries userinfo credentials loses the userinfo and
///   keeps the host;
/// - a long (32+ character) opaque run that mixes letters and digits is
///   treated as high-entropy credential material.
///
/// Everything else is copied verbatim. Removed material is replaced with
/// [`REDACTED`], which contains no part of the removed value. Control
/// characters other than newline and tab are dropped and the result is capped
/// at [`MAX_ERROR_DETAIL`] characters, exactly as before redaction existed.
fn sanitize_detail(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut output = String::new();
    let mut emitted = 0usize;
    let mut cursor = 0usize;
    while cursor < bytes.len() {
        if let Some((consumed, rendered)) = redact_at(value, bytes, cursor) {
            let rendered_chars = rendered.chars().count();
            if emitted + rendered_chars > MAX_ERROR_DETAIL {
                break;
            }
            emitted += rendered_chars;
            output.push_str(&rendered);
            cursor += consumed;
            continue;
        }
        let Some(character) = value.get(cursor..).and_then(|rest| rest.chars().next()) else {
            break;
        };
        let width = character.len_utf8();
        if character.is_control() && !matches!(character, '\n' | '\t') {
            cursor += width;
            continue;
        }
        if emitted + 1 > MAX_ERROR_DETAIL {
            break;
        }
        emitted += 1;
        output.push(character);
        cursor += width;
    }
    output
}

/// Field labels whose value is credential material.
///
/// Matching is case-insensitive and only applies when the label is followed by
/// a `:` or `=` separator, so usage counters such as `input_tokens` and
/// ordinary prose are not touched. The JSON spelling of a field puts the
/// closing quote of the key between the label and the separator, so that
/// quote is skipped before the separator is required.
const CREDENTIAL_LABELS: [&str; 34] = [
    "access-token",
    "access_token",
    "anthropic-api-key",
    "api-key",
    "api-secret",
    "api_key",
    "api_secret",
    "authorization",
    "client-secret",
    "client_secret",
    "credential",
    "credentials",
    "id-token",
    "id_token",
    "openai-api-key",
    "passwd",
    "password",
    "private-key",
    "private_key",
    "proxy-authorization",
    "refresh-token",
    "refresh_token",
    "secret",
    "secret-key",
    "secret_key",
    "session-token",
    "session_token",
    "token",
    "x-access-token",
    "x-api-key",
    "x-api-token",
    "x-auth-token",
    "x-goog-api-key",
    "x-secret-key",
];

/// Scheme words that name the credential which follows them.
const CREDENTIAL_SCHEMES: [&str; 4] = ["bearer", "basic", "token", "digest"];

/// Vendor token prefixes and the shortest total token length that is treated
/// as credential material.
const VENDOR_TOKEN_PREFIXES: [(&str, usize); 11] = [
    ("sk-", 16),
    ("sk_", 16),
    ("ghp_", 24),
    ("gho_", 24),
    ("ghs_", 24),
    ("ghu_", 24),
    ("github_pat_", 30),
    ("xoxb-", 16),
    ("xoxp-", 16),
    ("AKIA", 20),
    ("AIza", 30),
];

/// Remove the first credential-shaped value at `cursor`, if any.
///
/// Returns the number of bytes consumed and the text to emit in their place.
fn redact_at(value: &str, bytes: &[u8], cursor: usize) -> Option<(usize, String)> {
    match_labeled_credential(value, bytes, cursor)
        .or_else(|| match_url_userinfo(value, bytes, cursor))
        .or_else(|| match_vendor_token(bytes, cursor))
        .or_else(|| match_standalone_credential(value, bytes, cursor))
        .or_else(|| match_json_web_token(bytes, cursor))
        .or_else(|| match_long_opaque_token(bytes, cursor))
}

/// A labeled credential field, such as `Authorization: Bearer <token>` or
/// `api_key=<value>`.
fn match_labeled_credential(value: &str, bytes: &[u8], cursor: usize) -> Option<(usize, String)> {
    if cursor > 0 && is_word_byte(bytes[cursor - 1]) {
        return None;
    }
    for label in CREDENTIAL_LABELS {
        let label_bytes = label.as_bytes();
        let label_end = cursor + label_bytes.len();
        if label_end > bytes.len() || !bytes[cursor..label_end].eq_ignore_ascii_case(label_bytes) {
            continue;
        }
        let mut position = skip_whitespace(bytes, label_end);
        // The JSON spelling of a field puts the closing quote of the key
        // between the label and the separator (`"api_key":"…"`), so the quote
        // is skipped before the separator is required. The opening quote of
        // the key is the byte before the label, so the word-boundary guard
        // above already admits that spelling.
        if matches!(bytes.get(position), Some(&b'"') | Some(&b'\'')) {
            position = skip_whitespace(bytes, position + 1);
        }
        let Some(separator) = bytes.get(position).copied() else {
            continue;
        };
        if separator != b':' && separator != b'=' {
            continue;
        }
        position += 1;
        position = skip_whitespace(bytes, position);
        if matches!(bytes.get(position), Some(&b'"') | Some(&b'\'')) {
            position += 1;
        }
        let value_start = position;
        let value_end = scan_credential_run(bytes, value_start);
        if value_end == value_start {
            continue;
        }
        let (secret_start, secret_end) = match scheme_credential(bytes, value_start, value_end) {
            Some(credential) => credential,
            None => (value_start, value_end),
        };
        return Some((
            secret_end - cursor,
            render_redaction(value, cursor, secret_start),
        ));
    }
    None
}

/// A vendor-prefixed token, such as `sk-…` or an AWS access key id.
///
/// The prefix is matched case-insensitively, like the field label, so an
/// upper-case spelling of a prefix is redacted as well. `AKIA` and `AIza` are
/// exact-case in the real world, so the wider match only over-redacts.
fn match_vendor_token(bytes: &[u8], cursor: usize) -> Option<(usize, String)> {
    for (prefix, minimum_length) in VENDOR_TOKEN_PREFIXES {
        let prefix_bytes = prefix.as_bytes();
        let prefix_end = cursor + prefix_bytes.len();
        if prefix_end > bytes.len() || !bytes[cursor..prefix_end].eq_ignore_ascii_case(prefix_bytes)
        {
            continue;
        }
        let token_end = scan_credential_run(bytes, prefix_end);
        if token_end - cursor < minimum_length {
            continue;
        }
        return Some((token_end - cursor, REDACTED.to_owned()));
    }
    None
}

/// A standalone credential scheme word (`Bearer`, `Basic`, `Token`, `Digest`)
/// that carries no field label.
///
/// The scheme word and the whitespace that follows it stay visible; only the
/// credential that follows is removed.
fn match_standalone_credential(
    value: &str,
    bytes: &[u8],
    cursor: usize,
) -> Option<(usize, String)> {
    if cursor > 0 && is_word_byte(bytes[cursor - 1]) {
        return None;
    }
    for scheme in CREDENTIAL_SCHEMES {
        let scheme_bytes = scheme.as_bytes();
        let scheme_end = cursor + scheme_bytes.len();
        if scheme_end > bytes.len() || !bytes[cursor..scheme_end].eq_ignore_ascii_case(scheme_bytes)
        {
            continue;
        }
        if !matches!(bytes.get(scheme_end), Some(&b' ') | Some(&b'\t')) {
            continue;
        }
        let value_start = skip_whitespace(bytes, scheme_end);
        let value_end = scan_credential_run(bytes, value_start);
        if value_end - value_start < MIN_STANDALONE_CREDENTIAL_LENGTH {
            continue;
        }
        return Some((
            value_end - cursor,
            render_redaction(value, cursor, value_start),
        ));
    }
    None
}

/// A URL authority that carries userinfo credentials.
///
/// The userinfo section of the authority (`user:secret@host`) is credential
/// material and is removed; the host is not, so the host survives and the
/// diagnostic still shows which upstream was contacted. This matches the
/// authority rule the status projection already applies. A URL without
/// userinfo, and a `mailto:`-style or bare `word@word` fragment that is not
/// an authority, is left untouched.
fn match_url_userinfo(value: &str, bytes: &[u8], cursor: usize) -> Option<(usize, String)> {
    if cursor > 0 && is_word_byte(bytes[cursor - 1]) {
        return None;
    }
    let scheme_end = scan_url_scheme(bytes, cursor);
    if scheme_end == cursor || scheme_end + 3 > bytes.len() {
        return None;
    }
    if bytes[scheme_end..scheme_end + 3] != *b"://" {
        return None;
    }
    let authority_start = scheme_end + 3;
    let authority_end = scan_authority(bytes, authority_start);
    let authority = &bytes[authority_start..authority_end];
    // The userinfo section ends at the last `@` of the authority.
    let userinfo_len = authority.iter().rposition(|byte| *byte == b'@')?;
    if userinfo_len == 0 {
        return None;
    }
    let host_start = authority_start + userinfo_len + 1;
    let mut rendered = String::new();
    rendered.push_str(&value[cursor..authority_start]);
    rendered.push_str(REDACTED);
    rendered.push('@');
    rendered.push_str(&value[host_start..authority_end]);
    Some((authority_end - cursor, rendered))
}

/// A JSON web token: three base64url segments separated by dots.
fn match_json_web_token(bytes: &[u8], cursor: usize) -> Option<(usize, String)> {
    const JWT_PREFIX: &[u8] = b"eyJ";
    let prefix_end = cursor + JWT_PREFIX.len();
    if prefix_end > bytes.len() || &bytes[cursor..prefix_end] != JWT_PREFIX {
        return None;
    }
    let first_end = scan_jwt_segment(bytes, prefix_end);
    if first_end == prefix_end || bytes.get(first_end) != Some(&b'.') {
        return None;
    }
    let second_start = first_end + 1;
    let second_end = scan_jwt_segment(bytes, second_start);
    if second_end == second_start || bytes.get(second_end) != Some(&b'.') {
        return None;
    }
    let third_start = second_end + 1;
    let third_end = scan_jwt_segment(bytes, third_start);
    if third_end == third_start {
        return None;
    }
    Some((third_end - cursor, REDACTED.to_owned()))
}

/// A long opaque run that mixes letters and digits: the shape of a
/// high-entropy credential that carries no recognizable label or prefix.
fn match_long_opaque_token(bytes: &[u8], cursor: usize) -> Option<(usize, String)> {
    if cursor > 0 && is_opaque_byte(bytes[cursor - 1]) {
        return None;
    }
    let end = scan_opaque_run(bytes, cursor);
    if end - cursor < MIN_OPAQUE_TOKEN_LENGTH {
        return None;
    }
    let run = &bytes[cursor..end];
    if !run.iter().any(|byte| byte.is_ascii_digit())
        || !run.iter().any(|byte| byte.is_ascii_alphabetic())
    {
        return None;
    }
    Some((end - cursor, REDACTED.to_owned()))
}

/// The credential that follows a scheme word, when the scheme word names one.
fn scheme_credential(bytes: &[u8], value_start: usize, value_end: usize) -> Option<(usize, usize)> {
    let word = &bytes[value_start..value_end];
    let names_credential = CREDENTIAL_SCHEMES
        .iter()
        .any(|scheme| word.eq_ignore_ascii_case(scheme.as_bytes()));
    if !names_credential {
        return None;
    }
    let credential_start = skip_whitespace(bytes, value_end);
    let credential_end = scan_credential_run(bytes, credential_start);
    (credential_end > credential_start).then_some((credential_start, credential_end))
}

/// Keep the label, separator, and scheme word so the diagnostic still shows
/// which credential field was present, and mark the value as removed.
fn render_redaction(value: &str, cursor: usize, secret_start: usize) -> String {
    let mut rendered = String::new();
    for character in value[cursor..secret_start].chars() {
        if character.is_control() && !matches!(character, '\n' | '\t') {
            continue;
        }
        rendered.push(character);
    }
    rendered.push_str(REDACTED);
    rendered
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

/// Characters that can be part of a credential value.
///
/// Line breaks and tabs end a value because they end a header or a field;
/// other control characters stay inside it so an embedded control byte cannot
/// split a credential into a redactable prefix and a surviving suffix.
fn is_credential_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(
            byte,
            b'_' | b'-' | b'.' | b'~' | b'+' | b'/' | b'=' | b':' | b'%'
        )
        || (byte.is_ascii_control() && !matches!(byte, b'\n' | b'\r' | b'\t'))
}

/// Characters that can be part of an opaque high-entropy token.
fn is_opaque_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(byte, b'_' | b'-' | b'+' | b'/' | b'=')
        || (byte.is_ascii_control() && !matches!(byte, b'\n' | b'\r' | b'\t'))
}

fn skip_whitespace(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && bytes[index].is_ascii_whitespace() {
        index += 1;
    }
    index
}

fn scan_credential_run(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && is_credential_byte(bytes[index]) {
        index += 1;
    }
    index
}

fn scan_opaque_run(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && is_opaque_byte(bytes[index]) {
        index += 1;
    }
    index
}

/// Scan a URL scheme token that starts at `index`.
///
/// A scheme starts with an ASCII letter and continues with letters, digits,
/// `+`, `-`, or `.`. The scan stops at the first byte that cannot continue a
/// scheme, so the caller can require the `://` that follows it.
fn scan_url_scheme(bytes: &[u8], index: usize) -> usize {
    if index >= bytes.len() || !bytes[index].is_ascii_alphabetic() {
        return index;
    }
    let mut cursor = index + 1;
    while cursor < bytes.len()
        && (bytes[cursor].is_ascii_alphanumeric() || matches!(bytes[cursor], b'+' | b'-' | b'.'))
    {
        cursor += 1;
    }
    cursor
}

/// Scan the authority of a URL that starts at `index`.
///
/// The authority ends at the first path, query, fragment, or whitespace byte,
/// so a prose fragment that follows the URL is never swallowed.
fn scan_authority(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && is_authority_byte(bytes[index]) {
        index += 1;
    }
    index
}

/// Bytes that can appear in a URL authority.
fn is_authority_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(
            byte,
            b'-' | b'.' | b'_' | b'~' | b'%' | b'+' | b':' | b'@' | b'[' | b']'
        )
}

fn scan_jwt_segment(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len()
        && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_' || bytes[index] == b'-')
    {
        index += 1;
    }
    index
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

    fn take_claim(&mut self) -> Option<SelectionClaim> {
        match self {
            Self::Request { claim, .. } | Self::FailedAttempt { claim, .. } => claim.take(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SupervisorSnapshot {
    pub active_jobs: usize,
    pub capacity: usize,
    pub last_failure: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum FinalizationDrainError {
    #[error("retained finalization worker failed: {detail}")]
    WorkerFailure { detail: String },
    #[error("retained finalization drain timed out with {active_jobs} active jobs")]
    Timeout { active_jobs: usize },
}

#[derive(Debug, Clone)]
pub struct FinalizationHandle {
    receiver: watch::Receiver<Option<Result<FinalizationResult, String>>>,
}

pub trait TerminalReference: std::fmt::Debug + Send + Sync {}

pub trait TerminalReferenceOwner: std::fmt::Debug + Send + Sync {
    fn retain_terminal_reference(&self) -> Option<Box<dyn TerminalReference>>;
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
    terminal_reference: Option<Box<dyn TerminalReference>>,
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
    last_failure: Mutex<Option<String>>,
    terminal_owner: Mutex<Option<Arc<dyn TerminalReferenceOwner>>>,
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
                last_failure: Mutex::new(None),
                terminal_owner: Mutex::new(None),
                fault_injector: None,
            }),
        }
    }

    pub fn with_retry_delay(mut self, retry_delay: Duration) -> Self {
        if let Some(inner) = Arc::get_mut(&mut self.inner) {
            inner.retry_delay = retry_delay;
        } else {
            debug_assert!(
                false,
                "retry delay must be configured before supervisor sharing"
            );
        }
        self
    }

    /// Attach a test-only crash fault injector for terminal-job
    /// registration and completion boundaries. Must be called before the
    /// supervisor is shared.
    pub fn with_fault_injector(mut self, injector: CoordinatorFaultInjector) -> Self {
        if let Some(inner) = Arc::get_mut(&mut self.inner) {
            inner.fault_injector = Some(injector);
        } else {
            debug_assert!(
                false,
                "fault injector must be configured before supervisor sharing"
            );
        }
        self
    }

    pub fn set_terminal_owner(&self, owner: Arc<dyn TerminalReferenceOwner>) {
        *self
            .inner
            .terminal_owner
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(owner);
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
            let mut jobs = self.inner.jobs.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(existing) = jobs.get(&key) {
                if existing.compatibility != compatibility {
                    return Err(refuse_command(
                        command,
                        FinalizationError::IncompatibleCommand,
                    ));
                }
                return Ok(FinalizationHandle {
                    receiver: existing.receiver.clone(),
                });
            }
            if jobs.len() >= self.inner.capacity {
                return Err(refuse_command(command, FinalizationError::Capacity));
            }
            let terminal_reference = self
                .inner
                .terminal_owner
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .as_ref()
                .and_then(|owner| owner.retain_terminal_reference());
            jobs.insert(
                key,
                JobEntry {
                    receiver: receiver.clone(),
                    compatibility,
                    terminal_reference,
                },
            );
        }
        if self.inner.fault_injector.as_ref().is_some_and(|injector| {
            injector.should_fail(CrashFaultPoint::TerminalJobRegistrationAfter)
        }) {
            self.inner
                .jobs
                .lock()
                .unwrap_or_else(|e| e.into_inner())
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
                    *inner.last_failure.lock().unwrap_or_else(|e| e.into_inner()) = Some(format!(
                        "injected crash fault at {:?}",
                        CrashFaultPoint::TerminalJobCompletionBefore
                    ));
                    let _ = sender.send(Some(Err(format!(
                        "injected crash fault at {:?}",
                        CrashFaultPoint::TerminalJobCompletionBefore
                    ))));
                    remove_job(&inner, key);
                    return;
                }
            }
            let result = run_command(&inner.finalizer, command, inner.retry_delay).await;
            if let Some(injector) = inner.fault_injector.as_ref() {
                injector.pause_at(CrashFaultPoint::TerminalJobCompletionAfter);
                if injector.should_fail(CrashFaultPoint::TerminalJobCompletionAfter) {
                    *inner.last_failure.lock().unwrap_or_else(|e| e.into_inner()) = Some(format!(
                        "injected crash fault at {:?}",
                        CrashFaultPoint::TerminalJobCompletionAfter
                    ));
                    let _ = sender.send(Some(Err(format!(
                        "injected crash fault at {:?}",
                        CrashFaultPoint::TerminalJobCompletionAfter
                    ))));
                    remove_job(&inner, key);
                    return;
                }
            }
            let output = match result {
                Ok(value) => Ok(value),
                Err(error) => {
                    let detail = sanitize_detail(&error.to_string());
                    *inner.last_failure.lock().unwrap_or_else(|e| e.into_inner()) =
                        Some(detail.clone());
                    Err(detail)
                }
            };
            let _ = sender.send(Some(output));
            remove_job(&inner, key);
        });
        Ok(FinalizationHandle { receiver })
    }

    pub fn snapshot(&self) -> SupervisorSnapshot {
        SupervisorSnapshot {
            active_jobs: self
                .inner
                .jobs
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .len(),
            capacity: self.inner.capacity,
            last_failure: self
                .inner
                .last_failure
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone(),
        }
    }

    /// Return whether two coordinator handles share one retained-finalization
    /// boundary.  The generation factory uses this to make the finite and
    /// streaming paths observably share one supervisor.
    pub fn same_as(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.inner, &other.inner)
    }

    pub async fn drain(&self) {
        let _ = self.drain_with_timeout(Duration::from_secs(30)).await;
    }

    /// Drain retained terminal work while preserving a typed worker failure.
    /// The lifecycle manager supplies its own short deadline; the compatibility
    /// `drain` method above intentionally retains its historical unbounded
    /// behavior for existing M7 callers.
    pub async fn drain_with_timeout(
        &self,
        timeout_duration: Duration,
    ) -> Result<SupervisorSnapshot, FinalizationDrainError> {
        let result = tokio::time::timeout(timeout_duration, async {
            loop {
                let snapshot = self.snapshot();
                if let Some(detail) = snapshot.last_failure.clone() {
                    return Err(FinalizationDrainError::WorkerFailure { detail });
                }
                if snapshot.active_jobs == 0 {
                    return Ok(snapshot);
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await;
        match result {
            Ok(result) => result,
            Err(_) => Err(FinalizationDrainError::Timeout {
                active_jobs: self.snapshot().active_jobs,
            }),
        }
    }

    pub async fn reconcile_once(&self) -> SupervisorSnapshot {
        tokio::task::yield_now().await;
        self.snapshot()
    }
}

fn remove_job(inner: &SupervisorInner, key: (i64, i64)) {
    let Some(entry) = inner
        .jobs
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .remove(&key)
    else {
        return;
    };
    drop(entry.terminal_reference);
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
                // A permanent error cannot succeed on a retry: re-running it
                // only burns the bounded delay and hides the real variant
                // behind `RetryExhausted` at the call site.
                if error.is_permanent() {
                    return Err(error);
                }
                last = Some(error.to_string());
                tokio::time::sleep(retry_delay).await;
            }
        }
    }
    Err(FinalizationError::RetryExhausted(
        last.unwrap_or_else(|| "unknown finalization failure".into()),
    ))
}
