//! C010 crash/restart reconciliation and deterministic fault injection.
//!
//! A process restart is a hard boundary: every M5 active count, quota
//! reservation mirror, health probe, wire-negotiation flight, and retained
//! supervisor job vanishes with the old process. Only the durable SQLite
//! rows survive. [`CrashReconciler::reconcile_once`] converges those rows
//! without resurrecting ephemeral state, replaying unknown in-flight work,
//! or charging usage twice.
//!
//! ## Python oracle
//!
//! The frozen oracle is the historical Python `_crash_recovery` implementation. On
//! startup it marks **all** `pending` requests `interrupted`, releases
//! **all** `active` reservations with `release_reason = 'crash_recovery'`,
//! and completes **all** open attempts with
//! `error_class = 'process_interrupted'`. No time gate, no body access, no
//! cost/token mutation, and no new attempts or reservations. This module
//! freezes exactly that policy, applied through bounded indexed scans so a
//! fresh process can invoke it explicitly without a scheduler.
//!
//! ## Durable cases
//!
//! Every required C010 state converges through the same three conditional
//! updates, classified here without raw body access (only ids and status
//! labels are read):
//!
//! - request nonterminal with no attempt → request terminalized;
//! - attempt nonterminal with active reservation → both terminalized;
//! - attempt terminal with active reservation → reservation released;
//! - request terminal with nonterminal attempt/reservation → attempt and
//!   reservation terminalized, request untouched;
//! - interrupted post-commit publication → indistinguishable durably from
//!   the attempt-with-reservation case; same fail-closed convergence;
//! - failed-attempt cleanup pending while the request is still nonterminal
//!   → fail closed: the request is terminalized as `interrupted`, never
//!   left retryable and never replayed upstream;
//! - terminal request with already released/expired reservation → no-op;
//! - stale duplicate terminal command evidence → no durable write; the
//!   retained supervisor already converges duplicates (C006/C014).
//!
//! ## What reconciliation never does
//!
//! - No `INSERT` into requests, attempts, reservations, or routing
//!   decisions: repeated passes cannot fan out rows.
//! - No cost/token/byte mutation: usage stays exactly as persisted, so
//!   reconciliation can never double-charge.
//! - No M5 hydration: active counts, quota mirrors, and probes are owned
//!   by the live process. A fresh process starts at zero and stays there;
//!   reconciliation only repairs durable truth.
//! - No scheduling: M8 owns when reconciliation runs. This is an explicit
//!   one-shot primitive.
//!
//! ## Fault injection
//!
//! [`CrashFaultPoint`] names every deterministic crash boundary from the
//! C010 plan, and [`CoordinatorFaultInjector`] is the single test-only hook
//! for failing or pausing immediately before/after each one. Durable
//! boundaries (publication writes/commit, finalizer writes, runtime
//! release, supervisor registration/completion, provider send) are checked
//! in live code; purely process-local boundaries (claim acquisition, wire
//! negotiation gates, response-start handoff, stream first-byte/terminal
//! events/EOF) vanish on crash by construction, so tests simulate them by
//! placing the corresponding durable leftover and restarting fresh state
//! over the same database file.

use std::sync::{
    Arc, Barrier, Mutex,
    atomic::{AtomicBool, Ordering},
};

use serde::Serialize;
use thiserror::Error;

use crate::db::{Database, DatabaseError};

/// Maximum rows touched by one [`CrashReconciler::reconcile_once`] pass per
/// table. Keeps restart scans bounded on SBC-scale databases.
pub const DEFAULT_RECONCILIATION_BATCH_LIMIT: usize = 500;
/// Hard ceiling for an explicit batch override.
pub const MAX_RECONCILIATION_BATCH_LIMIT: usize = 5_000;

