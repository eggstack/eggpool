//! Authenticated, owner-bound xAI Web UI device-code login.
//!
//! Device codes and eventual credentials live only in this bounded process
//! registry. Provider traffic uses the active generation's Eggfetch transport;
//! durable custody remains exclusively in OAuthAccountManager.

use std::{
    collections::HashMap,
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use axum::{
    Extension, Form, Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Redirect, Response},
};
use eggpass_auth::{
    CancellationToken, DeviceCode, Error as EggpassError, OAuthClient, OAuthProviderConfig,
};
use serde::{Deserialize, Serialize};

use crate::{
    config::{AccountAuthMode, Config},
    oauth::OAuthCredentials,
    personal_upstream::PersonalBinding,
    providers::ProviderClientPool,
    web_session::Principal,
};

use super::AppState;

const XAI_PROVIDER_ID: &str = "xai";
const MAX_FLOW_COUNT: usize = 64;
const MAX_DEVICE_LIFETIME: Duration = Duration::from_secs(10 * 60);
const COMMIT_WINDOW: Duration = Duration::from_secs(2 * 60);
const TERMINAL_WINDOW: Duration = Duration::from_secs(2 * 60);
const START_WINDOW: Duration = Duration::from_secs(60);
const PERMISSION_RECHECK_INTERVAL: Duration = Duration::from_secs(1);
pub(super) const MAX_FORM_BYTES: usize = 2048;
static NEXT_FLOW_GENERATION: AtomicU64 = AtomicU64::new(1);

type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;
type ProgressSink = Arc<dyn Fn(PollProgress) + Send + Sync>;

#[derive(Clone, Copy)]
enum PollProgress {
    Pending,
    // Eggpass's helper handles slow_down internally; this remains injectable
    // so the in-memory status projection can be exercised deterministically.
    #[allow(dead_code)]
    SlowDown,
}

enum PollOutcome {
    Authorized(OAuthCredentials),
    Denied,
    Expired,
    Canceled,
    Failed,
}

trait DevicePoller: Send + Sync {
    fn poll<'a>(
        &'a self,
        cancellation: CancellationToken,
        timeout: Duration,
        progress: ProgressSink,
    ) -> BoxFuture<'a, PollOutcome>;
}

struct DeviceOffer {
    verification_uri: String,
    user_code: String,
    expires_in: Duration,
    interval_seconds: u64,
    poller: Arc<dyn DevicePoller>,
}

trait DeviceBackend: Send + Sync {
    fn start<'a>(
        &'a self,
        clients: &'a ProviderClientPool,
        account_name: &'a str,
    ) -> BoxFuture<'a, Result<DeviceOffer, StartFailure>>;
}

struct EggpassDeviceBackend;

impl DeviceBackend for EggpassDeviceBackend {
    fn start<'a>(
        &'a self,
        clients: &'a ProviderClientPool,
        account_name: &'a str,
    ) -> BoxFuture<'a, Result<DeviceOffer, StartFailure>> {
        Box::pin(async move {
            let provider_http = clients
                .get_client(XAI_PROVIDER_ID, Some(account_name))
                .map_err(|_| StartFailure::ProviderUnavailable)?;
            let client = OAuthClient::new(
                provider_http.oauth_eggfetch_client(),
                OAuthProviderConfig::xai(),
            );
            let device = client
                .request_device_code()
                .await
                .map_err(|error| match error {
                    EggpassError::UntrustedVerificationUrl { .. } => {
                        StartFailure::UntrustedVerificationUrl
                    }
                    _ => StartFailure::ProviderUnavailable,
                })?;

            // request_device_code already enforces the configured exact HTTPS
            // allowlist. Keep a second narrow display check at the UI seam.
            let verification_uri = device.verification_uri().to_owned();
            if !trusted_xai_verification_uri(&verification_uri) {
                return Err(StartFailure::UntrustedVerificationUrl);
            }
            let user_code = device.user_code().to_owned();
            if user_code.is_empty()
                || user_code.len() > 128
                || user_code.chars().any(char::is_control)
                || verification_uri.len() > 2048
            {
                return Err(StartFailure::ProviderUnavailable);
            }
            let expires_in = device.expires_in().min(MAX_DEVICE_LIFETIME);
            if expires_in.is_zero() {
                return Err(StartFailure::ProviderUnavailable);
            }
            let interval_seconds = device.interval().as_secs().clamp(1, 600);
            let poller = Arc::new(EggpassDevicePoller { client, device });
            Ok(DeviceOffer {
                verification_uri,
                user_code,
                expires_in,
                interval_seconds,
                poller,
            })
        })
    }
}

struct EggpassDevicePoller {
    client: OAuthClient,
    device: DeviceCode,
}

impl DevicePoller for EggpassDevicePoller {
    fn poll<'a>(
        &'a self,
        cancellation: CancellationToken,
        timeout: Duration,
        _progress: ProgressSink,
    ) -> BoxFuture<'a, PollOutcome> {
        Box::pin(async move {
            match self
                .client
                .poll_device_authorization(&self.device, &cancellation, timeout)
                .await
            {
                Ok(credentials) => PollOutcome::Authorized(credentials),
                Err(EggpassError::ProviderDenied(_)) => PollOutcome::Denied,
                Err(
                    EggpassError::DeviceExpired | EggpassError::Timeout("device authorization"),
                ) => PollOutcome::Expired,
                Err(EggpassError::Cancelled) => PollOutcome::Canceled,
                Err(_) => PollOutcome::Failed,
            }
        })
    }
}

#[derive(Clone, Hash, PartialEq, Eq)]
struct FlowKey {
    session_id: String,
    account_name: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FlowStatus {
    Starting,
    Pending,
    SlowDown,
    ReadyToCommit,
    Committing,
    Denied,
    Expired,
    Canceled,
    Failed,
}

impl FlowStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Pending => "pending",
            Self::SlowDown => "slow_down",
            Self::ReadyToCommit => "ready_to_commit",
            Self::Committing => "committing",
            Self::Denied => "denied",
            Self::Expired => "expired",
            Self::Canceled => "canceled",
            Self::Failed => "failed",
        }
    }

    fn is_active(self) -> bool {
        matches!(
            self,
            Self::Starting
                | Self::Pending
                | Self::SlowDown
                | Self::ReadyToCommit
                | Self::Committing
        )
    }

    fn is_polling(self) -> bool {
        matches!(self, Self::Pending | Self::SlowDown)
    }

    fn error_code(self) -> Option<&'static str> {
        match self {
            Self::Denied => Some("provider_denied"),
            Self::Expired => Some("device_expired"),
            Self::Canceled => Some("flow_canceled"),
            Self::Failed => Some("provider_unavailable"),
            _ => None,
        }
    }
}

struct FlowEntry {
    user_id: String,
    generation: u64,
    status: FlowStatus,
    verification_uri: Option<String>,
    user_code: Option<String>,
    interval_seconds: u64,
    expires_at: Instant,
    poll_started: bool,
    cancellation: CancellationToken,
    poller: Option<Arc<dyn DevicePoller>>,
    credentials: Option<OAuthCredentials>,
}

struct FlowStore {
    entries: Mutex<HashMap<FlowKey, FlowEntry>>,
    changed: tokio::sync::Notify,
}

