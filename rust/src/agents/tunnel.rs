//! Process-owned Eggress reverse servers used by configured Agent accounts.

use std::{
    collections::BTreeMap,
    net::SocketAddr,
    path::Path,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use eggress_protocol_reverse::{
    ProtocolError,
    metrics::ReverseMetrics,
    server::{ReverseServer, ReverseServerConfig, ReverseServerState},
    tls::ReverseServerTlsConfig,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tokio::{net::TcpStream, task::JoinHandle};

use crate::{Config, config::AccountAuthMode};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(5);

#[derive(Debug, thiserror::Error)]
pub enum AgentTunnelError {
    #[error("agent support is disabled")]
    Disabled,
    #[error(
        "agent account {account_name:?} for provider {provider_id:?} has no transport credential"
    )]
    MissingTransportCredential {
        provider_id: String,
        account_name: String,
    },
    #[error(
        "agent account {account_name:?} for provider {provider_id:?} has invalid tunnel configuration"
    )]
    InvalidConfiguration {
        provider_id: String,
        account_name: String,
    },
    #[error(
        "agent account {account_name:?} for provider {provider_id:?} TLS material could not be loaded"
    )]
    TlsMaterial {
        provider_id: String,
        account_name: String,
    },
    #[error(
        "agent account {account_name:?} for provider {provider_id:?} tunnel listener could not start"
    )]
    ListenerStart {
        provider_id: String,
        account_name: String,
    },
    #[error(
        "agent account {account_name:?} for provider {provider_id:?} tunnel changed while an earlier generation still owns it"
    )]
    IncompatibleGeneration {
        provider_id: String,
        account_name: String,
    },
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AgentTunnelCloseReport {
    pub released: usize,
    pub shared: usize,
    pub shutdown_timed_out: usize,
}

/// Raw connection facts for one Agent account, straight from the reverse
/// server's own counters.
///
/// These are deliberately separate from the availability projection: reading
/// them does NOT change or latch any projected state. The accepted counter
/// rises only after a control connection has AUTHENTICATED, so EggPool's own
/// startup readiness probe never counts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AgentTunnelConnectionFacts {
    /// Control connections that authenticated since the listener started.
    pub control_connections_accepted: u64,
    /// Control connections that are open right now.
    pub active_control_connections: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentTunnelState {
    Online,
    Starting,
    Offline,
    Failed,
}

