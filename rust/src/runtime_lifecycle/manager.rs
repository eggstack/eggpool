use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};

use arc_swap::ArcSwap;
use tokio::{sync::Notify, task::JoinHandle};

use super::{
    DEFAULT_GENERATION_CLOSE_TIMEOUT, GenerationAcquireError, GenerationCloseFailure,
    GenerationCloseReport, GenerationLease, GenerationSlot, GenerationSlotState,
    GenerationStageError, GenerationSwapError, MAX_RETIREMENT_DIAGNOSTICS,
    MAX_RETIRING_GENERATIONS, PreparedGeneration, RuntimeGeneration,
};
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SwapPhase {
    Staged,
    PointerCommitted,
    RolledBack,
    Accepted,
}

struct RuntimeManagerInner {
    active: ArcSwap<GenerationSlot>,
    state: Mutex<ManagerState>,
    gate_notify: Notify,
    publication_epoch: AtomicU64,
    retiring: Mutex<Vec<Arc<GenerationSlot>>>,
    retirement_tasks: Mutex<BTreeMap<u64, JoinHandle<()>>>,
    retirement_diagnostics: Mutex<VecDeque<RetirementDiagnostic>>,
    close_timeout: Mutex<Duration>,
    gate_waiters: AtomicUsize,
    retirement_completed: AtomicU64,
    retirement_failed: AtomicU64,
}

#[derive(Debug, Clone, Copy)]
struct ManagerState {
    admission_closed: bool,
    pending_swap: bool,
    shutdown: bool,
}

/// Process-owned active-generation publication and lease manager.
#[derive(Clone)]
pub struct RuntimeManager {
    inner: Arc<RuntimeManagerInner>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RetirementFailure {
    FinalizationReferences { count: usize },
    GenerationClose(GenerationCloseFailure),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetirementDiagnostic {
    pub generation_id: u64,
    pub digest_prefix: String,
    pub state: GenerationSlotState,
    pub active_leases: usize,
    pub terminal_references: usize,
    pub close_report: Option<GenerationCloseReport>,
    pub failure: Option<RetirementFailure>,
}

#[derive(Clone)]
pub(crate) struct PublicationManagerDiagnostics {
    pub active: Arc<GenerationSlot>,
    pub retiring: Vec<Arc<GenerationSlot>>,
    pub publication_epoch: u64,
    pub admission_closed: bool,
    pub gate_waiters: usize,
    pub retirement_completed: u64,
    pub retirement_failed: u64,
}

/// Bounded process-shutdown evidence.  The report contains structural
/// generation identifiers and close outcomes only; it never serializes
/// configuration or provider error bodies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeManagerShutdownReport {
    pub forced: bool,
    pub active_leases_at_deadline: usize,
    pub terminal_references_at_deadline: usize,
    pub closed_generations: Vec<GenerationCloseReport>,
}

impl std::fmt::Debug for RuntimeManager {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RuntimeManager")
            .field("active", &self.active_slot().snapshot())
            .field("publication_epoch", &self.publication_epoch())
            .field("admission_closed", &self.admission_closed())
            .field("shutdown", &self.is_shutting_down())
            .field("retiring_slots", &self.retiring_slot_count())
            .finish()
    }
}

impl RuntimeManager {
    pub fn new(generation: Arc<RuntimeGeneration>) -> Self {
        let slot = Arc::new(GenerationSlot::new(generation, true));
        slot.generation()
            .install_terminal_owner(Arc::new(Arc::clone(&slot)));
        Self {
            inner: Arc::new(RuntimeManagerInner {
                active: ArcSwap::from(slot),
                state: Mutex::new(ManagerState {
                    admission_closed: false,
                    pending_swap: false,
                    shutdown: false,
                }),
                gate_notify: Notify::new(),
                publication_epoch: AtomicU64::new(0),
                retiring: Mutex::new(Vec::new()),
                retirement_tasks: Mutex::new(BTreeMap::new()),
                retirement_diagnostics: Mutex::new(VecDeque::new()),
                close_timeout: Mutex::new(DEFAULT_GENERATION_CLOSE_TIMEOUT),
                gate_waiters: AtomicUsize::new(0),
                retirement_completed: AtomicU64::new(0),
                retirement_failed: AtomicU64::new(0),
            }),
        }
    }

