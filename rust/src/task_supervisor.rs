//! Process-owned recurring task supervision for the M8 runtime.
//!
//! The supervisor deliberately owns only scheduling and lifecycle state.  A
//! callback receives either a process marker or a fresh generation lease for
//! the current tick; it never receives a generation captured by a long-lived
//! task loop.

use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use thiserror::Error;
use tokio::{
    sync::{Notify, watch},
    task::JoinHandle,
};

use crate::{
    db::Database,
    runtime_lifecycle::{GenerationAcquireError, GenerationLease, RuntimeManager},
};

const DEFAULT_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);
const MAX_TASK_NAME_BYTES: usize = 128;
const MAX_CALLBACK_KIND_BYTES: usize = 128;

/// The canonical task names frozen by R001.
pub const RUNTIME_TASK_NAMES: [&str; 6] = [
    "catalog_refresh",
    "retention_cleanup",
    "checkpoint",
    "metrics_flush",
    "update_checker",
    "automatic_backup",
];

/// Whether a task is bound to process state or leases generation state per
/// tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum TaskOwnership {
    Process,
    ActiveGenerationLeased,
    /// Reserved only so unsupported ownership is rejected before commit.
    Unsupported,
}

impl TaskOwnership {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Process => "process",
            Self::ActiveGenerationLeased => "generation_leased",
            Self::Unsupported => "unsupported",
        }
    }
}

/// Immutable, secret-free description of one recurring task.
#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeTaskSpec {
    pub name: String,
    pub interval_s: f64,
    pub initial_delay_s: Option<f64>,
    pub run_immediately: bool,
    pub timeout_s: Option<f64>,
    pub ownership: TaskOwnership,
    pub enabled: bool,
    pub description: String,
    pub reloadable_fields: Vec<String>,
    pub generation_dependencies: Vec<String>,
    pub process_dependencies: Vec<String>,
    pub callback_kind: String,
}

impl RuntimeTaskSpec {
    fn validate(&self) -> Result<(), TaskSpecError> {
        if self.name.is_empty() || self.name.len() > MAX_TASK_NAME_BYTES {
            return Err(TaskSpecError::InvalidName {
                name: self.name.clone(),
            });
        }
        if !valid_seconds(self.interval_s) || self.interval_s < 0.0 {
            return Err(TaskSpecError::InvalidInterval {
                name: self.name.clone(),
            });
        }
        // Disabled inventory rows may carry zero as their resolved interval
        // (catalog_refresh is the R001 example).  A task that owns a loop
        // must always have a positive interval.
        if self.enabled && self.interval_s <= 0.0 {
            return Err(TaskSpecError::InvalidInterval {
                name: self.name.clone(),
            });
        }
        if let Some(delay) = self.initial_delay_s
            && (!valid_seconds(delay) || delay < 0.0)
        {
            return Err(TaskSpecError::InvalidInitialDelay {
                name: self.name.clone(),
            });
        }
        if self.run_immediately && self.initial_delay_s.is_some() {
            return Err(TaskSpecError::ConflictingFirstRunSchedule {
                name: self.name.clone(),
            });
        }
        if let Some(timeout) = self.timeout_s
            && (!valid_seconds(timeout) || timeout <= 0.0)
        {
            return Err(TaskSpecError::InvalidTimeout {
                name: self.name.clone(),
            });
        }
        if self.callback_kind.is_empty() || self.callback_kind.len() > MAX_CALLBACK_KIND_BYTES {
            return Err(TaskSpecError::InvalidCallbackKind {
                name: self.name.clone(),
            });
        }
        Ok(())
    }

    fn first_delay(&self) -> Duration {
        if self.run_immediately {
            Duration::ZERO
        } else {
            Duration::from_secs_f64(self.initial_delay_s.unwrap_or(self.interval_s))
        }
    }
}

