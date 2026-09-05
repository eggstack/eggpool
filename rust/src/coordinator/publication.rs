//! Durable request/attempt/reservation publication after an M5 local claim.

use std::sync::{
    Arc, Barrier, Mutex,
    atomic::{AtomicBool, Ordering},
};

use serde::Serialize;
use thiserror::Error;
use tokio::sync::oneshot;
use tokio_rusqlite::rusqlite::{OptionalExtension, params};

use crate::{
    db::{Database, DatabaseError},
    routing::{ClaimError, ClaimTransition, SelectionClaim, SelectionSnapshot},
};

const DEFAULT_RESERVATION_TTL_SECONDS: i64 = 900;

/// Immutable identity retained by every later terminal or compensation path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FinalizationIdentity {
    pub proxy_request_id: String,
    pub db_request_id: i64,
    pub attempt_id: i64,
    pub reservation_id: i64,
    pub account_id: i64,
    pub account_name: String,
    pub provider_id: String,
    pub model_id: String,
    pub client_protocol: String,
    pub upstream_protocol: String,
    pub attempt_number: i64,
}

/// The local ownership conversion record for one durable publication.
///
/// A successful publication always has `pending_load_converted = true` and
/// `pending_load_released = false`. The fields remain explicit so a later
/// retained compensation command can safely resume from a partially completed
/// post-commit transition without inferring ownership from mutable context.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RuntimePublicationReceipt {
    pub pending_request_added: bool,
    pub pending_tokens_added: bool,
    pub pending_load_converted: bool,
    pub pending_load_released: bool,
    pub active_count_added: bool,
    pub quota_reservation_added: bool,
    pub health_probe_acquired: bool,
    pub health_probe_released: bool,
    pub durable_request_id: i64,
    pub attempt_id: i64,
    pub reservation_id: i64,
    pub routing_decision_persisted: bool,
}

/// The facts supplied by the future canonical request layer to C002.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublicationInput {
    pub proxy_request_id: String,
    pub client_protocol: String,
    pub upstream_protocol: String,
    pub streamed: bool,
    pub attempt_number: i64,
    pub reservation_ttl_seconds: i64,
}

impl PublicationInput {
    pub fn new(
        proxy_request_id: impl Into<String>,
        client_protocol: impl Into<String>,
        upstream_protocol: impl Into<String>,
        streamed: bool,
        attempt_number: i64,
    ) -> Self {
        Self {
            proxy_request_id: proxy_request_id.into(),
            client_protocol: client_protocol.into(),
            upstream_protocol: upstream_protocol.into(),
            streamed,
            attempt_number,
            reservation_ttl_seconds: DEFAULT_RESERVATION_TTL_SECONDS,
        }
    }
}

/// Named deterministic boundaries used by the C002 fault matrix.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum PublicationStage {
    Validation,
    RequestInsert,
    ReservationInsert,
    AttemptInsert,
    RoutingDecisionInsert,
    BeforeCommit,
    AfterCommit,
    ClaimConversion,
}

/// One-shot fault injection for deterministic transaction and handoff tests.
///
/// The injector is inert unless explicitly supplied to
/// [`PublicationService::with_fault_injector`]. It never changes the normal
/// publication path and carries no request data, credentials, or bodies.
#[derive(Debug, Clone)]
pub struct PublicationFaultInjector {
    requested: Arc<Mutex<Option<PublicationStage>>>,
    fired: Arc<Mutex<Option<PublicationStage>>>,
    pause: Arc<Mutex<Option<PublicationPause>>>,
}

#[derive(Debug, Clone)]
struct PublicationPause {
    stage: PublicationStage,
    barrier: Arc<Barrier>,
    entered: Arc<AtomicBool>,
}

impl PublicationFaultInjector {
    pub fn fail_once_at(stage: PublicationStage) -> Self {
        Self {
            requested: Arc::new(Mutex::new(Some(stage))),
            fired: Arc::new(Mutex::new(None)),
            pause: Arc::new(Mutex::new(None)),
        }
    }