    /// Use a deterministic lifecycle timeout for tests and local embedding.
    /// Production callers should retain the bounded default.
    pub fn with_close_timeout(self, timeout: Duration) -> Self {
        let timeout = timeout.max(Duration::from_millis(1));
        *self
            .inner
            .close_timeout
            .lock()
            .expect("runtime close timeout lock") = timeout;
        self
    }

    pub fn active_slot(&self) -> Arc<GenerationSlot> {
        self.inner.active.load_full()
    }

    pub fn active_generation(&self) -> Arc<RuntimeGeneration> {
        self.active_slot().generation().clone()
    }

    pub fn publication_epoch(&self) -> u64 {
        self.inner.publication_epoch.load(Ordering::Acquire)
    }

    pub(crate) fn publication_diagnostics(&self) -> PublicationManagerDiagnostics {
        let state = self.inner.state.lock().expect("runtime manager state lock");
        PublicationManagerDiagnostics {
            active: self.active_slot(),
            retiring: self.retiring_slots(),
            publication_epoch: self.publication_epoch(),
            admission_closed: state.admission_closed || state.pending_swap,
            gate_waiters: self.inner.gate_waiters.load(Ordering::Acquire),
            retirement_completed: self.inner.retirement_completed.load(Ordering::Relaxed),
            retirement_failed: self.inner.retirement_failed.load(Ordering::Relaxed),
        }
    }

    pub fn admission_closed(&self) -> bool {
        self.inner
            .state
            .lock()
            .expect("runtime manager state lock")
            .admission_closed
    }

    pub fn is_shutting_down(&self) -> bool {
        self.inner
            .state
            .lock()
            .expect("runtime manager state lock")
            .shutdown
    }

    pub fn retiring_slot_count(&self) -> usize {
        self.reap_retirements();
        self.inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .len()
    }

    pub fn retiring_slots(&self) -> Vec<Arc<GenerationSlot>> {
        self.reap_retirements();
        self.inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .clone()
    }

    pub fn retirement_task_count(&self) -> usize {
        self.reap_retirements();
        self.inner
            .retirement_tasks
            .lock()
            .expect("retirement tasks lock")
            .len()
    }

    pub fn retirement_diagnostics(&self) -> Vec<RetirementDiagnostic> {
        self.inner
            .retirement_diagnostics
            .lock()
            .expect("retirement diagnostics lock")
            .iter()
            .cloned()
            .collect()
    }

    /// Reap completed retirement tasks and closed slots. Failed slots remain
    /// resident so accepted work and the failed close can be diagnosed rather
    /// than being forgotten or force-closed.
    pub fn reap_retirements(&self) {
        let mut retiring = self.inner.retiring.lock().expect("retiring slots lock");
        retiring.retain(|slot| slot.state() != GenerationSlotState::Closed);
        drop(retiring);
        self.inner
            .retirement_tasks
            .lock()
            .expect("retirement tasks lock")
            .retain(|_, task| !task.is_finished());
    }

    pub async fn drain_retirements(&self) {
        loop {
            self.reap_retirements();
            if self.retirement_task_count() == 0 {
                return;
            }
            tokio::task::yield_now().await;
        }
    }