/// Every deterministic crash boundary named by the C010 plan.
///
/// Variants carry no request data, credentials, bodies, or session
/// identities — only the boundary label — so injectors are safe to share
/// across tasks and to log.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum CrashFaultPoint {
    /// Immediately before/after local M5 claim acquisition.
    LocalClaimAcquisitionBefore,
    LocalClaimAcquisitionAfter,
    /// Immediately before/after each durable publication write and commit.
    PublicationWriteBefore,
    PublicationWriteAfter,
    PublicationCommitBefore,
    PublicationCommitAfter,
    /// Immediately before/after runtime publication component conversion.
    PublicationConversionBefore,
    PublicationConversionAfter,
    /// Immediately before/after wire-negotiation gate acquisition/finish.
    WireNegotiationGateBefore,
    WireNegotiationGateAfter,
    WireNegotiationFinishBefore,
    WireNegotiationFinishAfter,
    /// Immediately before provider send and after header receipt.
    ProviderSendStartBefore,
    ProviderSendStartAfter,
    ProviderHeaderReceiptBefore,
    ProviderHeaderReceiptAfter,
    /// Immediately before/after the retry decision and failed-attempt
    /// terminalization.
    RetryDecisionBefore,
    RetryDecisionAfter,
    FailedAttemptTerminalizationBefore,
    FailedAttemptTerminalizationAfter,
    /// Immediately before/after downstream response-start handoff.
    ResponseStartHandoffBefore,
    ResponseStartHandoffAfter,
    /// Stream first byte, terminal event, and EOF boundaries.
    StreamFirstByteBefore,
    StreamFirstByteAfter,
    StreamTerminalEventBefore,
    StreamTerminalEventAfter,
    StreamEofBefore,
    StreamEofAfter,
    /// Immediately before/after retained terminal-job registration.
    TerminalJobRegistrationBefore,
    TerminalJobRegistrationAfter,
    /// Immediately before/after each durable finalizer write.
    DurableFinalizerWriteBefore,
    DurableFinalizerWriteAfter,
    /// Immediately before/after each runtime component release.
    RuntimeComponentReleaseBefore,
    RuntimeComponentReleaseAfter,
    /// Immediately before/after terminal-job completion bookkeeping.
    TerminalJobCompletionBefore,
    TerminalJobCompletionAfter,
}

/// Deterministic test-only fault hook for crash simulation.
///
/// Inert unless a test arms it with [`Self::fail_once_at`] or
/// [`Self::block_once_at`]. It carries no request data and never changes
/// the normal path when absent.
#[derive(Debug, Clone, Default)]
pub struct CoordinatorFaultInjector {
    requested: Arc<Mutex<Option<CrashFaultPoint>>>,
    fired: Arc<Mutex<Option<CrashFaultPoint>>>,
    pause: Arc<Mutex<Option<FaultPause>>>,
}

#[derive(Debug, Clone)]
struct FaultPause {
    point: CrashFaultPoint,
    barrier: Arc<Barrier>,
    entered: Arc<AtomicBool>,
}

impl CoordinatorFaultInjector {
    /// Fail exactly once at `point`; every other point passes through.
    pub fn fail_once_at(point: CrashFaultPoint) -> Self {
        Self {
            requested: Arc::new(Mutex::new(Some(point))),
            fired: Arc::new(Mutex::new(None)),
            pause: Arc::new(Mutex::new(None)),
        }
    }

    /// Block the next arrival at `point` until the test releases `barrier`.
    /// `entered` flips before parking so the test can rendezvous without a
    /// sleep.
    pub fn block_once_at(
        point: CrashFaultPoint,
        barrier: Arc<Barrier>,
        entered: Arc<AtomicBool>,
    ) -> Self {
        Self {
            requested: Arc::new(Mutex::new(None)),
            fired: Arc::new(Mutex::new(None)),
            pause: Arc::new(Mutex::new(Some(FaultPause {
                point,
                barrier,
                entered,
            }))),
        }
    }

    /// The point that fired, if any.
    pub fn fired_point(&self) -> Option<CrashFaultPoint> {
        *self.fired.lock().expect("crash fault lock")
    }

