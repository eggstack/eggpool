//! Process/generation ownership and publication for the M8 runtime boundary.
//!
//! The process owns the active-generation manager and the manager publishes
//! immutable generation slots.  Request work receives an explicit lease from
//! that manager; the lease pins the generation across every await and releases
//! only when its owning request/body task is finished.

use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU8, AtomicU64, AtomicUsize, Ordering},
    },
    time::Instant,
};

use arc_swap::ArcSwap;
use tokio::sync::Notify;

use crate::{
    Config,
    coordinator::{
        FinalizationSupervisor, InferenceState, WireResolver, WireResolverConfig,
        build_inference_state_with_shared, compile_provider_profiles,
    },
    db::{Database, DatabaseError},
    model_router::ModelRouterAffinity,
    providers::{ProviderClientPool, ProviderClientPoolCloseReport, ProviderClientPoolError},
};

const MAX_RETIRING_PLACEHOLDER_SLOTS: usize = 4;

/// Errors raised before a candidate can be published.  Messages contain only
/// bounded structural/configuration diagnostics; credentials and proxy URLs
/// are never retained here.
#[derive(Debug, thiserror::Error)]
pub enum GenerationBuildError {
    #[error("generation configuration validation failed: {0}")]
    Config(#[from] crate::config::ConfigError),
    #[error("generation id must be greater than zero")]
    InvalidGenerationId,
    #[error("generation digest must not be empty")]
    EmptyDigest,
    #[error("generation provider client pool construction failed: {0}")]
    ProviderPool(#[from] ProviderClientPoolError),
    #[error("generation wire/model-router compilation failed: {detail}")]
    Compilation { detail: String },
    #[error("generation inference graph construction failed: {detail}")]
    Graph {
        detail: String,
        provider_clients: ProviderClientPoolCloseReport,
    },
    #[error("generation database precondition failed: {0}")]
    Database(#[from] DatabaseError),
}

/// Process-owned state that is intentionally independent of one configuration
/// generation.  The database is cloned as a shared handle, while the affinity
/// and wire resolver are the one process-lifetime instances used by every
/// candidate built from this context.
pub struct ProcessRuntime {
    database: Database,
    model_router_affinity: Arc<ModelRouterAffinity>,
    wire_profile_resolver: WireResolver,
    config_path: Option<PathBuf>,
}

impl Clone for ProcessRuntime {
    fn clone(&self) -> Self {
        Self {
            database: self.database.clone(),
            model_router_affinity: Arc::clone(&self.model_router_affinity),
            wire_profile_resolver: self.wire_profile_resolver.clone(),
            config_path: self.config_path.clone(),
        }
    }
}

impl std::fmt::Debug for ProcessRuntime {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProcessRuntime")
            .field("has_database", &true)
            .field(
                "affinity_entries",
                &self.model_router_affinity.stats().entry_count,
            )
            .field("wire_state", &self.wire_profile_resolver.snapshot())
            .field(
                "config_path",
                &self
                    .config_path
                    .as_ref()
                    .map(|path| path.display().to_string()),
            )
            .finish()
    }
}

impl ProcessRuntime {
    pub fn new(database: Database) -> Self {
        Self {
            database,
            model_router_affinity: Arc::new(ModelRouterAffinity::new()),
            wire_profile_resolver: WireResolver::new(WireResolverConfig::default()),
            config_path: None,
        }
    }

    pub fn with_config_path(database: Database, config_path: impl Into<PathBuf>) -> Self {
        let mut runtime = Self::new(database);
        runtime.config_path = Some(config_path.into());
        runtime
    }

    pub fn database(&self) -> Database {
        self.database.clone()
    }

    pub fn model_router_affinity(&self) -> Arc<ModelRouterAffinity> {
        Arc::clone(&self.model_router_affinity)
    }

    pub fn wire_profile_resolver(&self) -> WireResolver {
        self.wire_profile_resolver.clone()
    }

    pub fn config_path(&self) -> Option<&Path> {
        self.config_path.as_deref()
    }
}

/// One immutable request-visible M7 graph plus its generation close boundary.
pub struct RuntimeGeneration {
    generation_id: u64,
    config: Config,
    content_digest: String,
    inference: Arc<InferenceState>,
    resources: Arc<GenerationResources>,
}

impl std::fmt::Debug for RuntimeGeneration {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RuntimeGeneration")
            .field("generation_id", &self.generation_id)
            .field("content_digest", &digest_prefix(&self.content_digest))
            .field("provider_count", &self.config.providers.len())
            .field("account_count", &self.config.all_accounts().len())
            .field("virtual_model_count", &self.inference.registry().len())
            .field("provider_pool", &self.resources.provider_clients.snapshot())
            .finish()
    }
}

impl RuntimeGeneration {
    fn new(
        generation_id: u64,
        config: Config,
        content_digest: String,
        inference: InferenceState,
        provider_clients: ProviderClientPool,
        finalization: FinalizationSupervisor,
    ) -> Self {
        Self {
            generation_id,
            config,
            content_digest,
            inference: Arc::new(inference),
            resources: Arc::new(GenerationResources::new(provider_clients, finalization)),
        }
    }