impl AgentTunnelState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Online => "online",
            Self::Starting => "starting",
            Self::Offline => "offline",
            Self::Failed => "failed",
        }
    }

    pub const fn reason_code(self) -> Option<&'static str> {
        match self {
            Self::Online => None,
            Self::Starting => Some("waiting_for_agent"),
            Self::Offline => Some("control_disconnected"),
            Self::Failed => Some("tunnel_failed"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AgentTunnelStatus {
    pub provider_id: String,
    pub account_name: String,
    pub state: AgentTunnelState,
    pub reason_code: Option<&'static str>,
    pub active_control: u32,
    pub control_connected: bool,
    pub active_streams: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AgentTunnelSnapshot {
    pub tunnels: Vec<AgentTunnelStatus>,
}

#[derive(Clone, Default)]
pub struct AgentTunnelAvailability {
    tunnels: BTreeMap<String, BTreeMap<String, AgentTunnelAvailabilityEntry>>,
}

#[derive(Clone)]
struct AgentTunnelAvailabilityEntry {
    state: Arc<ReverseServerState>,
    running: Arc<AtomicBool>,
    metrics: Arc<ReverseMetrics>,
}

impl std::fmt::Debug for AgentTunnelAvailability {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AgentTunnelAvailability")
            .field(
                "tunnel_count",
                &self.tunnels.values().map(BTreeMap::len).sum::<usize>(),
            )
            .finish()
    }
}

impl AgentTunnelAvailability {
    fn state_for_entry(entry: &AgentTunnelAvailabilityEntry) -> (AgentTunnelState, u32, u32) {
        let snapshot = entry.state.snapshot();
        // The reverse server increments this counter only after a control
        // connection has AUTHENTICATED, so EggPool's own startup readiness
        // probe never counts. It is therefore the connection-state owner for
        // "this agent has connected at least once", and it survives a
        // connection that appeared and dropped before the first read.
        let ever_connected = entry
            .metrics
            .control_connections_accepted_total
            .load(Ordering::Acquire)
            > 0;
        let state = if snapshot.active_control > 0 {
            AgentTunnelState::Online
        } else if !entry.running.load(Ordering::Acquire) {
            AgentTunnelState::Failed
        } else if ever_connected {
            AgentTunnelState::Offline
        } else {
            AgentTunnelState::Starting
        };
        (state, snapshot.active_control, snapshot.active_streams)
    }

    pub fn state_for(&self, provider_id: &str, account_name: &str) -> AgentTunnelState {
        self.tunnels
            .get(provider_id)
            .and_then(|accounts| accounts.get(account_name))
            .map_or(AgentTunnelState::Offline, |entry| {
                Self::state_for_entry(entry).0
            })
    }

    /// Raw connection facts for one account, read from the reverse server.
    ///
    /// Reading these does not latch or otherwise change the projected state.
    pub fn connection_facts(
        &self,
        provider_id: &str,
        account_name: &str,
    ) -> AgentTunnelConnectionFacts {
        self.tunnels
            .get(provider_id)
            .and_then(|accounts| accounts.get(account_name))
            .map_or(
                AgentTunnelConnectionFacts {
                    control_connections_accepted: 0,
                    active_control_connections: 0,
                },
                |entry| AgentTunnelConnectionFacts {
                    control_connections_accepted: entry
                        .metrics
                        .control_connections_accepted_total
                        .load(Ordering::Acquire),
                    active_control_connections: entry.state.snapshot().active_control,
                },
            )
    }

    pub fn status_for(&self, provider_id: &str, account_name: &str) -> AgentTunnelStatus {
        let Some(entry) = self
            .tunnels
            .get(provider_id)
            .and_then(|accounts| accounts.get(account_name))
        else {
            return AgentTunnelStatus {
                provider_id: provider_id.to_owned(),
                account_name: account_name.to_owned(),
                state: AgentTunnelState::Offline,
                reason_code: Some("tunnel_unavailable"),
                active_control: 0,
                control_connected: false,
                active_streams: 0,
            };
        };
        let (state, active_control, active_streams) = Self::state_for_entry(entry);
        AgentTunnelStatus {
            provider_id: provider_id.to_owned(),
            account_name: account_name.to_owned(),
            state,
            reason_code: state.reason_code(),
            active_control,
            control_connected: active_control > 0,
            active_streams,
        }
    }

    pub fn snapshot(&self) -> AgentTunnelSnapshot {
        let tunnels = self
            .tunnels
            .iter()
            .flat_map(|(provider_id, accounts)| {
                accounts
                    .keys()
                    .map(|account_name| self.status_for(provider_id, account_name))
            })
            .collect();
        AgentTunnelSnapshot { tunnels }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct AgentKey {
    provider_id: String,
    account_name: String,
}

struct TunnelEntry {
    fingerprint: String,
    server: Weak<ManagedTunnel>,
}

/// Shared, process-owned tunnel registry. It contains only weak tunnel
/// references; active generations own the listeners through their sets.
#[derive(Clone, Default)]
pub struct AgentTunnelManager {
    entries: Arc<tokio::sync::Mutex<BTreeMap<AgentKey, TunnelEntry>>>,
}

impl std::fmt::Debug for AgentTunnelManager {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AgentTunnelManager")
            .field("registered_tunnels", &"[redacted]")
            .finish()
    }
}

impl AgentTunnelManager {
    pub fn new() -> Self {
        Self::default()
    }

    /// Prepare all enabled Agent listeners before the candidate generation is
    /// published. Identical listeners are shared with the previous generation.
    pub async fn prepare_for_generation(
        &self,
        config: &Config,
        config_path: Option<&Path>,
    ) -> Result<AgentTunnelSet, AgentTunnelError> {
        let mut set = AgentTunnelSet {
            manager: self.clone(),
            tunnels: Vec::new(),
        };
        if !config.agents.enabled {
            return Ok(set);
        }
        for (provider_id, provider) in &config.providers {
            for account in &provider.accounts {
                if !account.enabled || account.auth_mode != AccountAuthMode::Agent {
                    continue;
                }
                let key = AgentKey {
                    provider_id: provider_id.clone(),
                    account_name: account.name.clone(),
                };
                let result = build_server_config(config, provider_id, account, config_path, None)
                    .map(|(server_config, fingerprint)| (server_config, fingerprint, key.clone()));
                let (server_config, fingerprint, key) = match result {
                    Ok(value) => value,
                    Err(error) => {
                        let _ = set.close(STARTUP_TIMEOUT).await;
                        return Err(error);
                    }
                };
                match self.acquire(key, fingerprint, server_config).await {
                    Ok(server) => set.tunnels.push(server),
                    Err(error) => {
                        let _ = set.close(STARTUP_TIMEOUT).await;
                        return Err(error);
                    }
                }
            }
        }
        Ok(set)
    }

    /// Start one account's tunnel before its configuration is published.
    /// The returned set owns the listener until a live generation acquires it
    /// or rollback closes it. The supplied ticket credential is never logged.
    pub async fn start_account(
        &self,
        config: &Config,
        provider_id: &str,
        account: &crate::config::AccountConfig,
        config_path: Option<&Path>,
        transport_secret: &str,
    ) -> Result<AgentTunnelSet, AgentTunnelError> {
        if !config.agents.enabled {
            return Err(AgentTunnelError::Disabled);
        }
        let key = AgentKey {
            provider_id: provider_id.to_owned(),
            account_name: account.name.clone(),
        };
        let (server_config, fingerprint) = build_server_config(
            config,
            provider_id,
            account,
            config_path,
            Some(transport_secret),
        )?;
        let server = self
            .acquire(key.clone(), fingerprint, server_config)
            .await?;
        Ok(AgentTunnelSet {
            manager: self.clone(),
            tunnels: vec![server],
        })
    }

    async fn acquire(
        &self,
        key: AgentKey,
        fingerprint: String,
        server_config: ReverseServerConfig,
    ) -> Result<(AgentKey, Arc<ManagedTunnel>), AgentTunnelError> {
        let mut entries = self.entries.lock().await;
        if let Some(entry) = entries.get(&key)
            && let Some(server) = entry.server.upgrade()
            && server.is_running()
        {
            if entry.fingerprint != fingerprint {
                return Err(AgentTunnelError::IncompatibleGeneration {
                    provider_id: key.provider_id,
                    account_name: key.account_name,
                });
            }
            return Ok((key, server));
        }
        entries.remove(&key);
        let server = start_server(server_config, &key).await?;
        entries.insert(
            key.clone(),
            TunnelEntry {
                fingerprint,
                server: Arc::downgrade(&server),
            },
        );
        Ok((key, server))
    }

    async fn release(
        &self,
        key: AgentKey,
        server: Arc<ManagedTunnel>,
        remaining: Duration,
    ) -> TunnelRelease {
        let mut entries = self.entries.lock().await;
        if Arc::strong_count(&server) > 1 {
            drop(entries);
            drop(server);
            return TunnelRelease::Shared;
        }
        if entries
            .get(&key)
            .is_some_and(|entry| Weak::ptr_eq(&entry.server, &Arc::downgrade(&server)))
        {
            entries.remove(&key);
        }
        (server.cancel)();
        let joined = server.join(remaining).await;
        drop(entries);
        drop(server);
        if joined {
            TunnelRelease::Released
        } else {
            TunnelRelease::TimedOut
        }
    }
}

enum TunnelRelease {
    Shared,
    Released,
    TimedOut,
}

/// A generation's references to the listeners it prepared. Not cloneable so
/// each generation releases its ownership exactly once.
pub struct AgentTunnelSet {
    manager: AgentTunnelManager,
    tunnels: Vec<(AgentKey, Arc<ManagedTunnel>)>,
}

impl std::fmt::Debug for AgentTunnelSet {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AgentTunnelSet")
            .field("tunnel_count", &self.tunnels.len())
            .finish()
    }
}

impl AgentTunnelSet {
    pub fn empty() -> Self {
        Self {
            manager: AgentTunnelManager::new(),
            tunnels: Vec::new(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.tunnels.is_empty()
    }

    /// Raw connection facts for one account, read from the reverse server.
    ///
    /// Reading these does not latch or otherwise change the projected state.
    pub fn connection_facts(
        &self,
        provider_id: &str,
        account_name: &str,
    ) -> AgentTunnelConnectionFacts {
        self.availability()
            .connection_facts(provider_id, account_name)
    }

    pub fn availability(&self) -> AgentTunnelAvailability {
        let mut tunnels = BTreeMap::<String, BTreeMap<String, AgentTunnelAvailabilityEntry>>::new();
        for (key, server) in &self.tunnels {
            tunnels.entry(key.provider_id.clone()).or_default().insert(
                key.account_name.clone(),
                AgentTunnelAvailabilityEntry {
                    state: Arc::clone(&server.state),
                    running: Arc::clone(&server.running),
                    metrics: Arc::clone(&server.metrics),
                },
            );
        }
        AgentTunnelAvailability { tunnels }
    }

    pub fn snapshot(&self) -> AgentTunnelSnapshot {
        self.availability().snapshot()
    }

    /// Release all listener references. The last generation owner cancels and
    /// joins each reverse server within the supplied aggregate grace period.
    pub async fn close(mut self, timeout: Duration) -> AgentTunnelCloseReport {
        let deadline = tokio::time::Instant::now() + timeout;
        let mut report = AgentTunnelCloseReport::default();
        for (key, server) in self.tunnels.drain(..) {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            match self.manager.release(key, server, remaining).await {
                TunnelRelease::Shared => report.shared += 1,
                TunnelRelease::Released => report.released += 1,
                TunnelRelease::TimedOut => report.shutdown_timed_out += 1,
            }
        }
        report
    }
}

struct ManagedTunnel {
    state: Arc<ReverseServerState>,
    cancel: Box<dyn Fn() + Send + Sync>,
    running: Arc<AtomicBool>,
    metrics: Arc<ReverseMetrics>,
    task: Mutex<Option<JoinHandle<Result<(), ProtocolError>>>>,
}

impl ManagedTunnel {
    fn is_running(&self) -> bool {
        self.running.load(Ordering::Acquire)
    }

    async fn join(&self, timeout: Duration) -> bool {
        let task = self
            .task
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        let Some(task) = task else {
            return true;
        };
        matches!(tokio::time::timeout(timeout, task).await, Ok(Ok(Ok(()))))
    }
}

impl Drop for ManagedTunnel {
    fn drop(&mut self) {
        (self.cancel)();
    }
}

fn build_server_config(
    _config: &Config,
    provider_id: &str,
    account: &crate::config::AccountConfig,
    config_path: Option<&Path>,
    transport_secret_override: Option<&str>,
) -> Result<(ReverseServerConfig, String), AgentTunnelError> {
    let invalid = || AgentTunnelError::InvalidConfiguration {
        provider_id: provider_id.to_owned(),
        account_name: account.name.clone(),
    };
    let agent = account.agent.as_ref().ok_or_else(invalid)?;
    let control_bind = agent
        .control_bind
        .parse::<SocketAddr>()
        .map_err(|_| invalid())?;
    let external_bind = agent
        .external_bind
        .parse::<SocketAddr>()
        .map_err(|_| invalid())?;
    if !external_bind.ip().is_loopback() || control_bind.port() == 0 || external_bind.port() == 0 {
        return Err(invalid());
    }
    let tls_config = agent.tls.as_ref().ok_or_else(invalid)?;
    let password = transport_secret_override
        .map(str::to_owned)
        .or_else(|| {
            crate::config::account_secret_from_env(config_path, &agent.transport_secret_env)
        })
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| AgentTunnelError::MissingTransportCredential {
            provider_id: provider_id.to_owned(),
            account_name: account.name.clone(),
        })?;
    if !valid_reverse_auth_field(&account.name) || !valid_reverse_auth_field(&password) {
        return Err(invalid());
    }

    let read_material = |path: &str| {
        std::fs::read(Config::runtime_path(path)).map_err(|_| AgentTunnelError::TlsMaterial {
            provider_id: provider_id.to_owned(),
            account_name: account.name.clone(),
        })
    };
    let material = ReverseServerTlsConfig {
        cert_pem: read_material(&tls_config.cert_path)?,
        key_pem: read_material(&tls_config.key_path)?,
        client_ca_pem: tls_config
            .client_ca_path
            .as_deref()
            .map(read_material)
            .transpose()?,
        require_client_cert: tls_config.require_client_cert,
    };
    if material.build_server_config().is_err() {
        return Err(AgentTunnelError::TlsMaterial {
            provider_id: provider_id.to_owned(),
            account_name: account.name.clone(),
        });
    }
    let tls = Some(material);

    let mut digest = Sha256::new();
    let mut add = |bytes: &[u8]| {
        digest.update((bytes.len() as u64).to_be_bytes());
        digest.update(bytes);
    };
    add(provider_id.as_bytes());
    add(account.name.as_bytes());
    add(agent.control_bind.as_bytes());
    add(agent.external_bind.as_bytes());
    add(agent.max_streams.to_be_bytes().as_slice());
    add(agent.heartbeat_interval_s.to_be_bytes().as_slice());
    add(password.as_bytes());
    if let Some(material) = &tls {
        add(&material.cert_pem);
        add(&material.key_pem);
        if let Some(ca) = &material.client_ca_pem {
            add(ca);
        }
        add(&[u8::from(material.require_client_cert)]);
    }
    let fingerprint = format!("{:x}", digest.finalize());
    let config = ReverseServerConfig {
        control_bind,
        external_bind: Some(external_bind),
        auth_username: Some(account.name.clone()),
        auth_password: Some(password),
        max_control_connections: 8,
        read_timeout_ms: agent.heartbeat_interval_s.saturating_mul(3_000),
        allow_bind: Some(vec![external_bind]),
        max_listeners_per_client: 1,
        max_streams_per_listener: agent.max_streams,
        max_pending_external: agent.max_streams,
        tls,
    };
    config.validate().map_err(|_| invalid())?;
    // The account endpoint is validated by Config::validate and remains in
    // the provider client topology; it is intentionally not a reverse target.
    Ok((config, fingerprint))
}

fn valid_reverse_auth_field(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte != b':' && byte >= 0x20 && byte != 0x7f)
}

async fn start_server(
    config: ReverseServerConfig,
    key: &AgentKey,
) -> Result<Arc<ManagedTunnel>, AgentTunnelError> {
    let control_bind = config.control_bind;
    let mut server = ReverseServer::new(config);
    // The reverse server's own metrics record an accepted control connection
    // only after that connection authenticates. That counter is the
    // connection-state owner for "this agent has connected at least once".
    let metrics = Arc::new(ReverseMetrics::new());
    server.set_metrics(Arc::clone(&metrics));
    let state = server.state_handle();
    let cancel_token = server.cancel_token();
    let cancel_for_startup = cancel_token.clone();
    let running = Arc::new(AtomicBool::new(true));
    let task_running = Arc::clone(&running);
    let mut task = tokio::spawn(async move {
        let result = server.run().await;
        task_running.store(false, Ordering::Release);
        result
    });
    let ready = tokio::time::timeout(
        STARTUP_TIMEOUT,
        wait_for_listener(control_bind, &state, &mut task, &running),
    )
    .await
    .is_ok_and(|result| result.is_ok());
    if !ready {
        cancel_for_startup.cancel();
        let _ = tokio::time::timeout(Duration::from_secs(1), &mut task).await;
        return Err(AgentTunnelError::ListenerStart {
            provider_id: key.provider_id.clone(),
            account_name: key.account_name.clone(),
        });
    }
    Ok(Arc::new(ManagedTunnel {
        state,
        cancel: Box::new(move || cancel_token.cancel()),
        running,
        metrics,
        task: Mutex::new(Some(task)),
    }))
}

async fn wait_for_listener(
    control_bind: SocketAddr,
    state: &ReverseServerState,
    task: &mut JoinHandle<Result<(), ProtocolError>>,
    running: &AtomicBool,
) -> Result<(), ()> {
    let mut probe = None;
    loop {
        if state.snapshot().active_control > 0 {
            drop(probe.take());
            break;
        }
        if !running.load(Ordering::Acquire) {
            return Err(());
        }
        if probe.is_none()
            && let Ok(stream) = TcpStream::connect(control_bind).await
        {
            probe = Some(stream);
        }
        tokio::select! {
            result = &mut *task => {
                let _ = result;
                return Err(());
            }
            _ = tokio::time::sleep(STARTUP_POLL_INTERVAL) => {}
        }
    }
    // Closing the readiness probe makes the reverse server discard it. Wait
    // for the control counter to drain rather than leaving a phantom client in
    // the accept queue.
    drop(probe.take());
    loop {
        if state.snapshot().active_control == 0 {
            return Ok(());
        }
        if !running.load(Ordering::Acquire) {
            return Err(());
        }
        tokio::select! {
            result = &mut *task => {
                let _ = result;
                return Err(());
            }
            _ = tokio::time::sleep(STARTUP_POLL_INTERVAL) => {}
        }
    }
}
