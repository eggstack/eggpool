use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};

use tokio::sync::Mutex as AsyncMutex;

use crate::{
    Config,
    coordinator::{
        WireResolver, WireResolverConfig, WireResolverConfigError, WireResolverPolicyStage,
    },
    db::Database,
    model_router::ModelRouterAffinity,
};

use super::{
    ActiveGenerationDiagnostics, GenerationBuildError, PublicationDiagnostics,
    ReloadDiagnosticRecord, ReloadDiagnostics, RuntimeDiagnosticsSnapshot, RuntimeManager,
    RuntimeTaskCapability, RuntimeTaskSupervisor, ShutdownDiagnostics, StartupRecoveryError,
    StartupRecoveryReport, TaskSpecError, TaskTransition,
};
use super::{
    RuntimeDiagnosticState, bounded_strings, bounded_text, digest_prefix, format_reload_category,
    retiring_diagnostic, task_diagnostic,
};

/// Process-owned state that is intentionally independent of one configuration
/// generation.  The database is cloned as a shared handle, while the affinity
/// and wire resolver are the one process-lifetime instances used by every
/// candidate built from this context.
pub struct ProcessRuntime {
    database: Database,
    model_router_affinity: Arc<ModelRouterAffinity>,
    wire_profile_resolver: WireResolver,
    config_path: Option<PathBuf>,
    task_supervisor: RuntimeTaskSupervisor,
    metrics_coalescer: Arc<crate::operations::metrics::MetricsWriteCoalescer>,
    update_checker: Arc<crate::operations::update::UpdateCheckerState>,
    reload_lock: Arc<AsyncMutex<()>>,
    next_reload_owner: Arc<AtomicU64>,
    startup_recovery_report: Arc<Mutex<Option<StartupRecoveryReport>>>,
    diagnostics: Arc<Mutex<RuntimeDiagnosticState>>,
}

