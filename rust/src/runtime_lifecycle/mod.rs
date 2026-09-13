//! Process/generation ownership and publication for the native runtime.
//!
//! The process owns the active-generation manager and the manager publishes
//! immutable generation slots. Request work receives an explicit lease from
//! that manager; the lease pins the generation across every await and releases
//! only when its owning request/body task is finished.
//!
//! The implementation is split by lifecycle ownership. This facade keeps the
//! historical `runtime_lifecycle` API stable for server, reload, operations,
//! and integration-test callers.

mod diagnostics;
mod generation;
mod lease;
mod manager;
mod process;
mod recovery;

pub use diagnostics::{
    ActiveGenerationDiagnostics, PublicationDiagnostics, ReloadDiagnosticRecord, ReloadDiagnostics,
    RuntimeDiagnosticCounters, RuntimeDiagnosticsSnapshot, ShutdownDiagnostics, TaskDiagnostics,
};
pub use generation::{
    CandidateAbortReport, CandidateOwnership, CandidateTransferError, GenerationBuildError,
    GenerationCloseFailure, GenerationCloseReport, GenerationCloseStep, PreparedGeneration,
    RuntimeGeneration, RuntimeGenerationFactory,
};
pub use lease::{
    GenerationAcquireError, GenerationFinalizationGuard, GenerationLease, GenerationSlot,
    GenerationSlotSnapshot, GenerationSlotState, GenerationStageError, GenerationSwapError,
};
pub use manager::{
    AcceptedGenerationPublication, RetirementDiagnostic, RetirementFailure, RuntimeManager,
    RuntimeManagerShutdownReport, StagedGenerationSwap,
};
pub use process::ProcessRuntime;
pub use recovery::{StartupRecoveryError, StartupRecoveryReport};

pub(crate) use diagnostics::{
    RuntimeDiagnosticState, bounded_strings, bounded_text, digest_prefix, format_reload_category,
    retiring_diagnostic, task_diagnostic,
};

pub use crate::task_supervisor::{
    PreparedTaskDiff, RUNTIME_TASK_NAMES, RuntimeTaskCapability, RuntimeTaskSnapshot,
    RuntimeTaskSpec, RuntimeTaskSupervisor, TaskCallback, TaskCallbackError, TaskCallbackFuture,
    TaskCallbackRegistry, TaskOutcome, TaskOwnership, TaskShutdownReport, TaskSpecDiff,
    TaskSpecError, TaskTickContext, TaskTransition, runtime_task_inventory,
    runtime_task_specs_for_config, task_callback,
};

pub const MAX_RETIRING_GENERATIONS: usize = 4;
pub const DEFAULT_GENERATION_CLOSE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(1);

pub(crate) const MAX_RETIREMENT_DIAGNOSTICS: usize = 16;
pub(crate) const MAX_STARTUP_RECONCILIATION_PASSES: usize = 1024;
pub(crate) const MAX_DIAGNOSTIC_PATHS: usize = 32;
pub(crate) const MAX_DIAGNOSTIC_TEXT_BYTES: usize = 96;