    /// Consume the armed failure if this is the armed point. Returns true
    /// exactly once per [`Self::fail_once_at`].
    pub fn should_fail(&self, point: CrashFaultPoint) -> bool {
        let mut requested = self.requested.lock().expect("crash fault lock");
        if requested.as_ref() != Some(&point) {
            return false;
        }
        *requested = None;
        *self.fired.lock().expect("crash fault lock") = Some(point);
        true
    }

    /// Park at `point` if a barrier was armed there.
    pub fn pause_at(&self, point: CrashFaultPoint) {
        let pause = self.pause.lock().expect("crash pause lock").take();
        if let Some(pause) = pause.as_ref().filter(|pause| pause.point == point) {
            pause.entered.store(true, Ordering::Release);
            pause.barrier.wait();
        } else if let Some(pause) = pause {
            *self.pause.lock().expect("crash pause lock") = Some(pause);
        }
    }
}

/// Bounds for one reconciliation pass.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReconciliationConfig {
    /// Maximum rows fixed per table per pass. Clamped to
    /// `1..=MAX_RECONCILIATION_BATCH_LIMIT`.
    pub batch_limit: usize,
}

impl Default for ReconciliationConfig {
    fn default() -> Self {
        Self {
            batch_limit: DEFAULT_RECONCILIATION_BATCH_LIMIT,
        }
    }
}

impl ReconciliationConfig {
    /// Override the per-table row bound, clamped to the hard ceiling.
    pub fn with_batch_limit(mut self, batch_limit: usize) -> Self {
        self.batch_limit = batch_limit.clamp(1, MAX_RECONCILIATION_BATCH_LIMIT);
        self
    }
}

/// Bounded classification counts for one reconciliation pass.
///
/// Counts are capped at the batch limit: a count equal to the limit means
/// more rows of that class may remain for a later pass. No row contents,
/// bodies, or secrets are retained — only scalar counts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Default)]
pub struct ReconciliationClassification {
    /// Pending requests with no attempt row (bounded sample).
    pub pending_requests_without_attempt: usize,
    /// Pending requests with at least one open attempt (includes
    /// post-commit interruptions and pending failed-attempt cleanups).
    pub pending_requests_with_open_attempt: usize,
    /// Open attempts holding an active reservation.
    pub open_attempts_with_active_reservation: usize,
    /// Open attempts whose parent request is already terminal.
    pub open_attempts_with_terminal_parent: usize,
    /// Terminal attempts still holding an active reservation.
    pub terminal_attempts_with_active_reservation: usize,
    /// Terminal requests whose reservations already converged (no-op).
    pub converged_terminal_requests: usize,
}

/// What one [`CrashReconciler::reconcile_once`] pass changed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct ReconciliationReport {
    /// Bounded classification observed before fixing.
    pub classification: ReconciliationClassification,
    /// Requests moved `pending → interrupted` by this pass.
    pub requests_interrupted: usize,
    /// Reservations moved `active → released` by this pass.
    pub reservations_released: usize,
    /// Attempts completed with `process_interrupted` by this pass.
    pub attempts_terminalized: usize,
    /// True when every fix ran under the batch bound.
    pub bounded: bool,
    /// True when any per-table fix hit the batch bound, meaning another
    /// pass may still find work.
    pub truncated: bool,
    /// True when this pass fixed nothing (durable state already converged).
    pub converged: bool,
}

impl ReconciliationReport {
    /// Total durable rows terminalized or released by this pass.
    pub fn fixed_total(&self) -> usize {
        self.requests_interrupted + self.reservations_released + self.attempts_terminalized
    }
}

#[derive(Debug, Error)]
pub enum ReconciliationError {
    #[error("crash reconciliation database operation failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("injected crash fault at {point:?}")]
    Injected { point: CrashFaultPoint },
}

