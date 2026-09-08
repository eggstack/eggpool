//! Process/generation ownership for the pre-publication M8 boundary.
//!
//! R002 deliberately stops before active-generation publication.  This module
//! owns the graph-building seam that R003 will publish: process-scoped
//! database/affinity/wire state, immutable generation metadata, and an
//! explicit candidate owner with asynchronous abort.

use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

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