    /// Wrap an already-built M7 graph for compatibility callers that provide
    /// the graph directly. Production startup and reload use the factory.
    pub fn from_inference(
        generation_id: u64,
        config: Config,
        content_digest: String,
        inference: Arc<InferenceState>,
        provider_clients: ProviderClientPool,
    ) -> Arc<Self> {
        let finalization = inference.finalization_supervisor();
        Arc::new(Self {
            generation_id,
            config,
            content_digest,
            inference,
            resources: Arc::new(GenerationResources::new(provider_clients, finalization)),
        })
    }

    pub fn generation_id(&self) -> u64 {
        self.generation_id
    }

    pub fn config(&self) -> &Config {
        &self.config
    }

    pub fn content_digest(&self) -> &str {
        &self.content_digest
    }

    pub fn inference(&self) -> &Arc<InferenceState> {
        &self.inference
    }

    pub fn finalization_supervisor(&self) -> FinalizationSupervisor {
        self.resources.finalization.clone()
    }

    pub fn provider_client_pool(&self) -> &ProviderClientPool {
        &self.resources.provider_clients
    }

    pub async fn drain_finalization(&self) {
        self.resources.finalization.drain().await;
    }

    pub fn close_provider_clients(&self) -> ProviderClientPoolCloseReport {
        self.resources.provider_clients.close()
    }

    pub async fn shutdown_generation_tasks(&self) {
        // R002 has no generation-local recurring tasks.  Keeping this explicit
        // operation makes the R004/R008 close ordering a stable interface.
    }

    pub async fn close(&self) -> GenerationCloseReport {
        self.resources.close(self.generation_id).await
    }
}

struct GenerationResources {
    provider_clients: ProviderClientPool,
    finalization: FinalizationSupervisor,
    closed: AtomicBool,
    close_report: Mutex<Option<GenerationCloseReport>>,
    close_notify: Notify,
}

impl GenerationResources {
    fn new(provider_clients: ProviderClientPool, finalization: FinalizationSupervisor) -> Self {
        Self {
            provider_clients,
            finalization,
            closed: AtomicBool::new(false),
            close_report: Mutex::new(None),
            close_notify: Notify::new(),
        }
    }

    async fn close(&self, generation_id: u64) -> GenerationCloseReport {
        if !self.closed.swap(true, Ordering::AcqRel) {
            let finalization_before = self.finalization.snapshot();
            self.finalization.drain().await;
            let provider_clients = self.provider_clients.close();
            let report = GenerationCloseReport {
                generation_id,
                finalization_before,
                finalization_after: self.finalization.snapshot(),
                provider_clients,
                generation_tasks_closed: true,
            };
            *self
                .close_report
                .lock()
                .expect("generation close report lock") = Some(report.clone());
            self.close_notify.notify_waiters();
            return report;
        }

        loop {
            if let Some(report) = self
                .close_report
                .lock()
                .expect("generation close report lock")
                .clone()
            {
                return report;
            }
            self.close_notify.notified().await;
        }
    }
}

/// Secret-free evidence from the explicit generation close surface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GenerationCloseReport {
    pub generation_id: u64,
    pub finalization_before: crate::coordinator::SupervisorSnapshot,
    pub finalization_after: crate::coordinator::SupervisorSnapshot,
    pub provider_clients: ProviderClientPoolCloseReport,
    pub generation_tasks_closed: bool,
}

/// Candidate ownership state.  Only the future manager may transition a
/// prepared candidate to `Transferred`; R002 does not implement that manager.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CandidateOwnership {
    Prepared,
    Transferred,
    Aborting,
    Aborted,
}