/// Explicit one-shot crash/restart reconciler over M7 durable state.
///
/// Holds only a [`Database`] handle and a row bound. It never touches M5
/// active counts, quota mirrors, health probes, wire-resolver flights, or
/// retained supervisor jobs: those are process-local and a fresh process
/// correctly starts them empty.
#[derive(Debug, Clone)]
pub struct CrashReconciler {
    database: Database,
    batch_limit: usize,
    fault_injector: Option<CoordinatorFaultInjector>,
}

impl CrashReconciler {
    /// Build a reconciler with the default bounded batch limit.
    pub fn new(database: Database) -> Self {
        Self {
            database,
            batch_limit: DEFAULT_RECONCILIATION_BATCH_LIMIT,
            fault_injector: None,
        }
    }

    /// Override the per-table row bound (clamped to the hard ceiling).
    pub fn with_batch_limit(mut self, batch_limit: usize) -> Self {
        self.batch_limit = batch_limit.clamp(1, MAX_RECONCILIATION_BATCH_LIMIT);
        self
    }

    /// Attach a test-only fault injector. `None` (the default) disables
    /// all injection without changing the normal reconciliation path.
    pub fn with_fault_injector(mut self, injector: CoordinatorFaultInjector) -> Self {
        self.fault_injector = Some(injector);
        self
    }

    /// Scan the bounded nonterminal durable state and converge it to the
    /// frozen Python crash-recovery policy.
    ///
    /// Classification reads only ids and status labels through
    /// indexed/bounded queries (`requests(status, started_at)`,
    /// `reservations(status, …)`, ordered `id` scans with `LIMIT`); fixing
    /// applies three conditional bounded updates that never insert rows
    /// and never touch cost/token/byte accounting. Concurrent passes are
    /// safe: every update re-guards on its nonterminal predicate, so a
    /// racing pass simply fixes zero rows.
    pub async fn reconcile_once(&self) -> Result<ReconciliationReport, ReconciliationError> {
        if self.fault_injector.as_ref().is_some_and(|injector| {
            injector.should_fail(CrashFaultPoint::DurableFinalizerWriteBefore)
        }) {
            return Err(ReconciliationError::Injected {
                point: CrashFaultPoint::DurableFinalizerWriteBefore,
            });
        }
        let batch = i64::try_from(self.batch_limit).unwrap_or(i64::MAX);
        let classification = self.classify_bounded(batch).await?;
        let fixed = self.fix_bounded(batch).await?;
        if self.fault_injector.as_ref().is_some_and(|injector| {
            injector.should_fail(CrashFaultPoint::DurableFinalizerWriteAfter)
        }) {
            return Err(ReconciliationError::Injected {
                point: CrashFaultPoint::DurableFinalizerWriteAfter,
            });
        }
        let truncated = fixed.0 >= self.batch_limit
            || fixed.1 >= self.batch_limit
            || fixed.2 >= self.batch_limit;
        let report = ReconciliationReport {
            classification,
            requests_interrupted: fixed.0,
            reservations_released: fixed.1,
            attempts_terminalized: fixed.2,
            bounded: true,
            truncated,
            converged: fixed.0 == 0 && fixed.1 == 0 && fixed.2 == 0,
        };
        Ok(report)
    }

