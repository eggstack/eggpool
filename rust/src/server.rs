//! Axum server for the side-by-side migration candidate.
//!
//! Health/readiness, dashboard reads, authentication, static resources, and
//! the C009 public inference endpoints (Chat Completions, Responses,
//! Messages) through the thin M7 coordinator boundary. Handlers invoke one
//! coordinator entry point; routing/retry/finalization live in the
//! coordinator, not here.

use axum::{
    Router,
    body::Bytes,
    extract::{Query, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    middleware::{Next, from_fn_with_state},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use std::time::Duration;
use thiserror::Error;
use tokio::net::TcpListener;
use tokio::sync::{Notify, oneshot};
use tower_http::limit::RequestBodyLimitLayer;

use crate::{
    Config,
    coordinator::{InferenceState, endpoint_error_body},
    db,
    providers::ProviderClientPool,
    runtime_lifecycle::{
        GenerationAcquireError, GenerationBuildError, GenerationLease, ProcessRuntime,
        RuntimeGeneration, RuntimeGenerationFactory, RuntimeManager, StartupRecoveryError,
        TaskSpecError,
    },
    wire::ir::ClientSurface,
};
use std::sync::Arc;

const DEFAULT_THEME: &str = "Cyber Red";
const MAX_THEME_NAME_BYTES: usize = 128;

const DASHBOARD_CSS: &[u8] = include_bytes!("../assets/dashboard/static/dashboard.css");
const DASHBOARD_JS: &[u8] = include_bytes!("../assets/dashboard/static/dashboard.js");
const CHART_JS: &[u8] = include_bytes!("../assets/dashboard/static/chart.umd.min.js");
const FAVICON_SVG: &[u8] = include_bytes!("../assets/dashboard/static/favicon.svg");

const THEME_NAMES: &[&str] = &[
    "default",
    "Booberry",
    "Catppuccin Latte",
    "Catppuccin Macchiato",
    "Catppuccin Mocha",
    "Cyber Red",
    "Cyberpunk",
    "Dark Green",
    "Discord (80_ Saturation)",
    "Discord",
    "Dracula",
    "Ferra Light",
    "Flexor Dark",
    "Gruvbox",
    "Halcyon Dark",
    "IntelliJ Light",
    "Kanagawa",
    "Macaw Dark",
    "Macaw Light",
    "Matrix",
    "Noctis Lilac",
    "Nord",
    "Nostromo Terminal",
    "One Dark",
    "Oxocarbon",
    "Rose Pine Dawn",
    "Rose Pine Moon",
    "Rose Pine",
    "Solarized Dark",
    "Sonokai",
    "Tokyo Night Storm",
    "VESPER",
    "Zenburn",
    "acton",
    "bam",
    "base16-atelier-forest-light",
    "berlin",
    "black but with important highlights",
    "broc",
    "cork",
    "ferra",
    "forest",
    "lisbon",
    "midnight",
    "oslo",
    "plum",
    "portland",
    "sunset",
    "tofino",
    "vanimo",
    "vik",
];

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
    #[error("server signal handler failed: {0}")]
    Signal(#[from] SignalError),
    #[error("server forced shutdown exceeded its graceful deadline")]
    ForcedShutdown(ShutdownReport),
    #[error("database close failed during server shutdown: {detail}")]
    ShutdownDatabase { detail: String },
    #[error("inference state construction failed: {0}")]
    Inference(String),
}

const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);

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
    config: Config,
    shutdown_timeout: Duration,
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
            config,
            shutdown_timeout: GRACEFUL_SHUTDOWN_TIMEOUT,
        }
    }

    /// Use a shorter bounded window for deterministic embedding/tests.
    pub fn with_shutdown_timeout(mut self, timeout: Duration) -> Self {
        self.shutdown_timeout = timeout.max(Duration::from_millis(1));
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

    pub async fn serve_listener(
        &self,
        listener: TcpListener,
    ) -> Result<ShutdownReport, ServerError> {
        let app = build_router(AppState {
            config: self.config.clone(),
            database: self.inner.process.database(),
            runtime: Arc::clone(&self.inner.manager),
            body_tasks: self.inner.body_tasks.clone(),
        });
        let handle = self.handle();
        let shutdown = handle.clone();
        let server = tokio::spawn(
            axum::serve(listener, app)
                .with_graceful_shutdown(async move { shutdown.wait_for_quiesce().await })
                .into_future(),
        );
        let signal_handle = handle.clone();
        let signal_task = tokio::spawn(async move {
            if let Err(error) = shutdown_signal(signal_handle.clone()).await {
                signal_handle.record_signal_failure(error);
            }
        });

        let mut server = server;
        let mut forced = false;
        let server_result = tokio::select! {
            result = &mut server => result.map_err(|_| ServerError::Bind(std::io::Error::other("server task failed")))?.map_err(ServerError::Bind),
            _ = handle.wait_for_quiesce() => {
                match tokio::time::timeout(self.shutdown_timeout, &mut server).await {
                    Ok(result) => result.map_err(|_| ServerError::Bind(std::io::Error::other("server task failed")))?.map_err(ServerError::Bind),
                    Err(_) => {
                        forced = true;
                        handle.begin_forced_close();
                        server.abort();
                        let _ = server.await;
                        Ok(())
                    }
                }
            }
        };
        signal_task.abort();
        let _ = signal_task.await;

        if self.phase() == ShutdownPhase::Running {
            handle.request_shutdown(ShutdownReason::ServerCompleted);
        }
        let report = self.close_resources(forced).await;
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

    async fn close_resources(&self, initially_forced: bool) -> ShutdownReport {
        close_runtime_resources(
            Arc::clone(&self.inner),
            initially_forced,
            self.shutdown_timeout,
        )
        .await
    }
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
    let handle = ServerRuntimeHandle {
        inner: Arc::clone(&inner),
    };
    let deadline = tokio::time::Instant::now() + shutdown_timeout;
    handle.request_shutdown(ShutdownReason::Requested);
    handle.set_phase(if initially_forced {
        ShutdownPhase::ForcedClosing
    } else {
        ShutdownPhase::Draining
    });
    let task_supervisor = inner.process.task_supervisor();
    let task_started = task_supervisor.task_count();
    let task_report = task_supervisor
        .shutdown_with_timeout(deadline.saturating_duration_since(tokio::time::Instant::now()))
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
        self.inner.notify.notify_waiters();
        true
    }

    fn set_phase(&self, phase: ShutdownPhase) {
        self.inner.phase.store(phase as u8, Ordering::Release);
        self.inner.notify.notify_waiters();
    }

    fn begin_forced_close(&self) {
        self.set_phase(ShutdownPhase::ForcedClosing);
        self.inner.body_tasks.abort_all();
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
pub struct AppState {
    pub config: Config,
    pub database: db::Database,
    pub runtime: Arc<RuntimeManager>,
    body_tasks: BodyTaskTracker,
}

impl AppState {
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
            config,
            database,
            runtime: Arc::new(RuntimeManager::new(generation)),
            body_tasks: BodyTaskTracker::new(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct PeriodQuery {
    period: Option<String>,
    theme: Option<String>,
}

/// Start the development server using the configured address and database.
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
    if config.server.threads != 1 {
        tracing::warn!(
            configured_threads = config.server.threads,
            "server.threads is accepted for config compatibility; the Rust candidate remains single-threaded until the runtime milestone"
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
    let process = match config_path {
        Some(path) => ProcessRuntime::with_config_path(database.clone(), path),
        None => ProcessRuntime::new(database.clone()),
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
    tracing::info!(address, "Rust development server listening");
    let runtime = ServerRuntime::new(process, manager, config);
    runtime.serve_listener(listener).await.map(|_| ())
}

/// Serve a prepared database on a caller-owned listener.
pub async fn serve_listener(
    config: Config,
    database: db::Database,
    listener: TcpListener,
) -> Result<(), ServerError> {
    let process = ProcessRuntime::new(database.clone());
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
    let process = ProcessRuntime::new(database.clone());
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
    let dashboard = state.config.dashboard.enabled;
    let mut router = Router::new()
        .route("/v1/healthz", get(healthz))
        .route("/v1/readyz", get(readyz))
        .route("/v1/chat/completions", post(chat_completions))
        .route("/v1/messages", post(messages))
        .route("/v1/responses", post(responses))
        .route("/static/dashboard.css", get(static_css))
        .route("/static/dashboard.js", get(static_js))
        .route("/static/chart.js", get(static_chart_js))
        .route("/static/favicon.svg", get(static_favicon))
        .route("/static/theme.css", get(theme_css));

    if dashboard {
        router = router
            .route("/", get(overview))
            .route("/api/stats/summary", get(summary));
    }

    router
        .layer(RequestBodyLimitLayer::new(
            state.config.server.max_request_body_bytes as usize,
        ))
        .layer(from_fn_with_state(state.clone(), authenticate))
        .with_state(state)
}

async fn authenticate(
    State(state): State<AppState>,
    request: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    if !requires_auth(request.uri().path(), &state.config) {
        return next.run(request).await;
    }
    let expected = state.config.resolved_server_api_key();
    if expected
        .as_deref()
        .is_none_or(|key| verify_api_key(request.headers(), key))
    {
        return next.run(request).await;
    }
    (
        StatusCode::UNAUTHORIZED,
        axum::Json(json!({
            "detail": "Invalid or missing API key"
        })),
    )
        .into_response()
}

fn requires_auth(path: &str, config: &Config) -> bool {
    if path == "/v1/healthz" || path == "/v1/readyz" || path.starts_with("/static/") {
        return false;
    }
    if path.starts_with("/v1/") {
        return true;
    }
    config.dashboard.enabled
        && !config.dashboard.public
        && (path == "/" || path.starts_with("/api/"))
}

fn validate_server_key(config: &Config) -> Result<(), ServerError> {
    let key = config.resolved_server_api_key();
    if !is_loopback_host(&config.server.host) && key.is_none() {
        return Err(ServerError::InvalidApiKey);
    }
    if key.as_deref().is_some_and(|value| !valid_key_shape(value)) {
        return Err(ServerError::InvalidApiKey);
    }
    Ok(())
}

fn valid_key_shape(value: &str) -> bool {
    (8..=512).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

fn map_generation_error(error: GenerationBuildError) -> ServerError {
    match error {
        GenerationBuildError::ProviderPool(error) => ServerError::ProviderPool(error),
        error => ServerError::Generation(error),
    }
}

fn verify_api_key(headers: &HeaderMap, expected: &str) -> bool {
    let authorization = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim();
    let provided = authorization
        .strip_prefix("Bearer ")
        .or_else(|| authorization.strip_prefix("bearer "))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            headers
                .get("x-api-key")
                .and_then(|value| value.to_str().ok())
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or("");
    let left = fixed_key(provided);
    let right = fixed_key(expected);
    let different = left
        .iter()
        .zip(right.iter())
        .fold(0u8, |accumulator, (a, b)| accumulator | (a ^ b));
    valid_key_shape(provided) && valid_key_shape(expected) && different == 0
}

fn fixed_key(value: &str) -> [u8; 512] {
    let mut result = [0u8; 512];
    let bytes = value.as_bytes();
    if bytes.len() > result.len() {
        return [0xff; 512];
    }
    result[..bytes.len()].copy_from_slice(bytes);
    result
}

fn is_loopback_host(host: &str) -> bool {
    let normalized = host.trim().trim_matches(['[', ']']);
    normalized == "localhost"
        || normalized == "127.0.0.1"
        || normalized == "::1"
        || normalized == "0:0:0:0:0:0:0:1"
}

async fn healthz() -> Response {
    json_response(StatusCode::OK, json!({"status": "ok"}))
}

async fn readyz(State(state): State<AppState>) -> Response {
    let accounts = match db::AccountRepository::new(&state.database)
        .list_enabled()
        .await
    {
        Ok(accounts) => accounts,
        Err(_) => return degraded("database not writable"),
    };
    if state.config.all_accounts().is_empty() {
        return degraded("no accounts configured");
    }
    if accounts.is_empty() {
        return degraded("no enabled accounts");
    }
    if !has_loaded_credentials(&state.config) {
        return degraded("no loaded credentials");
    }
    match db::ModelRepository::new(&state.database).list(None).await {
        Ok(models) if !models.is_empty() => json_response(StatusCode::OK, json!({"status": "ok"})),
        Ok(_) => degraded("no usable model catalog"),
        Err(_) => degraded("database not writable"),
    }
}

fn has_loaded_credentials(config: &Config) -> bool {
    config.providers.values().any(|provider| {
        let provider_is_anonymous = provider.auth.mode == "none"
            && provider
                .wire_surfaces
                .values()
                .all(|surface| surface.auth.as_ref().is_none_or(|auth| auth.mode == "none"));
        provider.accounts.iter().any(|account| {
            account.enabled
                && (provider_is_anonymous
                    || account.api_key.as_ref().is_some_and(|key| !key.is_empty())
                    || std::env::var(&account.api_key_env).is_ok_and(|key| !key.is_empty()))
        })
    })
}

async fn overview(State(state): State<AppState>, Query(query): Query<PeriodQuery>) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let repository = db::UsageRollupRepository::new(&state.database);
    let summary = match repository.dashboard_summary_basic(period).await {
        Ok(summary) => summary,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let accounts = match db::AccountRepository::new(&state.database)
        .list_enabled()
        .await
    {
        Ok(accounts) => accounts,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let theme_name = selected_theme(
        query
            .theme
            .as_deref()
            .unwrap_or(&state.config.dashboard.theme),
    );
    let html = render_overview(
        &summary,
        &accounts,
        period,
        theme_name,
        state.config.dashboard.refresh_interval_s,
    );
    html_response(html)
}

async fn summary(State(state): State<AppState>, Query(query): Query<PeriodQuery>) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let summary = match db::UsageRollupRepository::new(&state.database)
        .dashboard_summary_basic(period)
        .await
    {
        Ok(summary) => summary,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    json_response(StatusCode::OK, summary_json(&summary, period))
}

fn normalize_period(value: Option<&str>) -> Result<&'static str, Box<Response>> {
    match value.unwrap_or("24h") {
        "1h" => Ok("1h"),
        "24h" => Ok("24h"),
        "7d" => Ok("7d"),
        "30d" => Ok("30d"),
        _ => Err(Box::new(json_response(
            StatusCode::BAD_REQUEST,
            json!({"detail": "Invalid period"}),
        ))),
    }
}

/// Thin Axum handlers: admission/auth/body limits are existing boundaries;
/// each handler invokes exactly one coordinator entry point and translates
/// its typed result to the established client surface. No routing, retry,
/// or finalization loops live here.
async fn chat_completions(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    handle_inference(state, ClientSurface::ChatCompletions, headers, body).await
}

async fn messages(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response {
    handle_inference(state, ClientSurface::Messages, headers, body).await
}

async fn responses(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response {
    handle_inference(state, ClientSurface::Responses, headers, body).await
}

async fn handle_inference(
    state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // Peek the stream flag without consuming the body: finite and streaming
    // coordinators own their full lifecycle and must not be mixed.
    let is_stream = serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| value.get("stream").cloned())
        .is_some_and(|value| value == serde_json::Value::Bool(true));
    // A present-but-non-boolean stream flag is a 400 (Python parity).
    let stream_shape_valid = serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| value.get("stream").cloned())
        .is_none_or(|value| value.is_null() || value.is_boolean());
    if !stream_shape_valid {
        let detail = endpoint_error_body(surface, "Invalid stream value: must be a boolean");
        return error_body_response(StatusCode::BAD_REQUEST, surface, detail);
    }
    let session = headers
        .get("x-eggpool-route-session")
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let proxy_request_id = crate::coordinator::new_proxy_request_id();
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(error) => return runtime_acquire_error(surface, error),
    };
    if is_stream {
        handle_stream_inference(
            state,
            surface,
            headers,
            body,
            session,
            proxy_request_id,
            lease,
        )
        .await
    } else {
        handle_finite_inference(
            state,
            surface,
            headers,
            body,
            session,
            proxy_request_id,
            lease,
        )
        .await
    }
}

async fn handle_finite_inference(
    _state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
    session: Option<String>,
    proxy_request_id: String,
    lease: GenerationLease,
) -> Response {
    let incoming = filtered_incoming_headers(&headers);
    match crate::coordinator::execute_finite(
        lease.generation().inference(),
        surface,
        body,
        incoming,
        session,
        proxy_request_id.clone(),
    )
    .await
    {
        Ok((execution, _virtual)) => {
            // Mark response start immediately before sending response start,
            // then converge retained C006 ownership after the finite body
            // write. The Axum body write happens after this return; a handler
            // task cancellation before return drops the execution and
            // converges as interrupted without replay.
            execution.mark_started();
            let status = execution.response.status;
            let mut outgoing = HeaderMap::new();
            for (name, value) in &execution.response.headers {
                outgoing.insert(name.clone(), value.clone());
            }
            let body = execution.response.body.clone();
            match execution
                .complete(crate::coordinator::DownstreamResult::Delivered)
                .await
            {
                Ok(_) => (status, outgoing, body).into_response(),
                Err(_) => {
                    let detail = endpoint_error_body(surface, "Finalization failed");
                    error_body_response(StatusCode::SERVICE_UNAVAILABLE, surface, detail)
                }
            }
        }
        Err(error) => {
            let status = error.status();
            let detail = endpoint_error_body(surface, &error.to_string());
            error_body_response(status, surface, detail)
        }
    }
}

async fn handle_stream_inference(
    state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
    session: Option<String>,
    proxy_request_id: String,
    lease: GenerationLease,
) -> Response {
    let incoming = filtered_incoming_headers(&headers);
    let execution = match crate::coordinator::execute_stream(
        lease.generation().inference(),
        surface,
        body,
        incoming,
        session,
        proxy_request_id.clone(),
    )
    .await
    {
        Ok((execution, _virtual)) => execution,
        Err(error) => {
            let status = error.status();
            let detail = endpoint_error_body(surface, &error.to_string());
            return error_body_response(status, surface, detail);
        }
    };
    // Pre-handoff terminal error envelope (e.g. exhaustion without a live
    // stream): finite JSON body with the coordinator's status/headers.
    if let Some(error_body) = execution.error_body.clone() {
        let status = execution.headers.status;
        let mut outgoing = HeaderMap::new();
        for (name, value) in &execution.headers.headers {
            outgoing.insert(name.clone(), value.clone());
        }
        execution.mark_started();
        let _ = execution
            .complete(crate::coordinator::DownstreamResult::Delivered)
            .await;
        return (status, outgoing, error_body).into_response();
    }
    let status = execution.headers.status;
    let mut outgoing = HeaderMap::new();
    for (name, value) in &execution.headers.headers {
        outgoing.insert(name.clone(), value.clone());
    }
    // Drive the incremental body without buffering the complete stream:
    // each pulled chunk is forwarded as one Axum frame. Terminal ownership
    // is stored on clean or failed terminal; failures never retry.
    let (sender, receiver) = tokio::sync::mpsc::channel::<Result<Bytes, std::io::Error>>(32);
    state.body_tasks.spawn(async move {
        let _lease = lease;
        let mut execution = execution;
        execution.mark_started();
        loop {
            match execution.next_chunk().await {
                Some(Ok(chunk)) => {
                    if sender.send(Ok(chunk)).await.is_err() {
                        // Downstream disconnected: drop converges as
                        // cancelled without replay.
                        drop(execution);
                        break;
                    }
                }
                None => {
                    let _ = execution
                        .complete(crate::coordinator::DownstreamResult::Delivered)
                        .await;
                    break;
                }
                Some(Err(_)) => {
                    // Failed terminal after handoff: end the stream without
                    // replay; durable status already converges as error.
                    let _ = execution
                        .complete(crate::coordinator::DownstreamResult::Delivered)
                        .await;
                    break;
                }
            }
        }
    });
    let stream = tokio_stream::wrappers::ReceiverStream::new(receiver);
    let body = axum::body::Body::from_stream(stream);
    (status, outgoing, body).into_response()
}

fn runtime_acquire_error(surface: ClientSurface, error: GenerationAcquireError) -> Response {
    let status = StatusCode::SERVICE_UNAVAILABLE;
    error_body_response(
        status,
        surface,
        endpoint_error_body(surface, &error.to_string()),
    )
}

fn error_body_response(status: StatusCode, surface: ClientSurface, detail: Vec<u8>) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    let _ = surface;
    (status, headers, Bytes::from(detail)).into_response()
}

/// Incoming headers forwarded toward provider selection, minus credentials
/// and hop-by-hop framing. The session header is hashed, never forwarded.
fn filtered_incoming_headers(headers: &HeaderMap) -> HeaderMap {
    let mut outgoing = HeaderMap::new();
    for (name, value) in headers {
        let lower = name.as_str().to_ascii_lowercase();
        if matches!(
            lower.as_str(),
            "authorization"
                | "proxy-authorization"
                | "x-api-key"
                | "host"
                | "content-length"
                | "x-eggpool-route-session"
                | "connection"
                | "transfer-encoding"
                | "upgrade"
                | "keep-alive"
        ) {
            continue;
        }
        outgoing.insert(name.clone(), value.clone());
    }
    outgoing
}

async fn static_css() -> Response {
    static_response(DASHBOARD_CSS, "text/css", "public, max-age=300")
}

async fn static_js() -> Response {
    static_response(DASHBOARD_JS, "text/javascript", "public, max-age=300")
}

async fn static_chart_js() -> Response {
    static_response(CHART_JS, "text/javascript", "public, max-age=300")
}

async fn static_favicon() -> Response {
    static_response(FAVICON_SVG, "image/svg+xml", "public, max-age=86400")
}

async fn theme_css(Query(query): Query<ThemeQuery>) -> Response {
    let requested = query.theme.unwrap_or_else(|| "default".to_owned());
    if requested == "default" || !THEME_NAMES.contains(&requested.as_str()) {
        return static_response(b"", "text/css", "public, max-age=300");
    }
    let css = theme_variables(&requested);
    static_response(css.as_bytes(), "text/css", "public, max-age=300")
}

#[derive(Debug, Deserialize)]
struct ThemeQuery {
    theme: Option<String>,
}

fn static_response(body: &[u8], content_type: &str, cache_control: &str) -> Response {
    (
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, cache_control),
        ],
        body.to_vec(),
    )
        .into_response()
}

fn json_response(status: StatusCode, value: Value) -> Response {
    (status, axum::Json(value)).into_response()
}

fn degraded(reason: &str) -> Response {
    json_response(
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"status": "degraded", "reason": reason}),
    )
}

fn html_response(body: String) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        body,
    )
        .into_response()
}