    /// Pause a synchronous transaction boundary until the test releases the
    /// barrier. `entered` makes the handoff deterministic without a sleep.
    pub fn block_once_at(
        stage: PublicationStage,
        barrier: Arc<Barrier>,
        entered: Arc<AtomicBool>,
    ) -> Self {
        Self {
            requested: Arc::new(Mutex::new(None)),
            fired: Arc::new(Mutex::new(None)),
            pause: Arc::new(Mutex::new(Some(PublicationPause {
                stage,
                barrier,
                entered,
            }))),
        }
    }

    pub fn fired_stage(&self) -> Option<PublicationStage> {
        *self.fired.lock().expect("publication fault lock")
    }

    fn take(&self, stage: PublicationStage) -> bool {
        let mut requested = self.requested.lock().expect("publication fault lock");
        if requested.as_ref() != Some(&stage) {
            return false;
        }
        *requested = None;
        *self.fired.lock().expect("publication fault lock") = Some(stage);
        true
    }

    fn pause(&self, stage: PublicationStage) {
        let pause = self.pause.lock().expect("publication pause lock").take();
        if let Some(pause) = pause.as_ref().filter(|pause| pause.stage == stage) {
            pause.entered.store(true, Ordering::Release);
            pause.barrier.wait();
        } else if let Some(pause) = pause {
            *self.pause.lock().expect("publication pause lock") = Some(pause);
        }
    }
}

#[derive(Debug, Error)]
pub enum PublicationError {
    #[error("invalid durable publication input: {0}")]
    InvalidInput(String),
    #[error("selected claim is inconsistent with durable publication: {0}")]
    ClaimIdentity(String),
    #[error("durable publication database transaction failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("durable publication failed at {stage:?}")]
    Injected { stage: PublicationStage },
    #[error("duplicate publication conflicts with existing request identity {proxy_request_id:?}")]
    DuplicateConflict { proxy_request_id: String },
    #[error("claim transition failed: {0}")]
    Claim(#[from] ClaimError),
    #[error(
        "claim compensation failed after publication failure: {compensation}; primary error: {primary}"
    )]
    Compensation {
        primary: Box<Self>,
        compensation: ClaimError,
    },
    #[error("post-commit publication interruption")]
    PostCommit {
        interruption: Box<PostCommitInterruption>,
    },
}

/// Durable identity and local claim retained when commit succeeded but
/// publication handoff was interrupted. The caller may pass it to
/// [`PublicationService::compensate_post_commit`].
#[derive(Debug)]
pub struct PostCommitInterruption {
    pub identity: FinalizationIdentity,
    pub receipt: RuntimePublicationReceipt,
    pub claim: SelectionClaim,
    pub reason: String,
}

#[derive(Debug)]
pub struct PublishedAttempt {
    pub identity: FinalizationIdentity,
    pub receipt: RuntimePublicationReceipt,
    /// The converted M5 claim remains owned by the caller for C003+ dispatch
    /// and terminal-release work. It has no Drop side effects.
    pub claim: SelectionClaim,
}

#[derive(Debug)]
pub enum PublicationOutcome {
    Published(Box<PublishedAttempt>),
    /// A duplicate invocation observed the already-published attempt. The
    /// incoming claim has been rolled back and must not be released again.
    AlreadyPublished(FinalizationIdentity),
}

#[derive(Debug)]
enum TransactionOutcome {
    Created {
        request_id: i64,
        attempt_id: i64,
        reservation_id: i64,
        routing_decision_id: i64,
    },
    Observed(FinalizationIdentity),
    Conflict,
}

/// C002 durable publication service.
#[derive(Debug, Clone)]
pub struct PublicationService {
    database: Database,
    fault_injector: Option<PublicationFaultInjector>,
}

impl PublicationService {
    pub fn new(database: Database) -> Self {
        Self {
            database,
            fault_injector: None,
        }
    }

    pub fn with_fault_injector(mut self, injector: PublicationFaultInjector) -> Self {
        self.fault_injector = Some(injector);
        self
    }