#[derive(Debug, Clone)]
struct CandidateInner {
    state: CandidateOwnership,
    generation: Option<Arc<RuntimeGeneration>>,
    close_report: Option<GenerationCloseReport>,
}

/// Explicit owner for an unpublished generation candidate.
pub struct PreparedGeneration {
    inner: Arc<Mutex<CandidateInner>>,
    abort_notify: Arc<Notify>,
}

impl std::fmt::Debug for PreparedGeneration {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let inner = self.inner.lock().expect("candidate ownership lock");
        formatter
            .debug_struct("PreparedGeneration")
            .field("state", &inner.state)
            .field(
                "generation_id",
                &inner
                    .generation
                    .as_ref()
                    .map(|generation| generation.generation_id()),
            )
            .finish()
    }
}

impl PreparedGeneration {
    fn new(generation: Arc<RuntimeGeneration>) -> Self {
        Self {
            inner: Arc::new(Mutex::new(CandidateInner {
                state: CandidateOwnership::Prepared,
                generation: Some(generation),
                close_report: None,
            })),
            abort_notify: Arc::new(Notify::new()),
        }
    }

    pub fn ownership(&self) -> CandidateOwnership {
        self.inner.lock().expect("candidate ownership lock").state
    }

    pub fn generation_id(&self) -> Option<u64> {
        self.inner
            .lock()
            .expect("candidate ownership lock")
            .generation
            .as_ref()
            .map(|generation| generation.generation_id())
    }

    /// Transfer cleanup ownership to the future manager exactly once.
    pub fn transfer(&self) -> Result<Arc<RuntimeGeneration>, CandidateTransferError> {
        let mut inner = self.inner.lock().expect("candidate ownership lock");
        if inner.state != CandidateOwnership::Prepared {
            return Err(CandidateTransferError { state: inner.state });
        }
        inner.state = CandidateOwnership::Transferred;
        inner.generation.take().ok_or(CandidateTransferError {
            state: CandidateOwnership::Transferred,
        })
    }

    /// Abort candidate-owned resources.  Concurrent and repeated callers
    /// observe one completed close report; transferred candidates are a
    /// no-op because ownership has already moved to the manager boundary.
    pub async fn abort(&self) -> CandidateAbortReport {
        loop {
            let notified = self.abort_notify.notified();
            let generation = {
                let mut inner = self.inner.lock().expect("candidate ownership lock");
                match inner.state {
                    CandidateOwnership::Prepared => {
                        inner.state = CandidateOwnership::Aborting;
                        inner.generation.take()
                    }
                    CandidateOwnership::Aborting => None,
                    CandidateOwnership::Aborted => {
                        return CandidateAbortReport {
                            ownership: CandidateOwnership::Aborted,
                            close_report: inner.close_report.clone(),
                            transferred: false,
                        };
                    }
                    CandidateOwnership::Transferred => {
                        return CandidateAbortReport {
                            ownership: CandidateOwnership::Transferred,
                            close_report: None,
                            transferred: true,
                        };
                    }
                }
            };

            if let Some(generation) = generation {
                let report = generation.close().await;
                let mut inner = self.inner.lock().expect("candidate ownership lock");
                inner.state = CandidateOwnership::Aborted;
                inner.close_report = Some(report.clone());
                self.abort_notify.notify_waiters();
                return CandidateAbortReport {
                    ownership: CandidateOwnership::Aborted,
                    close_report: Some(report),
                    transferred: false,
                };
            }

            notified.await;
        }
    }
}

impl Drop for PreparedGeneration {
    fn drop(&mut self) {
        let state = self.inner.lock().expect("candidate ownership lock").state;
        if matches!(
            state,
            CandidateOwnership::Prepared | CandidateOwnership::Aborting
        ) {
            tracing::error!(
                ?state,
                "prepared generation dropped before explicit ownership transfer or abort"
            );
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateTransferError {
    pub state: CandidateOwnership,
}

impl std::fmt::Display for CandidateTransferError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "candidate ownership is {:?}", self.state)
    }
}

impl std::error::Error for CandidateTransferError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateAbortReport {
    pub ownership: CandidateOwnership,
    pub close_report: Option<GenerationCloseReport>,
    pub transferred: bool,
}

/// The one construction path used by startup and future reload candidates.
pub struct RuntimeGenerationFactory;

