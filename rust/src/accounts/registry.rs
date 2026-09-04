//! Immutable account identity used by the routing domain.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fmt,
};

use serde::Serialize;
use thiserror::Error;

use crate::{
    Config, ConfigError,
    db::{Account, AccountRepository, DatabaseError},
};

/// Public request surfaces understood by the routing domain.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub enum RequestSurface {
    ChatCompletions,
    Responses,
}

impl RequestSurface {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ChatCompletions => "chat_completions",
            Self::Responses => "responses",
        }
    }
}

/// Config-derived quota offsets. These are policy, not durable account identity.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct QuotaOffsets {
    pub five_hour: i64,
    pub weekly: i64,
    pub monthly: i64,
}

/// Immutable, credential-free account data used by routing and diagnostics.
#[derive(Clone, PartialEq, Serialize)]
pub struct AccountIdentity {
    pub account_id: i64,
    pub account_name: String,
    pub provider_id: String,
    pub enabled: bool,
    pub has_usable_credentials: bool,
    pub routing_priority: u32,
    pub weight: f64,
    pub supported_protocols: Vec<String>,
    pub supported_request_surfaces: Vec<RequestSurface>,
    pub quota_offsets: QuotaOffsets,
}

impl fmt::Debug for AccountIdentity {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("AccountIdentity")
            .field("account_id", &self.account_id)
            .field("account_name", &self.account_name)
            .field("provider_id", &self.provider_id)
            .field("enabled", &self.enabled)
            .field("has_usable_credentials", &self.has_usable_credentials)
            .field("routing_priority", &self.routing_priority)
            .field("weight", &self.weight)
            .field("supported_protocols", &self.supported_protocols)
            .field(
                "supported_request_surfaces",
                &self.supported_request_surfaces,
            )
            .field("quota_offsets", &self.quota_offsets)
            .finish()
    }
}

/// Secret account credentials kept outside routing identity objects.
#[derive(Clone, Default)]
pub struct CredentialStore {
    keys: BTreeMap<String, String>,
}

impl fmt::Debug for CredentialStore {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CredentialStore")
            .field(
                "accounts_with_credentials",
                &self.keys.keys().collect::<Vec<_>>(),
            )
            .finish()
    }
}

impl CredentialStore {
    /// Resolve configured inline/environment keys once, outside routing identity.
    pub fn from_config(config: &Config) -> Self {
        let mut store = Self::default();
        for provider in config.providers.values() {
            for account in &provider.accounts {
                let value = account.api_key.clone().or_else(|| {
                    (!account.api_key_env.is_empty())
                        .then(|| env::var(&account.api_key_env).ok())
                        .flatten()
                });
                if let Some(value) = value.filter(|value| !value.trim().is_empty()) {
                    store.keys.insert(account.name.clone(), value);
                }
            }
        }
        store
    }

    pub fn insert(&mut self, account_name: impl Into<String>, key: impl Into<String>) {
        self.keys.insert(account_name.into(), key.into());
    }

    pub fn get(&self, account_name: &str) -> Option<&str> {
        self.keys.get(account_name).map(String::as_str)
    }

    pub fn has_usable(&self, account_name: &str) -> bool {
        self.get(account_name)
            .is_some_and(|key| !key.trim().is_empty())
    }
}