    /// Publish one M5 claim. The transaction is retained in a spawned task so
    /// cancellation of the caller cannot strand a claim while SQLite finishes
    /// its atomic closure. If the receiver is dropped, the worker compensates
    /// the durable rows and local ownership before exiting.
    pub async fn publish(
        &self,
        claim: SelectionClaim,
        input: PublicationInput,
    ) -> Result<PublicationOutcome, PublicationError> {
        let service = self.clone();
        let (sender, receiver) = oneshot::channel();
        tokio::spawn(async move {
            let outcome = service.publish_owned(claim, input).await;
            if let Err(outcome) = sender.send(outcome) {
                service.compensate_lost_delivery(outcome).await;
            }
        });
        receiver.await.map_err(|_| {
            PublicationError::InvalidInput(
                "publication worker stopped before returning an outcome".to_owned(),
            )
        })?
    }

    async fn publish_owned(
        &self,
        claim: SelectionClaim,
        input: PublicationInput,
    ) -> Result<PublicationOutcome, PublicationError> {
        if let Err(error) = self.validate(&claim, &input) {
            return self.rollback_after_error(claim, error);
        }
        let snapshot = claim.selection_snapshot().clone();
        let transaction = self.publish_transaction(&claim, &input, &snapshot).await;
        let transaction = match transaction {
            Ok(value) => value,
            Err(error) => return self.rollback_after_error(claim, error),
        };

        let TransactionOutcome::Created {
            request_id,
            attempt_id,
            reservation_id,
            routing_decision_id,
        } = transaction
        else {
            return match transaction {
                TransactionOutcome::Observed(identity) => match claim.rollback_claim() {
                    Ok(ClaimTransition::RolledBack | ClaimTransition::AlreadyTransitioned) => {
                        Ok(PublicationOutcome::AlreadyPublished(identity))
                    }
                    Ok(_) => unreachable!("rollback returned a conversion transition"),
                    Err(compensation) => Err(PublicationError::Claim(compensation)),
                },
                TransactionOutcome::Conflict => self.rollback_after_error(
                    claim,
                    PublicationError::DuplicateConflict {
                        proxy_request_id: input.proxy_request_id,
                    },
                ),
                TransactionOutcome::Created { .. } => unreachable!(),
            };
        };

        let identity = self.identity(&claim, &input, request_id, attempt_id, reservation_id);
        let receipt = RuntimePublicationReceipt {
            pending_request_added: true,
            pending_tokens_added: true,
            pending_load_converted: false,
            pending_load_released: false,
            active_count_added: true,
            quota_reservation_added: false,
            health_probe_acquired: claim.owns_probe(),
            health_probe_released: false,
            durable_request_id: request_id,
            attempt_id,
            reservation_id,
            routing_decision_persisted: routing_decision_id > 0,
        };
        if self.should_fail(PublicationStage::AfterCommit) {
            return Err(PublicationError::PostCommit {
                interruption: Box::new(PostCommitInterruption {
                    identity,
                    receipt,
                    claim,
                    reason: "injected after durable commit".to_owned(),
                }),
            });
        }
        if self.should_fail(PublicationStage::ClaimConversion) {
            return Err(PublicationError::PostCommit {
                interruption: Box::new(PostCommitInterruption {
                    identity,
                    receipt,
                    claim,
                    reason: "injected before local claim conversion".to_owned(),
                }),
            });
        }
        match claim.convert_claim_after_durable_publication() {
            Ok(ClaimTransition::Converted | ClaimTransition::AlreadyTransitioned) => {
                Ok(PublicationOutcome::Published(Box::new(PublishedAttempt {
                    identity,
                    receipt: RuntimePublicationReceipt {
                        pending_load_converted: true,
                        quota_reservation_added: true,
                        ..receipt
                    },
                    claim,
                })))
            }
            Ok(ClaimTransition::Released | ClaimTransition::RolledBack) => {
                Err(PublicationError::PostCommit {
                    interruption: Box::new(PostCommitInterruption {
                        identity,
                        receipt,
                        claim,
                        reason: "claim was not pending at conversion".to_owned(),
                    }),
                })
            }
            Err(error) => Err(PublicationError::PostCommit {
                interruption: Box::new(PostCommitInterruption {
                    identity,
                    receipt,
                    claim,
                    reason: error.to_string(),
                }),
            }),
        }
    }