/// Build the authoritative R001 inventory in canonical order.
pub fn runtime_task_inventory() -> Vec<RuntimeTaskSpec> {
    vec![
        spec(
            "catalog_refresh",
            300.0,
            None,
            false,
            TaskOwnership::ActiveGenerationLeased,
            "Periodically refresh the model catalog from enabled accounts",
            &["models.refresh_interval_s"],
            &["catalog", "model_info", "registry", "health_manager"],
            &["db"],
        ),
        spec(
            "retention_cleanup",
            86_400.0,
            None,
            false,
            TaskOwnership::ActiveGenerationLeased,
            "Clean up old requests, events, pings, and expired reservations",
            &[
                "dashboard.retain_request_stats_days",
                "dashboard.retain_event_days",
                "metrics.operational_event_retain_days",
                "metrics.routing_decision_retain_days",
                "models.ping_retain_days",
            ],
            &["router"],
            &["db"],
        ),
        spec(
            "checkpoint",
            14_400.0,
            None,
            true,
            TaskOwnership::Process,
            "Periodic SQLite WAL checkpoint",
            &[],
            &[],
            &["db"],
        ),
        spec(
            "metrics_flush",
            30.0,
            Some(5.0),
            false,
            TaskOwnership::Process,
            "Flush buffered metrics analytics to SQLite",
            &["metrics.write_mode", "metrics.flush_interval_s"],
            &[],
            &["metrics_coalescer"],
        ),
        spec(
            "update_checker",
            86_400.0,
            None,
            true,
            TaskOwnership::Process,
            "Periodically check PyPI for new EggPool releases",
            &[],
            &[],
            &["outbound_manager"],
        ),
        spec(
            "automatic_backup",
            86_400.0,
            Some(300.0),
            false,
            TaskOwnership::Process,
            "Create periodic backup archives of config and database",
            &[
                "backup.enabled",
                "backup.interval_s",
                "backup.startup_delay_s",
                "backup.retain_count",
            ],
            &[],
            &["db"],
        ),
    ]
}

/// Resolve the inventory against the config values that R001 identifies as
/// task schedule gates.  Disabled rows remain in the inventory so a staged
/// diff can report the transition deterministically, but own no loop.
pub fn runtime_task_specs_for_config(
    config: &crate::Config,
    include_update_checker: bool,
) -> Vec<RuntimeTaskSpec> {
    let mut specs = runtime_task_inventory();
    for task in &mut specs {
        match task.name.as_str() {
            "catalog_refresh" => {
                task.interval_s = config.models.refresh_interval_s as f64;
                task.enabled = task.interval_s > 0.0;
            }
            "retention_cleanup" => {
                task.interval_s = config.metrics.cleanup_interval_s as f64;
            }
            "metrics_flush" => {
                task.interval_s = config.metrics.flush_interval_s as f64;
                task.enabled = config.metrics.write_mode != "immediate";
            }
            "update_checker" => {
                task.enabled = include_update_checker && config.update_checker.enabled;
            }
            "automatic_backup" => {
                task.interval_s = config.backup.interval_s as f64;
                task.initial_delay_s = Some(config.backup.startup_delay_s as f64);
                task.enabled = config.backup.enabled && task.interval_s > 0.0;
            }
            _ => {}
        }
    }
    specs
}

#[allow(clippy::too_many_arguments)]
fn spec(
    name: &str,
    interval_s: f64,
    initial_delay_s: Option<f64>,
    run_immediately: bool,
    ownership: TaskOwnership,
    description: &str,
    reloadable_fields: &[&str],
    generation_dependencies: &[&str],
    process_dependencies: &[&str],
) -> RuntimeTaskSpec {
    RuntimeTaskSpec {
        name: name.to_owned(),
        interval_s,
        initial_delay_s,
        run_immediately,
        timeout_s: None,
        ownership,
        enabled: true,
        description: description.to_owned(),
        reloadable_fields: reloadable_fields.iter().map(|v| (*v).to_owned()).collect(),
        generation_dependencies: generation_dependencies
            .iter()
            .map(|v| (*v).to_owned())
            .collect(),
        process_dependencies: process_dependencies
            .iter()
            .map(|v| (*v).to_owned())
            .collect(),
        callback_kind: name.to_owned(),
    }
}

/// The context supplied to one callback invocation.  A generation callback
/// owns the lease for the duration of that invocation and drops it before the
/// next sleep; process callbacks receive no generation authority.
#[derive(Debug)]
pub enum TaskTickContext {
    Process,
    Generation(GenerationLease),
}

pub type TaskCallbackFuture = Pin<Box<dyn Future<Output = Result<(), TaskCallbackError>> + Send>>;
pub type TaskCallback = Arc<dyn Fn(TaskTickContext) -> TaskCallbackFuture + Send + Sync>;

