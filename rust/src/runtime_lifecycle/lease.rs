use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering},
    },
    time::Instant,
};

use serde::Serialize;
use tokio::sync::Notify;

use crate::coordinator::{TerminalReference, TerminalReferenceOwner};

use super::{CandidateTransferError, RuntimeGeneration, digest_prefix};
// ---------------------------------------------------------------------------
// Active-generation publication and request leases (R003)
// ---------------------------------------------------------------------------

/// Monotonic lifecycle state exposed by a generation slot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum GenerationSlotState {
    Active,
    Retiring,
    DrainingFinalization,
    Closing,
    Closed,
    FailedClose,
}

impl GenerationSlotState {
    fn as_u8(self) -> u8 {
        match self {
            Self::Active => 0,
            Self::Retiring => 1,
            Self::DrainingFinalization => 2,
            Self::Closing => 3,
            Self::Closed => 4,
            Self::FailedClose => 5,
        }
    }

    fn from_u8(value: u8) -> Self {
        match value {
            1 => Self::Retiring,
            2 => Self::DrainingFinalization,
            3 => Self::Closing,
            4 => Self::Closed,
            5 => Self::FailedClose,
            _ => Self::Active,
        }
    }
}

/// Secret-free point-in-time slot diagnostics.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GenerationSlotSnapshot {
    pub generation_id: u64,
    pub digest_prefix: String,
    pub state: GenerationSlotState,
    pub accepting: bool,
    pub active_leases: usize,
    pub terminal_references: usize,
    pub published_elapsed_ms: Option<u128>,
}

/// One published generation and its process-local lifecycle metadata.
pub struct GenerationSlot {
    generation: Arc<RuntimeGeneration>,
    generation_id: u64,
    digest_prefix: String,
    accepting: AtomicBool,
    active_leases: AtomicUsize,
    terminal_references: AtomicUsize,
    state: AtomicU8,
    pub(crate) retirement_scheduled: AtomicBool,
    published_at: Mutex<Option<Instant>>,
    drain_notify: Notify,
}

impl std::fmt::Debug for GenerationSlot {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("GenerationSlot")
            .field("generation_id", &self.generation_id)
            .field("digest_prefix", &self.digest_prefix)
            .field("state", &self.state())
            .field("accepting", &self.accepting())
            .field("active_leases", &self.active_lease_count())
            .field("terminal_references", &self.terminal_reference_count())
            .finish()
    }
}

impl GenerationSlot {
    pub(crate) fn new(generation: Arc<RuntimeGeneration>, accepting: bool) -> Self {
        let generation_id = generation.generation_id();
        Self {
            digest_prefix: digest_prefix(generation.content_digest()),
            generation,
            generation_id,
            accepting: AtomicBool::new(accepting),
            active_leases: AtomicUsize::new(0),
            terminal_references: AtomicUsize::new(0),
            state: AtomicU8::new(GenerationSlotState::Active.as_u8()),
            retirement_scheduled: AtomicBool::new(false),
            published_at: Mutex::new(Some(Instant::now())),
            drain_notify: Notify::new(),
        }
    }

    pub fn generation(&self) -> &Arc<RuntimeGeneration> {
        &self.generation
    }

    pub fn generation_id(&self) -> u64 {
        self.generation_id
    }

    pub fn digest_prefix(&self) -> &str {
        &self.digest_prefix
    }

    pub fn accepting(&self) -> bool {
        self.accepting.load(Ordering::Acquire)
    }

    pub fn active_lease_count(&self) -> usize {
        self.active_leases.load(Ordering::Acquire)
    }

    pub fn terminal_reference_count(&self) -> usize {
        self.terminal_references.load(Ordering::Acquire)
    }

    pub fn state(&self) -> GenerationSlotState {
        GenerationSlotState::from_u8(self.state.load(Ordering::Acquire))
    }

    pub fn snapshot(&self) -> GenerationSlotSnapshot {
        let published_elapsed_ms = self
            .published_at
            .lock()
            .expect("generation publication timestamp lock")
            .map(|published| published.elapsed().as_millis());
        GenerationSlotSnapshot {
            generation_id: self.generation_id,
            digest_prefix: self.digest_prefix.clone(),
            state: self.state(),
            accepting: self.accepting(),
            active_leases: self.active_lease_count(),
            terminal_references: self.terminal_reference_count(),
            published_elapsed_ms,
        }
    }

    pub(crate) fn set_accepting(&self, accepting: bool) {
        self.accepting.store(accepting, Ordering::Release);
    }

    pub(crate) fn set_state(&self, state: GenerationSlotState) {
        self.state.store(state.as_u8(), Ordering::Release);
    }

    pub(crate) fn mark_published(&self) {
        *self
            .published_at
            .lock()
            .expect("generation publication timestamp lock") = Some(Instant::now());
    }

    pub(crate) fn claim_arc(slot: &Arc<Self>) -> GenerationLease {
        slot.active_leases.fetch_add(1, Ordering::AcqRel);
        GenerationLease {
            slot: Arc::clone(slot),
        }
    }

    fn release(&self) {
        let previous = self.active_leases.fetch_sub(1, Ordering::AcqRel);
        if previous == 0 {
            self.active_leases.store(0, Ordering::Release);
            tracing::error!(
                generation_id = self.generation_id,
                "generation lease count underflow"
            );
            return;
        }
        if previous == 1 {
            self.drain_notify.notify_waiters();
        }
    }

