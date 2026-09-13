use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use tokio::sync::Notify;

use crate::{
    Config,
    coordinator::{
        FinalizationDrainError, FinalizationSupervisor, InferenceState, TerminalReferenceOwner,
        WireResolverConfigError, build_inference_state_with_shared,
        build_inference_state_with_shared_and_accounts, compile_provider_profiles,
    },
    db::{Account, DatabaseError},
    providers::{ProviderClientPool, ProviderClientPoolCloseReport, ProviderClientPoolError},
};

use super::{
    DEFAULT_GENERATION_CLOSE_TIMEOUT, GenerationFinalizationGuard, GenerationSlot, ProcessRuntime,
    digest_prefix,
};

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
    #[error("generation wire resolver policy construction failed: {0}")]
    WirePolicy(#[from] WireResolverConfigError),
    #[error("generation inference graph construction failed: {detail}")]
    Graph {
        detail: String,
        provider_clients: ProviderClientPoolCloseReport,
    },
    #[error("generation database precondition failed: {0}")]
    Database(#[from] DatabaseError),
}
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

    pub(crate) fn install_terminal_owner(&self, owner: Arc<dyn TerminalReferenceOwner>) {
        self.resources.finalization.set_terminal_owner(owner);
    }

    pub fn try_retain_finalization(
        self: &Arc<Self>,
        slot: &Arc<GenerationSlot>,
    ) -> Option<GenerationFinalizationGuard> {
        if !Arc::ptr_eq(self, slot.generation()) {
            return None;
        }
        slot.try_retain_finalization()
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
        self.close_with_timeout(DEFAULT_GENERATION_CLOSE_TIMEOUT)
            .await
    }

    pub async fn close_with_timeout(&self, timeout: Duration) -> GenerationCloseReport {
        self.shutdown_generation_tasks().await;
        self.resources.close(self.generation_id, timeout).await
    }

    /// Close process-exit resources after the graceful window has expired.
    ///
    /// A live rehash must leave a failed generation open so accepted work can
    /// finish.  Process shutdown is the one exception: the process is
    /// exiting, so the provider handles are closed even when retained
    /// finalization could not converge in its last bounded window.
    pub async fn force_close_with_timeout(&self, timeout: Duration) -> GenerationCloseReport {
        self.shutdown_generation_tasks().await;
        self.resources
            .force_close(self.generation_id, timeout)
            .await
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

    async fn close(&self, generation_id: u64, timeout: Duration) -> GenerationCloseReport {
        if !self.closed.swap(true, Ordering::AcqRel) {
            let finalization_before = self.finalization.snapshot();
            let mut close_order = vec![GenerationCloseStep::GenerationTasksClosed];
            let finalization_result = self.finalization.drain_with_timeout(timeout).await;
            let finalization_after = match finalization_result {
                Ok(snapshot) => {
                    close_order.push(GenerationCloseStep::FinalizationDrained);
                    snapshot
                }
                Err(error) => {
                    let snapshot = self.finalization.snapshot();
                    return self.finish_close(
                        generation_id,
                        finalization_before,
                        snapshot,
                        close_order,
                        ProviderClientPoolCloseReport {
                            closed_now: false,
                            close_count: 0,
                        },
                        Some(GenerationCloseFailure::Finalization(error)),
                    );
                }
            };
            let provider_clients = self.provider_clients.close();
            close_order.push(GenerationCloseStep::ProviderClientsClosed);
            close_order.push(GenerationCloseStep::GenerationHandlesReleased);
            self.finish_close(
                generation_id,
                finalization_before,
                finalization_after,
                close_order,
                provider_clients,
                None,
            )
        } else {
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

    async fn force_close(&self, generation_id: u64, timeout: Duration) -> GenerationCloseReport {
        let report = self.close(generation_id, timeout).await;
        if report.provider_clients.closed_now || report.provider_clients.close_count > 0 {
            return report;
        }

        // `close` records a failed finalization drain and intentionally keeps
        // transports open for live retirement.  A process exit is allowed to
        // finish that close boundary deterministically.
        let provider_clients = self.provider_clients.close();
        let mut forced = report;
        forced.provider_clients = provider_clients;
        if !forced
            .close_order
            .contains(&GenerationCloseStep::ProviderClientsClosed)
        {
            forced
                .close_order
                .push(GenerationCloseStep::ProviderClientsClosed);
        }
        *self
            .close_report
            .lock()
            .expect("generation close report lock") = Some(forced.clone());
        self.close_notify.notify_waiters();
        forced
    }

    fn finish_close(
        &self,
        generation_id: u64,
        finalization_before: crate::coordinator::SupervisorSnapshot,
        finalization_after: crate::coordinator::SupervisorSnapshot,
        close_order: Vec<GenerationCloseStep>,
        provider_clients: ProviderClientPoolCloseReport,
        failure: Option<GenerationCloseFailure>,
    ) -> GenerationCloseReport {
        let report = GenerationCloseReport {
            generation_id,
            finalization_before,
            finalization_after,
            provider_clients,
            generation_tasks_closed: true,
            close_order,
            failure,
        };
        *self
            .close_report
            .lock()
            .expect("generation close report lock") = Some(report.clone());
        self.close_notify.notify_waiters();
        report
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GenerationCloseStep {
    GenerationTasksClosed,
    FinalizationDrained,
    ProviderClientsClosed,
    GenerationHandlesReleased,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GenerationCloseFailure {
    Finalization(FinalizationDrainError),
}

/// Secret-free evidence from the explicit generation close surface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GenerationCloseReport {
    pub generation_id: u64,
    pub finalization_before: crate::coordinator::SupervisorSnapshot,
    pub finalization_after: crate::coordinator::SupervisorSnapshot,
    pub provider_clients: ProviderClientPoolCloseReport,
    pub generation_tasks_closed: bool,
    pub close_order: Vec<GenerationCloseStep>,
    pub failure: Option<GenerationCloseFailure>,
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

    pub fn generation(&self) -> Option<Arc<RuntimeGeneration>> {
        self.inner
            .lock()
            .expect("candidate ownership lock")
            .generation
            .clone()
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
        Self::prepare_with_durable_accounts_inner(process, config, digest, generation_id, None)
            .await
    }

    /// Prepare a candidate using a preflighted durable-account projection.
    /// This lets reload construct a complete graph for newly configured
    /// accounts without mutating SQLite before the acceptance window.
    pub async fn prepare_with_durable_accounts(
        process: &ProcessRuntime,
        config: Config,
        digest: String,
        generation_id: u64,
        durable_accounts: Vec<Account>,
    ) -> Result<PreparedGeneration, GenerationBuildError> {
        Self::prepare_with_durable_accounts_inner(
            process,
            config,
            digest,
            generation_id,
            Some(durable_accounts),
        )
        .await
    }

    async fn prepare_with_durable_accounts_inner(
        process: &ProcessRuntime,
        config: Config,
        digest: String,
        generation_id: u64,
        durable_accounts: Option<Vec<Account>>,
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

        let configured_preferences = config.providers.iter().flat_map(|(provider_id, provider)| {
            provider
                .model_wire
                .iter()
                .filter_map(|(model_id, preference)| {
                    crate::wire::WireSurface::try_from(preference.preferred_surface.as_str())
                        .ok()
                        .map(|surface| {
                            (
                                provider_id.clone(),
                                model_id.clone(),
                                surface,
                                preference.fixed,
                            )
                        })
                })
        });
        process
            .wire_profile_resolver()
            .set_configured_preferences(configured_preferences);

        // The process-owned handles are cloned only after structural
        // compilation succeeds.  They remain untouched by candidate abort.
        let affinity = process.model_router_affinity();
        let wire_resolver = process.wire_profile_resolver();
        let provider_clients = ProviderClientPool::from_config(&config)?;
        let inference = match match durable_accounts {
            Some(accounts) => {
                build_inference_state_with_shared_and_accounts(
                    &config,
                    &process.database(),
                    provider_clients.clone(),
                    wire_resolver,
                    affinity,
                    model_registry,
                    provider_profiles,
                    Some(accounts),
                )
                .await
            }
            None => {
                build_inference_state_with_shared(
                    &config,
                    &process.database(),
                    provider_clients.clone(),
                    wire_resolver,
                    affinity,
                    model_registry,
                    provider_profiles,
                )
                .await
            }
        } {
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