    async fn publish_transaction(
        &self,
        claim: &SelectionClaim,
        input: &PublicationInput,
        snapshot: &SelectionSnapshot,
    ) -> Result<TransactionOutcome, PublicationError> {
        let database = self.database.clone();
        let input = input.clone();
        let account_name = claim.account_name().to_owned();
        let account_id = claim.account_id();
        let provider_id = claim.provider_id().to_owned();
        let model_id = claim.canonical_model_id().to_owned();
        let upstream_protocol = input.upstream_protocol.clone();
        let projected_tokens = claim.projected_tokens();
        let projected_cost = claim.projected_cost_microdollars();
        let snapshot = snapshot.clone();
        let injector = self.fault_injector.clone();
        let transaction_injector = injector.clone();
        let result = database
            .with_transaction(move |connection| {
                let existing = connection
                    .query_row(
                        "SELECT id, account_id, model_id, provider_id, protocol, streamed, status\n\
                         FROM requests WHERE proxy_request_id = ?1",
                        [&input.proxy_request_id],
                        |row| {
                            Ok((
                                row.get::<_, i64>(0)?,
                                row.get::<_, i64>(1)?,
                                row.get::<_, String>(2)?,
                                row.get::<_, String>(3)?,
                                row.get::<_, String>(4)?,
                                row.get::<_, i64>(5)?,
                                row.get::<_, String>(6)?,
                            ))
                        },
                    )
                    .optional()?;

                if let Some((
                    request_id,
                    _existing_account_id,
                    existing_model_id,
                    existing_provider_id,
                    existing_protocol,
                    existing_streamed,
                    existing_status,
                )) = existing
                {
                    if existing_model_id != model_id
                        || existing_provider_id != provider_id
                        || existing_protocol != input.client_protocol
                        || existing_streamed != i64::from(input.streamed)
                    {
                        return Ok(TransactionOutcome::Conflict);
                    }
                    if existing_status != "pending" {
                        return Ok(TransactionOutcome::Conflict);
                    }
                    let existing_attempt = connection
                        .query_row(
                            "SELECT a.id, a.account_id, a.provider_id, a.model_id,\n\
                                    a.protocol, r.id\n\
                             FROM request_attempts a\n\
                             LEFT JOIN reservations r ON r.request_id = a.request_id\n\
                                 AND r.account_id = a.account_id\n\
                                 AND r.model_id = a.model_id\n\
                             WHERE a.request_id = ?1 AND a.attempt_number = ?2\n\
                             ORDER BY r.id DESC LIMIT 1",
                            params![request_id, input.attempt_number],
                            |row| {
                                Ok((
                                    row.get::<_, i64>(0)?,
                                    row.get::<_, i64>(1)?,
                                    row.get::<_, String>(2)?,
                                    row.get::<_, String>(3)?,
                                    row.get::<_, String>(4)?,
                                    row.get::<_, Option<i64>>(5)?,
                                ))
                            },
                        )
                        .optional()?;
                    if let Some((
                        attempt_id,
                        existing_account_id,
                        existing_provider_id,
                        existing_model_id,
                        existing_protocol,
                        reservation_id,
                    )) = existing_attempt
                    {
                        if existing_account_id != account_id
                            || existing_provider_id != provider_id
                            || existing_model_id != model_id
                            || existing_protocol != upstream_protocol
                            || reservation_id.is_none()
                        {
                            return Ok(TransactionOutcome::Conflict);
                        }
                        let existing_account_name = connection.query_row(
                            "SELECT name FROM accounts WHERE id = ?1",
                            [existing_account_id],
                            |row| row.get::<_, String>(0),
                        )?;
                        return Ok(TransactionOutcome::Observed(FinalizationIdentity {
                            proxy_request_id: input.proxy_request_id,
                            db_request_id: request_id,
                            attempt_id,
                            reservation_id: reservation_id.expect("checked above"),
                            account_id: existing_account_id,
                            account_name: existing_account_name,
                            provider_id: existing_provider_id,
                            model_id: existing_model_id,
                            client_protocol: existing_protocol,
                            upstream_protocol,
                            attempt_number: input.attempt_number,
                        }));
                    }
                    // A retry attempt on an existing pending request is valid.
                    connection.execute(
                        "UPDATE requests SET account_id = ?, reserved_microdollars = ?,\n\
                         provider_id = ? WHERE id = ? AND status = 'pending'",
                        params![account_id, projected_cost, provider_id, request_id],
                    )?;
                    return insert_attempt_rows(
                        connection,
                        transaction_injector.as_ref(),
                        AttemptRows {
                            input: &input,
                            snapshot: &snapshot,
                            request_id,
                            account_id,
                            account_name: &account_name,
                            provider_id: &provider_id,
                            model_id: &model_id,
                            upstream_protocol: &upstream_protocol,
                            reservation_ttl_seconds: input.reservation_ttl_seconds,
                            projected_tokens,
                            projected_cost,
                        },
                    );
                }

                fail_sql(
                    transaction_injector.as_ref(),
                    PublicationStage::RequestInsert,
                )?;
                connection.execute(
                    "INSERT INTO requests\n\
                     (account_id, model_id, status, protocol, streamed,\n\
                      reserved_microdollars, proxy_request_id, provider_id, first_attempt_at)\n\
                     VALUES (?1, ?2, 'pending', ?3, ?4, ?5, ?6, ?7,\n\
                             CASE WHEN ?8 = 1 THEN CURRENT_TIMESTAMP ELSE NULL END)",
                    params![
                        account_id,
                        model_id,
                        input.client_protocol,
                        i64::from(input.streamed),
                        projected_cost,
                        input.proxy_request_id,
                        provider_id,
                        i64::from(input.attempt_number == 1),
                    ],
                )?;
                let request_id = connection.last_insert_rowid();
                insert_attempt_rows(
                    connection,
                    transaction_injector.as_ref(),
                    AttemptRows {
                        input: &input,
                        snapshot: &snapshot,
                        request_id,
                        account_id,
                        account_name: &account_name,
                        provider_id: &provider_id,
                        model_id: &model_id,
                        upstream_protocol: &upstream_protocol,
                        reservation_ttl_seconds: input.reservation_ttl_seconds,
                        projected_tokens,
                        projected_cost,
                    },
                )
            })
            .await;
        match result {
            Ok(outcome) => Ok(outcome),
            Err(error) => {
                if let Some(stage) = injector
                    .as_ref()
                    .and_then(PublicationFaultInjector::fired_stage)
                {
                    Err(PublicationError::Injected { stage })
                } else {
                    Err(PublicationError::Database(error))
                }
            }
        }
    }