impl Drop for FlowStore {
    fn drop(&mut self) {
        let entries = self
            .entries
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        for flow in entries.values() {
            flow.cancellation.cancel();
        }
    }
}

#[derive(Clone)]
pub(super) struct FlowRegistry {
    inner: Arc<FlowStore>,
    backend: Arc<dyn DeviceBackend>,
}

impl Default for FlowRegistry {
    fn default() -> Self {
        Self::with_backend(Arc::new(EggpassDeviceBackend))
    }
}

impl FlowRegistry {
    fn with_backend(backend: Arc<dyn DeviceBackend>) -> Self {
        Self {
            inner: Arc::new(FlowStore {
                entries: Mutex::new(HashMap::new()),
                changed: tokio::sync::Notify::new(),
            }),
            backend,
        }
    }

    async fn begin(
        &self,
        session_id: &str,
        user_id: &str,
        account_name: &str,
        clients: &ProviderClientPool,
    ) -> Result<FlowView, FlowError> {
        if session_id.is_empty() || user_id.is_empty() || !valid_account_name(account_name) {
            return Err(FlowError::NotFound);
        }
        let key = FlowKey {
            session_id: session_id.to_owned(),
            account_name: account_name.to_owned(),
        };
        let generation = NEXT_FLOW_GENERATION.fetch_add(1, Ordering::Relaxed);
        {
            let mut entries = lock(&self.inner.entries);
            reap(&mut entries);
            // One active flow per owner and account. A retry from another
            // authenticated session supersedes the earlier attempt: the
            // previous entry is canceled instead of returning a conflict the
            // new session can neither see nor clear.
            let superseded = entries
                .iter()
                .find(|(existing_key, entry)| {
                    existing_key.account_name == account_name
                        && entry.user_id == user_id
                        && entry.status.is_active()
                })
                .map(|(existing_key, _)| existing_key.clone());
            if let Some(superseded) = superseded
                && let Some(previous) = entries.remove(&superseded)
            {
                previous.cancellation.cancel();
            }
            if let Some(previous) = entries.remove(&key) {
                previous.cancellation.cancel();
            }
            if entries.len() >= MAX_FLOW_COUNT {
                return Err(FlowError::Capacity);
            }
            entries.insert(
                key.clone(),
                FlowEntry {
                    user_id: user_id.to_owned(),
                    generation,
                    status: FlowStatus::Starting,
                    verification_uri: None,
                    user_code: None,
                    interval_seconds: 1,
                    expires_at: Instant::now() + START_WINDOW,
                    poll_started: false,
                    cancellation: CancellationToken::new(),
                    poller: None,
                    credentials: None,
                },
            );
        }
        self.inner.changed.notify_waiters();

        let offer = match self.backend.start(clients, account_name).await {
            Ok(offer) => offer,
            Err(error) => {
                self.remove_if_generation(&key, generation);
                return Err(match error {
                    StartFailure::UntrustedVerificationUrl => FlowError::UntrustedVerificationUrl,
                    StartFailure::ProviderUnavailable => FlowError::ProviderUnavailable,
                });
            }
        };
        if offer.expires_in.is_zero()
            || !trusted_xai_verification_uri(&offer.verification_uri)
            || offer.verification_uri.len() > 2048
            || offer.user_code.is_empty()
            || offer.user_code.len() > 128
            || offer.user_code.chars().any(char::is_control)
        {
            self.remove_if_generation(&key, generation);
            return Err(FlowError::UntrustedVerificationUrl);
        }
        let now = Instant::now();
        let expires_in = offer.expires_in.min(MAX_DEVICE_LIFETIME);
        let mut entries = lock(&self.inner.entries);
        let Some(entry) = entries.get_mut(&key) else {
            return Err(FlowError::Canceled);
        };
        if entry.generation != generation || entry.status != FlowStatus::Starting {
            return Err(FlowError::Canceled);
        }
        entry.status = FlowStatus::Pending;
        entry.verification_uri = Some(offer.verification_uri);
        entry.user_code = Some(offer.user_code);
        entry.interval_seconds = offer.interval_seconds.clamp(1, 600);
        entry.expires_at = now + expires_in;
        entry.poller = Some(offer.poller);
        let view = project(entry, now);
        drop(entries);
        self.inner.changed.notify_waiters();
        Ok(view)
    }

    fn poll(
        &self,
        session_id: &str,
        user_id: &str,
        account_name: &str,
        authorization: Option<(AppState, Principal)>,
    ) -> Result<FlowView, FlowError> {
        let key = FlowKey {
            session_id: session_id.to_owned(),
            account_name: account_name.to_owned(),
        };
        let mut task = None;
        let view = {
            let mut entries = lock(&self.inner.entries);
            reap(&mut entries);
            let Some(entry) = entries.get_mut(&key) else {
                return Err(FlowError::NotFound);
            };
            if entry.user_id != user_id {
                return Err(FlowError::NotFound);
            }
            if entry.status.is_polling() && !entry.poll_started {
                let Some(poller) = entry.poller.clone() else {
                    return Err(FlowError::NotReady);
                };
                entry.poll_started = true;
                let timeout = entry.expires_at.saturating_duration_since(Instant::now());
                task = Some((
                    entry.generation,
                    poller,
                    entry.cancellation.clone(),
                    timeout,
                ));
            }
            project(entry, Instant::now())
        };
        if let Some((generation, poller, cancellation, timeout)) = task {
            let weak = Arc::downgrade(&self.inner);
            let key_for_task = key.clone();
            let registry = self.clone();
            tokio::spawn(async move {
                let progress_weak = weak.clone();
                let progress_key = key_for_task.clone();
                let progress: ProgressSink = Arc::new(move |next| {
                    if let Some(inner) = progress_weak.upgrade() {
                        set_progress(&inner, &progress_key, generation, next);
                    }
                });
                progress(PollProgress::Pending);

                let outcome = if let Some((state, session)) = authorization.as_ref() {
                    let account_name = key_for_task.account_name.clone();
                    if !xai_flow_authorized(state, session, &account_name).await {
                        PollOutcome::Canceled
                    } else {
                        tokio::select! {
                            outcome = poller.poll(cancellation.clone(), timeout, progress) => outcome,
                            _ = watch_xai_flow_authorization(
                                state.clone(),
                                session.clone(),
                                account_name,
                                cancellation.clone(),
                            ) => PollOutcome::Canceled,
                        }
                    }
                } else {
                    poller.poll(cancellation.clone(), timeout, progress).await
                };
                let outcome = match outcome {
                    PollOutcome::Authorized(credentials) => {
                        let still_authorized = match authorization.as_ref() {
                            Some((state, session)) => {
                                xai_flow_authorized(state, session, &key_for_task.account_name)
                                    .await
                            }
                            None => true,
                        };
                        if still_authorized {
                            PollOutcome::Authorized(credentials)
                        } else {
                            PollOutcome::Canceled
                        }
                    }
                    other => other,
                };
                let keep_authorization_watch =
                    matches!(&outcome, PollOutcome::Authorized(_)) && authorization.is_some();
                if matches!(&outcome, PollOutcome::Canceled) {
                    cancellation.cancel();
                }
                if let Some(inner) = weak.upgrade() {
                    finish_poll(&inner, &key_for_task, generation, outcome);
                }
                if keep_authorization_watch && let Some((state, session)) = authorization {
                    let watch = watch_xai_flow_authorization(
                        state,
                        session,
                        key_for_task.account_name.clone(),
                        cancellation.clone(),
                    );
                    let _ = tokio::time::timeout(COMMIT_WINDOW, watch).await;
                    registry.remove_if_generation(&key_for_task, generation);
                }
            });
        }
        Ok(view)
    }

