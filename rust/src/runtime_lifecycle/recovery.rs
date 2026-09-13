use crate::coordinator::{CrashReconciler, ReconciliationError};
use serde::Serialize;

use super::{MAX_STARTUP_RECONCILIATION_PASSES, ProcessRuntime};
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StartupRecoveryReport {
    pub passes: usize,
    pub requests_interrupted: usize,
    pub reservations_released: usize,
    pub attempts_terminalized: usize,
    pub last_classification: crate::coordinator::ReconciliationClassification,
    pub converged: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum StartupRecoveryError {
    #[error("startup crash reconciliation failed: {0}")]
    Reconciliation(#[from] ReconciliationError),
    #[error("startup crash reconciliation exceeded the bounded pass limit")]
    PassLimit,
}

/// Run C010 to convergence before candidate construction or request
/// acceptance. Each pass is bounded per table and the aggregate report keeps
/// only scalar, secret-free diagnostics.
pub(crate) async fn reconcile(
    process: &ProcessRuntime,
) -> Result<StartupRecoveryReport, StartupRecoveryError> {
    let reconciler = CrashReconciler::new(process.database());
    let mut aggregate = StartupRecoveryReport {
        passes: 0,
        requests_interrupted: 0,
        reservations_released: 0,
        attempts_terminalized: 0,
        last_classification: Default::default(),
        converged: false,
    };
    for _ in 0..MAX_STARTUP_RECONCILIATION_PASSES {
        let report = reconciler.reconcile_once().await?;
        aggregate.passes += 1;
        aggregate.requests_interrupted = aggregate
            .requests_interrupted
            .saturating_add(report.requests_interrupted);
        aggregate.reservations_released = aggregate
            .reservations_released
            .saturating_add(report.reservations_released);
        aggregate.attempts_terminalized = aggregate
            .attempts_terminalized
            .saturating_add(report.attempts_terminalized);
        aggregate.last_classification = report.classification;
        if !report.truncated && report.converged {
            aggregate.converged = true;
            process.set_startup_recovery_report(aggregate.clone());
            return Ok(aggregate);
        }
    }
    Err(StartupRecoveryError::PassLimit)
}