    /// Adopt every generation for process shutdown and close it exactly once.
    /// Live retirement deliberately keeps failed old generations resident;
    /// process exit may force that final boundary because no accepted work can
    /// outlive the process.
    pub async fn close_for_shutdown(
        &self,
        timeout: Duration,
        initially_forced: bool,
    ) -> RuntimeManagerShutdownReport {
        self.shutdown();
        let mut slots = vec![self.active_slot()];
        for slot in self.retiring_slots() {
            if !slots.iter().any(|existing| Arc::ptr_eq(existing, &slot)) {
                slots.push(slot);
            }
        }

        let deadline = tokio::time::Instant::now() + timeout;
        let mut forced = initially_forced;
        let mut active_leases_at_deadline: usize = 0;
        let mut terminal_references_at_deadline: usize = 0;
        for slot in &slots {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if tokio::time::timeout(remaining, slot.wait_for_drain())
                .await
                .is_err()
            {
                forced = true;
                active_leases_at_deadline =
                    active_leases_at_deadline.saturating_add(slot.active_lease_count());
                terminal_references_at_deadline =
                    terminal_references_at_deadline.saturating_add(slot.terminal_reference_count());
            }
        }

        if !forced {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if tokio::time::timeout(remaining, self.drain_retirements())
                .await
                .is_err()
            {
                forced = true;
            }
        }

        if forced {
            self.abort_retirement_tasks().await;
        }

        let mut closed_generations = Vec::new();
        for slot in slots {
            if slot.state() == GenerationSlotState::Closed {
                continue;
            }
            slot.set_state(GenerationSlotState::Closing);
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            let report = if forced {
                slot.generation().force_close_with_timeout(remaining).await
            } else {
                slot.generation().close_with_timeout(remaining).await
            };
            if report.failure.is_some() {
                forced = true;
                // A failed graceful finalization boundary must not leave
                // process-owned transports open.  The second call is
                // idempotent and only completes the forced provider close.
                let report = slot.generation().force_close_with_timeout(remaining).await;
                slot.set_state(GenerationSlotState::FailedClose);
                closed_generations.push(report);
            } else {
                slot.set_state(GenerationSlotState::Closed);
                closed_generations.push(report);
            }
        }

        RuntimeManagerShutdownReport {
            forced,
            active_leases_at_deadline,
            terminal_references_at_deadline,
            closed_generations,
        }
    }

    async fn abort_retirement_tasks(&self) {
        let tasks = std::mem::take(
            &mut *self
                .inner
                .retirement_tasks
                .lock()
                .expect("retirement tasks lock"),
        );
        for (_, task) in tasks {
            task.abort();
            let _ = task.await;
        }
    }

    /// Acquire one generation lease.  The notification future is registered
    /// before the state lock is inspected, so closing/opening the gate cannot
    /// lose a wakeup.  The lock covers the gate re-check, active Arc load, and
    /// lease increment, which is the publication linearization section.
    pub async fn acquire(&self) -> Result<GenerationLease, GenerationAcquireError> {
        loop {
            let notified = self.inner.gate_notify.notified();
            let maybe_lease = {
                let state = self.inner.state.lock().expect("runtime manager state lock");
                if state.shutdown {
                    return Err(GenerationAcquireError::ShuttingDown);
                }
                if state.admission_closed {
                    None
                } else {
                    let slot = self.inner.active.load_full();
                    if !slot.accepting() {
                        None
                    } else {
                        Some(GenerationSlot::claim_arc(&slot))
                    }
                }
            };
            if let Some(lease) = maybe_lease {
                return Ok(lease);
            }
            let _waiter = GateWaiter::new(&self.inner.gate_waiters);
            notified.await;
        }
    }

    /// Close admission for one staged publication and transfer candidate
    /// ownership to the staged swap.  No await occurs while the state lock is
    /// held and candidate construction is entirely outside this method.
    pub fn stage(
        &self,
        expected_generation: u64,
        candidate: &PreparedGeneration,
    ) -> Result<StagedGenerationSwap, GenerationStageError> {
        self.reap_retirements();
        let mut state = self.inner.state.lock().expect("runtime manager state lock");
        if state.shutdown {
            return Err(GenerationStageError::ShuttingDown);
        }
        if state.admission_closed {
            return Err(GenerationStageError::AdmissionClosed);
        }
        if state.pending_swap {
            return Err(GenerationStageError::PendingSwap);
        }
        if self.retiring_slot_count() >= MAX_RETIRING_GENERATIONS {
            return Err(GenerationStageError::RetirementBacklog);
        }
        let old = self.inner.active.load_full();
        if old.generation_id() != expected_generation {
            return Err(GenerationStageError::StaleGeneration {
                expected: expected_generation,
                actual: old.generation_id(),
            });
        }
        if !old.accepting() {
            return Err(GenerationStageError::ActiveGenerationNotAccepting);
        }
        if candidate.generation_id() == Some(old.generation_id()) {
            return Err(GenerationStageError::CandidateGenerationAlreadyActive);
        }
        let generation = candidate
            .transfer()
            .map_err(GenerationStageError::CandidateTransfer)?;
        let new = Arc::new(GenerationSlot::new(generation, false));
        new.generation()
            .install_terminal_owner(Arc::new(Arc::clone(&new)));
        state.admission_closed = true;
        state.pending_swap = true;
        Ok(StagedGenerationSwap {
            manager: self.clone(),
            old,
            new,
            phase: SwapPhase::Staged,
        })
    }