impl RuntimeGenerationFactory {
    pub async fn prepare(
        process: &ProcessRuntime,
        config: Config,
        digest: String,
        generation_id: u64,
    ) -> Result<PreparedGeneration, GenerationBuildError> {
        if generation_id == 0 {
            return Err(GenerationBuildError::InvalidGenerationId);
        }
        if digest.trim().is_empty() {
            return Err(GenerationBuildError::EmptyDigest);
        }

        // CLI/file startup already supplies a fully validated snapshot.  The
        // factory repeats the generation-specific structural checks below so
        // direct Rust callers get the same fail-closed candidate boundary
        // without turning credential-source validation into a second pool
        // construction gate.
        let model_registry = config.compile_model_router_registry()?;
        let provider_profiles = compile_provider_profiles(&config)
            .map_err(|detail| GenerationBuildError::Compilation { detail })?;

        // The process-owned handles are cloned only after structural
        // compilation succeeds.  They remain untouched by candidate abort.
        let affinity = process.model_router_affinity();
        let wire_resolver = process.wire_profile_resolver();
        let provider_clients = ProviderClientPool::from_config(&config)?;
        let inference = match build_inference_state_with_shared(
            &config,
            &process.database,
            provider_clients.clone(),
            wire_resolver,
            affinity,
            model_registry,
            provider_profiles,
        )
        .await
        {
            Ok(inference) => inference,
            Err(detail) => {
                return Err(GenerationBuildError::Graph {
                    detail,
                    provider_clients: provider_clients.close(),
                });
            }
        };
        let finalization = inference.finalization_supervisor();
        let generation = Arc::new(RuntimeGeneration::new(
            generation_id,
            config,
            digest,
            inference,
            provider_clients,
            finalization,
        ));
        Ok(PreparedGeneration::new(generation))
    }
}

fn digest_prefix(digest: &str) -> String {
    digest.chars().take(12).collect()
}

// ---------------------------------------------------------------------------
// Active-generation publication and request leases (R003)
// ---------------------------------------------------------------------------

/// Lifecycle state exposed by a generation slot.  R004 adds retirement and
/// close transitions; R003 keeps the states explicit so a publication cannot
/// be mistaken for an in-place mutation of the immutable generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GenerationSlotState {
    Active,
    Retiring,
    Closed,
}

impl GenerationSlotState {
    fn as_u8(self) -> u8 {
        match self {
            Self::Active => 0,
            Self::Retiring => 1,
            Self::Closed => 2,
        }
    }

    fn from_u8(value: u8) -> Self {
        match value {
            1 => Self::Retiring,
            2 => Self::Closed,
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
    pub published_elapsed_ms: Option<u128>,
}

/// One published generation and its process-local lifecycle metadata.
pub struct GenerationSlot {
    generation: Arc<RuntimeGeneration>,
    generation_id: u64,
    digest_prefix: String,
    accepting: AtomicBool,
    active_leases: AtomicUsize,
    state: AtomicU8,
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
            .finish()
    }
}

impl GenerationSlot {
    fn new(generation: Arc<RuntimeGeneration>, accepting: bool) -> Self {
        let generation_id = generation.generation_id();
        Self {
            digest_prefix: digest_prefix(generation.content_digest()),
            generation,
            generation_id,
            accepting: AtomicBool::new(accepting),
            active_leases: AtomicUsize::new(0),
            state: AtomicU8::new(GenerationSlotState::Active.as_u8()),
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
            published_elapsed_ms,
        }
    }

    fn set_accepting(&self, accepting: bool) {
        self.accepting.store(accepting, Ordering::Release);
    }

    fn set_state(&self, state: GenerationSlotState) {
        self.state.store(state.as_u8(), Ordering::Release);
    }

    fn mark_published(&self) {
        *self
            .published_at
            .lock()
            .expect("generation publication timestamp lock") = Some(Instant::now());
    }

    fn claim_arc(slot: &Arc<Self>) -> GenerationLease {
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
            }),
        }
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
        self.inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .len()
    }

    pub fn retiring_slots(&self) -> Vec<Arc<GenerationSlot>> {
        self.inner
            .retiring
            .lock()
            .expect("retiring slots lock")
            .clone()
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
        if self.retiring_slot_count() >= MAX_RETIRING_PLACEHOLDER_SLOTS {
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
    /// retained in the manager's placeholder retirement collection for R004.
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
        Ok(AcceptedGenerationPublication {
            epoch,
            old_slot: Arc::clone(&self.old),
            new_slot: Arc::clone(&self.new),
        })
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