#[derive(Debug, Error)]
pub enum AccountRegistryError {
    #[error("database account {account_name:?} is missing from validated configuration")]
    MissingConfiguredAccount { account_name: String },
    #[error(
        "account {account_name:?} belongs to provider {durable_provider:?}, expected {config_provider:?}"
    )]
    ProviderMismatch {
        account_name: String,
        durable_provider: String,
        config_provider: String,
    },
    #[error("configured account {account_name:?} has no stable durable id")]
    InvalidAccountId { account_name: String },
    #[error("configured account {account_name:?} has invalid weight")]
    InvalidWeight { account_name: String },
    #[error("account {account_name:?} references unknown provider {provider_id:?}")]
    UnknownProvider {
        account_name: String,
        provider_id: String,
    },
    #[error("enabled durable account {account_name:?} is not present in validated configuration")]
    UnknownDurableAccount { account_name: String },
    #[error("enabled account {account_name:?} has no usable credentials")]
    MissingCredential { account_name: String },
    #[error("database error while hydrating account registry: {0}")]
    Database(#[from] DatabaseError),
    #[error("configuration error while building account registry: {0}")]
    Config(#[from] ConfigError),
}

/// Immutable account registry for one validated configuration generation.
#[derive(Debug, Clone)]
pub struct AccountRegistry {
    identities: BTreeMap<String, AccountIdentity>,
    provider_accounts: BTreeMap<String, Vec<String>>,
}

impl AccountRegistry {
    /// Build identities from stable rows returned by `AccountRepository`.
    pub fn from_config(
        config: &Config,
        durable_accounts: &[Account],
        credentials: &CredentialStore,
    ) -> Result<Self, AccountRegistryError> {
        let durable_by_name: BTreeMap<&str, &Account> = durable_accounts
            .iter()
            .map(|account| (account.name.as_str(), account))
            .collect();
        let configured_names: BTreeSet<&str> = config
            .providers
            .values()
            .flat_map(|provider| {
                provider
                    .accounts
                    .iter()
                    .map(|account| account.name.as_str())
            })
            .collect();
        for durable in durable_accounts {
            if durable.enabled && !configured_names.contains(durable.name.as_str()) {
                return Err(AccountRegistryError::UnknownDurableAccount {
                    account_name: durable.name.clone(),
                });
            }
        }
        let mut identities = BTreeMap::new();
        let mut provider_accounts: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for (provider_id, provider) in &config.providers {
            for configured in &provider.accounts {
                let durable = durable_by_name
                    .get(configured.name.as_str())
                    .ok_or_else(|| AccountRegistryError::MissingConfiguredAccount {
                        account_name: configured.name.clone(),
                    })?;
                if durable.id <= 0 {
                    return Err(AccountRegistryError::InvalidAccountId {
                        account_name: configured.name.clone(),
                    });
                }
                if durable.provider_id != *provider_id {
                    return Err(AccountRegistryError::ProviderMismatch {
                        account_name: configured.name.clone(),
                        durable_provider: durable.provider_id.clone(),
                        config_provider: provider_id.clone(),
                    });
                }
                if !configured.weight.is_finite() || configured.weight <= 0.0 {
                    return Err(AccountRegistryError::InvalidWeight {
                        account_name: configured.name.clone(),
                    });
                }
                let has_usable_credentials = (provider.auth.mode == "none"
                    && provider.wire_surfaces.values().all(|surface| {
                        surface.auth.as_ref().is_none_or(|auth| auth.mode == "none")
                    }))
                    || credentials.has_usable(&configured.name);
                if configured.enabled && !has_usable_credentials {
                    return Err(AccountRegistryError::MissingCredential {
                        account_name: configured.name.clone(),
                    });
                }
                let identity = AccountIdentity {
                    account_id: durable.id,
                    account_name: configured.name.clone(),
                    provider_id: provider_id.clone(),
                    enabled: configured.enabled,
                    has_usable_credentials,
                    routing_priority: provider.routing_priority,
                    weight: configured.weight,
                    supported_protocols: provider.protocols.to_vec(),
                    supported_request_surfaces: request_surfaces(provider),
                    quota_offsets: QuotaOffsets {
                        five_hour: configured.five_hour_offset_microdollars,
                        weekly: configured.weekly_offset_microdollars,
                        monthly: configured.monthly_offset_microdollars,
                    },
                };
                provider_accounts
                    .entry(provider_id.clone())
                    .or_default()
                    .push(configured.name.clone());
                identities.insert(configured.name.clone(), identity);
            }
        }
        for names in provider_accounts.values_mut() {
            names.sort();
        }
        Ok(Self {
            identities,
            provider_accounts,
        })
    }

    /// Hydrate stable account ids from the existing schema-54 database.
    pub async fn hydrate_from_db(
        config: &Config,
        repository: &AccountRepository,
        credentials: &CredentialStore,
    ) -> Result<Self, AccountRegistryError> {
        let durable_accounts = repository.list_all().await?;
        Self::from_config(config, &durable_accounts, credentials)
    }

    pub fn get(&self, account_name: &str) -> Option<&AccountIdentity> {
        self.identities.get(account_name)
    }
    pub fn get_by_name(&self, account_name: &str) -> Option<&AccountIdentity> {
        self.get(account_name)
    }
    pub fn get_by_provider(&self, provider_id: &str) -> Vec<&AccountIdentity> {
        self.provider_accounts
            .get(provider_id)
            .into_iter()
            .flatten()
            .filter_map(|name| self.get(name))
            .collect()
    }
    pub fn provider_for_account(&self, account_name: &str) -> Option<&str> {
        self.get(account_name)
            .map(|identity| identity.provider_id.as_str())
    }
    pub fn supports_protocol(&self, account_name: &str, protocol: &str) -> bool {
        self.get(account_name).is_some_and(|identity| {
            identity
                .supported_protocols
                .iter()
                .any(|candidate| candidate == protocol)
        })
    }
    pub fn account_supports_protocol(&self, account_name: &str, protocol: &str) -> bool {
        self.supports_protocol(account_name, protocol)
    }
    pub fn account_supports_protocol_any<I, S>(&self, account_name: &str, protocols: I) -> bool
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        protocols
            .into_iter()
            .any(|protocol| self.supports_protocol(account_name, protocol.as_ref()))
    }
    pub fn supports_request_surface(&self, account_name: &str, surface: RequestSurface) -> bool {
        self.get(account_name)
            .is_some_and(|identity| identity.supported_request_surfaces.contains(&surface))
    }
    pub fn account_supports_request_surface(
        &self,
        account_name: &str,
        surface: RequestSurface,
    ) -> bool {
        self.supports_request_surface(account_name, surface)
    }
    pub fn get_accounts_for_provider(&self, provider_id: &str) -> Vec<&AccountIdentity> {
        self.get_by_provider(provider_id)
    }
    pub fn provider_ids(&self) -> Vec<&str> {
        self.provider_accounts.keys().map(String::as_str).collect()
    }
    pub fn all(&self) -> impl Iterator<Item = &AccountIdentity> {
        self.identities.values()
    }
    pub fn enabled(&self) -> Vec<&AccountIdentity> {
        self.identities
            .values()
            .filter(|identity| identity.enabled)
            .collect()
    }
    pub fn enabled_snapshot(&self) -> Vec<AccountIdentity> {
        self.enabled().into_iter().cloned().collect()
    }
}

fn request_surfaces(provider: &crate::config::ProviderConfig) -> Vec<RequestSurface> {
    let mut surfaces = Vec::new();
    if provider
        .protocols
        .iter()
        .any(|protocol| protocol == "openai")
    {
        surfaces.push(RequestSurface::ChatCompletions);
    }
    if provider.wire_surfaces.keys().any(|surface| {
        matches!(
            surface.as_str(),
            "openai_chat_completions"
                | "openai_responses"
                | "anthropic_messages"
                | "gemini_interactions"
                | "gemini_generate_content"
        )
    }) {
        surfaces.push(RequestSurface::Responses);
    }
    surfaces
}

/// Ensure future users cannot accidentally depend on the credential value via
/// an identity equality/debug snapshot.
#[allow(dead_code)]
fn _credential_boundary_is_separate(_store: &CredentialStore, _identity: &AccountIdentity) {}