    fn view(&self, session_id: &str, user_id: &str, account_name: &str) -> Option<FlowView> {
        let key = FlowKey {
            session_id: session_id.to_owned(),
            account_name: account_name.to_owned(),
        };
        let mut entries = lock(&self.inner.entries);
        reap(&mut entries);
        entries
            .get(&key)
            .filter(|entry| entry.user_id == user_id)
            .map(|entry| project(entry, Instant::now()))
    }

    fn cancel(&self, session_id: &str, user_id: &str, account_name: &str) -> bool {
        let key = FlowKey {
            session_id: session_id.to_owned(),
            account_name: account_name.to_owned(),
        };
        let removed = {
            let mut entries = lock(&self.inner.entries);
            let matches_owner = entries
                .get(&key)
                .is_some_and(|entry| entry.user_id == user_id);
            if matches_owner {
                entries.remove(&key)
            } else {
                None
            }
        };
        if let Some(flow) = removed {
            flow.cancellation.cancel();
            self.inner.changed.notify_waiters();
            true
        } else {
            false
        }
    }

    fn take_credentials(
        &self,
        session_id: &str,
        user_id: &str,
        account_name: &str,
    ) -> Result<(u64, OAuthCredentials), FlowError> {
        let key = FlowKey {
            session_id: session_id.to_owned(),
            account_name: account_name.to_owned(),
        };
        let mut entries = lock(&self.inner.entries);
        reap(&mut entries);
        let Some(entry) = entries.get_mut(&key) else {
            return Err(FlowError::NotFound);
        };
        if entry.user_id != user_id {
            return Err(FlowError::NotFound);
        }
        if entry.status != FlowStatus::ReadyToCommit {
            return Err(FlowError::NotReady);
        }
        let credentials = entry.credentials.take().ok_or(FlowError::NotReady)?;
        entry.status = FlowStatus::Committing;
        Ok((entry.generation, credentials))
    }

    fn finish_commit(&self, session_id: &str, account_name: &str, generation: u64) {
        let key = FlowKey {
            session_id: session_id.to_owned(),
            account_name: account_name.to_owned(),
        };
        let removed = {
            let mut entries = lock(&self.inner.entries);
            if entries
                .get(&key)
                .is_some_and(|entry| entry.generation == generation)
            {
                entries.remove(&key)
            } else {
                None
            }
        };
        if let Some(flow) = removed {
            flow.cancellation.cancel();
            self.inner.changed.notify_waiters();
        }
    }

