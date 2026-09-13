use std::{sync::Arc, time::Instant};

use serde::Serialize;

use super::{
    GenerationSlot, GenerationSlotState, MAX_DIAGNOSTIC_PATHS, MAX_DIAGNOSTIC_TEXT_BYTES,
    RuntimeTaskSnapshot, StartupRecoveryReport,
};
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RuntimeDiagnosticsSnapshot {
    pub active_generation: ActiveGenerationDiagnostics,
    pub publication: PublicationDiagnostics,
    pub retiring_generations: Vec<RetiringGenerationDiagnostics>,
    pub reload: ReloadDiagnostics,
    pub tasks: Vec<TaskDiagnostics>,
    pub startup_recovery: Option<StartupRecoveryReport>,
    pub shutdown: ShutdownDiagnostics,
    pub counters: RuntimeDiagnosticCounters,
    pub metrics: crate::operations::metrics::MetricsSnapshot,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ActiveGenerationDiagnostics {
    pub generation_id: u64,
    pub digest_prefix: String,
    pub published_elapsed_ms: Option<u128>,
    pub active_leases: usize,
    pub provider_count: usize,
    pub account_count: usize,
    pub model_count: usize,
    pub finalization_active_jobs: usize,
    pub finalization_capacity: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PublicationDiagnostics {
    pub publication_epoch: u64,
    pub admission_closed: bool,
    pub reload_gate_closed: bool,
    pub reload_gate_waiters: usize,
    pub reload_in_progress: bool,
    pub reload_phase: String,
    pub retirement_pending: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RetiringGenerationDiagnostics {
    pub generation_id: u64,
    pub digest_prefix: String,
    pub state: GenerationSlotState,
    pub active_leases: usize,
    pub terminal_references: usize,
    pub finalization_active_jobs: usize,
    pub published_elapsed_ms: Option<u128>,
    pub failed_close: bool,
    pub close_error_category: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReloadDiagnostics {
    pub in_progress: bool,
    pub phase: String,
    pub last_result: Option<ReloadDiagnosticRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReloadDiagnosticRecord {
    pub category: String,
    pub active_generation_id: u64,
    pub active_digest_prefix: String,
    pub changed_sections: Vec<String>,
    pub restart_required_paths: Vec<String>,
    pub duration_ms: u64,
    pub reason_code: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TaskDiagnostics {
    pub name: String,
    pub ownership: String,
    pub enabled: bool,
    pub running: bool,
    pub tick_count: u64,
    pub last_outcome: Option<String>,
    pub last_elapsed_ms: Option<u64>,
    pub in_tick: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default)]
pub struct ShutdownDiagnostics {
    pub phase: String,
    pub forced: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default)]
pub struct RuntimeDiagnosticCounters {
    pub reload_attempts: u64,
    pub reload_accepted: u64,
    pub reload_failures: u64,
    pub retirement_completed: u64,
    pub retirement_failed: u64,
    pub task_transitions: u64,
    pub shutdowns: u64,
}

#[derive(Debug, Clone)]
pub(crate) struct RuntimeDiagnosticState {
    pub(crate) reload_in_progress: bool,
    pub(crate) reload_phase: String,
    pub(crate) last_reload: Option<ReloadDiagnosticRecord>,
    pub(crate) reload_started_at: Option<Instant>,
    pub(crate) reload_owner: Option<u64>,
    pub(crate) counters: RuntimeDiagnosticCounters,
    pub(crate) shutdown: ShutdownDiagnostics,
}

impl Default for RuntimeDiagnosticState {
    fn default() -> Self {
        Self {
            reload_in_progress: false,
            reload_phase: "idle".to_owned(),
            last_reload: None,
            reload_started_at: None,
            reload_owner: None,
            counters: RuntimeDiagnosticCounters::default(),
            shutdown: ShutdownDiagnostics {
                phase: "running".to_owned(),
                forced: false,
            },
        }
    }
}

pub(crate) fn digest_prefix(digest: &str) -> String {
    digest.chars().take(12).collect()
}

pub(crate) fn bounded_text(value: &str) -> String {
    value.chars().take(MAX_DIAGNOSTIC_TEXT_BYTES).collect()
}

pub(crate) fn bounded_strings(values: &[String]) -> Vec<String> {
    values
        .iter()
        .take(MAX_DIAGNOSTIC_PATHS)
        .map(|value| bounded_text(value))
        .collect()
}

pub(crate) fn format_reload_category(category: crate::reload::ReloadResultCategory) -> String {
    match category {
        crate::reload::ReloadResultCategory::Applied => "applied",
        crate::reload::ReloadResultCategory::Noop => "noop",
        crate::reload::ReloadResultCategory::RestartRequired => "restart_required",
        crate::reload::ReloadResultCategory::ValidationFailed => "validation_failed",
        crate::reload::ReloadResultCategory::StaleDigest => "stale_digest",
        crate::reload::ReloadResultCategory::Busy => "busy",
        crate::reload::ReloadResultCategory::RetirementBacklog => "retirement_backlog",
        crate::reload::ReloadResultCategory::Aborted => "aborted",
        crate::reload::ReloadResultCategory::CompensationFailed => "compensation_failed",
    }
    .to_owned()
}

pub(crate) fn task_diagnostic(task: RuntimeTaskSnapshot) -> TaskDiagnostics {
    TaskDiagnostics {
        name: bounded_text(&task.name),
        ownership: task.ownership.as_str().to_owned(),
        enabled: task.enabled,
        running: task.running,
        tick_count: task.tick_count,
        last_outcome: task.last_outcome.map(|outcome| outcome.as_str().to_owned()),
        last_elapsed_ms: task.last_elapsed_ms,
        in_tick: task.in_tick,
    }
}

pub(crate) fn retiring_diagnostic(slot: &Arc<GenerationSlot>) -> RetiringGenerationDiagnostics {
    let finalization = slot.generation().finalization_supervisor().snapshot();
    let state = slot.state();
    RetiringGenerationDiagnostics {
        generation_id: slot.generation_id(),
        digest_prefix: slot.digest_prefix().to_owned(),
        state,
        active_leases: slot.active_lease_count(),
        terminal_references: slot.terminal_reference_count(),
        finalization_active_jobs: finalization.active_jobs,
        published_elapsed_ms: slot.snapshot().published_elapsed_ms,
        failed_close: state == GenerationSlotState::FailedClose,
        close_error_category: (state == GenerationSlotState::FailedClose)
            .then(|| "generation_close_failed".to_owned()),
    }
}