/// Create a callback from an async closure without requiring an async-trait
/// dependency.
pub fn task_callback<F, Fut>(callback: F) -> TaskCallback
where
    F: Fn(TaskTickContext) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<(), TaskCallbackError>> + Send + 'static,
{
    Arc::new(move |context| Box::pin(callback(context)))
}

#[derive(Debug, Error, Clone)]
pub enum TaskCallbackError {
    #[error("task callback failed")]
    Failed,
}

/// Callback capabilities are prepared before publication.  Missing entries
/// are a typed error, making deferred M9 business callbacks visible instead
/// of silently registering a no-op.
#[derive(Clone, Default)]
pub struct TaskCallbackRegistry {
    callbacks: BTreeMap<String, TaskCallback>,
}

impl std::fmt::Debug for TaskCallbackRegistry {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TaskCallbackRegistry")
            .field("callback_kinds", &self.callbacks.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl TaskCallbackRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, callback_kind: impl Into<String>, callback: TaskCallback) {
        self.callbacks.insert(callback_kind.into(), callback);
    }

    pub fn with_callback(
        mut self,
        callback_kind: impl Into<String>,
        callback: TaskCallback,
    ) -> Self {
        self.register(callback_kind, callback);
        self
    }

    fn get(&self, callback_kind: &str) -> Option<TaskCallback> {
        self.callbacks.get(callback_kind).cloned()
    }

    /// The only business callback available in R006.  R008 supplies the
    /// generation-leased maintenance callbacks and M9 supplies update/backup.
    pub fn with_checkpoint(database: Database) -> Self {
        Self::new().with_callback(
            "checkpoint",
            task_callback(move |_| {
                let database = database.clone();
                async move {
                    database
                        .checkpoint()
                        .await
                        .map_err(|_| TaskCallbackError::Failed)
                }
            }),
        )
    }

    pub fn available_kinds(&self) -> Vec<String> {
        self.callbacks.keys().cloned().collect()
    }
}

/// Stable result of comparing two inventory snapshots.
#[derive(Debug, Clone, PartialEq)]
pub struct TaskSpecDiff {
    pub added: Vec<RuntimeTaskSpec>,
    pub removed: Vec<RuntimeTaskSpec>,
    pub rescheduled: Vec<(RuntimeTaskSpec, RuntimeTaskSpec)>,
    pub unchanged: Vec<RuntimeTaskSpec>,
}

impl TaskSpecDiff {
    fn names(specs: &[RuntimeTaskSpec]) -> BTreeMap<String, RuntimeTaskSpec> {
        specs
            .iter()
            .cloned()
            .map(|spec| (spec.name.clone(), spec))
            .collect()
    }

    fn compute(
        current: &[RuntimeTaskSpec],
        candidate: &[RuntimeTaskSpec],
    ) -> Result<Self, TaskSpecError> {
        let current = validate_specs(current)?;
        let candidate = validate_specs(candidate)?;
        let current = Self::names(&current);
        let candidate = Self::names(&candidate);
        let names = current
            .keys()
            .chain(candidate.keys())
            .cloned()
            .collect::<BTreeSet<_>>();
        let mut diff = Self {
            added: Vec::new(),
            removed: Vec::new(),
            rescheduled: Vec::new(),
            unchanged: Vec::new(),
        };
        for name in names {
            match (current.get(&name), candidate.get(&name)) {
                (None, Some(next)) if next.enabled => diff.added.push(next.clone()),
                (Some(previous), None) if previous.enabled => diff.removed.push(previous.clone()),
                (Some(previous), Some(next)) if !previous.enabled && next.enabled => {
                    diff.added.push(next.clone())
                }
                (Some(previous), Some(next)) if previous.enabled && !next.enabled => {
                    diff.removed.push(previous.clone())
                }
                (Some(previous), Some(next)) if previous.enabled && next.enabled => {
                    if previous != next {
                        diff.rescheduled.push((previous.clone(), next.clone()));
                    } else {
                        diff.unchanged.push(previous.clone());
                    }
                }
                (Some(previous), Some(_next)) => diff.unchanged.push(previous.clone()),
                _ => {}
            }
        }
        Ok(diff)
    }

    pub fn added_names(&self) -> Vec<String> {
        self.added.iter().map(|spec| spec.name.clone()).collect()
    }

    pub fn removed_names(&self) -> Vec<String> {
        self.removed.iter().map(|spec| spec.name.clone()).collect()
    }