    fn remove_if_generation(&self, key: &FlowKey, generation: u64) {
        let removed = {
            let mut entries = lock(&self.inner.entries);
            if entries
                .get(key)
                .is_some_and(|entry| entry.generation == generation)
            {
                entries.remove(key)
            } else {
                None
            }
        };
        if let Some(flow) = removed {
            flow.cancellation.cancel();
            self.inner.changed.notify_waiters();
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
struct FlowView {
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    error_code: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    verification_uri: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    user_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    expires_in_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    retry_after_seconds: Option<u64>,
}

fn project(entry: &FlowEntry, now: Instant) -> FlowView {
    let show_presentation = matches!(entry.status, FlowStatus::Pending | FlowStatus::SlowDown);
    FlowView {
        status: entry.status.as_str(),
        error_code: entry.status.error_code(),
        verification_uri: show_presentation
            .then(|| entry.verification_uri.clone())
            .flatten(),
        user_code: show_presentation.then(|| entry.user_code.clone()).flatten(),
        expires_in_seconds: entry.status.is_active().then(|| {
            entry
                .expires_at
                .saturating_duration_since(now)
                .as_secs()
                .min(600)
        }),
        retry_after_seconds: show_presentation.then_some(entry.interval_seconds.min(600)),
    }
}

fn set_progress(inner: &FlowStore, key: &FlowKey, generation: u64, next: PollProgress) {
    let updated = {
        let mut entries = lock(&inner.entries);
        entries.get_mut(key).is_some_and(|entry| {
            if entry.generation != generation || !entry.status.is_polling() {
                return false;
            }
            entry.status = match next {
                PollProgress::Pending => FlowStatus::Pending,
                PollProgress::SlowDown => FlowStatus::SlowDown,
            };
            true
        })
    };
    if updated {
        inner.changed.notify_waiters();
    }
}

fn finish_poll(inner: &FlowStore, key: &FlowKey, generation: u64, outcome: PollOutcome) {
    let updated = {
        let mut entries = lock(&inner.entries);
        let Some(entry) = entries.get_mut(key) else {
            return;
        };
        if entry.generation != generation || !entry.status.is_polling() {
            return;
        }
        let now = Instant::now();
        entry.poller = None;
        entry.verification_uri = None;
        entry.user_code = None;
        entry.status = match outcome {
            PollOutcome::Authorized(credentials) => {
                entry.credentials = Some(credentials);
                entry.expires_at = now + COMMIT_WINDOW;
                FlowStatus::ReadyToCommit
            }
            PollOutcome::Denied => {
                entry.expires_at = now + TERMINAL_WINDOW;
                FlowStatus::Denied
            }
            PollOutcome::Expired => {
                entry.expires_at = now + TERMINAL_WINDOW;
                FlowStatus::Expired
            }
            PollOutcome::Canceled => {
                entry.expires_at = now + TERMINAL_WINDOW;
                FlowStatus::Canceled
            }
            PollOutcome::Failed => {
                entry.expires_at = now + TERMINAL_WINDOW;
                FlowStatus::Failed
            }
        };
        true
    };
    if updated {
        inner.changed.notify_waiters();
    }
}

fn reap(entries: &mut HashMap<FlowKey, FlowEntry>) {
    let now = Instant::now();
    let mut remove = Vec::new();
    for (key, entry) in entries.iter_mut() {
        if now < entry.expires_at {
            continue;
        }
        match entry.status {
            FlowStatus::Starting => remove.push(key.clone()),
            FlowStatus::Pending | FlowStatus::SlowDown | FlowStatus::ReadyToCommit => {
                entry.cancellation.cancel();
                entry.poller = None;
                entry.credentials = None;
                entry.verification_uri = None;
                entry.user_code = None;
                entry.status = FlowStatus::Expired;
                entry.expires_at = now + TERMINAL_WINDOW;
            }
            FlowStatus::Committing => {}
            FlowStatus::Denied
            | FlowStatus::Expired
            | FlowStatus::Canceled
            | FlowStatus::Failed => remove.push(key.clone()),
        }
    }
    for key in remove {
        if let Some(flow) = entries.remove(&key) {
            flow.cancellation.cancel();
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum FlowError {
    NotFound,
    Capacity,
    Canceled,
    NotReady,
    UntrustedVerificationUrl,
    ProviderUnavailable,
}

#[derive(Clone, Copy)]
enum StartFailure {
    UntrustedVerificationUrl,
    ProviderUnavailable,
}

/// Verification pages EggPool is willing to display for xAI.
///
/// The set and the comparison mirror the pinned provider profile and the
/// Eggpass reference listener: DNS names are case-insensitive and one trailing
/// dot is not a different host, while `http::uri::Authority::host` preserves
/// the received case.
const XAI_VERIFICATION_HOSTS: &[&str] = &["accounts.x.ai", "auth.x.ai", "x.ai"];

fn trusted_xai_verification_uri(raw: &str) -> bool {
    // The pinned profile already validated this value. Repeat the same
    // fail-closed raw-byte refusal at the display seam: a backslash or
    // whitespace byte must not be able to re-aim the presentation.
    if raw.contains('#')
        || raw
            .bytes()
            .any(|byte| byte == b'\\' || byte.is_ascii_whitespace() || byte < 0x20)
    {
        return false;
    }
    let Ok(uri) = raw.parse::<Uri>() else {
        return false;
    };
    let Some(authority) = uri.authority() else {
        return false;
    };
    let host = authority.host();
    let host = host.strip_suffix('.').unwrap_or(host);
    uri.scheme_str()
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case("https"))
        && XAI_VERIFICATION_HOSTS
            .iter()
            .any(|trusted| trusted.eq_ignore_ascii_case(host))
        && !authority.as_str().contains('@')
        && authority.port_u16().is_none_or(|port| port == 443)
}

fn valid_account_name(account_name: &str) -> bool {
    !account_name.is_empty()
        && account_name.len() <= 128
        && !account_name.chars().any(char::is_control)
}

fn configured_owned_provider_account(
    config: &Config,
    bindings: &[PersonalBinding],
    user_id: &str,
    provider_id: &str,
    account_name: &str,
) -> bool {
    if !valid_account_name(account_name) {
        return false;
    }
    let configured = config.providers.get(provider_id).is_some_and(|provider| {
        provider.accounts.iter().any(|account| {
            account.name == account_name
                && account.enabled
                && account.auth_mode == AccountAuthMode::OAuth
        })
    });
    configured
        && bindings.iter().any(|binding| {
            binding.provider_id == provider_id
                && binding.account_name == account_name
                && binding.user_id == user_id
                && binding.state == "active"
        })
}

fn configured_owned_xai_account(
    config: &Config,
    bindings: &[PersonalBinding],
    user_id: &str,
    account_name: &str,
) -> bool {
    configured_owned_provider_account(config, bindings, user_id, XAI_PROVIDER_ID, account_name)
}

fn oauth_gate(config: &Config) -> Result<(), &'static str> {
    if config.oauth.enabled {
        Ok(())
    } else {
        Err("oauth_disabled")
    }
}

fn effective_user_grant_allows_upstream(grant: &crate::personal_upstream::GrantEffect) -> bool {
    matches!(grant, crate::personal_upstream::GrantEffect::Allow)
}

async fn account_available_for_provider(
    state: &AppState,
    config: &Config,
    session: &Principal,
    provider_id: &str,
    account_name: &str,
) -> Result<(), &'static str> {
    oauth_gate(config)?;
    let grant = crate::personal_upstream::effective_grant(&state.database, &session.user_id)
        .await
        .map_err(|_| "oauth_unavailable")?;
    if !effective_user_grant_allows_upstream(&grant) {
        return Err("upstream_access_denied");
    }
    let bindings = crate::personal_upstream::list_bindings(&state.database, Some(&session.user_id))
        .await
        .map_err(|_| "oauth_unavailable")?;
    if !configured_owned_provider_account(
        config,
        &bindings,
        &session.user_id,
        provider_id,
        account_name,
    ) {
        return Err("account_not_available");
    }
    Ok(())
}

async fn account_available(
    state: &AppState,
    config: &Config,
    session: &Principal,
    account_name: &str,
) -> Result<(), &'static str> {
    account_available_for_provider(state, config, session, XAI_PROVIDER_ID, account_name).await
}

async fn xai_flow_authorized(state: &AppState, session: &Principal, account_name: &str) -> bool {
    let Ok(lease) = state.runtime.acquire().await else {
        return false;
    };
    account_available(state, lease.generation().config(), session, account_name)
        .await
        .is_ok()
        && session_is_current(state, session).await
}

async fn watch_xai_flow_authorization(
    state: AppState,
    session: Principal,
    account_name: String,
    cancellation: CancellationToken,
) {
    let mut interval = tokio::time::interval(PERMISSION_RECHECK_INTERVAL);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    interval.tick().await;
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => return,
            _ = interval.tick() => {
                if !xai_flow_authorized(&state, &session, &account_name).await {
                    cancellation.cancel();
                    return;
                }
            }
        }
    }
}