    async fn classify_bounded(
        &self,
        batch: i64,
    ) -> Result<ReconciliationClassification, ReconciliationError> {
        self.database
            .call(move |connection| {
                let pending_without_attempt: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM (SELECT r.id FROM requests r
                      WHERE r.status = 'pending'
                        AND NOT EXISTS (SELECT 1 FROM request_attempts a WHERE a.request_id = r.id)
                      ORDER BY r.id LIMIT ?1)",
                    [batch],
                    |row| row.get(0),
                )?;
                let pending_with_open: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM (SELECT r.id FROM requests r
                      WHERE r.status = 'pending'
                        AND EXISTS (SELECT 1 FROM request_attempts a
                                    WHERE a.request_id = r.id AND a.completed_at IS NULL)
                      ORDER BY r.id LIMIT ?1)",
                    [batch],
                    |row| row.get(0),
                )?;
                let open_with_reservation: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM (SELECT a.id FROM request_attempts a
                      JOIN reservations res ON res.request_id = a.request_id
                        AND res.account_id = a.account_id AND res.model_id = a.model_id
                      WHERE a.completed_at IS NULL AND res.status = 'active'
                      ORDER BY a.id LIMIT ?1)",
                    [batch],
                    |row| row.get(0),
                )?;
                let open_with_terminal_parent: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM (SELECT a.id FROM request_attempts a
                      JOIN requests r ON r.id = a.request_id
                      WHERE a.completed_at IS NULL AND r.status != 'pending'
                      ORDER BY a.id LIMIT ?1)",
                    [batch],
                    |row| row.get(0),
                )?;
                let terminal_with_reservation: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM (SELECT a.id FROM request_attempts a
                      JOIN reservations res ON res.request_id = a.request_id
                        AND res.account_id = a.account_id AND res.model_id = a.model_id
                      WHERE a.completed_at IS NOT NULL AND res.status = 'active'
                      ORDER BY a.id LIMIT ?1)",
                    [batch],
                    |row| row.get(0),
                )?;
                let converged_terminal: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM (SELECT r.id FROM requests r
                      WHERE r.status != 'pending'
                        AND NOT EXISTS (SELECT 1 FROM request_attempts a
                                        WHERE a.request_id = r.id AND a.completed_at IS NULL)
                        AND NOT EXISTS (SELECT 1 FROM reservations res
                                        WHERE res.request_id = r.id AND res.status = 'active')
                      ORDER BY r.id LIMIT ?1)",
                    [batch],
                    |row| row.get(0),
                )?;
                Ok(ReconciliationClassification {
                    pending_requests_without_attempt: pending_without_attempt.max(0) as usize,
                    pending_requests_with_open_attempt: pending_with_open.max(0) as usize,
                    open_attempts_with_active_reservation: open_with_reservation.max(0) as usize,
                    open_attempts_with_terminal_parent: open_with_terminal_parent.max(0) as usize,
                    terminal_attempts_with_active_reservation: terminal_with_reservation.max(0)
                        as usize,
                    converged_terminal_requests: converged_terminal.max(0) as usize,
                })
            })
            .await
            .map_err(ReconciliationError::Database)
    }

    async fn fix_bounded(&self, batch: i64) -> Result<(usize, usize, usize), ReconciliationError> {
        self.database
            .with_transaction(move |connection| {
                // Frozen Python `_crash_recovery` policy: every pending
                // request is definitively dead after process death. No
                // time gate, no cost/token mutation.
                let requests = connection.execute(
                    "UPDATE requests SET status = 'interrupted', completed_at = CURRENT_TIMESTAMP
                      WHERE id IN (SELECT id FROM requests WHERE status = 'pending'
                                   ORDER BY id LIMIT ?1)",
                    [batch],
                )?;
                // Every active reservation is released with the explicit
                // crash-recovery reason. Monetary and token estimates stay
                // exactly as persisted: no double-charge path exists here.
                let reservations = connection.execute(
                    "UPDATE reservations SET status = 'released', released_at = CURRENT_TIMESTAMP,
                      release_reason = 'crash_recovery'
                      WHERE id IN (SELECT id FROM reservations WHERE status = 'active'
                                   ORDER BY id LIMIT ?1)",
                    [batch],
                )?;
                // Every open attempt is terminalized with the explicit
                // interruption class. The request row is never resurrected
                // and no provider replay is scheduled: callers must issue a
                // brand-new proxy request id for new work.
                let attempts = connection.execute(
                    "UPDATE request_attempts SET completed_at = CURRENT_TIMESTAMP,
                      error_class = 'process_interrupted'
                      WHERE id IN (SELECT id FROM request_attempts WHERE completed_at IS NULL
                                   ORDER BY id LIMIT ?1)",
                    [batch],
                )?;
                Ok((requests, reservations, attempts))
            })
            .await
            .map_err(ReconciliationError::Database)
    }
}