    /// Permanently close new admission. Existing leases remain valid.
    pub fn shutdown(&self) {
        let mut state = self.inner.state.lock().expect("runtime manager state lock");
        state.shutdown = true;
        state.admission_closed = true;
        self.inner.active.load_full().set_accepting(false);
        self.inner.gate_notify.notify_waiters();
    }

    fn finish_gate(&self) {
        let mut state = self.inner.state.lock().expect("runtime manager state lock");
        state.pending_swap = false;
        if !state.shutdown {
            state.admission_closed = false;
        }
        self.inner.gate_notify.notify_waiters();
    }

    /// Request retirement for a published old slot. Repeated calls share the
    /// existing manager-owned task and never create a second closer.
    pub fn schedule_retirement(&self, slot: Arc<GenerationSlot>) -> bool {
        let generation_id = slot.generation_id();
        self.reap_retirements();
        if slot.retirement_scheduled.swap(true, Ordering::AcqRel) {
            return false;
        }
        if self
            .inner
            .retirement_tasks
            .lock()
            .expect("retirement tasks lock")
            .contains_key(&generation_id)
        {
            slot.retirement_scheduled.store(false, Ordering::Release);
            return false;
        }
        if !self
            .inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .iter()
            .any(|existing| Arc::ptr_eq(existing, &slot))
        {
            self.inner
                .retiring
                .lock()
                .expect("retiring slots lock")
                .push(Arc::clone(&slot));
        }
        let manager = self.clone();
        let timeout = *self
            .inner
            .close_timeout
            .lock()
            .expect("runtime close timeout lock");
        let task = tokio::spawn(async move {
            manager.retire_slot(slot, timeout).await;
        });
        self.inner
            .retirement_tasks
            .lock()
            .expect("retirement tasks lock")
            .insert(generation_id, task);
        true
    }

    async fn retire_slot(&self, slot: Arc<GenerationSlot>, timeout: Duration) {
        slot.wait_for_drain().await;
        slot.set_state(GenerationSlotState::DrainingFinalization);
        if tokio::time::timeout(timeout, slot.wait_for_finalization_references())
            .await
            .is_err()
        {
            let failure = RetirementFailure::FinalizationReferences {
                count: slot.terminal_reference_count(),
            };
            slot.set_state(GenerationSlotState::FailedClose);
            self.record_retirement(&slot, None, Some(failure));
            return;
        }

        slot.set_state(GenerationSlotState::Closing);
        let report = slot.generation().close_with_timeout(timeout).await;
        if let Some(failure) = report.failure.clone() {
            slot.set_state(GenerationSlotState::FailedClose);
            self.record_retirement(
                &slot,
                Some(report),
                Some(RetirementFailure::GenerationClose(failure)),
            );
        } else {
            slot.set_state(GenerationSlotState::Closed);
            self.record_retirement(&slot, Some(report), None);
        }
    }

    fn record_retirement(
        &self,
        slot: &Arc<GenerationSlot>,
        close_report: Option<GenerationCloseReport>,
        failure: Option<RetirementFailure>,
    ) {
        let failed = failure.is_some();
        let mut diagnostics = self
            .inner
            .retirement_diagnostics
            .lock()
            .expect("retirement diagnostics lock");
        diagnostics.push_back(RetirementDiagnostic {
            generation_id: slot.generation_id(),
            digest_prefix: slot.digest_prefix().to_owned(),
            state: slot.state(),
            active_leases: slot.active_lease_count(),
            terminal_references: slot.terminal_reference_count(),
            close_report,
            failure,
        });
        if failed {
            self.inner.retirement_failed.fetch_add(1, Ordering::Relaxed);
        } else {
            self.inner
                .retirement_completed
                .fetch_add(1, Ordering::Relaxed);
        }
        while diagnostics.len() > MAX_RETIREMENT_DIAGNOSTICS {
            diagnostics.pop_front();
        }
    }
}

struct GateWaiter<'a> {
    count: &'a AtomicUsize,
}

impl<'a> GateWaiter<'a> {
    fn new(count: &'a AtomicUsize) -> Self {
        count.fetch_add(1, Ordering::AcqRel);
        Self { count }
    }
}

