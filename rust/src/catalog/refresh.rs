//! Routing-essential catalog discovery and schema-54 persistence.
//!
//! This module deliberately owns only the provider models endpoint.  It uses
//! the M4 client pool, gathers immutable account results concurrently, and
//! mutates the D002 cache only after each result has been classified.

use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    sync::Arc,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, Method};
use serde::Serialize;
use serde_json::{Map, Value};
use thiserror::Error;
use tokio::{sync::Mutex, task::JoinSet};

use crate::{
    Config,
    accounts::{AccountIdentity, AccountRegistry, CredentialStore},
    catalog::{
        AccountCatalogOutcome, AccountCatalogUpdateResult, ModelCatalogCache, ModelInput,
        ProtocolResolutionStatus,
    },
    db::{
        CatalogModelWrite, CatalogPersistenceBatch, CatalogPingWrite, CatalogRefreshWrite,
        CatalogRepository, Database, DatabaseError, ProviderModelWrite,
    },
    providers::{ProviderClientPool, ProviderClientPoolError, ProviderHttpClient, TransportError},
};

const MAX_CATALOG_RESPONSE_BYTES: usize = 10 * 1024 * 1024;
const SUPPORTED_PROTOCOLS: [&str; 2] = ["openai", "anthropic"];
const DEPRECATED_MODEL_ID: &str = "__deprecated__";