    pub fn rescheduled_names(&self) -> Vec<String> {
        self.rescheduled
            .iter()
            .map(|(_, spec)| spec.name.clone())
            .collect()
    }
}

fn validate_specs(specs: &[RuntimeTaskSpec]) -> Result<Vec<RuntimeTaskSpec>, TaskSpecError> {
    let mut names = BTreeSet::new();
    for spec in specs {
        spec.validate()?;
        if !names.insert(spec.name.clone()) {
            return Err(TaskSpecError::DuplicateName {
                name: spec.name.clone(),
            });
        }
    }
    Ok(specs.to_vec())
}

#[derive(Debug, Error, Clone, PartialEq)]
pub enum TaskSpecError {
    #[error("task name is invalid: {name}")]
    InvalidName { name: String },
    #[error("task {name} has an invalid interval")]
    InvalidInterval { name: String },
    #[error("task {name} has an invalid initial delay")]
    InvalidInitialDelay { name: String },
    #[error("task {name} has both immediate and explicit first-run scheduling")]
    ConflictingFirstRunSchedule { name: String },
    #[error("task {name} has an invalid timeout")]
    InvalidTimeout { name: String },
    #[error("task {name} has an invalid callback kind")]
    InvalidCallbackKind { name: String },
    #[error("duplicate task name: {name}")]
    DuplicateName { name: String },
    #[error("task {name} has no registered callback capability ({callback_kind})")]
    MissingCallbackCapability { name: String, callback_kind: String },
    #[error("task ownership is unsupported for {name}")]
    UnsupportedOwnership { name: String },
    #[error("task supervisor is shutting down")]
    ShuttingDown,
    #[error("task diff has already been finalized")]
    AlreadyFinalized,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TaskControl {
    Running,
    Stop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskOutcome {
    Success,
    Error,
    TimedOut,
    Panicked,
    GenerationUnavailable,
    Cancelled,
}

impl TaskOutcome {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Error => "error",
            Self::TimedOut => "timeout",
            Self::Panicked => "panic_or_join_failure",
            Self::GenerationUnavailable => "generation_unavailable",
            Self::Cancelled => "cancelled",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeTaskSnapshot {
    pub name: String,
    pub ownership: TaskOwnership,
    pub enabled: bool,
    pub running: bool,
    pub interval_s: f64,
    pub initial_delay_s: Option<f64>,
    pub initial_delay_class: &'static str,
    pub tick_count: u64,
    pub last_outcome: Option<TaskOutcome>,
    pub last_elapsed_ms: Option<u64>,
    pub in_tick: bool,
    pub reschedule_count: u64,
}

struct TaskState {
    spec: RuntimeTaskSpec,
    callback: TaskCallback,
    cancel: watch::Sender<TaskControl>,
    cancel_notify: Notify,
    cancelled: AtomicBool,
    join: Mutex<Option<JoinHandle<()>>>,
    running: AtomicBool,
    in_tick: AtomicBool,
    tick_count: AtomicU64,
    reschedule_count: AtomicU64,
    last_outcome: Mutex<Option<TaskOutcome>>,
    last_elapsed_ms: Mutex<Option<u64>>,
}

impl TaskState {
    fn new(spec: RuntimeTaskSpec, callback: TaskCallback) -> Arc<Self> {
        let (cancel, _) = watch::channel(TaskControl::Running);
        Arc::new(Self {
            spec,
            callback,
            cancel,
            cancel_notify: Notify::new(),
            cancelled: AtomicBool::new(false),
            join: Mutex::new(None),
            running: AtomicBool::new(false),
            in_tick: AtomicBool::new(false),
            tick_count: AtomicU64::new(0),
            reschedule_count: AtomicU64::new(0),
            last_outcome: Mutex::new(None),
            last_elapsed_ms: Mutex::new(None),
        })
    }

    fn start(self: &Arc<Self>, supervisor: Arc<SupervisorInner>) {
        self.running.store(true, Ordering::Release);
        let state = Arc::clone(self);
        let join = tokio::spawn(async move { run_task(state, supervisor).await });
        *self.join.lock().expect("task join lock") = Some(join);
    }

    fn record(&self, outcome: TaskOutcome, elapsed: Duration) {
        self.tick_count.fetch_add(1, Ordering::Relaxed);
        *self.last_outcome.lock().expect("task outcome lock") = Some(outcome);
        *self.last_elapsed_ms.lock().expect("task elapsed lock") =
            Some(elapsed.as_millis().min(u64::MAX as u128) as u64);
    }

    fn snapshot(&self) -> RuntimeTaskSnapshot {
        let initial_delay_class = if self.spec.run_immediately {
            "immediate"
        } else if self.spec.initial_delay_s.is_some() {
            "explicit"
        } else {
            "interval"
        };
        RuntimeTaskSnapshot {
            name: self.spec.name.clone(),
            ownership: self.spec.ownership,
            enabled: self.spec.enabled,
            running: self.running.load(Ordering::Acquire),
            interval_s: self.spec.interval_s,
            initial_delay_s: self.spec.initial_delay_s,
            initial_delay_class,
            tick_count: self.tick_count.load(Ordering::Relaxed),
            last_outcome: *self.last_outcome.lock().expect("task outcome lock"),
            last_elapsed_ms: *self.last_elapsed_ms.lock().expect("task elapsed lock"),
            in_tick: self.in_tick.load(Ordering::Acquire),
            reschedule_count: self.reschedule_count.load(Ordering::Relaxed),
        }
    }
}

struct SupervisorInner {
    tasks: Mutex<BTreeMap<String, Arc<TaskState>>>,
    callbacks: Mutex<TaskCallbackRegistry>,
    generation_manager: Mutex<Option<RuntimeManager>>,
    shutting_down: AtomicBool,
    transition_count: AtomicUsize,
}

/// The single process-owned recurring task supervisor.
#[derive(Clone)]
pub struct RuntimeTaskSupervisor {
    inner: Arc<SupervisorInner>,
}

impl std::fmt::Debug for RuntimeTaskSupervisor {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RuntimeTaskSupervisor")
            .field("task_count", &self.task_count())
            .field("running_count", &self.running_count())
            .field("shutting_down", &self.is_shutting_down())
            .finish()
    }
}

impl Default for RuntimeTaskSupervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl RuntimeTaskSupervisor {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(SupervisorInner {
                tasks: Mutex::new(BTreeMap::new()),
                callbacks: Mutex::new(TaskCallbackRegistry::new()),
                generation_manager: Mutex::new(None),
                shutting_down: AtomicBool::new(false),
                transition_count: AtomicUsize::new(0),
            }),
        }
    }