async fn session_is_current(state: &AppState, session: &Principal) -> bool {
    let fence = crate::web_session::SessionFence::from_principal(session);
    let actor = session.user_id.clone();
    state
        .database
        .call(move |connection| fence.validates_current_session(connection, &actor))
        .await
        .is_ok_and(|valid| valid)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FlowForm {
    account_name: String,
    csrf: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct StatusQuery {
    account_name: String,
}

fn csrf_matches(session: &Principal, headers: &HeaderMap, supplied: &str) -> bool {
    crate::web_session::verify_csrf(session, supplied)
        && crate::web_session::csrf_cookie_value(headers)
            .is_some_and(|cookie| crate::web_session::verify_csrf(session, &cookie))
}

fn csrf_rejected(state: &AppState, session: &Principal) {
    state.record_security_event(
        crate::security_events::SecurityEventCode::CsrfRejected,
        crate::security_events::SecurityEventOutcome::Denied,
        crate::security_events::SecurityPrincipalKind::BrowserSession,
        Some(&session.user_id),
    );
}

fn error_response(status: StatusCode, code: &'static str) -> Response {
    (
        status,
        Json(serde_json::json!({"status": "error", "error_code": code})),
    )
        .into_response()
}

fn flow_error(error: FlowError) -> Response {
    let (status, code) = match error {
        FlowError::NotFound => (StatusCode::NOT_FOUND, "flow_not_found"),
        FlowError::Capacity => (StatusCode::TOO_MANY_REQUESTS, "flow_capacity"),
        FlowError::Canceled => (StatusCode::CONFLICT, "flow_canceled"),
        FlowError::NotReady => (StatusCode::CONFLICT, "flow_not_ready"),
        FlowError::UntrustedVerificationUrl => {
            (StatusCode::BAD_GATEWAY, "verification_url_untrusted")
        }
        FlowError::ProviderUnavailable => (StatusCode::BAD_GATEWAY, "provider_unavailable"),
    };
    error_response(status, code)
}

pub(super) async fn start(
    State(state): State<AppState>,
    Extension(session): Extension<Principal>,
    headers: HeaderMap,
    Form(form): Form<FlowForm>,
) -> Response {
    if !csrf_matches(&session, &headers, &form.csrf) {
        csrf_rejected(&state, &session);
        return error_response(StatusCode::FORBIDDEN, "csrf_invalid");
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return error_response(StatusCode::SERVICE_UNAVAILABLE, "oauth_unavailable"),
    };
    if let Err(code) = account_available(
        &state,
        lease.generation().config(),
        &session,
        &form.account_name,
    )
    .await
    {
        return error_response(StatusCode::FORBIDDEN, code);
    }
    if !session_is_current(&state, &session).await {
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    let result = state
        .oauth_flows
        .begin(
            &session.session_id,
            &session.user_id,
            &form.account_name,
            lease.generation().provider_client_pool(),
        )
        .await;
    let view = match result {
        Ok(view) => view,
        Err(error) => return flow_error(error),
    };
    if !session_is_current(&state, &session).await {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &form.account_name);
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    let _ = view;
    Redirect::to("/my-account").into_response()
}

pub(super) async fn poll(
    State(state): State<AppState>,
    Extension(session): Extension<Principal>,
    headers: HeaderMap,
    Form(form): Form<FlowForm>,
) -> Response {
    if !csrf_matches(&session, &headers, &form.csrf) {
        csrf_rejected(&state, &session);
        return error_response(StatusCode::FORBIDDEN, "csrf_invalid");
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return error_response(StatusCode::SERVICE_UNAVAILABLE, "oauth_unavailable"),
    };
    if let Err(code) = account_available(
        &state,
        lease.generation().config(),
        &session,
        &form.account_name,
    )
    .await
    {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &form.account_name);
        return error_response(StatusCode::FORBIDDEN, code);
    }
    if !session_is_current(&state, &session).await {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &form.account_name);
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    if let Err(error) = state.oauth_flows.poll(
        &session.session_id,
        &session.user_id,
        &form.account_name,
        Some((state.clone(), session.clone())),
    ) {
        return flow_error(error);
    }
    Redirect::to("/my-account").into_response()
}

pub(super) async fn status(
    State(state): State<AppState>,
    Extension(session): Extension<Principal>,
    Query(query): Query<StatusQuery>,
) -> Response {
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return error_response(StatusCode::SERVICE_UNAVAILABLE, "oauth_unavailable"),
    };
    if let Err(code) = account_available(
        &state,
        lease.generation().config(),
        &session,
        &query.account_name,
    )
    .await
    {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &query.account_name);
        return error_response(StatusCode::FORBIDDEN, code);
    }
    if !session_is_current(&state, &session).await {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &query.account_name);
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    match state
        .oauth_flows
        .view(&session.session_id, &session.user_id, &query.account_name)
    {
        Some(view) => Json(view).into_response(),
        None => error_response(StatusCode::NOT_FOUND, "flow_not_found"),
    }
}

pub(super) async fn cancel(
    State(state): State<AppState>,
    Extension(session): Extension<Principal>,
    headers: HeaderMap,
    Form(form): Form<FlowForm>,
) -> Response {
    if !csrf_matches(&session, &headers, &form.csrf) {
        csrf_rejected(&state, &session);
        return error_response(StatusCode::FORBIDDEN, "csrf_invalid");
    }
    if !valid_account_name(&form.account_name) {
        return error_response(StatusCode::BAD_REQUEST, "account_not_available");
    }
    state
        .oauth_flows
        .cancel(&session.session_id, &session.user_id, &form.account_name);
    Redirect::to("/my-account").into_response()
}

pub(super) async fn commit(
    State(state): State<AppState>,
    Extension(session): Extension<Principal>,
    headers: HeaderMap,
    Form(form): Form<FlowForm>,
) -> Response {
    if !csrf_matches(&session, &headers, &form.csrf) {
        csrf_rejected(&state, &session);
        return error_response(StatusCode::FORBIDDEN, "csrf_invalid");
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return error_response(StatusCode::SERVICE_UNAVAILABLE, "oauth_unavailable"),
    };
    if let Err(code) = account_available(
        &state,
        lease.generation().config(),
        &session,
        &form.account_name,
    )
    .await
    {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &form.account_name);
        return error_response(StatusCode::FORBIDDEN, code);
    }
    if !session_is_current(&state, &session).await {
        state
            .oauth_flows
            .cancel(&session.session_id, &session.user_id, &form.account_name);
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    let (generation, credentials) = match state.oauth_flows.take_credentials(
        &session.session_id,
        &session.user_id,
        &form.account_name,
    ) {
        Ok(credentials) => credentials,
        Err(error) => return flow_error(error),
    };
    if !session_is_current(&state, &session).await {
        state
            .oauth_flows
            .finish_commit(&session.session_id, &form.account_name, generation);
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    let commit_lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => {
            state
                .oauth_flows
                .finish_commit(&session.session_id, &form.account_name, generation);
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "oauth_unavailable");
        }
    };
    if let Err(code) = account_available(
        &state,
        commit_lease.generation().config(),
        &session,
        &form.account_name,
    )
    .await
    {
        state
            .oauth_flows
            .finish_commit(&session.session_id, &form.account_name, generation);
        return error_response(StatusCode::FORBIDDEN, code);
    }
    if !session_is_current(&state, &session).await {
        state
            .oauth_flows
            .finish_commit(&session.session_id, &form.account_name, generation);
        return error_response(StatusCode::FORBIDDEN, "session_changed");
    }
    let Some(manager) = commit_lease
        .generation()
        .inference()
        .oauth_account_manager()
    else {
        state
            .oauth_flows
            .finish_commit(&session.session_id, &form.account_name, generation);
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "oauth_unavailable");
    };
    let result = manager
        .commit_credentials_for_user(
            &session.user_id,
            XAI_PROVIDER_ID,
            &form.account_name,
            credentials,
        )
        .await;
    state
        .oauth_flows
        .finish_commit(&session.session_id, &form.account_name, generation);
    match result {
        Ok(()) => Redirect::to("/my-account").into_response(),
        Err(crate::oauth::OAuthAccountError::Disabled) => {
            error_response(StatusCode::FORBIDDEN, "oauth_disabled")
        }
        Err(
            crate::oauth::OAuthAccountError::AccountNotConfigured
            | crate::oauth::OAuthAccountError::InvalidOwner,
        ) => error_response(StatusCode::FORBIDDEN, "account_not_available"),
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "credential_commit_failed"),
    }
}

pub(super) async fn account_ui(state: &AppState, session: &Principal, csrf: &str) -> String {
    let Ok(lease) = state.runtime.acquire().await else {
        return String::new();
    };
    let config = lease.generation().config();
    if !matches!(
        crate::personal_upstream::effective_grant(&state.database, &session.user_id).await,
        Ok(crate::personal_upstream::GrantEffect::Allow)
    ) {
        return String::new();
    }
    let Ok(bindings) =
        crate::personal_upstream::list_bindings(&state.database, Some(&session.user_id)).await
    else {
        return String::new();
    };
    let Some(provider) = config.providers.get(XAI_PROVIDER_ID) else {
        return String::new();
    };
    let accounts = provider
        .accounts
        .iter()
        .filter(|account| {
            account.enabled
                && account.auth_mode == AccountAuthMode::OAuth
                && configured_owned_xai_account(config, &bindings, &session.user_id, &account.name)
        })
        .map(|account| account.name.as_str())
        .collect::<Vec<_>>();
    if accounts.is_empty() {
        return String::new();
    }
    let mut html = String::from(
        r#"<section class="panel"><h2>xAI Web UI authorization</h2><p>Authorize only an xAI account already bound to your user. EggPool shows the standard verification URL and user code; the device code and tokens stay server-side.</p>"#,
    );
    for account in accounts {
        let view = state
            .oauth_flows
            .view(&session.session_id, &session.user_id, account);
        html.push_str(&render_account_flow(
            account,
            csrf,
            view.as_ref(),
            config.oauth.enabled,
        ));
    }
    html.push_str("</section>");
    html
}