/// Stable result names used by refresh diagnostics and durable state.
pub type RefreshOutcome = AccountCatalogOutcome;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CatalogTransportObservation {
    pub account_name: String,
    pub provider_id: String,
    pub latency_ms: u64,
    pub status_code: Option<u16>,
    pub error: Option<String>,
    pub model_count: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AccountCatalogFetch {
    pub account_name: String,
    pub provider_id: String,
    pub outcome: RefreshOutcome,
    pub models: Vec<ModelInput>,
    pub observation: CatalogTransportObservation,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ModelReappearance {
    pub provider_id: String,
    pub account_id: i64,
    pub account_name: String,
    pub canonical_model_id: String,
    pub upstream_model_id: Option<String>,
    pub upstream_protocol: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub enum CatalogModelEvent {
    Reappeared(ModelReappearance),
    Withdrawn(ModelReappearance),
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CatalogRefreshResult {
    pub live_model_ids: Vec<String>,
    pub new_model_ids: Vec<String>,
    pub withdrawn_model_ids: Vec<String>,
    pub changed_provider_keys: Vec<(String, String)>,
    pub outcomes: BTreeMap<String, RefreshOutcome>,
    pub observations: Vec<CatalogTransportObservation>,
    pub events: Vec<CatalogModelEvent>,
    pub refreshed_at: i64,
}

#[derive(Debug, Error)]
pub enum CatalogRefreshError {
    #[error("catalog database operation failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("catalog provider client lookup failed: {0}")]
    Client(#[from] ProviderClientPoolError),
    #[error("catalog response body is invalid JSON")]
    InvalidJson,
    #[error("catalog response has an invalid shape")]
    InvalidShape,
    #[error("catalog request configuration is invalid")]
    InvalidRequest,
    #[error("catalog response failed: {0}")]
    Transport(#[from] TransportError),
    #[error("catalog cache mutation failed: {0}")]
    Cache(#[from] super::CatalogCacheError),
    #[error("catalog header value is invalid")]
    InvalidHeader,
}

#[derive(Debug, Clone)]
struct PendingRefresh {
    provider_id: String,
    refreshed_at: i64,
    outcome: RefreshOutcome,
    model_count: usize,
}

#[derive(Debug, Default)]
struct ServiceState {
    cache: ModelCatalogCache,
    cache_loaded: bool,
    pending_refresh: BTreeMap<String, PendingRefresh>,
    pending_pings: Vec<CatalogTransportObservation>,
}

/// The D003 catalog lifecycle boundary.
pub struct CatalogService {
    config: Config,
    registry: AccountRegistry,
    credentials: CredentialStore,
    database: Database,
    client_pool: ProviderClientPool,
    state: Arc<Mutex<ServiceState>>,
    refresh_lock: Arc<Mutex<()>>,
}

impl std::fmt::Debug for CatalogService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CatalogService")
            .field("providers", &self.client_pool.providers())
            .field("state", &"private")
            .finish()
    }
}

impl CatalogService {
    /// Construct a service using credentials resolved from the validated
    /// configuration.  The credential store is never part of cache snapshots.
    pub fn new(
        config: Config,
        registry: AccountRegistry,
        database: Database,
        client_pool: ProviderClientPool,
    ) -> Self {
        let credentials = CredentialStore::from_config(&config);
        Self::with_credentials(config, registry, database, client_pool, credentials)
    }

    pub fn with_credentials(
        config: Config,
        registry: AccountRegistry,
        database: Database,
        client_pool: ProviderClientPool,
        credentials: CredentialStore,
    ) -> Self {
        let mut cache = ModelCatalogCache::default();
        cache.set_config(&config);
        Self {
            config,
            registry,
            credentials,
            database,
            client_pool,
            state: Arc::new(Mutex::new(ServiceState {
                cache,
                ..ServiceState::default()
            })),
            refresh_lock: Arc::new(Mutex::new(())),
        }
    }

    /// Obtain a cheap, stable D002 snapshot for diagnostics/tests.
    pub async fn cache_snapshot(&self) -> super::CacheSnapshot {
        self.state.lock().await.cache.snapshot()
    }

    pub async fn refresh(&self) -> Result<CatalogRefreshResult, CatalogRefreshError> {
        let _refresh_guard = self.refresh_lock.lock().await;
        self.refresh_locked(None).await
    }

    /// Refresh one enabled account.  Ordinary provider failures become a
    /// stable `Failed` result; unknown/disabled/uncredentialed accounts return
    /// `None` so D006 can distinguish an unavailable recovery operation.
    pub async fn refresh_one_account(
        &self,
        account_name: &str,
    ) -> Result<Option<RefreshOutcome>, CatalogRefreshError> {
        let _refresh_guard = self.refresh_lock.lock().await;
        let Some(identity) = self.registry.get(account_name).cloned() else {
            return Ok(None);
        };
        if !identity.enabled || !identity.has_usable_credentials {
            return Ok(None);
        }
        let result = self.refresh_locked(Some(account_name)).await?;
        Ok(result.outcomes.get(account_name).copied())
    }

    async fn refresh_locked(
        &self,
        only_account: Option<&str>,
    ) -> Result<CatalogRefreshResult, CatalogRefreshError> {
        self.ensure_hydrated().await?;
        let before = self.cache_snapshot().await;
        self.seed_static_models(only_account).await?;

        let identities: Vec<AccountIdentity> = self
            .registry
            .enabled_snapshot()
            .into_iter()
            .filter(|identity| only_account.is_none_or(|name| name == identity.account_name))
            .collect();

        let mut outcomes = BTreeMap::new();
        let mut jobs = JoinSet::new();
        let mut scheduled_accounts = BTreeSet::new();
        for identity in identities {
            let provider = self.config.providers.get(&identity.provider_id).cloned();
            let api_key = self
                .credentials
                .get(&identity.account_name)
                .map(str::to_owned)
                .or_else(|| {
                    provider
                        .as_ref()
                        .filter(|provider| auth_is_none(provider))
                        .map(|_| String::new())
                });
            let Some(api_key) = api_key else {
                outcomes.insert(identity.account_name.clone(), RefreshOutcome::Skipped);
                continue;
            };
            let client = match self
                .client_pool
                .get_client(&identity.provider_id, Some(&identity.account_name))
            {
                Ok(client) => client,
                Err(error) => {
                    tracing::warn!(
                        provider = %identity.provider_id,
                        account = %identity.account_name,
                        "catalog client unavailable"
                    );
                    outcomes.insert(identity.account_name.clone(), RefreshOutcome::Skipped);
                    let _ = error;
                    continue;
                }
            };
            let account_name = identity.account_name.clone();
            let provider_id = identity.provider_id.clone();
            scheduled_accounts.insert(account_name.clone());
            jobs.spawn(async move {
                fetch_account(account_name, provider_id, api_key, client, provider).await
            });
        }

        let mut fetched = BTreeMap::new();
        while let Some(joined) = jobs.join_next().await {
            match joined {
                Ok(fetch) => {
                    outcomes.insert(fetch.account_name.clone(), fetch.outcome);
                    fetched.insert(fetch.account_name.clone(), fetch);
                }
                Err(error) => {
                    tracing::warn!("catalog account task failed: {error}");
                }
            }
        }
        for account_name in scheduled_accounts {
            outcomes
                .entry(account_name)
                .or_insert(RefreshOutcome::Failed);
        }

        let mut observations = Vec::new();
        let mut events = Vec::new();
        for (account_name, fetch) in fetched {
            observations.push(fetch.observation.clone());
            let account_id = self
                .registry
                .get(&account_name)
                .map_or(0, |identity| identity.account_id);
            let result = self.apply_fetch(fetch, account_id, &mut events).await?;
            outcomes.insert(account_name, result);
        }

        self.persist().await?;
        let after = self.cache_snapshot().await;
        let live_model_ids: BTreeSet<String> = after.model_ids.iter().cloned().collect();
        let before_model_ids: BTreeSet<String> = before.model_ids.iter().cloned().collect();
        let before_provider_keys: BTreeSet<(String, String)> =
            before.provider_model_keys.iter().cloned().collect();
        let after_provider_keys: BTreeSet<(String, String)> =
            after.provider_model_keys.iter().cloned().collect();
        let refreshed_at = unix_now();
        Ok(CatalogRefreshResult {
            live_model_ids: live_model_ids.iter().cloned().collect(),
            new_model_ids: live_model_ids
                .difference(&before_model_ids)
                .cloned()
                .collect(),
            withdrawn_model_ids: before_model_ids
                .difference(&live_model_ids)
                .cloned()
                .collect(),
            changed_provider_keys: before_provider_keys
                .symmetric_difference(&after_provider_keys)
                .cloned()
                .collect(),
            outcomes,
            observations,
            events,
            refreshed_at,
        })
    }

    async fn ensure_hydrated(&self) -> Result<(), CatalogRefreshError> {
        let mut state = self.state.lock().await;
        if state.cache_loaded {
            return Ok(());
        }
        state.cache.hydrate_from_db(&self.database).await?;
        state.cache_loaded = true;
        Ok(())
    }

    async fn seed_static_models(
        &self,
        only_account: Option<&str>,
    ) -> Result<(), CatalogRefreshError> {
        let mut state = self.state.lock().await;
        for (provider_id, provider) in &self.config.providers {
            let models = static_models(provider);
            if models.is_empty() {
                continue;
            }
            for account in &provider.accounts {
                if !account.enabled || only_account.is_some_and(|name| name != account.name) {
                    continue;
                }
                state.cache.set_account_provider(&account.name, provider_id);
                state
                    .cache
                    .seed_from_account(&account.name, provider_id, &models)?;
            }
        }
        Ok(())
    }

    async fn apply_fetch(
        &self,
        fetch: AccountCatalogFetch,
        account_id: i64,
        events: &mut Vec<CatalogModelEvent>,
    ) -> Result<RefreshOutcome, CatalogRefreshError> {
        let mut state = self.state.lock().await;
        state.pending_pings.push(fetch.observation.clone());
        if fetch.outcome != RefreshOutcome::SuccessEmpty
            && fetch.outcome != RefreshOutcome::SuccessPartial
            && fetch.outcome != RefreshOutcome::SuccessAuthoritative
        {
            state
                .cache
                .record_outcome(&fetch.account_name, fetch.outcome);
            return Ok(fetch.outcome);
        }
        let before_models = state.cache.models_for_account(&fetch.account_name);
        let before_protocols = before_models
            .iter()
            .filter_map(|model_id| {
                state
                    .cache
                    .get_provider_model(model_id, &fetch.provider_id)
                    .and_then(|row| row.protocol.clone())
                    .map(|protocol| (model_id.clone(), protocol))
            })
            .collect::<BTreeMap<_, _>>();
        let provider_id = fetch.provider_id.clone();
        let models = resolve_models(&self.config, &state.cache, &provider_id, fetch.models);
        let outcome = if models.is_empty() {
            RefreshOutcome::SuccessEmpty
        } else if models.iter().any(|model| model.protocol.is_none()) {
            RefreshOutcome::SuccessPartial
        } else {
            RefreshOutcome::SuccessAuthoritative
        };
        let authoritative = outcome == RefreshOutcome::SuccessAuthoritative;
        let allow_withdrawals = authoritative
            && self.config.models.catalog_withdrawal_policy != "preserve_until_health";
        let update = state.cache.update_from_account(
            &fetch.account_name,
            &provider_id,
            &models,
            authoritative,
            allow_withdrawals,
        )?;
        emit_model_events(
            &state.cache,
            &fetch.account_name,
            &provider_id,
            account_id,
            &before_models,
            &before_protocols,
            &models,
            &update,
            events,
        );
        let refreshed_at = unix_now();
        state.pending_refresh.insert(
            fetch.account_name,
            PendingRefresh {
                provider_id,
                refreshed_at,
                outcome,
                model_count: fetch.observation.model_count,
            },
        );
        Ok(outcome)
    }

    async fn persist(&self) -> Result<(), CatalogRefreshError> {
        let (cache, pending_refresh, pings, account_ids) = {
            let state = self.state.lock().await;
            let accounts = self
                .registry
                .all()
                .map(|identity| (identity.account_name.clone(), identity.account_id));
            (
                state.cache.clone(),
                state.pending_refresh.clone(),
                state.pending_pings.clone(),
                accounts.collect::<BTreeMap<_, _>>(),
            )
        };
        let repository = CatalogRepository::new(&self.database);
        let existing_models = repository.list_models().await?;
        let existing_provider_models = repository.list_provider_models().await?;
        let existing_support = repository.list_account_model_support().await?;
        let mut batch = desired_rows(&cache, &account_ids, &pending_refresh);
        batch.pings = pings
            .into_iter()
            .map(|ping| CatalogPingWrite {
                provider_id: ping.provider_id,
                account_name: ping.account_name,
                latency_ms: ping.latency_ms as i64,
                status_code: ping.status_code.map(i64::from),
                error: ping.error,
                model_count: ping.model_count as i64,
            })
            .collect();
        repository
            .apply_persistence_batch(
                existing_models,
                existing_provider_models,
                existing_support,
                batch,
            )
            .await?;
        let mut state = self.state.lock().await;
        state.pending_refresh.clear();
        state.pending_pings.clear();
        Ok(())
    }
}

async fn fetch_account(
    account_name: String,
    provider_id: String,
    api_key: String,
    client: ProviderHttpClient,
    provider: Option<crate::config::ProviderConfig>,
) -> AccountCatalogFetch {
    let started = Instant::now();
    let observed_account = account_name.clone();
    let observed_provider = provider_id.clone();
    let base_observation = move |status_code, error, model_count| CatalogTransportObservation {
        account_name: observed_account.clone(),
        provider_id: observed_provider.clone(),
        latency_ms: started.elapsed().as_millis() as u64,
        status_code,
        error,
        model_count,
    };
    let Some(provider) = provider else {
        return failed_fetch(
            account_name,
            provider_id,
            base_observation(None, Some("provider unavailable".into()), 0),
        );
    };
    let endpoint = provider.models_endpoint.clone();
    let method_name = endpoint
        .as_ref()
        .map_or(provider.models_method.as_str(), |endpoint| {
            endpoint.method.as_str()
        });
    if method_name.eq_ignore_ascii_case("DISABLED") {
        return AccountCatalogFetch {
            account_name,
            provider_id,
            outcome: RefreshOutcome::Skipped,
            models: Vec::new(),
            observation: base_observation(None, None, 0),
        };
    }
    let method = match method_name.to_ascii_uppercase().as_str() {
        "GET" => Method::GET,
        "POST" => Method::POST,
        _ => {
            return failed_fetch(
                account_name,
                provider_id,
                base_observation(None, Some("invalid method".into()), 0),
            );
        }
    };
    let path = endpoint
        .as_ref()
        .map_or(provider.models_path.as_str(), |endpoint| {
            endpoint.path.as_str()
        });
    let query = endpoint
        .as_ref()
        .map_or_else(String::new, |endpoint| query_string(&endpoint.query));
    let target = format!("{path}{query}");
    let headers = match catalog_headers(&provider, &api_key) {
        Ok(headers) => headers,
        Err(error) => {
            return failed_fetch(
                account_name,
                provider_id,
                base_observation(None, Some(error), 0),
            );
        }
    };
    let body = if method == Method::POST {
        let value = endpoint
            .as_ref()
            .and_then(|endpoint| endpoint.body.as_ref())
            .map(toml_to_json)
            .unwrap_or_else(|| Value::Object(Map::new()));
        match serde_json::to_vec(&value) {
            Ok(bytes) => Bytes::from(bytes),
            Err(_) => {
                return failed_fetch(
                    account_name,
                    provider_id,
                    base_observation(None, Some("invalid request body".into()), 0),
                );
            }
        }
    } else {
        Bytes::new()
    };
    let response = match client.send(method, &target, headers, body).await {
        Ok(response) => response,
        Err(error) => {
            return failed_fetch(
                account_name,
                provider_id,
                base_observation(None, Some(error.to_string()), 0),
            );
        }
    };
    let status = response.status;
    let status_code = Some(status.as_u16());
    let bytes = match read_response(response).await {
        Ok(bytes) => bytes,
        Err(error) => {
            return failed_fetch(
                account_name,
                provider_id,
                base_observation(status_code, Some(error.to_string()), 0),
            );
        }
    };
    if !status.is_success() {
        return failed_fetch(
            account_name,
            provider_id,
            base_observation(status_code, Some(format!("HTTP {}", status.as_u16())), 0),
        );
    }
    let raw: Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(_) => {
            return failed_fetch(
                account_name,
                provider_id,
                base_observation(status_code, Some("Invalid JSON response".into()), 0),
            );
        }
    };
    let Some(object) = raw.as_object() else {
        return failed_fetch(
            account_name,
            provider_id,
            base_observation(
                status_code,
                Some("Invalid model catalog response".into()),
                0,
            ),
        );
    };
    let Some(data) = object.get("data").and_then(Value::as_array) else {
        return failed_fetch(
            account_name,
            provider_id,
            base_observation(
                status_code,
                Some("Invalid model catalog response".into()),
                0,
            ),
        );
    };
    let valid_count = data
        .iter()
        .filter(|item| {
            item.as_object()
                .and_then(|row| row.get("id"))
                .and_then(Value::as_str)
                .is_some_and(|id| !id.trim().is_empty())
        })
        .count();
    if !data.is_empty() && valid_count == 0 {
        return failed_fetch(
            account_name,
            provider_id,
            base_observation(
                status_code,
                Some("Invalid model catalog response".into()),
                0,
            ),
        );
    }
    let models = normalize_models(object);
    AccountCatalogFetch {
        account_name,
        provider_id,
        outcome: if models.is_empty() {
            RefreshOutcome::SuccessEmpty
        } else {
            RefreshOutcome::SuccessAuthoritative
        },
        models,
        observation: base_observation(status_code, None, valid_count),
    }
}

fn failed_fetch(
    account_name: String,
    provider_id: String,
    observation: CatalogTransportObservation,
) -> AccountCatalogFetch {
    AccountCatalogFetch {
        account_name,
        provider_id,
        outcome: RefreshOutcome::Failed,
        models: Vec::new(),
        observation,
    }
}

async fn read_response(
    mut response: crate::providers::ProviderResponse,
) -> Result<Bytes, TransportError> {
    response
        .body
        .read_to_bytes(MAX_CATALOG_RESPONSE_BYTES)
        .await
}

fn catalog_headers(
    provider: &crate::config::ProviderConfig,
    api_key: &str,
) -> Result<HeaderMap, String> {
    let mut headers = HeaderMap::new();
    for header in &provider.headers {
        let value = header.value.clone().or_else(|| {
            header
                .value_env
                .as_deref()
                .and_then(|name| env::var(name).ok())
        });
        if let Some(value) = value {
            let name =
                HeaderName::from_bytes(header.name.as_bytes()).map_err(|_| "invalid header")?;
            let value = HeaderValue::from_str(&value).map_err(|_| "invalid header")?;
            headers.insert(name, value);
        }
    }
    add_auth_header(&mut headers, &provider.auth, api_key)?;
    headers.insert(
        HeaderName::from_static("accept"),
        HeaderValue::from_static("application/json"),
    );
    Ok(headers)
}

fn auth_is_none(provider: &crate::config::ProviderConfig) -> bool {
    provider.auth.mode == "none"
        && provider
            .auth
            .additional
            .iter()
            .all(|auth| auth.mode == "none")
        && provider
            .wire_surfaces
            .values()
            .all(|surface| surface.auth.as_ref().is_none_or(|auth| auth.mode == "none"))
}

fn add_auth_header(
    headers: &mut HeaderMap,
    auth: &crate::config::ProviderAuthConfig,
    api_key: &str,
) -> Result<(), String> {
    if auth.mode != "none" {
        insert_auth(headers, &auth.mode, &auth.header, &auth.scheme, api_key)?;
    }
    for additional in &auth.additional {
        insert_auth(
            headers,
            &additional.mode,
            &additional.header,
            &additional.scheme,
            api_key,
        )?;
    }
    Ok(())
}

fn insert_auth(
    headers: &mut HeaderMap,
    mode: &str,
    name: &str,
    scheme: &str,
    key: &str,
) -> Result<(), String> {
    let value = if matches!(mode, "api_key" | "raw_authorization") {
        key.to_owned()
    } else {
        format!("{scheme} {key}")
    };
    headers.insert(
        HeaderName::from_bytes(name.as_bytes()).map_err(|_| "invalid header")?,
        HeaderValue::from_str(&value).map_err(|_| "invalid header")?,
    );
    Ok(())
}

fn query_string(query: &BTreeMap<String, String>) -> String {
    if query.is_empty() {
        return String::new();
    }
    let values = query
        .iter()
        .map(|(key, value)| format!("{}={}", encode_query(key), encode_query(value)))
        .collect::<Vec<_>>();
    format!("?{}", values.join("&"))
}

fn encode_query(value: &str) -> String {
    value
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

fn toml_to_json(value: &toml::Value) -> Value {
    match value {
        toml::Value::String(value) => Value::String(value.clone()),
        toml::Value::Integer(value) => Value::Number((*value).into()),
        toml::Value::Float(value) => serde_json::Number::from_f64(*value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        toml::Value::Boolean(value) => Value::Bool(*value),
        toml::Value::Datetime(value) => Value::String(value.to_string()),
        toml::Value::Array(values) => Value::Array(values.iter().map(toml_to_json).collect()),
        toml::Value::Table(values) => Value::Object(
            values
                .iter()
                .map(|(key, value)| (key.clone(), toml_to_json(value)))
                .collect(),
        ),
    }
}

fn normalize_models(raw: &Map<String, Value>) -> Vec<ModelInput> {
    let anthropic = raw.get("type").and_then(Value::as_str) == Some("list")
        || ["first_id", "has_more", "last_id"]
            .iter()
            .any(|key| raw.contains_key(*key))
        || raw
            .get("data")
            .and_then(Value::as_array)
            .and_then(|rows| rows.first())
            .and_then(Value::as_object)
            .is_some_and(|row| row.contains_key("display_name") && !row.contains_key("object"));
    raw.get("data")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .filter_map(|row| {
            let model_id = row.get("id").and_then(Value::as_str)?.trim();
            if model_id.is_empty() {
                return None;
            }
            let mut input = ModelInput::new(model_id);
            input.display_name = ["name", "title", "display_name"].iter().find_map(|key| {
                row.get(*key)
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(str::to_owned)
            });
            input.protocol = anthropic.then(|| "anthropic".into());
            input.capabilities = metadata_capabilities(row, input.protocol.as_deref());
            input.source_metadata = row
                .iter()
                .filter(|(key, _)| {
                    !if anthropic {
                        ["id", "display_name", "type"].contains(&key.as_str())
                    } else {
                        ["id", "name", "title", "object"].contains(&key.as_str())
                    }
                })
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            Some(input)
        })
        .collect()
}

fn metadata_capabilities(
    row: &Map<String, Value>,
    protocol: Option<&str>,
) -> super::ModelCapabilities {
    let mut capabilities = super::ModelCapabilities::default();
    for key in ["supports_tools", "supports_vision"] {
        if let Some(value) = row.get(key).and_then(Value::as_bool) {
            if key == "supports_tools" {
                capabilities.supports_tools = Some(value);
            } else {
                capabilities.supports_vision = Some(value);
            }
        }
    }
    if row
        .get("modalities")
        .and_then(Value::as_array)
        .is_some_and(|values| values.iter().any(|value| value.as_str() == Some("image")))
    {
        capabilities.supports_vision = Some(true);
    }
    if row
        .get("supported_parameters")
        .and_then(Value::as_array)
        .is_some_and(|values| values.iter().any(|value| value.as_str() == Some("tools")))
    {
        capabilities.supports_tools = Some(true);
    }
    let reasoning = row.get("reasoning").and_then(Value::as_bool);
    let parameters = row
        .get("supported_parameters")
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_ascii_lowercase)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let options = row.get("reasoning_options");
    let has_controls = parameters
        .iter()
        .any(|value| value.contains("reasoning") || value.contains("thinking"))
        || options.is_some_and(|value| value.is_object());
    if reasoning.is_some() || has_controls {
        capabilities.thinking.status = if reasoning == Some(false) {
            super::CapabilityStatus::Unsupported
        } else {
            super::CapabilityStatus::Supported
        };
        capabilities.thinking.source = "provider_catalog".into();
        capabilities.thinking.native_protocols = protocol.map(str::to_owned).into_iter().collect();
        capabilities.thinking.toggle = super::CapabilityStatus::Supported;
        if parameters
            .iter()
            .any(|value| value == "reasoning_effort" || value == "effort")
        {
            capabilities.thinking.effort = super::CapabilityStatus::Supported;
        }
        if options.is_some() {
            capabilities.thinking.budget = super::CapabilityStatus::Supported;
        }
    }
    capabilities
}

fn static_models(provider: &crate::config::ProviderConfig) -> Vec<ModelInput> {
    provider
        .static_models
        .iter()
        .map(|model| {
            let mut input = ModelInput::new(model.id.clone());
            input.display_name = model
                .display_name
                .clone()
                .or_else(|| Some(model.id.clone()));
            input.protocol = model.protocol.clone();
            input.protocol_source = input.protocol.as_ref().map(|_| "static_config".into());
            input.resolution_status = if input.protocol.is_some() {
                ProtocolResolutionStatus::Resolved
            } else {
                ProtocolResolutionStatus::Unresolved
            };
            input.capabilities.supports_tools = model.supports_tools;
            input.capabilities.supports_vision = model.supports_vision;
            input.limits.context_tokens = model.max_context_tokens;
            input.limits.input_tokens = model.max_input_tokens;
            input.limits.output_tokens = model.max_output_tokens;
            input.limits.context_source = "static_config".into();
            input.limits.input_source = "static_config".into();
            input.limits.output_source = "static_config".into();
            input.source_metadata = model
                .source_metadata
                .iter()
                .map(|(key, value)| (key.clone(), toml_to_json(value)))
                .chain(
                    [
                        (
                            "max_context_tokens",
                            model.max_context_tokens.map(Value::from),
                        ),
                        ("max_input_tokens", model.max_input_tokens.map(Value::from)),
                        (
                            "max_output_tokens",
                            model.max_output_tokens.map(Value::from),
                        ),
                    ]
                    .into_iter()
                    .filter_map(|(key, value)| value.map(|value| (key.into(), value))),
                )
                .chain([(
                    String::from("source"),
                    Value::String(String::from("static_config")),
                )])
                .collect();
            input
        })
        .collect()
}

fn resolve_models(
    config: &Config,
    cache: &ModelCatalogCache,
    provider_id: &str,
    mut models: Vec<ModelInput>,
) -> Vec<ModelInput> {
    let provider = config.providers.get(provider_id);
    for model in &mut models {
        let provider_override =
            provider.and_then(|provider| provider.model_overrides.get(&model.model_id));
        let global_override = config.model_overrides.get(&model.model_id);
        if let Some(protocol) = provider_override
            .and_then(|value| value.protocol.clone())
            .or_else(|| global_override.and_then(|value| value.protocol.clone()))
        {
            model.protocol = Some(protocol);
            model.protocol_source = Some("config".into());
        } else if model.protocol.is_none() {
            let metadata_protocol = model
                .source_metadata
                .get("api_type")
                .or_else(|| model.source_metadata.get("protocol"))
                .and_then(Value::as_str)
                .filter(|value| SUPPORTED_PROTOCOLS.contains(value));
            if let Some(protocol) = metadata_protocol {
                model.protocol = Some(protocol.into());
                model.protocol_source = Some("upstream_metadata".into());
            } else if let Some(protocol) = known_model_protocol(&model.model_id) {
                model.protocol = Some(protocol.into());
                model.protocol_source = Some("family_mapping".into());
            } else if let Some(old) = cache
                .get_provider_model(&model.model_id, provider_id)
                .and_then(|row| row.protocol.clone())
            {
                model.protocol = Some(old);
                model.protocol_source = Some("persisted".into());
            }
        }
        if let Some(provider) = provider
            && model.protocol.as_deref().is_some_and(|protocol| {
                !provider
                    .protocols
                    .iter()
                    .any(|candidate| candidate == protocol)
            })
        {
            model.protocol = None;
            model.protocol_source = Some("provider_mismatch".into());
        }
        resolve_limits(config, provider_id, model);
        apply_capability_overrides(config, provider_id, model);
        model.resolution_status = if model.protocol.is_some() {
            ProtocolResolutionStatus::Resolved
        } else {
            ProtocolResolutionStatus::Unresolved
        };
    }
    models
}

fn known_model_protocol(model_id: &str) -> Option<&'static str> {
    let lower = model_id.to_ascii_lowercase();
    [
        ("gpt-", "openai"),
        ("o1-", "openai"),
        ("o3-", "openai"),
        ("claude-", "anthropic"),
        ("glm-", "openai"),
        ("kimi-", "openai"),
        ("mimo-", "openai"),
        ("deepseek-", "openai"),
        ("minimax-", "anthropic"),
        ("qwen3", "anthropic"),
        ("muse-", "openai"),
        ("longcat-", "openai"),
        ("hy3", "openai"),
    ]
    .into_iter()
    .find(|(prefix, _)| lower.starts_with(prefix))
    .map(|(_, protocol)| protocol)
}

fn positive(value: &Value) -> Option<u64> {
    value
        .as_u64()
        .filter(|value| *value > 0)
        .or_else(|| {
            value
                .as_f64()
                .filter(|value| value.is_finite() && value.fract() == 0.0 && *value > 0.0)
                .map(|value| value as u64)
        })
        .or_else(|| {
            value
                .as_str()
                .and_then(|value| value.trim().parse::<u64>().ok())
                .filter(|value| *value > 0)
        })
}

fn first_limit(model: &ModelInput, keys: &[&str]) -> Option<u64> {
    keys.iter()
        .find_map(|key| model.source_metadata.get(*key).and_then(positive))
}

fn resolve_limits(config: &Config, provider_id: &str, model: &mut ModelInput) {
    let provider_override = config
        .providers
        .get(provider_id)
        .and_then(|provider| provider.model_overrides.get(&model.model_id));
    let global_override = config.model_overrides.get(&model.model_id);
    let resolve =
        |current: Option<u64>, keys: &[&str], provider: Option<u64>, global: Option<u64>| {
            if let Some(value) = provider {
                (Some(value), "provider_override")
            } else if let Some(value) = global {
                (Some(value), "global_override")
            } else if current.is_some() {
                (current, "")
            } else if let Some(value) = first_limit(model, keys) {
                (Some(value), "upstream_metadata")
            } else {
                (None, "unknown")
            }
        };
    let (context, context_source) = resolve(
        model.limits.context_tokens,
        &[
            "max_context_tokens",
            "context_window",
            "context_length",
            "max_position_embeddings",
        ],
        provider_override.and_then(|value| value.max_context_tokens),
        global_override.and_then(|value| value.max_context_tokens),
    );
    let (input, input_source) = resolve(
        model.limits.input_tokens,
        &["max_input_tokens", "input_token_limit"],
        provider_override.and_then(|value| value.max_input_tokens),
        global_override.and_then(|value| value.max_input_tokens),
    );
    let (output, output_source) = resolve(
        model.limits.output_tokens,
        &[
            "max_output_tokens",
            "output_token_limit",
            "max_completion_tokens",
        ],
        provider_override.and_then(|value| value.max_output_tokens),
        global_override.and_then(|value| value.max_output_tokens),
    );
    model.limits.context_tokens = context;
    model.limits.context_source = context_source.into();
    model.limits.input_tokens = input;
    model.limits.input_source = input_source.into();
    model.limits.output_tokens = output;
    model.limits.output_source = output_source.into();
    if let Some(override_config) = provider_override.or(global_override) {
        model.limits.enforce = override_config.enforce_context_limit;
    }
}

fn apply_capability_overrides(config: &Config, provider_id: &str, model: &mut ModelInput) {
    let override_config = config
        .providers
        .get(provider_id)
        .and_then(|provider| provider.model_capabilities.get(&model.model_id))
        .or_else(|| config.model_capabilities.get(&model.model_id));
    let Some(override_config) = override_config else {
        return;
    };
    if let Some(media) = &override_config.multimodal {
        if let Some(image) = &media.image_input {
            if image.base64 == Some(true) || image.url == Some(true) {
                model.capabilities.supports_vision = Some(true);
            }
        }
    }
    if let Some(thinking) = &override_config.thinking {
        if let Some(status) = &thinking.status {
            model.capabilities.thinking.status = parse_capability_status(status);
        }
        if let Some(source) = &thinking.source {
            model.capabilities.thinking.source = source.clone();
        }
        for (target, value) in [
            (&mut model.capabilities.thinking.toggle, &thinking.toggle),
            (&mut model.capabilities.thinking.effort, &thinking.effort),
            (&mut model.capabilities.thinking.budget, &thinking.budget),
        ] {
            if let Some(value) = value {
                *target = parse_capability_status(value);
            }
        }
    }
}

fn parse_capability_status(value: &str) -> super::CapabilityStatus {
    match value {
        "supported" => super::CapabilityStatus::Supported,
        "unsupported" => super::CapabilityStatus::Unsupported,
        "mixed" => super::CapabilityStatus::Mixed,
        "conflicting" => super::CapabilityStatus::Conflicting,
        _ => super::CapabilityStatus::Unknown,
    }
}

#[allow(clippy::too_many_arguments)]
fn emit_model_events(
    cache: &ModelCatalogCache,
    account_name: &str,
    provider_id: &str,
    account_id: i64,
    before_models: &BTreeSet<String>,
    before_protocols: &BTreeMap<String, String>,
    models: &[ModelInput],
    update: &AccountCatalogUpdateResult,
    events: &mut Vec<CatalogModelEvent>,
) {
    for model in models {
        if !before_models.contains(&model.model_id) {
            events.push(CatalogModelEvent::Reappeared(ModelReappearance {
                provider_id: provider_id.into(),
                account_id,
                account_name: account_name.into(),
                canonical_model_id: model.model_id.clone(),
                upstream_model_id: Some(model.model_id.clone()),
                upstream_protocol: model.protocol.clone().unwrap_or_default(),
            }));
        }
    }
    if update.withdrawn_support == 0 {
        return;
    }
    for model_id in before_models {
        if !cache.account_supports_model(account_name, model_id) {
            let protocol = before_protocols.get(model_id).cloned().unwrap_or_default();
            events.push(CatalogModelEvent::Withdrawn(ModelReappearance {
                provider_id: provider_id.into(),
                account_id,
                account_name: account_name.into(),
                canonical_model_id: model_id.clone(),
                upstream_model_id: Some(model_id.clone()),
                upstream_protocol: protocol,
            }));
        }
    }
}

fn desired_rows(
    cache: &ModelCatalogCache,
    account_ids: &BTreeMap<String, i64>,
    pending: &BTreeMap<String, PendingRefresh>,
) -> CatalogPersistenceBatch {
    let mut models = Vec::new();
    let mut persisted_ids = BTreeSet::new();
    for model in cache.get_all_models() {
        if model.model_id == DEPRECATED_MODEL_ID || model.protocol.is_none() {
            continue;
        }
        persisted_ids.insert(model.model_id.clone());
        models.push(CatalogModelWrite {
            model_id: model.model_id.clone(),
            display_name: model.display_name.clone(),
            protocol: model.protocol.clone().unwrap_or_default(),
            capabilities: serde_json::to_value(&model.capabilities)
                .unwrap_or_else(|_| Value::Object(Map::new())),
            source_metadata: model.source_metadata.clone(),
            first_seen_at: model.first_seen_at,
            last_seen_at: model.last_seen_at,
            protocol_source: model.protocol_source.clone(),
        });
    }
    let provider_models = cache
        .get_provider_model_entries()
        .into_iter()
        .filter(|row| persisted_ids.contains(&row.model_id))
        .map(|row| ProviderModelWrite {
            model_id: row.model_id.clone(),
            provider_id: row.provider_id.clone(),
            display_name: row.display_name.clone(),
            protocol: row.protocol.clone(),
            capabilities: serde_json::to_value(&row.capabilities)
                .unwrap_or_else(|_| Value::Object(Map::new())),
            source_metadata: row.source_metadata.clone(),
            protocol_source: row.protocol_source.clone(),
            first_seen_at: row.first_seen_at,
            last_seen_at: row.last_seen_at,
            resolution_status: if row.protocol.is_some() {
                "resolved"
            } else {
                "unresolved"
            }
            .into(),
        })
        .collect();
    let support = cache
        .get_all_models()
        .into_iter()
        .filter(|model| persisted_ids.contains(&model.model_id))
        .flat_map(|model| {
            cache
                .supporting_accounts(&model.model_id)
                .into_iter()
                .filter_map(move |account| {
                    account_ids
                        .get(account)
                        .copied()
                        .map(|id| (id, model.model_id.clone()))
                })
        })
        .collect();
    let refresh = pending
        .iter()
        .filter_map(|(account, pending)| {
            account_ids
                .get(account)
                .copied()
                .map(|id| CatalogRefreshWrite {
                    account_id: id,
                    provider_id: pending.provider_id.clone(),
                    refreshed_at: pending.refreshed_at,
                    outcome: pending.outcome.as_str().into(),
                    model_count: pending.model_count as i64,
                })
        })
        .collect();
    CatalogPersistenceBatch {
        models,
        provider_models,
        support,
        refresh,
        pings: Vec::new(),
    }
}
fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64)
}