async fn sync_accounts(config: &Config, database: &db::Database) -> Result<(), ServerError> {
    let accounts = config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider
                .accounts
                .iter()
                .map(move |account| db::AccountConfig {
                    name: account.name.clone(),
                    api_key_env: account.api_key_env.clone(),
                    enabled: account.enabled,
                    weight: account.weight,
                    provider_id: provider_id.clone(),
                })
        })
        .collect();
    db::AccountRepository::new(database)
        .sync_from_config(accounts)
        .await
        .map(|_| ())
        .map_err(ServerError::Database)
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

fn selected_theme(configured: &str) -> &str {
    if configured.len() <= MAX_THEME_NAME_BYTES && THEME_NAMES.contains(&configured) {
        configured
    } else {
        DEFAULT_THEME
    }
}

fn html_escape(value: impl std::fmt::Display) -> String {
    value
        .to_string()
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn render_overview(
    summary: &db::DashboardSummary,
    accounts: &[db::Account],
    period: &str,
    theme: &str,
    refresh_interval_s: u64,
) -> String {
    let total = summary.total_requests;
    let success = summary.successful_requests;
    let errors = summary.error_requests;
    let error_rate = if total == 0 {
        0.0
    } else {
        errors as f64 / total as f64 * 100.0
    };
    let fresh_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        fresh_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let nav = THEME_NAMES
        .iter()
        .map(|name| {
            let selected = if *name == theme { " selected" } else { "" };
            format!(
                "<option value=\"{}\"{}>{}</option>",
                html_escape(name),
                selected,
                html_escape(name)
            )
        })
        .collect::<String>();
    let period_options = [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    .iter()
    .map(|(value, label)| {
        let selected = if *value == period {
            " selected=\"selected\""
        } else {
            ""
        };
        format!("<option value=\"{}\"{}>{}</option>", value, selected, label)
    })
    .collect::<String>();
    let account_rows = if accounts.is_empty() {
        "<p class=\"empty-state\">No accounts configured.</p>".to_owned()
    } else {
        accounts
            .iter()
            .map(|account| {
                format!(
                    "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
                    html_escape(&account.name),
                    html_escape(&account.provider_id),
                    if account.enabled { "yes" } else { "no" }
                )
            })
            .collect::<String>()
    };
    let html = format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Overview</title>\n<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\">\n<link rel=\"preload\" href=\"/static/dashboard.css\" as=\"style\">\n<link rel=\"stylesheet\" href=\"/static/dashboard.css\">\n<link rel=\"stylesheet\" href=\"/static/theme.css?theme={}\">\n</head>\n<body>\n<svg class=\"egg-background\" viewBox=\"0 0 256 256\" preserveAspectRatio=\"xMidYMid meet\" aria-hidden=\"true\" focusable=\"false\"><path class=\"shape\" d=\"M128 30 C82 30 55 88 57 145 C59 202 89 231 128 231 C167 231 197 202 199 145 C201 88 174 30 128 30 Z\" /><path class=\"thin\" d=\"M86 132 H112 L126 111 L144 158 L159 132 H174\" /><circle class=\"shape\" cx=\"85\" cy=\"132\" r=\"5\" /><circle class=\"shape\" cx=\"174\" cy=\"132\" r=\"5\" /></svg>\n<header class=\"topbar\"><button class=\"topnav-burger\" type=\"button\" aria-label=\"Open page menu\" aria-expanded=\"false\" aria-controls=\"topnav-menu\">☰</button><h1><a href=\"/?period={}&amp;theme={}\">EggPool</a></h1><nav class=\"topnav\"><div class=\"topnav-menu\" id=\"topnav-menu\"><a class=\"active\" href=\"/?period={}&amp;theme={}\">Overview</a><a href=\"/accounts?period={}&amp;theme={}\">Accounts</a><a href=\"/models?period={}&amp;theme={}\">Models</a><form method=\"get\" class=\"theme-selector\"><select name=\"theme\" onchange=\"this.form.submit()\">{}</select><input type=\"hidden\" name=\"period\" value=\"{}\"></form></div><button type=\"button\" class=\"topnav-refresh\" aria-label=\"Reload this page\" onclick=\"window.location.reload()\">↻</button></nav></header>\n<main id=\"dashboard-content\"><h2>Overview</h2><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\"><option value=\"1h\">Last hour</option><option value=\"24h\" selected=\"selected\">Last 24 hours</option><option value=\"7d\">Last 7 days</option><option value=\"30d\">Last 30 days</option></select></label></form><section class=\"cards\"><div class=\"card\"><h3>Requests</h3><p class=\"metric\">{}</p><p class=\"sub\">Success {} · Errors {}</p></div><div class=\"card\"><h3>Error rate</h3><p class=\"metric\">{:.2}%</p><p class=\"sub\">avg latency {:.1} ms</p></div><div class=\"card\"><h3>Total tokens</h3><p class=\"metric\">{}</p><p class=\"sub\">fresh {} · cache read {} · cache write {}</p></div><div class=\"card\"><h3>Total cost</h3><p class=\"metric\">${:.2}</p><p class=\"sub\">in {} · out {}</p></div></section><section class=\"panel\"><div class=\"panel-header\"><h2>Account breakdown</h2></div><table><thead><tr><th>Account</th><th>Provider</th><th>Enabled</th></tr></thead><tbody>{}</tbody></table></section></main><footer><small>Period: <span class=\"period-label\">{}</span> · auto-refresh {}s · <span id=\"dashboard-updated\">ready</span></small></footer><script defer src=\"/static/dashboard.js\"></script>\n</body>\n</html>",
        html_escape(theme),
        html_escape(period),
        html_escape(theme),
        html_escape(period),
        html_escape(theme),
        html_escape(period),
        html_escape(theme),
        html_escape(period),
        html_escape(theme),
        nav,
        html_escape(period),
        total,
        success,
        errors,
        error_rate,
        summary.avg_latency_ms,
        accounted_tokens,
        fresh_tokens,
        summary.total_cache_read_tokens,
        summary.total_cache_write_tokens,
        summary.total_cost_microdollars as f64 / 1_000_000.0,
        summary.total_input_tokens,
        summary.total_output_tokens,
        account_rows,
        html_escape(period),
        refresh_interval_s
    );
    html.replace(
        r#"<option value="1h">Last hour</option><option value="24h" selected="selected">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option>"#,
        &period_options,
    )
}

fn summary_json(summary: &db::DashboardSummary, period: &str) -> Value {
    let total_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        total_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let cache_denominator = summary.total_input_tokens
        + summary.total_cache_read_tokens
        + summary.total_cache_write_tokens;
    json!({
        "period": period,
        "total_requests": summary.total_requests,
        "successful_requests": summary.successful_requests,
        "error_requests": summary.error_requests,
        "error_rate": if summary.total_requests > 0 { summary.error_requests as f64 / summary.total_requests as f64 } else { 0.0 },
        "total_input_tokens": summary.total_input_tokens,
        "total_output_tokens": summary.total_output_tokens,
        "total_tokens": total_tokens,
        "fresh_tokens": total_tokens,
        "accounted_tokens": accounted_tokens,
        "total_cost_microdollars": summary.total_cost_microdollars,
        "avg_latency_ms": summary.avg_latency_ms,
        "total_cache_read_tokens": summary.total_cache_read_tokens,
        "total_cache_write_tokens": summary.total_cache_write_tokens,
        "total_reasoning_tokens": summary.total_reasoning_tokens,
        "cache_read_ratio": if cache_denominator > 0 { Some(summary.total_cache_read_tokens as f64 / cache_denominator as f64) } else { None },
        "streamed_requests": summary.streamed_requests,
        "non_streamed_requests": summary.non_streamed_requests,
        "exact_count": summary.exact_count,
        "derived_count": summary.derived_count,
        "partial_count": summary.partial_count,
        "estimated_count": summary.estimated_count,
        "unknown_count": summary.unknown_count,
        "provider_reported_count": summary.provider_reported_count,
        "provider_reported_cost_microdollars": summary.provider_reported_cost_microdollars,
        "estimated_cost_sum_microdollars": summary.estimated_cost_sum_microdollars,
        "reservation_fallback_rows": summary.reservation_fallback_rows,
        "reservation_fallback_excess_microdollars": summary.reservation_fallback_excess_microdollars,
        "total_bytes_received": summary.total_bytes_received,
        "total_bytes_emitted": summary.total_bytes_emitted,
        "total_providers": summary.total_providers,
        "avg_ttft_ms": summary.avg_ttft_ms,
        "tokens_per_second": summary.tokens_per_second,
        "p50_ttft_ms": summary.p50_ttft_ms,
        "p99_ttft_ms": summary.p99_ttft_ms,
    })
}

fn theme_variables(name: &str) -> String {
    let fallback = "#1e1e2e";
    let Some(bytes) = theme_bytes(name) else {
        return ":root {\n  --page-bg: #1e1e2e;\n  --page-text: #cdd6f4;\n  --topbar-bg: #1e1e2e;\n  --card-bg: #1e1e2e;\n  --card-border: #45475a;\n  --link-color: #89b4fa;\n  --color-success: #a6e3a1;\n  --color-error: #f38ba8;\n  --color-warning: #fab387;\n}\n".to_owned();
    };
    let value = std::str::from_utf8(bytes)
        .unwrap_or("")
        .parse::<toml::Value>()
        .unwrap_or_else(|_| toml::Value::Table(Default::default()));
    let background = theme_value(&value, &["general", "background"], fallback);
    let primary = theme_value(&value, &["text", "primary"], "#cdd6f4");
    let border = theme_value(&value, &["general", "border"], "#45475a");
    let success = theme_value(&value, &["text", "success"], "#a6e3a1");
    let error = theme_value(&value, &["text", "error"], "#f38ba8");
    format!(
        ":root {{\n  --page-bg: {};\n  --page-text: {};\n  --topbar-bg: {};\n  --topbar-text: {};\n  --card-bg: {};\n  --card-border: {};\n  --link-color: {};\n  --color-success: {};\n  --color-error: {};\n  --color-warning: {};\n}}\n",
        background,
        primary,
        background,
        primary,
        background,
        border,
        theme_value(&value, &["buffer", "url"], "#89b4fa"),
        success,
        error,
        theme_value(&value, &["buffer", "action"], "#fab387")
    )
}

fn theme_bytes(name: &str) -> Option<&'static [u8]> {
    Some(match name {
        "Booberry" => include_bytes!("../assets/dashboard/themes/Booberry.toml"),
        "Catppuccin Latte" => include_bytes!("../assets/dashboard/themes/Catppuccin Latte.toml"),
        "Catppuccin Macchiato" => {
            include_bytes!("../assets/dashboard/themes/Catppuccin Macchiato.toml")
        }
        "Catppuccin Mocha" => include_bytes!("../assets/dashboard/themes/Catppuccin Mocha.toml"),
        "Cyber Red" => include_bytes!("../assets/dashboard/themes/Cyber Red.toml"),
        "Cyberpunk" => include_bytes!("../assets/dashboard/themes/Cyberpunk.toml"),
        "Dark Green" => include_bytes!("../assets/dashboard/themes/Dark Green.toml"),
        "Discord (80_ Saturation)" => {
            include_bytes!("../assets/dashboard/themes/Discord (80_ Saturation).toml")
        }
        "Discord" => include_bytes!("../assets/dashboard/themes/Discord.toml"),
        "Dracula" => include_bytes!("../assets/dashboard/themes/Dracula.toml"),
        "Ferra Light" => include_bytes!("../assets/dashboard/themes/Ferra Light.toml"),
        "Flexor Dark" => include_bytes!("../assets/dashboard/themes/Flexor Dark.toml"),
        "Gruvbox" => include_bytes!("../assets/dashboard/themes/Gruvbox.toml"),
        "Halcyon Dark" => include_bytes!("../assets/dashboard/themes/Halcyon Dark.toml"),
        "IntelliJ Light" => include_bytes!("../assets/dashboard/themes/IntelliJ Light.toml"),
        "Kanagawa" => include_bytes!("../assets/dashboard/themes/Kanagawa.toml"),
        "Macaw Dark" => include_bytes!("../assets/dashboard/themes/Macaw Dark.toml"),
        "Macaw Light" => include_bytes!("../assets/dashboard/themes/Macaw Light.toml"),
        "Matrix" => include_bytes!("../assets/dashboard/themes/Matrix.toml"),
        "Noctis Lilac" => include_bytes!("../assets/dashboard/themes/Noctis Lilac.toml"),
        "Nord" => include_bytes!("../assets/dashboard/themes/Nord.toml"),
        "Nostromo Terminal" => include_bytes!("../assets/dashboard/themes/Nostromo Terminal.toml"),
        "One Dark" => include_bytes!("../assets/dashboard/themes/One Dark.toml"),
        "Oxocarbon" => include_bytes!("../assets/dashboard/themes/Oxocarbon.toml"),
        "Rose Pine Dawn" => include_bytes!("../assets/dashboard/themes/Rose Pine Dawn.toml"),
        "Rose Pine Moon" => include_bytes!("../assets/dashboard/themes/Rose Pine Moon.toml"),
        "Rose Pine" => include_bytes!("../assets/dashboard/themes/Rose Pine.toml"),
        "Solarized Dark" => include_bytes!("../assets/dashboard/themes/Solarized Dark.toml"),
        "Sonokai" => include_bytes!("../assets/dashboard/themes/Sonokai.toml"),
        "Tokyo Night Storm" => include_bytes!("../assets/dashboard/themes/Tokyo Night Storm.toml"),
        "VESPER" => include_bytes!("../assets/dashboard/themes/VESPER.toml"),
        "Zenburn" => include_bytes!("../assets/dashboard/themes/Zenburn.toml"),
        "acton" => include_bytes!("../assets/dashboard/themes/acton.toml"),
        "bam" => include_bytes!("../assets/dashboard/themes/bam.toml"),
        "base16-atelier-forest-light" => {
            include_bytes!("../assets/dashboard/themes/base16-atelier-forest-light.toml")
        }
        "berlin" => include_bytes!("../assets/dashboard/themes/berlin.toml"),
        "black but with important highlights" => {
            include_bytes!("../assets/dashboard/themes/black but with important highlights.toml")
        }
        "broc" => include_bytes!("../assets/dashboard/themes/broc.toml"),
        "cork" => include_bytes!("../assets/dashboard/themes/cork.toml"),
        "ferra" => include_bytes!("../assets/dashboard/themes/ferra.toml"),
        "forest" => include_bytes!("../assets/dashboard/themes/forest.toml"),
        "lisbon" => include_bytes!("../assets/dashboard/themes/lisbon.toml"),
        "midnight" => include_bytes!("../assets/dashboard/themes/midnight.toml"),
        "oslo" => include_bytes!("../assets/dashboard/themes/oslo.toml"),
        "plum" => include_bytes!("../assets/dashboard/themes/plum.toml"),
        "portland" => include_bytes!("../assets/dashboard/themes/portland.toml"),
        "sunset" => include_bytes!("../assets/dashboard/themes/sunset.toml"),
        "tofino" => include_bytes!("../assets/dashboard/themes/tofino.toml"),
        "vanimo" => include_bytes!("../assets/dashboard/themes/vanimo.toml"),
        "vik" => include_bytes!("../assets/dashboard/themes/vik.toml"),
        _ => return None,
    })
}