impl Drop for GateWaiter<'_> {
    fn drop(&mut self) {
        self.count.fetch_sub(1, Ordering::AcqRel);
    }
}

/// A staged publication.  The caller must explicitly accept or roll it back;
/// dropping an unfinished swap performs only synchronous gate restoration and
/// reports the ownership violation because async candidate cleanup belongs to
/// the caller/R007.
pub struct StagedGenerationSwap {
    manager: RuntimeManager,
    old: Arc<GenerationSlot>,
    new: Arc<GenerationSlot>,
    phase: SwapPhase,
}

impl std::fmt::Debug for StagedGenerationSwap {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("StagedGenerationSwap")
            .field("old_generation_id", &self.old.generation_id())
            .field("new_generation_id", &self.new.generation_id())
            .field("phase", &self.phase)
            .finish()
    }
}

impl StagedGenerationSwap {
    pub fn old_slot(&self) -> &Arc<GenerationSlot> {
        &self.old
    }

    pub fn new_slot(&self) -> &Arc<GenerationSlot> {
        &self.new
    }

    pub fn pointer_committed(&self) -> bool {
        self.phase == SwapPhase::PointerCommitted
    }

    /// Commit only the active pointer. Admission remains gated until
    /// [`Self::accept`] or [`Self::rollback`].
    pub fn commit_pointer(&mut self) -> Result<(), GenerationSwapError> {
        if self.phase != SwapPhase::Staged {
            return Err(GenerationSwapError::InvalidPhase);
        }
        let _state = self
            .manager
            .inner
            .state
            .lock()
            .expect("runtime manager state lock");
        if !self.manager.active_matches(&self.old) {
            return Err(GenerationSwapError::ActivePointerChanged);
        }
        self.old.set_accepting(false);
        self.old.set_state(GenerationSlotState::Retiring);
        self.new.set_accepting(false);
        self.manager.inner.active.store(Arc::clone(&self.new));
        self.new.mark_published();
        self.phase = SwapPhase::PointerCommitted;
        Ok(())
    }

    /// Restore the old pointer while keeping admission closed for the caller's
    /// remaining DB/task compensation work.
    pub fn rollback_pointer(&mut self) -> Result<(), GenerationSwapError> {
        if self.phase != SwapPhase::PointerCommitted {
            return Err(GenerationSwapError::InvalidPhase);
        }
        let state = self
            .manager
            .inner
            .state
            .lock()
            .expect("runtime manager state lock");
        if !self.manager.active_matches(&self.new) {
            return Err(GenerationSwapError::ActivePointerChanged);
        }
        self.manager.inner.active.store(Arc::clone(&self.old));
        self.old.set_state(GenerationSlotState::Active);
        self.old.set_accepting(!state.shutdown);
        self.new.set_accepting(false);
        self.phase = SwapPhase::Staged;
        Ok(())
    }

    /// Reopen admission and publish the staged generation. The old slot is
    /// retained and its manager-owned retirement task starts independently of
    /// the caller that initiated publication.
    pub fn accept(&mut self) -> Result<AcceptedGenerationPublication, GenerationSwapError> {
        if self.phase != SwapPhase::PointerCommitted {
            return Err(GenerationSwapError::InvalidPhase);
        }
        let mut state = self
            .manager
            .inner
            .state
            .lock()
            .expect("runtime manager state lock");
        if state.shutdown {
            return Err(GenerationSwapError::ShuttingDown);
        }
        if !self.manager.active_matches(&self.new) {
            return Err(GenerationSwapError::ActivePointerChanged);
        }
        self.new.set_state(GenerationSlotState::Active);
        self.new.set_accepting(true);
        state.pending_swap = false;
        state.admission_closed = false;
        let epoch = self
            .manager
            .inner
            .publication_epoch
            .fetch_add(1, Ordering::AcqRel)
            + 1;
        self.manager
            .inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .push(Arc::clone(&self.old));
        self.manager.inner.gate_notify.notify_waiters();
        self.phase = SwapPhase::Accepted;
        drop(state);
        if tokio::runtime::Handle::try_current().is_ok() {
            self.manager.schedule_retirement(Arc::clone(&self.old));
        }
        Ok(AcceptedGenerationPublication {
            epoch,
            old_slot: Arc::clone(&self.old),
            new_slot: Arc::clone(&self.new),
        })
    }