    pub fn with_callbacks(callbacks: TaskCallbackRegistry) -> Self {
        let supervisor = Self::new();
        *supervisor
            .inner
            .callbacks
            .lock()
            .expect("task callback registry lock") = callbacks;
        supervisor
    }

    /// Attach the process's active-generation authority.  The manager is
    /// cloned as a handle; no generation is captured by a task loop.
    pub fn set_generation_manager(&self, manager: RuntimeManager) {
        *self
            .inner
            .generation_manager
            .lock()
            .expect("generation manager lock") = Some(manager);
    }

    pub fn register_callback(&self, callback_kind: impl Into<String>, callback: TaskCallback) {
        self.inner
            .callbacks
            .lock()
            .expect("task callback registry lock")
            .register(callback_kind, callback);
    }

    /// Return callback capabilities available to the current process. Reload
    /// preflight uses this to keep deferred R008/M9 business callbacks
    /// explicit rather than silently installing no-op loops.
    pub fn available_callback_kinds(&self) -> Vec<String> {
        self.inner
            .callbacks
            .lock()
            .expect("task callback registry lock")
            .available_kinds()
    }

    pub fn task_count(&self) -> usize {
        self.inner.tasks.lock().expect("task map lock").len()
    }

    pub fn running_count(&self) -> usize {
        self.inner
            .tasks
            .lock()
            .expect("task map lock")
            .values()
            .filter(|task| task.running.load(Ordering::Acquire))
            .count()
    }

    pub fn join_handle_count(&self) -> usize {
        self.inner
            .tasks
            .lock()
            .expect("task map lock")
            .values()
            .filter(|task| task.join.lock().expect("task join lock").is_some())
            .count()
    }

    pub fn transition_count(&self) -> usize {
        self.inner.transition_count.load(Ordering::Relaxed)
    }

    pub fn is_shutting_down(&self) -> bool {
        self.inner.shutting_down.load(Ordering::Acquire)
    }