fn theme_value<'a>(value: &'a toml::Value, path: &[&str], fallback: &'a str) -> &'a str {
    path.iter()
        .try_fold(value, |current, key| current.get(*key))
        .and_then(toml::Value::as_str)
        .unwrap_or(fallback)
}

#[cfg(test)]
mod tests {
    use std::{fs, path::PathBuf};

    use super::{html_escape, is_loopback_host, valid_key_shape, verify_api_key};
    use axum::http::{HeaderMap, HeaderValue};
    use serde::Deserialize;
    use sha2::{Digest, Sha256};

    #[derive(Debug, Deserialize)]
    struct AssetRecord {
        path: String,
        sha256: String,
    }

    #[test]
    fn escape_covers_markup_and_quotes() {
        assert_eq!(
            html_escape("</script> & \" '"),
            "&lt;/script&gt; &amp; &quot; &#x27;"
        );
    }

    #[test]
    fn authentication_accepts_bearer_and_x_api_key() {
        let mut headers = HeaderMap::new();
        headers.insert("authorization", HeaderValue::from_static("Bearer test-key"));
        assert!(verify_api_key(&headers, "test-key"));
        headers.clear();
        headers.insert("x-api-key", HeaderValue::from_static("test-key"));
        assert!(verify_api_key(&headers, "test-key"));
    }

    #[test]
    fn startup_key_shape_and_loopback_rules_are_bounded() {
        assert!(valid_key_shape("test-key"));
        assert!(!valid_key_shape("short"));
        assert!(is_loopback_host("127.0.0.1"));
        assert!(is_loopback_host("[::1]"));
        assert!(!is_loopback_host("0.0.0.0"));
    }

    #[test]
    fn copied_asset_manifest_matches_the_frozen_python_source() {
        let manifest: Vec<AssetRecord> =
            serde_json::from_str(include_str!("../assets/dashboard/manifest.json"))
                .expect("asset manifest is valid JSON");
        assert_eq!(manifest.len(), 54);
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        for asset in manifest {
            let copied = root.join("assets/dashboard").join(&asset.path);
            let source = root.join("../src/eggpool/dashboard").join(&asset.path);
            let copied_bytes = fs::read(&copied).expect("copied asset exists");
            let source_bytes = fs::read(&source).expect("source asset exists");
            assert_eq!(copied_bytes, source_bytes, "asset drift: {}", asset.path);
            let digest = Sha256::digest(&copied_bytes);
            let actual = digest
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>();
            assert_eq!(actual, asset.sha256, "manifest drift: {}", asset.path);
        }
    }
}