    /// Compensate an interruption after the durable transaction committed.
    /// Both the durable attempt/reservation transition and local ownership
    /// release are idempotent.
    pub async fn compensate_post_commit(
        &self,
        interruption: &mut PostCommitInterruption,
    ) -> Result<(), PublicationError> {
        self.compensate_durable(&interruption.identity).await?;
        let transition = if interruption.receipt.pending_load_converted {
            interruption.claim.release_active_claim()?
        } else {
            interruption.claim.rollback_claim()?
        };
        if matches!(transition, ClaimTransition::Converted) {
            return Err(PublicationError::InvalidInput(
                "post-commit compensation unexpectedly converted a claim".to_owned(),
            ));
        }
        if !interruption.receipt.pending_load_converted {
            interruption.receipt.pending_load_released = true;
        }
        interruption.receipt.health_probe_released = interruption.receipt.health_probe_acquired;
        Ok(())
    }

    async fn compensate_durable(
        &self,
        identity: &FinalizationIdentity,
    ) -> Result<(), PublicationError> {
        let identity = identity.clone();
        self.database
            .with_transaction(move |connection| {
                connection.execute(
                    "UPDATE request_attempts SET error_class = 'PublicationInterrupted',\n\
                     release_reason = 'publication_interrupted',\n\
                     completed_at = CURRENT_TIMESTAMP\n\
                     WHERE id = ?1 AND completed_at IS NULL",
                    [identity.attempt_id],
                )?;
                connection.execute(
                    "UPDATE reservations SET status = 'released',\n\
                     released_at = CURRENT_TIMESTAMP,\n\
                     release_reason = 'publication_interrupted'\n\
                     WHERE id = ?1 AND status = 'active'",
                    [identity.reservation_id],
                )?;
                Ok(())
            })
            .await
            .map_err(PublicationError::Database)
    }