    pub async fn wait_for_drain(&self) {
        loop {
            let notified = self.drain_notify.notified();
            if self.active_lease_count() == 0 {
                return;
            }
            notified.await;
        }
    }

    /// Retain one synchronous reference for a terminal job before handing the
    /// job to the generation-owned finalization supervisor. The reference is
    /// rejected once retirement has entered the drain/close portion.
    pub fn try_retain_finalization(self: &Arc<Self>) -> Option<GenerationFinalizationGuard> {
        loop {
            let state = self.state();
            if matches!(
                state,
                GenerationSlotState::DrainingFinalization
                    | GenerationSlotState::Closing
                    | GenerationSlotState::Closed
                    | GenerationSlotState::FailedClose
            ) {
                return None;
            }
            let current = self.terminal_reference_count();
            if self
                .terminal_references
                .compare_exchange(
                    current,
                    current.saturating_add(1),
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_ok()
            {
                if !matches!(
                    self.state(),
                    GenerationSlotState::DrainingFinalization
                        | GenerationSlotState::Closing
                        | GenerationSlotState::Closed
                        | GenerationSlotState::FailedClose
                ) {
                    return Some(GenerationFinalizationGuard {
                        slot: Arc::clone(self),
                    });
                }
                let _ = self.terminal_references.fetch_sub(1, Ordering::AcqRel);
                self.drain_notify.notify_waiters();
                return None;
            }
        }
    }

    pub(crate) async fn wait_for_finalization_references(&self) {
        loop {
            let notified = self.drain_notify.notified();
            if self.terminal_reference_count() == 0 {
                return;
            }
            notified.await;
        }
    }
}

/// Synchronous ownership for one retained terminal/finalization job.
pub struct GenerationFinalizationGuard {
    slot: Arc<GenerationSlot>,
}

impl std::fmt::Debug for GenerationFinalizationGuard {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("GenerationFinalizationGuard")
            .field("generation_id", &self.slot.generation_id())
            .finish()
    }
}

impl GenerationFinalizationGuard {
    pub fn generation_id(&self) -> u64 {
        self.slot.generation_id()
    }
}

impl Drop for GenerationFinalizationGuard {
    fn drop(&mut self) {
        let previous = self.slot.terminal_references.fetch_sub(1, Ordering::AcqRel);
        if previous == 0 {
            self.slot.terminal_references.store(0, Ordering::Release);
            tracing::error!(
                generation_id = self.slot.generation_id(),
                "generation finalization reference count underflow"
            );
        } else if previous == 1 {
            self.slot.drain_notify.notify_waiters();
        }
    }
}

impl TerminalReference for GenerationFinalizationGuard {}

impl TerminalReferenceOwner for Arc<GenerationSlot> {
    fn retain_terminal_reference(&self) -> Option<Box<dyn TerminalReference>> {
        self.try_retain_finalization()
            .map(|guard| Box::new(guard) as Box<dyn TerminalReference>)
    }
}

/// A request/stream reference to one immutable generation.
pub struct GenerationLease {
    slot: Arc<GenerationSlot>,
}

impl std::fmt::Debug for GenerationLease {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("GenerationLease")
            .field("generation_id", &self.generation_id())
            .finish()
    }
}

impl GenerationLease {
    pub fn generation(&self) -> &RuntimeGeneration {
        self.slot.generation()
    }

    pub fn slot(&self) -> &Arc<GenerationSlot> {
        &self.slot
    }

    pub fn generation_id(&self) -> u64 {
        self.slot.generation_id()
    }
}

impl Drop for GenerationLease {
    fn drop(&mut self) {
        self.slot.release();
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GenerationAcquireError {
    AdmissionClosed,
    ShuttingDown,
}

impl std::fmt::Display for GenerationAcquireError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::AdmissionClosed => {
                write!(formatter, "generation admission is temporarily closed")
            }
            Self::ShuttingDown => write!(formatter, "runtime is shutting down"),
        }
    }
}

impl std::error::Error for GenerationAcquireError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GenerationStageError {
    ShuttingDown,
    AdmissionClosed,
    PendingSwap,
    StaleGeneration { expected: u64, actual: u64 },
    ActiveGenerationNotAccepting,
    CandidateGenerationAlreadyActive,
    RetirementBacklog,
    CandidateTransfer(CandidateTransferError),
}

impl std::fmt::Display for GenerationStageError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ShuttingDown => write!(formatter, "runtime is shutting down"),
            Self::AdmissionClosed => write!(formatter, "generation admission is already closed"),
            Self::PendingSwap => write!(formatter, "a generation swap is already staged"),
            Self::StaleGeneration { expected, actual } => {
                write!(
                    formatter,
                    "active generation is {actual}, expected {expected}"
                )
            }
            Self::ActiveGenerationNotAccepting => {
                write!(formatter, "active generation is not accepting requests")
            }
            Self::CandidateGenerationAlreadyActive => {
                write!(formatter, "candidate generation is already active")
            }
            Self::RetirementBacklog => write!(formatter, "retiring generation backlog is full"),
            Self::CandidateTransfer(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for GenerationStageError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GenerationSwapError {
    InvalidPhase,
    ActivePointerChanged,
    ShuttingDown,
}

impl std::fmt::Display for GenerationSwapError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidPhase => write!(formatter, "invalid staged generation phase"),
            Self::ActivePointerChanged => write!(formatter, "active generation pointer changed"),
            Self::ShuttingDown => write!(formatter, "runtime is shutting down"),
        }
    }
}

impl std::error::Error for GenerationSwapError {}