    pub fn task_snapshot(&self, name: &str) -> Option<RuntimeTaskSnapshot> {
        self.inner
            .tasks
            .lock()
            .expect("task map lock")
            .get(name)
            .map(|task| task.snapshot())
    }

    pub fn snapshot(&self) -> Vec<RuntimeTaskSnapshot> {
        self.inner
            .tasks
            .lock()
            .expect("task map lock")
            .values()
            .map(|task| task.snapshot())
            .collect()
    }

    pub fn active_specs(&self) -> Vec<RuntimeTaskSpec> {
        self.inner
            .tasks
            .lock()
            .expect("task map lock")
            .values()
            .map(|task| task.spec.clone())
            .collect()
    }

    pub fn prepare_diff(
        &self,
        current_specs: &[RuntimeTaskSpec],
        candidate_specs: &[RuntimeTaskSpec],
    ) -> Result<PreparedTaskDiff, TaskSpecError> {
        let callbacks = self
            .inner
            .callbacks
            .lock()
            .expect("task callback registry lock")
            .clone();
        self.prepare_diff_with_callbacks(current_specs, candidate_specs, &callbacks)
    }

    pub fn prepare_diff_with_callbacks(
        &self,
        current_specs: &[RuntimeTaskSpec],
        candidate_specs: &[RuntimeTaskSpec],
        callbacks: &TaskCallbackRegistry,
    ) -> Result<PreparedTaskDiff, TaskSpecError> {
        if self.is_shutting_down() {
            return Err(TaskSpecError::ShuttingDown);
        }
        let diff = TaskSpecDiff::compute(current_specs, candidate_specs)?;
        for spec in diff
            .added
            .iter()
            .chain(diff.rescheduled.iter().map(|(_, next)| next))
        {
            if spec.ownership != TaskOwnership::Process
                && spec.ownership != TaskOwnership::ActiveGenerationLeased
            {
                return Err(TaskSpecError::UnsupportedOwnership {
                    name: spec.name.clone(),
                });
            }
            if callbacks.get(&spec.callback_kind).is_none() {
                return Err(TaskSpecError::MissingCallbackCapability {
                    name: spec.name.clone(),
                    callback_kind: spec.callback_kind.clone(),
                });
            }
        }

        // Allocate callback/channel state during preflight.  Commit only
        // inserts these prepared states and starts their loops.
        let prepared = diff
            .rescheduled
            .iter()
            .map(|(_, next)| next)
            .chain(diff.added.iter())
            .map(|spec| {
                TaskState::new(
                    spec.clone(),
                    callbacks
                        .get(&spec.callback_kind)
                        .expect("callback validated during preflight"),
                )
            })
            .collect();
        Ok(PreparedTaskDiff {
            supervisor: self.clone(),
            diff,
            prepared,
            state: PreparedDiffState::Prepared,
            previous_specs: current_specs.to_vec(),
        })
    }

    pub async fn shutdown(&self) -> TaskShutdownReport {
        self.shutdown_with_timeout(DEFAULT_SHUTDOWN_TIMEOUT).await
    }