    fn validate(
        &self,
        claim: &SelectionClaim,
        input: &PublicationInput,
    ) -> Result<(), PublicationError> {
        if self.should_fail(PublicationStage::Validation) {
            return Err(PublicationError::Injected {
                stage: PublicationStage::Validation,
            });
        }
        for (name, value) in [
            ("proxy request ID", input.proxy_request_id.as_str()),
            ("client protocol", input.client_protocol.as_str()),
            ("upstream protocol", input.upstream_protocol.as_str()),
            ("account name", claim.account_name()),
            ("provider ID", claim.provider_id()),
            ("model ID", claim.canonical_model_id()),
        ] {
            if value.trim().is_empty() {
                return Err(PublicationError::InvalidInput(format!("{name} is empty")));
            }
        }
        if claim.account_id() <= 0 {
            return Err(PublicationError::ClaimIdentity(
                "selected account has no durable ID".to_owned(),
            ));
        }
        if input.attempt_number <= 0 {
            return Err(PublicationError::InvalidInput(
                "attempt number must be positive".to_owned(),
            ));
        }
        if claim.projected_tokens() < 0 || claim.projected_cost_microdollars() < 0 {
            return Err(PublicationError::ClaimIdentity(
                "claim estimates must be non-negative".to_owned(),
            ));
        }
        if input.reservation_ttl_seconds <= 0 {
            return Err(PublicationError::InvalidInput(
                "reservation TTL must be positive".to_owned(),
            ));
        }
        Ok(())
    }

    fn identity(
        &self,
        claim: &SelectionClaim,
        input: &PublicationInput,
        request_id: i64,
        attempt_id: i64,
        reservation_id: i64,
    ) -> FinalizationIdentity {
        FinalizationIdentity {
            proxy_request_id: input.proxy_request_id.clone(),
            db_request_id: request_id,
            attempt_id,
            reservation_id,
            account_id: claim.account_id(),
            account_name: claim.account_name().to_owned(),
            provider_id: claim.provider_id().to_owned(),
            model_id: claim.canonical_model_id().to_owned(),
            client_protocol: input.client_protocol.clone(),
            upstream_protocol: input.upstream_protocol.clone(),
            attempt_number: input.attempt_number,
        }
    }

    fn should_fail(&self, stage: PublicationStage) -> bool {
        self.fault_injector
            .as_ref()
            .is_some_and(|injector| injector.take(stage))
    }

    fn rollback_after_error(
        &self,
        claim: SelectionClaim,
        primary: PublicationError,
    ) -> Result<PublicationOutcome, PublicationError> {
        match claim.rollback_claim() {
            Ok(ClaimTransition::RolledBack | ClaimTransition::AlreadyTransitioned) => Err(primary),
            Ok(_) => unreachable!("rollback returned a conversion transition"),
            Err(compensation) => Err(PublicationError::Compensation {
                primary: Box::new(primary),
                compensation,
            }),
        }
    }

    async fn compensate_lost_delivery(
        &self,
        outcome: Result<PublicationOutcome, PublicationError>,
    ) {
        match outcome {
            Ok(PublicationOutcome::Published(published)) => {
                let mut interruption = PostCommitInterruption {
                    identity: published.identity,
                    receipt: published.receipt,
                    claim: published.claim,
                    reason: "publication result receiver was cancelled".to_owned(),
                };
                if let Err(error) = self.compensate_post_commit(&mut interruption).await {
                    tracing::error!(error = %error, "lost C002 publication result compensation failed");
                }
            }
            Err(PublicationError::PostCommit { mut interruption }) => {
                if let Err(error) = self.compensate_post_commit(&mut interruption).await {
                    tracing::error!(error = %error, "lost C002 post-commit compensation failed");
                }
            }
            Ok(PublicationOutcome::AlreadyPublished(_)) | Err(_) => {}
        }
    }
}