fn render_account_flow(
    account_name: &str,
    csrf: &str,
    view: Option<&FlowView>,
    oauth_enabled: bool,
) -> String {
    let account = html_escape(account_name);
    let csrf = html_escape(csrf);
    let mut html = format!(r#"<div class="oauth-flow"><h3>{account}</h3>"#);
    if let Some(view) = view {
        html.push_str(&format!(
            "<p>Authorization status: <code>{}</code></p>",
            html_escape(view.status)
        ));
        if let Some(code) = view.error_code {
            html.push_str(&format!(r#"<p role="status">{}</p>"#, html_escape(code)));
        }
        if let (Some(uri), Some(user_code)) = (&view.verification_uri, &view.user_code) {
            let uri = html_escape(uri);
            html.push_str(&format!(
                r#"<p>Open <a href="{uri}" rel="noreferrer" target="_blank">xAI verification page</a> and enter <code>{}</code>.</p>"#,
                html_escape(user_code)
            ));
        }
        if view.status == "pending" || view.status == "slow_down" {
            if oauth_enabled {
                html.push_str(&flow_form("poll", &account, &csrf, "Check status"));
            }
            html.push_str(&flow_form("cancel", &account, &csrf, "Cancel"));
        } else if view.status == "ready_to_commit" {
            if oauth_enabled {
                html.push_str(&flow_form(
                    "commit",
                    &account,
                    &csrf,
                    "Save xAI credentials",
                ));
            }
            html.push_str(&flow_form("cancel", &account, &csrf, "Discard"));
        } else if oauth_enabled {
            html.push_str(&flow_form("start", &account, &csrf, "Start again"));
        }
    } else if oauth_enabled {
        html.push_str(&flow_form(
            "start",
            &account,
            &csrf,
            "Start xAI authorization",
        ));
    } else {
        html.push_str("<p>OAuth login is disabled by the operator.</p>");
    }
    html.push_str("</div>");
    html
}

fn flow_form(action: &str, account: &str, csrf: &str, label: &str) -> String {
    format!(
        r#"<form method="post" action="/api/user/oauth/xai/{action}"><input type="hidden" name="account_name" value="{account}"><input type="hidden" name="csrf" value="{csrf}"><button type="submit">{label}</button></form>"#
    )
}

fn html_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        config::{AccountConfig, ProviderConfig},
        db::{Database, DatabaseConfig, MigrationRunner},
        personal_upstream::PersonalBinding,
        runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    };
    use tokio::sync::oneshot;

    const TEST_SESSION: &str = "session-a";
    const TEST_USER: &str = "user-a";
    const TEST_ACCOUNT: &str = "xai-personal";

    #[derive(Clone, Copy)]
    enum FakeOutcome {
        Authorized,
        Denied,
        Expired,
        WaitForCancel,
    }

    struct FakePoller {
        outcome: FakeOutcome,
        cancel_signal: Option<Arc<Mutex<Option<oneshot::Sender<()>>>>>,
    }

    fn test_credentials() -> OAuthCredentials {
        serde_json::from_value(serde_json::json!({
            "access_token": "access-token-secret",
            "refresh_token": "refresh-token-secret",
            "token_type": "Bearer",
            "scope": "openid offline_access",
            "client_id": "fixture-client-id",
            "issuer": "https://auth.x.ai",
            "expires_at": 1_900_000_000_i64,
            "obtained_at": 1_800_000_000_i64
        }))
        .expect("valid OAuth test credentials")
    }

    impl DevicePoller for FakePoller {
        fn poll<'a>(
            &'a self,
            cancellation: CancellationToken,
            _timeout: Duration,
            progress: ProgressSink,
        ) -> BoxFuture<'a, PollOutcome> {
            match self.outcome {
                FakeOutcome::Authorized => {
                    Box::pin(async { PollOutcome::Authorized(test_credentials()) })
                }
                FakeOutcome::Denied => Box::pin(async { PollOutcome::Denied }),
                FakeOutcome::Expired => Box::pin(async { PollOutcome::Expired }),
                FakeOutcome::WaitForCancel => {
                    let signal = self.cancel_signal.clone();
                    Box::pin(async move {
                        progress(PollProgress::SlowDown);
                        cancellation.cancelled().await;
                        if let Some(signal) = signal
                            && let Some(sender) = lock(&signal).take()
                        {
                            let _ = sender.send(());
                        }
                        PollOutcome::Canceled
                    })
                }
            }
        }
    }

    struct FakeBackend {
        poller: Arc<dyn DevicePoller>,
    }

    impl DeviceBackend for FakeBackend {
        fn start<'a>(
            &'a self,
            _clients: &'a ProviderClientPool,
            _account_name: &'a str,
        ) -> BoxFuture<'a, Result<DeviceOffer, StartFailure>> {
            let poller = self.poller.clone();
            Box::pin(async move {
                Ok(DeviceOffer {
                    verification_uri: "https://accounts.x.ai/device".to_owned(),
                    user_code: "XAI-USER-1234".to_owned(),
                    expires_in: Duration::from_secs(120),
                    interval_seconds: 5,
                    poller,
                })
            })
        }
    }

    fn registry(poller: FakePoller) -> FlowRegistry {
        FlowRegistry::with_backend(Arc::new(FakeBackend {
            poller: Arc::new(poller),
        }))
    }

    async fn begin(registry: &FlowRegistry) -> FlowView {
        registry
            .begin(
                TEST_SESSION,
                TEST_USER,
                TEST_ACCOUNT,
                &ProviderClientPool::new(),
            )
            .await
            .expect("fake device flow starts")
    }

    async fn wait_for_status(
        registry: &FlowRegistry,
        session_id: &str,
        user_id: &str,
        status: &'static str,
    ) -> FlowView {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let notified = registry.inner.changed.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                if let Some(view) = registry.view(session_id, user_id, TEST_ACCOUNT)
                    && view.status == status
                {
                    return view;
                }
                notified.await;
            }
        })
        .await
        .expect("flow status changes at the fixture boundary")
    }

    #[tokio::test]
    async fn presentation_contains_only_the_standard_verification_uri_and_user_code() {
        let registry = registry(FakePoller {
            outcome: FakeOutcome::WaitForCancel,
            cancel_signal: None,
        });
        let view = begin(&registry).await;
        assert_eq!(view.status, "pending");
        assert_eq!(
            view.verification_uri.as_deref(),
            Some("https://accounts.x.ai/device")
        );
        assert_eq!(view.user_code.as_deref(), Some("XAI-USER-1234"));

        let json = serde_json::to_string(&view).expect("safe view serializes");
        let html = render_account_flow(TEST_ACCOUNT, "csrf-token", Some(&view), true);
        for output in [&json, &html] {
            assert!(output.contains("https://accounts.x.ai/device"));
            assert!(output.contains("XAI-USER-1234"));
            assert!(!output.contains("device-code-secret"));
            assert!(!output.contains("verification_uri_complete"));
            assert!(!output.contains("access-token-secret"));
            assert!(!output.contains("refresh-token-secret"));
        }
    }

    #[tokio::test]
    async fn poll_exposes_pending_slow_down_and_cancel_without_leaking_flow_identity() {
        let (cancel_sender, cancel_receiver) = oneshot::channel();
        let signal = Arc::new(Mutex::new(Some(cancel_sender)));
        let registry = registry(FakePoller {
            outcome: FakeOutcome::WaitForCancel,
            cancel_signal: Some(signal),
        });
        let started = begin(&registry).await;
        assert_eq!(started.status, "pending");
        let pending = registry
            .poll(TEST_SESSION, TEST_USER, TEST_ACCOUNT, None)
            .expect("flow poll starts");
        assert_eq!(pending.status, "pending");
        let slowed = wait_for_status(&registry, TEST_SESSION, TEST_USER, "slow_down").await;
        assert_eq!(slowed.retry_after_seconds, Some(5));

        assert!(!registry.cancel("another-session", TEST_USER, TEST_ACCOUNT));
        assert!(registry.cancel(TEST_SESSION, TEST_USER, TEST_ACCOUNT));
        tokio::time::timeout(Duration::from_secs(2), cancel_receiver)
            .await
            .expect("cancel wakes provider poll")
            .expect("poller observed cancellation");
        assert!(
            registry
                .view(TEST_SESSION, TEST_USER, TEST_ACCOUNT)
                .is_none()
        );
    }

    #[tokio::test]
    async fn poll_completion_denial_and_expiry_are_bounded_statuses() {
        for (outcome, status, code) in [
            (FakeOutcome::Denied, "denied", Some("provider_denied")),
            (FakeOutcome::Expired, "expired", Some("device_expired")),
        ] {
            let registry = registry(FakePoller {
                outcome,
                cancel_signal: None,
            });
            begin(&registry).await;
            registry
                .poll(TEST_SESSION, TEST_USER, TEST_ACCOUNT, None)
                .expect("poll starts");
            let view = wait_for_status(&registry, TEST_SESSION, TEST_USER, status).await;
            assert_eq!(view.error_code, code);
            let json = serde_json::to_string(&view).expect("status serializes");
            assert!(!json.contains("access-token-secret"));
            assert!(!json.contains("device-code-secret"));
        }
    }

    #[tokio::test]
    async fn success_stages_credentials_until_the_owner_bound_commit_claim() {
        let registry = registry(FakePoller {
            outcome: FakeOutcome::Authorized,
            cancel_signal: None,
        });
        begin(&registry).await;
        registry
            .poll(TEST_SESSION, TEST_USER, TEST_ACCOUNT, None)
            .expect("poll starts");
        let view = wait_for_status(&registry, TEST_SESSION, TEST_USER, "ready_to_commit").await;
        let json = serde_json::to_string(&view).expect("status serializes");
        assert!(!json.contains("access-token-secret"));
        assert!(!json.contains("refresh-token-secret"));
        assert!(view.verification_uri.is_none());
        assert!(view.user_code.is_none());

        let (generation, credentials) = registry
            .take_credentials(TEST_SESSION, TEST_USER, TEST_ACCOUNT)
            .expect("authorized credentials are available only to the bound owner/session");
        assert_eq!(credentials.access_token(), "access-token-secret");
        registry.finish_commit(TEST_SESSION, TEST_ACCOUNT, generation);
        assert!(
            registry
                .view(TEST_SESSION, TEST_USER, TEST_ACCOUNT)
                .is_none()
        );
    }

    #[tokio::test]
    async fn flow_is_bound_to_exact_session_and_managed_user() {
        let registry = registry(FakePoller {
            outcome: FakeOutcome::WaitForCancel,
            cancel_signal: None,
        });
        begin(&registry).await;
        assert!(
            registry
                .view("another-session", TEST_USER, TEST_ACCOUNT)
                .is_none()
        );
        assert!(
            registry
                .view(TEST_SESSION, "another-user", TEST_ACCOUNT)
                .is_none()
        );
        assert!(!registry.cancel(TEST_SESSION, "another-user", TEST_ACCOUNT));
        // A retry from another authenticated session supersedes the earlier
        // attempt instead of returning a conflict that session cannot clear.
        let replacement = registry
            .begin(
                "another-session",
                TEST_USER,
                TEST_ACCOUNT,
                &ProviderClientPool::new(),
            )
            .await
            .expect("an owned retry supersedes the previous flow");
        assert_eq!(replacement.status, "pending");
        assert!(
            registry
                .view(TEST_SESSION, TEST_USER, TEST_ACCOUNT)
                .is_none(),
            "the superseded session no longer owns a flow"
        );
        assert_eq!(
            registry
                .view("another-session", TEST_USER, TEST_ACCOUNT)
                .expect("the replacement is bound to its own session")
                .status,
            "pending"
        );
    }

    #[test]
    fn verification_hosts_match_the_pinned_profile_case_insensitively() {
        for uri in [
            "https://accounts.x.ai/device",
            "https://auth.x.ai/oauth2/device?user_code=ABCD",
            "https://x.ai/device",
            "https://Accounts.X.AI/device",
            "https://accounts.x.ai./device",
            "https://auth.x.ai:443/device",
        ] {
            assert!(trusted_xai_verification_uri(uri), "{uri} is trusted");
        }
        for uri in [
            "http://accounts.x.ai/device",
            "https://accounts.x.ai.evil.test/device",
            "https://evil.test/device",
            "https://user@accounts.x.ai/device",
            r"https://accounts.x.ai\\@evil.test/device",
            "https://accounts.x.ai:8443/device",
            "https://accounts.x.ai/device#fragment",
            "not a url",
        ] {
            assert!(!trusted_xai_verification_uri(uri), "{uri} is refused");
        }
    }

    #[tokio::test]
    async fn revoking_xai_grant_discards_credentials_staged_for_commit() {
        let directory = tempfile::tempdir().expect("temporary OAuth database directory");
        let database = Database::open(DatabaseConfig {
            path: directory
                .path()
                .join("oauth.sqlite3")
                .to_string_lossy()
                .into_owned(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("test database opens");
        MigrationRunner::new(&database)
            .run()
            .await
            .expect("schema migrates");
        crate::identity::mark_fresh_uninitialized(&database)
            .await
            .expect("fresh state is initialized");
        let (user, _) = crate::identity::bootstrap_admin(
            &database,
            "oauth-revocation-admin",
            "M006 strong admin password passphrase",
        )
        .await
        .expect("managed test user is created");
        crate::identity::enable_managed(&database, &user.id)
            .await
            .expect("managed access is enabled");
        let user_id = user.id.clone();
        database
            .call(move |connection| {
                connection.execute(
                    "UPDATE users SET must_change_password = 0 WHERE id = ?1",
                    [&user_id],
                )?;
                Ok(())
            })
            .await
            .expect("test user can use OAuth");
        crate::personal_upstream::set_instance_grant(
            &database,
            &user.id,
            crate::personal_upstream::GrantEffect::Allow,
        )
        .await
        .expect("OAuth grant is enabled");
        let binding_user = user.id.clone();
        let binding_actor = user.id.clone();
        database
            .call(move |connection| {
                connection.execute(
                    "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state, actor_id) VALUES (?1, ?2, ?3, 'active', ?4)",
                    (XAI_PROVIDER_ID, TEST_ACCOUNT, &binding_user, &binding_actor),
                )?;
                Ok(())
            })
            .await
            .expect("owner-bound xAI account is configured");
        let issued = crate::web_session::issue(&database, &user.id)
            .await
            .expect("browser session is issued");
        let session = crate::web_session::authenticate(&database, &issued.session_token)
            .await
            .expect("session lookup succeeds")
            .expect("browser session is active");

        let mut config = Config::default();
        config.oauth.enabled = true;
        let provider = ProviderConfig {
            id: XAI_PROVIDER_ID.to_owned(),
            base_url: "https://api.x.ai/v1".to_owned(),
            protocols: vec!["openai".to_owned()],
            accounts: vec![AccountConfig {
                name: TEST_ACCOUNT.to_owned(),
                auth_mode: AccountAuthMode::OAuth,
                ..Default::default()
            }],
            ..Default::default()
        };
        config
            .providers
            .insert(XAI_PROVIDER_ID.to_owned(), provider);
        crate::db::AccountRepository::new(&database)
            .sync_from_config(vec![crate::db::AccountConfig {
                name: TEST_ACCOUNT.to_owned(),
                api_key_env: "UNUSED_XAI_OAUTH_TEST_KEY".to_owned(),
                enabled: true,
                weight: 1.0,
                provider_id: XAI_PROVIDER_ID.to_owned(),
            }])
            .await
            .expect("durable xAI account row is created");

        let process = ProcessRuntime::new(database.clone()).expect("process runtime");
        let candidate = RuntimeGenerationFactory::prepare(
            &process,
            config.clone(),
            "xai-grant-revocation-test".to_owned(),
            1,
        )
        .await
        .expect("runtime generation is prepared");
        let manager = Arc::new(RuntimeManager::new(
            candidate.transfer().expect("generation transfer"),
        ));
        let state = AppState::from_runtime(config, database.clone(), Arc::clone(&manager));
        let registry = registry(FakePoller {
            outcome: FakeOutcome::Authorized,
            cancel_signal: None,
        });
        registry
            .begin(
                &session.session_id,
                &session.user_id,
                TEST_ACCOUNT,
                &ProviderClientPool::new(),
            )
            .await
            .expect("xAI device flow starts");
        registry
            .poll(
                &session.session_id,
                &session.user_id,
                TEST_ACCOUNT,
                Some((state, session.clone())),
            )
            .expect("authorized poll starts");
        let ready = wait_for_status(
            &registry,
            &session.session_id,
            &session.user_id,
            "ready_to_commit",
        )
        .await;
        assert_eq!(ready.status, "ready_to_commit");

        crate::personal_upstream::set_instance_grant(
            &database,
            &user.id,
            crate::personal_upstream::GrantEffect::Deny,
        )
        .await
        .expect("OAuth grant is revoked while credentials are staged");
        wait_for_absence(&registry, &session.session_id, &session.user_id).await;
        assert!(matches!(
            registry.take_credentials(&session.session_id, &session.user_id, TEST_ACCOUNT),
            Err(FlowError::NotFound)
        ));

        manager.shutdown();
        let _ = manager
            .close_for_shutdown(Duration::from_secs(5), true)
            .await;
        drop(process);
        database.close().await.expect("database closes");
    }

    async fn wait_for_absence(registry: &FlowRegistry, session_id: &str, user_id: &str) {
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let notified = registry.inner.changed.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                if registry.view(session_id, user_id, TEST_ACCOUNT).is_none() {
                    return;
                }
                notified.await;
            }
        })
        .await
        .expect("grant revocation removes staged credentials at the authorization boundary");
    }

    fn account_config() -> (Config, Vec<PersonalBinding>) {
        let mut config = Config::default();
        let account = AccountConfig {
            name: TEST_ACCOUNT.to_owned(),
            auth_mode: AccountAuthMode::OAuth,
            ..Default::default()
        };
        let mut provider = ProviderConfig::default();
        provider.accounts.push(account);
        config
            .providers
            .insert(XAI_PROVIDER_ID.to_owned(), provider);
        let bindings = vec![PersonalBinding {
            provider_id: XAI_PROVIDER_ID.to_owned(),
            account_name: TEST_ACCOUNT.to_owned(),
            user_id: TEST_USER.to_owned(),
            state: "active".to_owned(),
            env_var: "unused-in-test".to_owned(),
        }];
        (config, bindings)
    }

    #[test]
    fn operator_gate_and_owner_binding_are_required_for_xai_account_selection() {
        let (mut config, bindings) = account_config();
        assert!(configured_owned_xai_account(
            &config,
            &bindings,
            TEST_USER,
            TEST_ACCOUNT
        ));
        assert!(!configured_owned_xai_account(
            &config,
            &bindings,
            "different-user",
            TEST_ACCOUNT
        ));
        assert!(!configured_owned_xai_account(
            &config,
            &bindings,
            TEST_USER,
            "other-account"
        ));
        assert_eq!(oauth_gate(&config), Ok(()));
        config.oauth.enabled = false;
        assert_eq!(oauth_gate(&config), Err("oauth_disabled"));
    }

    #[test]
    fn oauth_account_actions_require_effective_allow_grant() {
        use crate::personal_upstream::GrantEffect;

        assert!(effective_user_grant_allows_upstream(&GrantEffect::Allow));
        assert!(!effective_user_grant_allows_upstream(&GrantEffect::Deny));
        assert!(!effective_user_grant_allows_upstream(&GrantEffect::Inherit));
    }

    #[tokio::test]
    async fn csrf_requires_the_session_token_and_matching_cookie() {
        let directory = tempfile::tempdir().expect("temporary session database directory");
        let database = Database::open(DatabaseConfig {
            path: directory
                .path()
                .join("sessions.sqlite3")
                .to_string_lossy()
                .into_owned(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("test database opens");
        MigrationRunner::new(&database)
            .run()
            .await
            .expect("schema migrates");
        let (user, _) = crate::identity::bootstrap_admin(
            &database,
            "oauth-test-admin",
            "M006 strong admin password passphrase",
        )
        .await
        .expect("managed test user is created");
        let issued = crate::web_session::issue(&database, &user.id)
            .await
            .expect("session is issued");
        let session = crate::web_session::authenticate(&database, &issued.session_token)
            .await
            .expect("session lookup succeeds")
            .expect("session is active");
        let mut headers = HeaderMap::new();
        headers.insert(
            axum::http::header::COOKIE,
            axum::http::HeaderValue::from_str(&format!("eggpool_csrf={}", issued.csrf_token))
                .expect("cookie value is valid"),
        );
        assert!(csrf_matches(&session, &headers, &issued.csrf_token));
        assert!(!csrf_matches(&session, &headers, "wrong-token"));
        headers.insert(
            axum::http::header::COOKIE,
            axum::http::HeaderValue::from_static("eggpool_csrf=wrong-cookie"),
        );
        assert!(!csrf_matches(&session, &headers, &issued.csrf_token));
    }
}