    pub async fn shutdown_with_timeout(&self, timeout: Duration) -> TaskShutdownReport {
        self.inner.shutting_down.store(true, Ordering::Release);
        let tasks = {
            let mut map = self.inner.tasks.lock().expect("task map lock");
            std::mem::take(&mut *map).into_values().collect::<Vec<_>>()
        };
        let started = tasks.len();
        let deadline = tokio::time::Instant::now() + timeout;
        let mut joined = 0;
        for task in tasks {
            cancel_task(&task);
            let join = task.join.lock().expect("task join lock").take();
            let Some(mut join) = join else { continue };
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if tokio::time::timeout(remaining, &mut join).await.is_err() {
                join.abort();
                let _ = join.await;
            }
            task.running.store(false, Ordering::Release);
            joined += 1;
        }
        TaskShutdownReport {
            started,
            joined,
            remaining: self.task_count(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PreparedDiffState {
    Prepared,
    Committed,
    Discarded,
}

/// Side-effect-free task changes staged for a later accepted publication.
pub struct PreparedTaskDiff {
    supervisor: RuntimeTaskSupervisor,
    diff: TaskSpecDiff,
    prepared: Vec<Arc<TaskState>>,
    state: PreparedDiffState,
    previous_specs: Vec<RuntimeTaskSpec>,
}

impl std::fmt::Debug for PreparedTaskDiff {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PreparedTaskDiff")
            .field("added", &self.diff.added_names())
            .field("removed", &self.diff.removed_names())
            .field("rescheduled", &self.diff.rescheduled_names())
            .field("state", &self.state)
            .finish()
    }
}

impl PreparedTaskDiff {
    pub fn diff(&self) -> &TaskSpecDiff {
        &self.diff
    }

    pub fn preflight(&self) -> Result<(), TaskSpecError> {
        if self.state != PreparedDiffState::Prepared {
            return Err(TaskSpecError::AlreadyFinalized);
        }
        Ok(())
    }

    /// Apply exactly the affected operations.  The caller invokes this only
    /// after the runtime publication acceptance point; no callback can run
    /// while this diff is merely staged.
    pub async fn commit(&mut self) -> Result<TaskTransition, TaskSpecError> {
        self.preflight()?;
        if self.supervisor.is_shutting_down() {
            return Err(TaskSpecError::ShuttingDown);
        }

        let prepared = std::mem::take(&mut self.prepared);
        let mut prepared = prepared.into_iter();
        for spec in &self.diff.removed {
            if let Some(task) = self.remove_task(&spec.name) {
                stop_task(task).await;
            }
        }
        for spec in &self.diff.rescheduled {
            if let Some(task) = self.remove_task(&spec.0.name) {
                stop_task(task).await;
            }
            let task = prepared.next().expect("prepared rescheduled state");
            task.reschedule_count.fetch_add(1, Ordering::Relaxed);
            self.insert_and_start(task).await;
        }
        for spec in &self.diff.added {
            let task = prepared.next().expect("prepared added state");
            debug_assert_eq!(task.spec.name, spec.name);
            self.insert_and_start(task).await;
        }
        self.state = PreparedDiffState::Committed;
        self.supervisor
            .inner
            .transition_count
            .fetch_add(1, Ordering::Relaxed);
        Ok(TaskTransition {
            added: self.diff.added_names(),
            removed: self.diff.removed_names(),
            rescheduled: self.diff.rescheduled_names(),
            unchanged: self
                .diff
                .unchanged
                .iter()
                .map(|spec| spec.name.clone())
                .collect(),
        })
    }

    /// Restore the pre-commit spec set after a later acceptance step fails.
    /// The normal commit path is fail-fast before mutation; this inverse path
    /// is retained for the rare SQLite commit/compensation boundary.
    pub async fn rollback_committed(&mut self) -> Result<(), TaskSpecError> {
        if self.state != PreparedDiffState::Committed {
            return Err(TaskSpecError::AlreadyFinalized);
        }
        let current = self.supervisor.active_specs();
        let mut inverse = self
            .supervisor
            .prepare_diff(&current, &self.previous_specs)?;
        inverse.commit().await?;
        self.state = PreparedDiffState::Discarded;
        Ok(())
    }

    fn remove_task(&self, name: &str) -> Option<Arc<TaskState>> {
        self.supervisor
            .inner
            .tasks
            .lock()
            .expect("task map lock")
            .remove(name)
    }

    async fn insert_and_start(&self, task: Arc<TaskState>) {
        self.supervisor
            .inner
            .tasks
            .lock()
            .expect("task map lock")
            .insert(task.spec.name.clone(), Arc::clone(&task));
        task.start(Arc::clone(&self.supervisor.inner));
    }

    /// Discarding a prepared diff is synchronous and idempotent.  Prepared
    /// channels have no running task, so dropping them is sufficient cleanup.
    pub fn rollback(&mut self) {
        if self.state == PreparedDiffState::Prepared {
            self.prepared.clear();
            self.state = PreparedDiffState::Discarded;
        }
    }

    pub fn discard(&mut self) {
        self.rollback();
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskTransition {
    pub added: Vec<String>,
    pub removed: Vec<String>,
    pub rescheduled: Vec<String>,
    pub unchanged: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskShutdownReport {
    pub started: usize,
    pub joined: usize,
    pub remaining: usize,
}

async fn stop_task(task: Arc<TaskState>) {
    cancel_task(&task);
    let join = task.join.lock().expect("task join lock").take();
    if let Some(mut join) = join {
        if tokio::time::timeout(DEFAULT_SHUTDOWN_TIMEOUT, &mut join)
            .await
            .is_err()
        {
            join.abort();
            let _ = join.await;
        }
    }
    task.running.store(false, Ordering::Release);
}

async fn run_task(state: Arc<TaskState>, supervisor: Arc<SupervisorInner>) {
    if !wait_or_cancel(&state, state.spec.first_delay()).await {
        state.running.store(false, Ordering::Release);
        return;
    }

    loop {
        if state.cancelled.load(Ordering::Acquire) {
            state.running.store(false, Ordering::Release);
            return;
        }
        state.in_tick.store(true, Ordering::Release);
        let started = Instant::now();
        let outcome = match state.spec.ownership {
            TaskOwnership::Process => run_callback(&state, TaskTickContext::Process).await,
            TaskOwnership::ActiveGenerationLeased => {
                let manager = supervisor
                    .generation_manager
                    .lock()
                    .expect("generation manager lock")
                    .clone();
                match manager.as_ref() {
                    None => TaskOutcome::GenerationUnavailable,
                    Some(manager) => {
                        let lease = tokio::select! {
                            result = manager.acquire() => result,
                            _ = state.cancel_notify.notified() => {
                                state.in_tick.store(false, Ordering::Release);
                                state.running.store(false, Ordering::Release);
                                return;
                            }
                        };
                        match lease {
                            Ok(lease) => {
                                run_callback(&state, TaskTickContext::Generation(lease)).await
                            }
                            Err(GenerationAcquireError::ShuttingDown) => {
                                state.in_tick.store(false, Ordering::Release);
                                state.running.store(false, Ordering::Release);
                                return;
                            }
                            Err(GenerationAcquireError::AdmissionClosed) => {
                                TaskOutcome::GenerationUnavailable
                            }
                        }
                    }
                }
            }
            TaskOwnership::Unsupported => TaskOutcome::Error,
        };
        state.in_tick.store(false, Ordering::Release);
        state.record(outcome, started.elapsed());
        if outcome == TaskOutcome::Cancelled || state.cancelled.load(Ordering::Acquire) {
            state.running.store(false, Ordering::Release);
            return;
        }
        if !wait_or_cancel(&state, Duration::from_secs_f64(state.spec.interval_s)).await {
            state.running.store(false, Ordering::Release);
            return;
        }
    }
}

async fn wait_or_cancel(state: &TaskState, delay: Duration) -> bool {
    let notified = state.cancel_notify.notified();
    if state.cancelled.load(Ordering::Acquire) {
        return false;
    }
    if delay.is_zero() {
        return !state.cancelled.load(Ordering::Acquire);
    }
    tokio::select! {
        _ = tokio::time::sleep(delay) => true,
        _ = notified => false,
    }
}

async fn run_callback(state: &TaskState, context: TaskTickContext) -> TaskOutcome {
    let callback = Arc::clone(&state.callback);
    let mut tick = tokio::spawn(async move { (callback)(context).await });
    let notified = state.cancel_notify.notified();
    let completion = async {
        if let Some(timeout_s) = state.spec.timeout_s {
            tokio::select! {
                result = &mut tick => Some(match result {
                    Ok(Ok(())) => TaskOutcome::Success,
                    Ok(Err(_)) => TaskOutcome::Error,
                    Err(_) => TaskOutcome::Panicked,
                }),
                _ = tokio::time::sleep(Duration::from_secs_f64(timeout_s)) => {
                    tick.abort();
                    let _ = tick.await;
                    Some(TaskOutcome::TimedOut)
                },
                _ = notified => {
                    tick.abort();
                    let _ = tick.await;
                    Some(TaskOutcome::Cancelled)
                },
            }
        } else {
            tokio::select! {
                result = &mut tick => Some(match result {
                    Ok(Ok(())) => TaskOutcome::Success,
                    Ok(Err(_)) => TaskOutcome::Error,
                    Err(_) => TaskOutcome::Panicked,
                }),
                _ = notified => {
                    tick.abort();
                    let _ = tick.await;
                    Some(TaskOutcome::Cancelled)
                },
            }
        }
    };
    completion.await.unwrap_or(TaskOutcome::Cancelled)
}

fn cancel_task(task: &TaskState) {
    task.cancelled.store(true, Ordering::Release);
    let _ = task.cancel.send(TaskControl::Stop);
    task.cancel_notify.notify_one();
}

fn valid_seconds(value: f64) -> bool {
    value.is_finite() && value <= Duration::MAX.as_secs_f64()
}