fn fail_sql(
    injector: Option<&PublicationFaultInjector>,
    stage: PublicationStage,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    if let Some(injector) = injector {
        injector.pause(stage);
    }
    if injector.is_some_and(|value| value.take(stage)) {
        return Err(tokio_rusqlite::rusqlite::Error::InvalidQuery);
    }
    Ok(())
}

struct AttemptRows<'a> {
    input: &'a PublicationInput,
    snapshot: &'a SelectionSnapshot,
    request_id: i64,
    account_id: i64,
    account_name: &'a str,
    provider_id: &'a str,
    model_id: &'a str,
    upstream_protocol: &'a str,
    reservation_ttl_seconds: i64,
    projected_tokens: i64,
    projected_cost: i64,
}

fn insert_attempt_rows(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    injector: Option<&PublicationFaultInjector>,
    rows: AttemptRows<'_>,
) -> Result<TransactionOutcome, tokio_rusqlite::rusqlite::Error> {
    fail_sql(injector, PublicationStage::ReservationInsert)?;
    connection.execute(
        "INSERT INTO reservations\n\
         (request_id, account_id, model_id, reserved_microdollars,\n\
          estimated_tokens, expires_at)\n\
         VALUES (?1, ?2, ?3, ?4, ?5, datetime('now', ?6))",
        params![
            rows.request_id,
            rows.account_id,
            rows.model_id,
            rows.projected_cost,
            rows.projected_tokens,
            format!("+{} seconds", rows.reservation_ttl_seconds),
        ],
    )?;
    let reservation_id = connection.last_insert_rowid();

    fail_sql(injector, PublicationStage::AttemptInsert)?;
    connection.execute(
        "INSERT INTO request_attempts\n\
         (request_id, attempt_number, account_id, provider_id, model_id, protocol, streamed)\n\
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            rows.request_id,
            rows.input.attempt_number,
            rows.account_id,
            rows.provider_id,
            rows.model_id,
            rows.upstream_protocol,
            i64::from(rows.input.streamed),
        ],
    )?;
    let attempt_id = connection.last_insert_rowid();

    fail_sql(injector, PublicationStage::RoutingDecisionInsert)?;
    let exclusion_rows = rows
        .snapshot
        .exclusions
        .iter()
        .map(|exclusion| {
            serde_json::json!({
                "account": exclusion.account_name,
                "reason": exclusion.reason_code,
            })
        })
        .collect::<Vec<_>>();
    let exclusions_json = serde_json::to_string(&exclusion_rows)
        .map_err(|_| tokio_rusqlite::rusqlite::Error::InvalidQuery)?;
    let selected_score = rows
        .snapshot
        .selected_score
        .as_ref()
        .map(|score| score.final_score());
    let top_score = rows
        .snapshot
        .candidates
        .first()
        .map(|candidate| candidate.score.final_score());
    let score_components = rows
        .snapshot
        .selected_score
        .as_ref()
        .map(serde_json::to_string)
        .transpose()
        .map_err(|_| tokio_rusqlite::rusqlite::Error::InvalidQuery)?
        .unwrap_or_else(|| "{}".to_owned());
    connection.execute(
        "INSERT INTO routing_decisions\n\
         (request_id, attempt_number, model_id, provider_id, protocol,\n\
          selected_account_id, selected_account_name, selected_tier, selected_score,\n\
          eligible_count, scored_count, attempted_excluded_count, top_score,\n\
          top_score_account_name, exclude_reasons_json, score_components_json)\n\
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
        params![
            rows.request_id,
            rows.input.attempt_number,
            rows.model_id,
            rows.provider_id,
            rows.upstream_protocol,
            rows.snapshot.selected_account_id,
            rows.account_name,
            rows.snapshot.selected_priority,
            selected_score,
            rows.snapshot.eligible_candidate_count as i64,
            rows.snapshot.candidates.len() as i64,
            rows.snapshot.exclusions.len() as i64,
            top_score,
            rows.snapshot.top_account_name,
            exclusions_json,
            score_components,
        ],
    )?;
    let routing_decision_id = connection.last_insert_rowid();

    fail_sql(injector, PublicationStage::BeforeCommit)?;
    Ok(TransactionOutcome::Created {
        request_id: rows.request_id,
        attempt_id,
        reservation_id,
        routing_decision_id,
    })
}
