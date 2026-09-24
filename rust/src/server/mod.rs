//! Axum HTTP server for the native EggPool runtime.
//!
//! Health/readiness, dashboard reads, authentication, static resources, and
//! the public inference endpoints (Chat Completions, Responses,
//! Responses compact, Messages) through the thin coordinator boundary.
//! Handlers invoke one coordinator entry point; routing/retry/finalization
//! live in the coordinator, not here.

mod dashboard;
mod health;
mod inference;
mod middleware;

use dashboard::{
    accounts_page, bandwidth_page, cache_page, events_page, json_response, latency_page,
    model_detail_page, models_page, overview, pings_page, reliability_page, routing_page,
    runtime_page, static_chart_js, static_css, static_favicon, static_js, summary, sync_accounts,
    theme_css, timeseries_page, traces_page,
};
use health::{
    healthz, integration_profile, models_api, readyz, runtime_status, status_api, update_status,
};
use inference::{chat_completions, messages, responses, responses_compact};
use middleware::{admit_inference_body, authenticate, map_generation_error, validate_server_key};

use axum::{
    Router,
    body::{Body, Bytes},
    extract::{Extension, Path as AxumPath, Query, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    middleware::{Next, from_fn_with_state},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use http_body_util::{BodyExt, Limited};
use hyper::body::Body as HttpBody;
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use std::time::Duration;
use thiserror::Error;
use tokio::net::TcpListener;
use tokio::sync::{Notify, oneshot};

use crate::{
    Config,
    coordinator::{InferenceState, endpoint_error_body},
    db,
    operations::{paths::RuntimePaths, process},
    providers::ProviderClientPool,
    runtime_lifecycle::{
        GenerationBuildError, GenerationLease, ProcessRuntime, RuntimeGeneration,
        RuntimeGenerationFactory, RuntimeManager, StartupRecoveryError, TaskSpecError,
    },
    wire::ir::ClientSurface,
};
use std::{path::Path, sync::Arc, time::Instant};

#[derive(Debug, Error)]
pub enum ServerError {
    #[error("{0}")]
    Database(#[from] db::DatabaseError),
    #[error("cannot bind listener: {0}")]
    Bind(#[from] std::io::Error),
    #[error("invalid server API key configuration")]
    InvalidApiKey,
    #[error("provider client pool construction failed: {0}")]
    ProviderPool(#[from] crate::providers::ProviderClientPoolError),
    #[error("runtime generation construction failed: {0}")]
    Generation(#[from] GenerationBuildError),
    #[error("prepared generation transfer failed: {0}")]
    CandidateTransfer(String),
    #[error("startup crash reconciliation failed: {0}")]
    StartupRecovery(#[from] StartupRecoveryError),
    #[error("runtime task installation failed: {0}")]
    TaskSetup(#[from] TaskSpecError),
    #[error("local control listener failed: {0}")]
    Control(#[from] crate::operations::control::ControlError),
    #[error("server signal handler failed: {0}")]
    Signal(#[from] SignalError),
    #[error("server forced shutdown exceeded its graceful deadline")]
    ForcedShutdown(ShutdownReport),
    #[error("EggServe downstream HTTP runtime failed: {0}")]
    EggServe(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("database close failed during server shutdown: {detail}")]
    ShutdownDatabase { detail: String },
    #[error("inference state construction failed: {0}")]
    Inference(String),
    #[error("server lifecycle state failed: {0}")]
    Process(#[from] crate::operations::process::ProcessError),
    #[error("cannot bind listener: {detail}")]
    StartupConflict { detail: String },
}

const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
const EGG_SERVE_REQUEST_BODY_LIMIT: u64 = 1024 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[repr(u8)]
pub enum ShutdownPhase {
    Running = 0,
    Quiescing = 1,
    Draining = 2,
    Closing = 3,
    ForcedClosing = 4,
    Stopped = 5,
}

impl ShutdownPhase {
    fn from_u8(value: u8) -> Self {
        match value {
            1 => Self::Quiescing,
            2 => Self::Draining,
            3 => Self::Closing,
            4 => Self::ForcedClosing,
            5 => Self::Stopped,
            _ => Self::Running,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShutdownReason {
    CtrlC,
    Sigterm,
    ServerCompleted,
    Requested,
    SignalFailure,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SignalError {
    CtrlC(String),
    Sigterm(String),
}

impl std::fmt::Display for SignalError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::CtrlC(detail) => write!(formatter, "Ctrl-C registration failed: {detail}"),
            Self::Sigterm(detail) => write!(formatter, "SIGTERM registration failed: {detail}"),
        }
    }
}

impl std::error::Error for SignalError {}

/// Secret-free, bounded evidence for one completed process shutdown.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShutdownReport {
    pub phase: ShutdownPhase,
    pub reason: ShutdownReason,
    pub forced: bool,
    pub active_leases_at_deadline: usize,
    pub terminal_references_at_deadline: usize,
    pub body_tasks_at_deadline: usize,
    pub task_count_at_start: usize,
    pub task_count_joined: usize,
    pub database_closed: bool,
    pub database_error: Option<String>,
}

struct BodyTaskTrackerInner {
    next_id: AtomicU64,
    active: std::sync::Mutex<std::collections::BTreeMap<u64, tokio::task::AbortHandle>>,
    notify: Notify,
}

#[derive(Clone)]
struct BodyTaskTracker {
    inner: Arc<BodyTaskTrackerInner>,
}

impl BodyTaskTracker {
    fn new() -> Self {
        Self {
            inner: Arc::new(BodyTaskTrackerInner {
                next_id: AtomicU64::new(1),
                active: std::sync::Mutex::new(std::collections::BTreeMap::new()),
                notify: Notify::new(),
            }),
        }
    }

    fn active_count(&self) -> usize {
        self.inner.active.lock().expect("body task lock").len()
    }

    fn spawn<F>(&self, future: F)
    where
        F: std::future::Future<Output = ()> + Send + 'static,
    {
        let id = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let tracker = self.clone();
        let (started, ready) = oneshot::channel();
        let handle = tokio::spawn(async move {
            let _ = ready.await;
            future.await;
            let removed = tracker
                .inner
                .active
                .lock()
                .expect("body task lock")
                .remove(&id)
                .is_some();
            if removed {
                tracker.inner.notify.notify_waiters();
            }
        });
        self.inner
            .active
            .lock()
            .expect("body task lock")
            .insert(id, handle.abort_handle());
        let _ = started.send(());
    }

    async fn wait_empty(&self, timeout: Duration) -> bool {
        tokio::time::timeout(timeout, async {
            loop {
                let notified = self.inner.notify.notified();
                if self.active_count() == 0 {
                    return;
                }
                notified.await;
            }
        })
        .await
        .is_ok()
    }

    fn abort_all(&self) -> usize {
        let handles = {
            let mut active = self.inner.active.lock().expect("body task lock");
            let handles = active.values().cloned().collect::<Vec<_>>();
            active.clear();
            handles
        };
        for handle in &handles {
            handle.abort();
        }
        if !handles.is_empty() {
            self.inner.notify.notify_waiters();
        }
        handles.len()
    }
}

struct ServerRuntimeInner {
    process: ProcessRuntime,
    manager: Arc<RuntimeManager>,
    body_tasks: BodyTaskTracker,
    phase: AtomicU8,
    reason: std::sync::Mutex<Option<ShutdownReason>>,
    signal_failure: std::sync::Mutex<Option<SignalError>>,
    notify: Notify,
}

/// Explicit owner for the complete foreground process lifecycle.
pub struct ServerRuntime {
    inner: Arc<ServerRuntimeInner>,
    server_state: ServerState,
    shutdown_timeout: Duration,
    control_server: Option<crate::operations::control::ControlServerHandle>,
}

#[derive(Clone)]
pub struct ServerRuntimeHandle {
    inner: Arc<ServerRuntimeInner>,
}

impl ServerRuntime {
    pub fn new(process: ProcessRuntime, manager: Arc<RuntimeManager>, config: Config) -> Self {
        Self {
            inner: Arc::new(ServerRuntimeInner {
                process,
                manager,
                body_tasks: BodyTaskTracker::new(),
                phase: AtomicU8::new(ShutdownPhase::Running as u8),
                reason: std::sync::Mutex::new(None),
                signal_failure: std::sync::Mutex::new(None),
                notify: Notify::new(),
            }),
            server_state: ServerState::from_config(&config),
            shutdown_timeout: GRACEFUL_SHUTDOWN_TIMEOUT,
            control_server: None,
        }
    }

    /// Use a shorter bounded window for deterministic embedding/tests.
    pub fn with_shutdown_timeout(mut self, timeout: Duration) -> Self {
        self.shutdown_timeout = timeout.max(Duration::from_millis(1));
        self
    }

    /// Attach the one process-owned local control listener.  The listener is
    /// closed before M8 resources begin shutting down.
    pub fn with_control_server(
        mut self,
        control_server: crate::operations::control::ControlServerHandle,
    ) -> Self {
        self.control_server = Some(control_server);
        self
    }

    pub fn handle(&self) -> ServerRuntimeHandle {
        ServerRuntimeHandle {
            inner: Arc::clone(&self.inner),
        }
    }

    pub fn phase(&self) -> ShutdownPhase {
        ShutdownPhase::from_u8(self.inner.phase.load(Ordering::Acquire))
    }

    pub fn manager(&self) -> &Arc<RuntimeManager> {
        &self.inner.manager
    }

    /// Return one bounded process/runtime diagnostic projection. Generation
    /// metadata is read through the manager at call time; no generation graph
    /// is retained by the server state.
    pub fn diagnostics(&self) -> crate::runtime_lifecycle::RuntimeDiagnosticsSnapshot {
        self.inner.process.diagnostics(&self.inner.manager)
    }

    pub async fn serve_listener(
        &self,
        listener: TcpListener,
    ) -> Result<ShutdownReport, ServerError> {
        let app = build_router(AppState {
            server: self.server_state.clone(),
            database: self.inner.process.database(),
            runtime: Arc::clone(&self.inner.manager),
            process: Some(self.inner.process.clone()),
            body_tasks: self.inner.body_tasks.clone(),
        });
        let runtime_config =
            eggserve_runtime_config().map_err(|error| ServerError::EggServe(Box::new(error)))?;
        let server = eggserve_server::Server::builder()
            .runtime(runtime_config)
            .from_listener(listener)
            .build()
            .map_err(|error| ServerError::EggServe(Box::new(error)))?;
        let service = eggserve_core::server::TowerToEggserve::with_policy(
            app,
            eggserve_core::primitives::RequestBodyPolicy::Stream {
                max_bytes: EGG_SERVE_REQUEST_BODY_LIMIT,
            },
        );
        let eggserve_handle = server
            .start_with_service(service)
            .await
            .map_err(|error| ServerError::EggServe(Box::new(error)))?;
        let (control, mut completion) = eggserve_handle.into_parts();
        let handle = self.handle();
        let signal_handle = handle.clone();
        let signal_task = tokio::spawn(async move {
            if let Err(error) = shutdown_signal(signal_handle.clone()).await {
                signal_handle.record_signal_failure(error);
            }
        });

        let (server_result, shutdown_deadline) = tokio::select! {
            result = completion.wait() => {
                handle.request_shutdown(ShutdownReason::ServerCompleted);
                let deadline = tokio::time::Instant::now() + self.shutdown_timeout;
                let terminal = result.map_err(|error| ServerError::EggServe(Box::new(error)))
                    .and_then(|_| Err(ServerError::EggServe(Box::new(std::io::Error::other("HTTP runtime completed unexpectedly")))));
                (terminal, deadline)
            }
            _ = handle.wait_for_quiesce() => {
                let deadline = tokio::time::Instant::now() + self.shutdown_timeout;
                control.shutdown();
                (completion.wait().await.map_err(|error| ServerError::EggServe(Box::new(error))), deadline)
            }
        };
        signal_task.abort();
        let _ = signal_task.await;

        if self.phase() == ShutdownPhase::Running {
            handle.request_shutdown(ShutdownReason::ServerCompleted);
        }
        let control_error = if let Some(control_server) = &self.control_server {
            control_server.close().await.err()
        } else {
            None
        };
        let report =
            close_runtime_resources_until(Arc::clone(&self.inner), false, shutdown_deadline).await;
        if let Some(error) = control_error {
            return Err(ServerError::Control(error));
        }
        if let Some(error) = self
            .inner
            .signal_failure
            .lock()
            .expect("signal failure lock")
            .clone()
        {
            return Err(ServerError::Signal(error));
        }
        server_result?;
        if let Some(detail) = report.database_error.clone() {
            return Err(ServerError::ShutdownDatabase { detail });
        }
        if report.forced {
            return Err(ServerError::ForcedShutdown(report));
        }
        Ok(report)
    }
}

fn eggserve_runtime_config()
-> Result<eggserve_server::RuntimeConfig, eggserve_server::errors::ServerError> {
    // The long handler/body budgets intentionally avoid becoming EggPool's
    // provider timeout policy. Header, keep-alive, write-progress, and parser
    // ceilings remain explicit transport defenses. EggPool application body
    // admission continues to enforce its generation-owned live limit.
    eggserve_server::RuntimeConfig::builder()
        .bind("127.0.0.1:0".parse().expect("static socket address"))
        .max_connections(1024)
        .max_in_flight_requests(1024)
        .max_request_body_bytes(EGG_SERVE_REQUEST_BODY_LIMIT)
        .max_buf_size(256 * 1024)
        .max_headers(256)
        .max_header_bytes(128 * 1024)
        .max_request_target_bytes(16 * 1024)
        .disable_connection_total_timeout()
        .handler_timeout(Duration::from_secs(24 * 60 * 60))
        .body_read_timeout(Duration::from_secs(24 * 60 * 60))
        .header_read_timeout(Duration::from_secs(15))
        .keep_alive_idle_timeout(Duration::from_secs(120))
        .response_write_timeout(Duration::from_secs(120))
        .graceful_shutdown_timeout(Duration::from_secs(5))
        .build()
}

impl Drop for ServerRuntime {
    fn drop(&mut self) {
        if self.phase() >= ShutdownPhase::Stopped {
            return;
        }
        let handle = self.handle();
        handle.request_shutdown(ShutdownReason::Requested);
        let inner = Arc::clone(&self.inner);
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                let _ = close_runtime_resources(inner, false, GRACEFUL_SHUTDOWN_TIMEOUT).await;
            });
        }
    }
}

async fn close_runtime_resources(
    inner: Arc<ServerRuntimeInner>,
    initially_forced: bool,
    shutdown_timeout: Duration,
) -> ShutdownReport {
    close_runtime_resources_until(
        inner,
        initially_forced,
        tokio::time::Instant::now() + shutdown_timeout,
    )
    .await
}

async fn close_runtime_resources_until(
    inner: Arc<ServerRuntimeInner>,
    initially_forced: bool,
    deadline: tokio::time::Instant,
) -> ShutdownReport {
    let handle = ServerRuntimeHandle {
        inner: Arc::clone(&inner),
    };
    handle.request_shutdown(ShutdownReason::Requested);
    handle.set_phase(if initially_forced {
        ShutdownPhase::ForcedClosing
    } else {
        ShutdownPhase::Draining
    });
    inner.process.set_shutdown_diagnostics(
        if initially_forced {
            "forced_closing"
        } else {
            "draining"
        },
        initially_forced,
    );
    let task_supervisor = inner.process.task_supervisor();
    let task_started = task_supervisor.task_count();
    let task_report = task_supervisor
        .shutdown_with_timeout(deadline.saturating_duration_since(tokio::time::Instant::now()))
        .await;
    let _ = tokio::time::timeout(
        deadline.saturating_duration_since(tokio::time::Instant::now()),
        inner.process.flush_metrics(),
    )
    .await;
    let body_empty = if initially_forced {
        false
    } else {
        inner
            .body_tasks
            .wait_empty(deadline.saturating_duration_since(tokio::time::Instant::now()))
            .await
    };
    let mut forced = initially_forced || task_report.timed_out || !body_empty;
    handle.set_phase(if forced {
        ShutdownPhase::ForcedClosing
    } else {
        ShutdownPhase::Closing
    });
    inner
        .process
        .set_shutdown_diagnostics(if forced { "forced_closing" } else { "closing" }, forced);
    if forced {
        inner.body_tasks.abort_all();
    }
    let manager_report = inner
        .manager
        .close_for_shutdown(
            deadline.saturating_duration_since(tokio::time::Instant::now()),
            forced,
        )
        .await;
    forced |= manager_report.forced;
    let body_tasks_at_deadline = inner.body_tasks.active_count();
    let database_result = inner.process.database().close().await;
    let database_closed = database_result.is_ok();
    let database_error = database_result.err().map(|error| error.to_string());
    handle.set_phase(ShutdownPhase::Stopped);
    inner.process.set_shutdown_diagnostics("stopped", forced);
    ShutdownReport {
        phase: ShutdownPhase::Stopped,
        reason: *inner
            .reason
            .lock()
            .expect("shutdown reason lock")
            .get_or_insert(ShutdownReason::Requested),
        forced,
        active_leases_at_deadline: manager_report.active_leases_at_deadline,
        terminal_references_at_deadline: manager_report.terminal_references_at_deadline,
        body_tasks_at_deadline,
        task_count_at_start: task_started,
        task_count_joined: task_report.joined,
        database_closed,
        database_error,
    }
}

impl ServerRuntimeHandle {
    pub fn phase(&self) -> ShutdownPhase {
        ShutdownPhase::from_u8(self.inner.phase.load(Ordering::Acquire))
    }

    pub fn request_shutdown(&self, reason: ShutdownReason) -> bool {
        if self.inner.phase.load(Ordering::Acquire) >= ShutdownPhase::Quiescing as u8 {
            return false;
        }
        if self
            .inner
            .phase
            .compare_exchange(
                ShutdownPhase::Running as u8,
                ShutdownPhase::Quiescing as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return false;
        }
        *self.inner.reason.lock().expect("shutdown reason lock") = Some(reason);
        self.inner.manager.shutdown();
        self.inner.process.task_supervisor().begin_shutdown();
        self.inner
            .process
            .set_shutdown_diagnostics("quiescing", false);
        self.inner.notify.notify_waiters();
        true
    }

    fn set_phase(&self, phase: ShutdownPhase) {
        self.inner.phase.store(phase as u8, Ordering::Release);
        self.inner.notify.notify_waiters();
    }

    fn record_signal_failure(&self, error: SignalError) {
        *self
            .inner
            .signal_failure
            .lock()
            .expect("signal failure lock") = Some(error);
        self.request_shutdown(ShutdownReason::SignalFailure);
    }

    async fn wait_for_quiesce(&self) {
        loop {
            let notified = self.inner.notify.notified();
            if self.phase() >= ShutdownPhase::Quiescing {
                return;
            }
            notified.await;
        }
    }
}

#[derive(Clone)]
pub struct ServerState {
    api_key: Option<String>,
    dashboard_enabled: bool,
    dashboard_public: bool,
    dashboard_theme: String,
    dashboard_refresh_interval_s: u64,
    configured_server_threads: u32,
    database_path: String,
    started_at: Instant,
}

impl ServerState {
    fn from_config(config: &Config) -> Self {
        Self {
            api_key: config.resolved_server_api_key(),
            dashboard_enabled: config.dashboard.enabled,
            dashboard_public: config.dashboard.public,
            dashboard_theme: config.dashboard.theme.clone(),
            dashboard_refresh_interval_s: config.dashboard.refresh_interval_s,
            configured_server_threads: config.server.threads,
            database_path: Config::runtime_path(&config.database.path)
                .to_string_lossy()
                .into_owned(),
            started_at: Instant::now(),
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    pub server: ServerState,
    pub database: db::Database,
    pub runtime: Arc<RuntimeManager>,
    process: Option<ProcessRuntime>,
    body_tasks: BodyTaskTracker,
}

impl AppState {
    /// Construct router state around an already published manager. This is
    /// intended for embedded/test callers; production startup constructs the
    /// manager in `run_with_digest` before creating `ServerRuntime`.
    pub fn from_runtime(
        config: Config,
        database: db::Database,
        runtime: Arc<RuntimeManager>,
    ) -> Self {
        Self {
            server: ServerState::from_config(&config),
            database,
            runtime,
            process: None,
            body_tasks: BodyTaskTracker::new(),
        }
    }

    /// Build application state around a manager-owned generation. The direct
    /// graph arguments remain for existing test/integration callers; request
    /// handlers still acquire through the manager.
    pub fn from_inference(
        config: Config,
        database: db::Database,
        client_pool: ProviderClientPool,
        inference: Arc<InferenceState>,
    ) -> Self {
        let generation = RuntimeGeneration::from_inference(
            1,
            config.clone(),
            "runtime-config".to_owned(),
            inference,
            client_pool,
        );
        Self {
            server: ServerState::from_config(&config),
            database,
            runtime: Arc::new(RuntimeManager::new(generation)),
            process: None,
            body_tasks: BodyTaskTracker::new(),
        }
    }
}

/// A bodyless Axum route does not poll its request body. EggServe tracks the
/// canonical body lifecycle and closes a connection when an unread body is
/// abandoned, so explicitly finish only requests with transport framing that
/// proves no body exists. Non-empty/unknown bodies remain incremental and
/// untouched.
async fn finish_empty_transport_body(
    mut request: axum::http::Request<Body>,
    next: Next,
) -> Response {
    let body = std::mem::replace(request.body_mut(), Body::empty());
    let no_transfer_encoding = !request.headers().contains_key(header::TRANSFER_ENCODING);
    let no_declared_body = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .is_none_or(|length| length.as_bytes() == b"0");
    let exact_size = HttpBody::size_hint(&body).exact();
    let transport_has_no_body = no_transfer_encoding && no_declared_body && exact_size.is_none();
    if transport_has_no_body || exact_size == Some(0) {
        let mut body = body;
        while let Some(frame) = body.frame().await {
            if frame.is_err() {
                return StatusCode::BAD_REQUEST.into_response();
            }
        }
    } else {
        *request.body_mut() = body;
    }
    next.run(request).await
}

pub async fn run(config: Config) -> Result<(), ServerError> {
    run_with_digest(config, "runtime-config".to_owned(), None).await
}

/// Start the server through the R002 process/generation factory.  The normal
/// CLI supplies the file digest and path; the compatibility wrapper above is
/// used by direct Rust callers that already hold an in-memory config.
pub async fn run_with_digest(
    config: Config,
    content_digest: String,
    config_path: Option<std::path::PathBuf>,
) -> Result<(), ServerError> {
    validate_server_key(&config)?;
    ensure_start_state(&config).await?;
    if config.server.threads != 1 {
        tracing::warn!(
            configured_threads = config.server.threads,
            "server.threads is accepted for config compatibility; Tokio remains current-thread and the value does not select a worker pool"
        );
    }
    let address = format!("{}:{}", config.server.host, config.server.port);
    let listener = TcpListener::bind(&address).await?;

    let mut database_config = db::DatabaseConfig::from(&config.database);
    database_config.path = Config::runtime_path(&config.database.path)
        .to_string_lossy()
        .into_owned();
    let database = db::Database::open(database_config).await?;
    if let Err(error) = db::MigrationRunner::new(&database).run().await {
        let _ = database.close().await;
        return Err(error.into());
    }
    if let Err(error) = sync_accounts(&config, &database).await {
        let _ = database.close().await;
        return Err(error);
    }
    let process_result = match config_path {
        Some(path) => ProcessRuntime::with_config_path_and_config(database.clone(), path, &config),
        None => ProcessRuntime::new_with_config(database.clone(), &config),
    };
    let process = match process_result {
        Ok(process) => process,
        Err(error) => {
            let _ = database.close().await;
            return Err(error.into());
        }
    };
    if let Err(error) = process.reconcile_startup().await {
        let _ = database.close().await;
        return Err(error.into());
    }
    let prepared = match RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        content_digest,
        1,
    )
    .await
    {
        Ok(candidate) => candidate,
        Err(error) => {
            let _ = database.close().await;
            return Err(map_generation_error(error));
        }
    };
    if config.models.startup_refresh
        && let Some(generation) = prepared.generation()
        && let Some(catalog) = generation.inference().catalog_service()
        && let Err(error) = catalog.refresh().await
    {
        tracing::warn!(error = %error, "initial catalog refresh failed");
    }
    let generation = match prepared.transfer() {
        Ok(generation) => generation,
        Err(error) => {
            let _ = prepared.abort().await;
            let _ = database.close().await;
            return Err(ServerError::CandidateTransfer(error.to_string()));
        }
    };

    let manager = Arc::new(RuntimeManager::new(generation));
    if let Err(error) = process
        .install_initial_tasks((*manager).clone(), &config)
        .await
    {
        manager.shutdown();
        let _ = process.task_supervisor().shutdown().await;
        let _ = manager
            .close_for_shutdown(GRACEFUL_SHUTDOWN_TIMEOUT, true)
            .await;
        let _ = database.close().await;
        return Err(error.into());
    }
    let reload_service = process.reload_service((*manager).clone());
    let config_path_for_control = process.config_path().map(Path::to_path_buf);
    let control_path = crate::operations::paths::RuntimePaths::resolve().control_socket;
    let control_server = match crate::operations::control::start(control_path, move |request| {
        let reload_service = reload_service.clone();
        let config_path = config_path_for_control.clone();
        async move {
            let Some(config_path) = config_path else {
                return crate::operations::control::ControlResponse::error(
                    request.request_id,
                    "validation",
                    "config file path is unavailable",
                );
            };
            let result = reload_service
                .reload_path(config_path, request.validated_digest)
                .await;
            crate::operations::control::ControlResponse::from_reload(request.request_id, result)
        }
    })
    .await
    {
        Ok(control_server) => control_server,
        Err(error) => {
            manager.shutdown();
            let _ = process.task_supervisor().shutdown().await;
            let _ = manager
                .close_for_shutdown(GRACEFUL_SHUTDOWN_TIMEOUT, true)
                .await;
            let _ = database.close().await;
            return Err(ServerError::Control(error));
        }
    };
    let pid_path = RuntimePaths::resolve().pid_file;
    if let Err(error) = process::write_pid_atomic(&pid_path, std::process::id() as i32) {
        let _ = control_server.close().await;
        manager.shutdown();
        let _ = process.task_supervisor().shutdown().await;
        let _ = manager
            .close_for_shutdown(GRACEFUL_SHUTDOWN_TIMEOUT, true)
            .await;
        let _ = database.close().await;
        return Err(error.into());
    }
    tracing::info!(address, "Rust development server listening");
    let runtime = ServerRuntime::new(process, manager, config).with_control_server(control_server);
    let result = runtime.serve_listener(listener).await.map(|_| ());
    let clear_result = process::clear_pid_if_matches(&pid_path, std::process::id() as i32);
    match (result, clear_result) {
        (Err(error), _) => Err(error),
        (Ok(()), Err(error)) => Err(ServerError::Process(error)),
        (Ok(()), Ok(_)) => Ok(()),
    }
}

async fn ensure_start_state(config: &Config) -> Result<(), ServerError> {
    let paths = RuntimePaths::prepare().map_err(process::ProcessError::Path)?;
    if let Some(pid) = process::read_pid(&paths.pid_file)? {
        if process::process_exists(pid) {
            return Err(ServerError::StartupConflict {
                detail: format!("server is already running (PID {pid})"),
            });
        }
        process::clear_stale_pid(&paths.pid_file, Some(pid))?;
    }
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        return Err(ServerError::StartupConflict {
            detail: format!(
                "another process is already serving {}:{}",
                config.server.host, config.server.port
            ),
        });
    }
    if process::probe_control(&paths.control_socket).await == process::ControlProbe::Reachable {
        return Err(ServerError::StartupConflict {
            detail: "the local control socket is already owned".to_owned(),
        });
    }
    Ok(())
}

/// Serve a prepared database on a caller-owned listener.
pub async fn serve_listener(
    config: Config,
    database: db::Database,
    listener: TcpListener,
) -> Result<(), ServerError> {
    let process = match ProcessRuntime::new_with_config(database.clone(), &config) {
        Ok(process) => process,
        Err(error) => {
            let _ = database.close().await;
            return Err(error.into());
        }
    };
    if let Err(error) = db::MigrationRunner::new(&database).run().await {
        let _ = database.close().await;
        return Err(error.into());
    }
    if let Err(error) = sync_accounts(&config, &database).await {
        let _ = database.close().await;
        return Err(error);
    }
    if let Err(error) = process.reconcile_startup().await {
        let _ = database.close().await;
        return Err(error.into());
    }
    let prepared = match RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "runtime-config".to_owned(),
        1,
    )
    .await
    {
        Ok(prepared) => prepared,
        Err(error) => {
            let _ = database.close().await;
            return Err(map_generation_error(error));
        }
    };
    let generation = match prepared.transfer() {
        Ok(generation) => generation,
        Err(error) => {
            let _ = prepared.abort().await;
            let _ = database.close().await;
            return Err(ServerError::CandidateTransfer(error.to_string()));
        }
    };
    let manager = Arc::new(RuntimeManager::new(generation));
    if let Err(error) = process
        .install_initial_tasks((*manager).clone(), &config)
        .await
    {
        manager.shutdown();
        let _ = process.task_supervisor().shutdown().await;
        let _ = manager
            .close_for_shutdown(GRACEFUL_SHUTDOWN_TIMEOUT, true)
            .await;
        let _ = database.close().await;
        return Err(ServerError::TaskSetup(error));
    }
    let runtime = ServerRuntime::new(process, manager, config);
    runtime.serve_listener(listener).await.map(|_| ())
}

/// Serve with an explicitly built inference state (test and serve paths).
pub async fn serve_listener_with_inference(
    config: Config,
    database: db::Database,
    client_pool: ProviderClientPool,
    inference: Arc<InferenceState>,
    listener: TcpListener,
) -> Result<(), ServerError> {
    let process = match ProcessRuntime::new_with_config(database.clone(), &config) {
        Ok(process) => process,
        Err(error) => {
            let _ = database.close().await;
            return Err(error.into());
        }
    };
    let manager = Arc::new(RuntimeManager::new(RuntimeGeneration::from_inference(
        1,
        config.clone(),
        "runtime-config".to_owned(),
        inference,
        client_pool,
    )));
    if let Err(error) = process
        .install_initial_tasks((*manager).clone(), &config)
        .await
    {
        manager.shutdown();
        let _ = process.task_supervisor().shutdown().await;
        let _ = manager
            .close_for_shutdown(GRACEFUL_SHUTDOWN_TIMEOUT, true)
            .await;
        let _ = database.close().await;
        return Err(ServerError::TaskSetup(error));
    }
    let runtime = ServerRuntime::new(process, manager, config);
    runtime.serve_listener(listener).await.map(|_| ())
}

/// Build the testable Axum application for an already-open database.
pub fn build_router(state: AppState) -> Router {
    let dashboard = state.server.dashboard_enabled;
    let mut router = Router::new()
        .route("/v1/healthz", get(healthz))
        .route("/v1/readyz", get(readyz))
        .route("/v1/models", get(models_api))
        .route("/api/integrations/v1/profile", get(integration_profile))
        .route("/api/stats/runtime", get(runtime_status))
        .route("/api/stats/update", get(update_status))
        .route("/api/status", get(status_api))
        .route("/v1/chat/completions", post(chat_completions))
        .route("/v1/messages", post(messages))
        .route("/v1/responses", post(responses))
        .route("/v1/responses/compact", post(responses_compact))
        .route("/static/dashboard.css", get(static_css))
        .route("/static/dashboard.js", get(static_js))
        .route("/static/chart.js", get(static_chart_js))
        .route("/static/favicon.svg", get(static_favicon))
        .route("/static/theme.css", get(theme_css));

    if dashboard {
        router = router
            .route("/", get(overview))
            .route("/accounts", get(accounts_page))
            .route("/models", get(models_page))
            .route("/models/{*model_id}", get(model_detail_page))
            .route("/latency", get(latency_page))
            .route("/events", get(events_page))
            .route("/timeseries", get(timeseries_page))
            .route("/bandwidth", get(bandwidth_page))
            .route("/pings", get(pings_page))
            .route("/reliability", get(reliability_page))
            .route("/routing", get(routing_page))
            .route("/traces", get(traces_page))
            .route("/runtime", get(runtime_page))
            .route("/cache", get(cache_page))
            .route("/api/stats/summary", get(summary));
    }

    router
        .layer(from_fn_with_state(state.clone(), admit_inference_body))
        .layer(from_fn_with_state(state.clone(), authenticate))
        .layer(axum::middleware::from_fn(finish_empty_transport_body))
        .with_state(state)
}

async fn shutdown_signal(handle: ServerRuntimeHandle) -> Result<(), SignalError> {
    let ctrl_c = tokio::signal::ctrl_c();
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .map_err(|error| SignalError::Sigterm(error.to_string()))?;
        tokio::select! {
            result = ctrl_c => {
                result.map_err(|error| SignalError::CtrlC(error.to_string()))?;
                handle.request_shutdown(ShutdownReason::CtrlC);
            },
            _ = terminate.recv() => {
                handle.request_shutdown(ShutdownReason::Sigterm);
            },
        }
    }
    #[cfg(not(unix))]
    {
        ctrl_c
            .await
            .map_err(|error| SignalError::CtrlC(error.to_string()))?;
        handle.request_shutdown(ShutdownReason::CtrlC);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{middleware::is_inference_path, middleware::requires_auth, *};

    fn state_with_dashboard(public: bool) -> ServerState {
        ServerState {
            api_key: Some("test-server-key-12345678".to_owned()),
            dashboard_enabled: true,
            dashboard_public: public,
            dashboard_theme: "default".to_owned(),
            dashboard_refresh_interval_s: 60,
            configured_server_threads: 1,
            database_path: ":memory:".to_owned(),
            started_at: Instant::now(),
        }
    }

    #[test]
    fn public_dashboard_exempts_pages_and_data_but_not_sensitive_routes() {
        let public = state_with_dashboard(true);
        for path in [
            "/",
            "/accounts",
            "/models",
            "/latency",
            "/runtime",
            "/api/stats/summary",
            "/api/stats/accounts",
            "/static/dashboard.css",
            "/v1/healthz",
            "/v1/readyz",
        ] {
            assert!(!requires_auth(path, &public), "{path} is public");
        }
        for path in [
            "/v1/models",
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/responses",
            "/v1/responses/compact",
            "/api/integrations/v1/profile",
            "/api/stats/runtime",
            "/api/stats/update",
            "/api/status",
        ] {
            assert!(requires_auth(path, &public), "{path} stays authenticated");
        }
    }

    #[test]
    fn private_dashboard_restores_auth_on_pages_and_data() {
        let private = state_with_dashboard(false);
        for path in [
            "/",
            "/accounts",
            "/models",
            "/api/stats/summary",
            "/api/stats/accounts",
        ] {
            assert!(requires_auth(path, &private), "{path} requires a key");
        }
        for path in ["/static/dashboard.css", "/v1/healthz", "/v1/readyz"] {
            assert!(!requires_auth(path, &private), "{path} stays exempt");
        }
        for path in [
            "/v1/models",
            "/api/integrations/v1/profile",
            "/api/stats/runtime",
            "/api/stats/update",
            "/api/status",
        ] {
            assert!(requires_auth(path, &private), "{path} stays authenticated");
        }
    }

    #[test]
    fn inference_path_classification_covers_all_public_inference_routes() {
        for path in [
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/responses",
            "/v1/responses/compact",
        ] {
            assert!(
                is_inference_path(path),
                "{path} requires generation admission"
            );
        }
        for path in [
            "/v1/models",
            "/v1/healthz",
            "/v1/readyz",
            "/api/status",
            "/api/stats/runtime",
            "/api/integrations/v1/profile",
        ] {
            assert!(
                !is_inference_path(path),
                "{path} must not take body admission"
            );
        }
    }
}