    /// Finalize a transaction that reached durable commit after shutdown
    /// began. The new pointer is accepted as the shutdown-era active pointer,
    /// but admission remains closed and no new request can acquire it.
    pub fn accept_during_shutdown(
        &mut self,
    ) -> Result<AcceptedGenerationPublication, GenerationSwapError> {
        if self.phase != SwapPhase::PointerCommitted {
            return Err(GenerationSwapError::InvalidPhase);
        }
        let mut state = self
            .manager
            .inner
            .state
            .lock()
            .expect("runtime manager state lock");
        if !state.shutdown || !self.manager.active_matches(&self.new) {
            return Err(GenerationSwapError::ActivePointerChanged);
        }
        self.new.set_state(GenerationSlotState::Active);
        self.new.set_accepting(false);
        state.pending_swap = false;
        state.admission_closed = true;
        let epoch = self
            .manager
            .inner
            .publication_epoch
            .fetch_add(1, Ordering::AcqRel)
            + 1;
        self.manager
            .inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .push(Arc::clone(&self.old));
        self.inner_notify();
        self.phase = SwapPhase::Accepted;
        drop(state);
        if tokio::runtime::Handle::try_current().is_ok() {
            self.manager.schedule_retirement(Arc::clone(&self.old));
        }
        Ok(AcceptedGenerationPublication {
            epoch,
            old_slot: Arc::clone(&self.old),
            new_slot: Arc::clone(&self.new),
        })
    }

    /// Keep a pointer-committed swap active while leaving admission closed
    /// after an unrecoverable post-commit failure. This is the explicit
    /// fail-closed terminal state consumed by reload compensation diagnostics.
    pub fn fail_closed(mut self) {
        let mut state = self
            .manager
            .inner
            .state
            .lock()
            .expect("runtime manager state lock");
        state.pending_swap = false;
        state.admission_closed = true;
        self.manager
            .inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .push(Arc::clone(&self.old));
        self.phase = SwapPhase::Accepted;
        self.manager.inner.gate_notify.notify_waiters();
        drop(state);
        if tokio::runtime::Handle::try_current().is_ok() {
            self.manager.schedule_retirement(Arc::clone(&self.old));
        }
    }

    fn inner_notify(&self) {
        self.manager.inner.gate_notify.notify_waiters();
    }

    /// Abort the staged publication and return the candidate Arc for explicit
    /// asynchronous generation cleanup.
    pub fn rollback(&mut self) -> Result<Arc<RuntimeGeneration>, GenerationSwapError> {
        if self.phase == SwapPhase::PointerCommitted {
            self.rollback_pointer()?;
        }
        if self.phase != SwapPhase::Staged {
            return Err(GenerationSwapError::InvalidPhase);
        }
        self.old.set_state(GenerationSlotState::Active);
        self.old.set_accepting(!self.manager.is_shutting_down());
        self.new.set_accepting(false);
        self.manager.finish_gate();
        self.phase = SwapPhase::RolledBack;
        Ok(self.new.generation().clone())
    }
}

impl Drop for StagedGenerationSwap {
    fn drop(&mut self) {
        if matches!(self.phase, SwapPhase::Staged | SwapPhase::PointerCommitted) {
            let restored = if self.phase == SwapPhase::PointerCommitted {
                self.manager.inner.active.store(Arc::clone(&self.old));
                self.old.set_state(GenerationSlotState::Active);
                self.old.set_accepting(!self.manager.is_shutting_down());
                true
            } else {
                false
            };
            self.manager.finish_gate();
            tracing::error!(
                old_generation = self.old.generation_id(),
                new_generation = self.new.generation_id(),
                restored_pointer = restored,
                "staged generation dropped without explicit accept or rollback"
            );
        }
    }
}

#[derive(Debug, Clone)]
pub struct AcceptedGenerationPublication {
    pub epoch: u64,
    pub old_slot: Arc<GenerationSlot>,
    pub new_slot: Arc<GenerationSlot>,
}

impl RuntimeManager {
    fn active_matches(&self, expected: &Arc<GenerationSlot>) -> bool {
        let active = self.inner.active.load_full();
        Arc::ptr_eq(&active, expected)
    }
}