impl Clone for ProcessRuntime {
    fn clone(&self) -> Self {
        Self {
            database: self.database.clone(),
            model_router_affinity: Arc::clone(&self.model_router_affinity),
            wire_profile_resolver: self.wire_profile_resolver.clone(),
            config_path: self.config_path.clone(),
            task_supervisor: self.task_supervisor.clone(),
            metrics_coalescer: Arc::clone(&self.metrics_coalescer),
            update_checker: Arc::clone(&self.update_checker),
            reload_lock: Arc::clone(&self.reload_lock),
            next_reload_owner: Arc::clone(&self.next_reload_owner),
            startup_recovery_report: Arc::clone(&self.startup_recovery_report),
            diagnostics: Arc::clone(&self.diagnostics),
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
        let checkpoint_database = database.clone();
        let metrics_coalescer = Arc::new(crate::operations::metrics::MetricsWriteCoalescer::new(
            &crate::config::MetricsConfig::default(),
            database.clone(),
        ));
        let update_checker = Arc::new(crate::operations::update::UpdateCheckerState::new(
            crate::operations::update::UpdateService::new()
                .expect("default release authority URI is valid"),
        ));
        let mut callbacks =
            crate::task_supervisor::TaskCallbackRegistry::with_generation_maintenance(
                checkpoint_database,
            );
        callbacks.register_update_checker(Arc::clone(&update_checker));
        Self {
            database,
            model_router_affinity: Arc::new(ModelRouterAffinity::new()),
            wire_profile_resolver: WireResolver::new(WireResolverConfig::default()),
            config_path: None,
            task_supervisor: RuntimeTaskSupervisor::with_callbacks(callbacks),
            metrics_coalescer,
            update_checker,
            reload_lock: Arc::new(AsyncMutex::new(())),
            next_reload_owner: Arc::new(AtomicU64::new(1)),
            startup_recovery_report: Arc::new(Mutex::new(None)),
            diagnostics: Arc::new(Mutex::new(RuntimeDiagnosticState::default())),
        }
    }

    /// Production startup authority: the process-owned resolver is created
    /// with the validated wire-negotiation policy before the first generation
    /// can serve a request.
    pub fn new_with_config(
        database: Database,
        config: &Config,
    ) -> Result<Self, GenerationBuildError> {
        let wire_policy = WireResolverConfig::from_config(&config.routing.wire_negotiation)?;
        let mut runtime = Self::new(database);
        runtime.wire_profile_resolver = WireResolver::new(wire_policy);
        runtime.metrics_coalescer =
            Arc::new(crate::operations::metrics::MetricsWriteCoalescer::new(
                &config.metrics,
                runtime.database.clone(),
            ));
        runtime
            .task_supervisor
            .register_metrics_flush(Arc::clone(&runtime.metrics_coalescer));
        Ok(runtime)
    }

    pub fn with_config_path(database: Database, config_path: impl Into<PathBuf>) -> Self {
        let mut runtime = Self::new(database);
        runtime.config_path = Some(config_path.into());
        runtime
    }

    pub fn with_config_path_and_config(
        database: Database,
        config_path: impl Into<PathBuf>,
        config: &Config,
    ) -> Result<Self, GenerationBuildError> {
        let mut runtime = Self::new_with_config(database, config)?;
        let config_path = config_path.into();
        runtime.config_path = Some(config_path.clone());
        runtime
            .task_supervisor
            .register_automatic_backup(runtime.database.clone(), config_path);
        Ok(runtime)
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

    pub fn stage_wire_resolver_policy(
        &self,
        config: &Config,
    ) -> Result<WireResolverPolicyStage, WireResolverConfigError> {
        Ok(self
            .wire_profile_resolver
            .stage_config(WireResolverConfig::from_config(
                &config.routing.wire_negotiation,
            )?))
    }

    pub fn config_path(&self) -> Option<&Path> {
        self.config_path.as_deref()
    }

    pub fn task_supervisor(&self) -> RuntimeTaskSupervisor {
        self.task_supervisor.clone()
    }

    pub fn metrics_coalescer(&self) -> Arc<crate::operations::metrics::MetricsWriteCoalescer> {
        Arc::clone(&self.metrics_coalescer)
    }

    pub fn update_checker(&self) -> Arc<crate::operations::update::UpdateCheckerState> {
        Arc::clone(&self.update_checker)
    }

    pub async fn flush_metrics(
        &self,
    ) -> Result<usize, crate::operations::metrics::MetricsFlushError> {
        self.metrics_coalescer.flush().await
    }

    pub fn task_capability_inventory(&self) -> Vec<RuntimeTaskCapability> {
        self.task_supervisor.capability_inventory()
    }

    pub fn startup_recovery_report(&self) -> Option<StartupRecoveryReport> {
        self.startup_recovery_report
            .lock()
            .expect("startup recovery report lock")
            .clone()
    }

    pub(crate) fn set_startup_recovery_report(&self, report: StartupRecoveryReport) {
        *self
            .startup_recovery_report
            .lock()
            .expect("startup recovery report lock") = Some(report);
    }

    /// Run C010 to convergence before candidate construction or request
    /// acceptance. Each pass is bounded per table and the aggregate report
    /// keeps only scalar, secret-free diagnostics.
    pub async fn reconcile_startup(&self) -> Result<StartupRecoveryReport, StartupRecoveryError> {
        super::recovery::reconcile(self).await
    }

    /// Install the initial capability-filtered task set after the active
    /// generation exists. Deferred inventory rows are reported but never
    /// passed to the supervisor as runnable specs.
    pub async fn install_initial_tasks(
        &self,
        manager: RuntimeManager,
        config: &Config,
    ) -> Result<TaskTransition, TaskSpecError> {
        self.task_supervisor.set_generation_manager(manager);
        let current = self.task_supervisor.active_specs();
        let candidate = self
            .task_supervisor
            .available_specs_for_config(config, true);
        let mut diff = self.task_supervisor.prepare_diff(&current, &candidate)?;
        diff.commit().await
    }

    /// Bind the process-owned reload coordinator to the active-generation
    /// authority. The returned handle is cheap to clone and remains tied to
    /// this process runtime's database, affinity, wire resolver, and task
    /// supervisor.
    pub fn reload_service(&self, manager: RuntimeManager) -> crate::reload::ReloadService {
        crate::reload::ReloadService::new(self.clone(), manager)
    }

    pub(crate) fn reload_lock(&self) -> Arc<AsyncMutex<()>> {
        Arc::clone(&self.reload_lock)
    }

    pub(crate) fn begin_reload_diagnostics(
        &self,
        manager: &RuntimeManager,
    ) -> ReloadDiagnosticGuard {
        let mut diagnostics = self.diagnostics.lock().expect("runtime diagnostics lock");
        let owner = self.next_reload_owner.fetch_add(1, Ordering::Relaxed);
        diagnostics.reload_in_progress = true;
        diagnostics.reload_phase = "running".to_owned();
        diagnostics.reload_started_at = Some(Instant::now());
        diagnostics.reload_owner = Some(owner);
        diagnostics.counters.reload_attempts =
            diagnostics.counters.reload_attempts.saturating_add(1);
        ReloadDiagnosticGuard {
            process: self.clone(),
            manager: manager.clone(),
            owner,
            finished: false,
        }
    }

    fn record_reload_diagnostics(&self, owner: u64, result: &crate::reload::ReloadResult) {
        let mut diagnostics = self.diagnostics.lock().expect("runtime diagnostics lock");
        if diagnostics.reload_owner != Some(owner) {
            return;
        }
        let duration_ms = diagnostics.reload_started_at.take().map_or(0, |started| {
            started.elapsed().as_millis().min(u64::MAX as u128) as u64
        });
        let accepted = result.category == crate::reload::ReloadResultCategory::Applied;
        if accepted {
            diagnostics.counters.reload_accepted =
                diagnostics.counters.reload_accepted.saturating_add(1);
        } else {
            diagnostics.counters.reload_failures =
                diagnostics.counters.reload_failures.saturating_add(1);
        }
        diagnostics.last_reload = Some(ReloadDiagnosticRecord {
            category: format_reload_category(result.category),
            active_generation_id: result.active_generation_id,
            active_digest_prefix: digest_prefix(&result.active_digest_prefix),
            changed_sections: bounded_strings(&result.changed_sections),
            restart_required_paths: bounded_strings(&result.restart_required_paths),
            duration_ms,
            reason_code: bounded_text(&result.reason_code),
        });
        diagnostics.reload_in_progress = false;
        diagnostics.reload_phase = "idle".to_owned();
        diagnostics.reload_owner = None;
    }

    fn abort_reload_diagnostics(&self, owner: u64, manager: &RuntimeManager) {
        let result = crate::reload::ReloadResult::from_active(
            manager,
            crate::reload::ReloadResultCategory::Aborted,
            "worker_aborted",
        );
        self.record_reload_diagnostics(owner, &result);
    }

    pub(crate) fn set_shutdown_diagnostics(&self, phase: &str, forced: bool) {
        let mut diagnostics = self.diagnostics.lock().expect("runtime diagnostics lock");
        if diagnostics.shutdown.phase != phase && phase == "quiescing" {
            diagnostics.counters.shutdowns = diagnostics.counters.shutdowns.saturating_add(1);
        }
        diagnostics.shutdown = ShutdownDiagnostics {
            phase: bounded_text(phase),
            forced,
        };
    }

    /// Build one coherent diagnostics projection. The manager owns the active
    /// pointer and slot counters; this method never caches generation-owned
    /// services or configuration between calls.
    pub fn diagnostics(&self, manager: &RuntimeManager) -> RuntimeDiagnosticsSnapshot {
        let manager_snapshot = manager.publication_diagnostics();
        let active_slot = manager_snapshot.active.clone();
        let active_generation = active_slot.generation().clone();
        let finalization = active_generation.finalization_supervisor().snapshot();
        let retiring_generations = manager_snapshot
            .retiring
            .iter()
            .map(retiring_diagnostic)
            .collect();
        let task_supervisor = self.task_supervisor();
        let task_count = task_supervisor.transition_count() as u64;
        let mut diagnostics = self
            .diagnostics
            .lock()
            .expect("runtime diagnostics lock")
            .clone();
        diagnostics.counters.task_transitions = task_count;
        diagnostics.counters.retirement_completed = manager_snapshot.retirement_completed;
        diagnostics.counters.retirement_failed = manager_snapshot.retirement_failed;
        RuntimeDiagnosticsSnapshot {
            active_generation: ActiveGenerationDiagnostics {
                generation_id: active_slot.generation_id(),
                digest_prefix: active_slot.digest_prefix().to_owned(),
                published_elapsed_ms: active_slot.snapshot().published_elapsed_ms,
                active_leases: active_slot.active_lease_count(),
                provider_count: active_generation.config().providers.len(),
                account_count: active_generation.config().all_accounts().len(),
                model_count: active_generation
                    .inference()
                    .router_handle()
                    .catalog_model_count(),
                finalization_active_jobs: finalization.active_jobs,
                finalization_capacity: finalization.capacity,
            },
            publication: PublicationDiagnostics {
                publication_epoch: manager_snapshot.publication_epoch,
                admission_closed: manager_snapshot.admission_closed,
                reload_gate_closed: manager_snapshot.admission_closed,
                reload_gate_waiters: manager_snapshot.gate_waiters,
                reload_in_progress: diagnostics.reload_in_progress,
                reload_phase: diagnostics.reload_phase.clone(),
                retirement_pending: manager_snapshot.retiring.len(),
            },
            retiring_generations,
            reload: ReloadDiagnostics {
                in_progress: diagnostics.reload_in_progress,
                phase: diagnostics.reload_phase,
                last_result: diagnostics.last_reload,
            },
            tasks: task_supervisor
                .snapshot()
                .into_iter()
                .map(task_diagnostic)
                .collect(),
            startup_recovery: self.startup_recovery_report(),
            shutdown: diagnostics.shutdown,
            counters: diagnostics.counters,
            metrics: self.metrics_coalescer.snapshot(),
        }
    }
}

/// Owns one reload diagnostic lifecycle for the retained reload worker. A
/// caller dropping its join future cannot clear another operation's marker;
/// dropping this guard is the final cleanup path for worker abort/panic.
pub(crate) struct ReloadDiagnosticGuard {
    process: ProcessRuntime,
    manager: RuntimeManager,
    owner: u64,
    finished: bool,
}

impl ReloadDiagnosticGuard {
    pub(crate) fn finish(mut self, result: &crate::reload::ReloadResult) {
        self.process.record_reload_diagnostics(self.owner, result);
        self.finished = true;
    }
}

impl Drop for ReloadDiagnosticGuard {
    fn drop(&mut self) {
        if !self.finished {
            self.process
                .abort_reload_diagnostics(self.owner, &self.manager);
        }
    }
}
