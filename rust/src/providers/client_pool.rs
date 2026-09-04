//! Immutable provider/account client topology.
//!
//! The pool is built from one validated configuration snapshot.  It owns one
//! direct client per provider and one additional client for each configured
//! account with a resolved proxy.  Routing and account eligibility are
//! deliberately outside this boundary.

use std::collections::BTreeMap;

use serde::Serialize;
use thiserror::Error;

use crate::{Config, config::ConfigError};

use super::{ProviderHttpClient, ProviderHttpConfig, TransportError};

const DEFAULT_PROVIDER_ID: &str = "opencode-go";

/// Errors raised while constructing or looking up a provider client.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ProviderClientPoolError {
    /// A configured provider could not produce its direct client.
    #[error("provider {provider_id:?} transport construction failed: {kind}")]
    ProviderTransport {
        provider_id: String,
        kind: TransportError,
    },
    /// A configured account's proxy could not be resolved.
    #[error("provider {provider_id:?} account {account_name:?} proxy resolution failed")]
    ProxyResolution {
        provider_id: String,
        account_name: String,
    },
    /// A configured account's dedicated proxy client could not be built.
    #[error(
        "provider {provider_id:?} account {account_name:?} proxy transport construction failed: {kind}"
    )]
    AccountTransport {
        provider_id: String,
        account_name: String,
        kind: TransportError,
    },
    /// The requested provider is not part of this immutable pool.
    #[error("No client for provider {provider_id:?}")]
    ProviderNotFound { provider_id: String },
}

/// A safe account-client identity used by operator diagnostics.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AccountClientIdentity {
    pub provider_id: String,
    pub account_name: String,
}

/// Stable, credential-free client-pool diagnostics.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProviderClientPoolSnapshot {
    pub build_count: usize,
    pub providers: BTreeMap<String, usize>,
    pub account_client_count: usize,
    pub account_clients: Vec<AccountClientIdentity>,
}

/// Immutable provider/account client topology for one configuration snapshot.
#[derive(Clone, Debug)]
pub struct ProviderClientPool {
    clients: BTreeMap<String, ProviderHttpClient>,
    account_clients: BTreeMap<(String, String), ProviderHttpClient>,
}

impl Default for ProviderClientPool {
    fn default() -> Self {
        Self::new()
    }
}

impl ProviderClientPool {
    /// Create an empty pool.  This supports the valid no-provider migration
    /// configuration and is also useful for read-only callers.
    pub fn new() -> Self {
        Self {
            clients: BTreeMap::new(),
            account_clients: BTreeMap::new(),
        }
    }

    /// Build the complete topology before it is exposed to the server.
    ///
    /// Construction is all-or-nothing: if a later provider or account fails,
    /// the partially built local pool is dropped before the error returns.
    pub fn from_config(config: &Config) -> Result<Self, ProviderClientPoolError> {
        let mut pool = Self::new();
        for (provider_id, provider) in &config.providers {
            let provider_config = ProviderHttpConfig::try_from(provider).map_err(|kind| {
                ProviderClientPoolError::ProviderTransport {
                    provider_id: provider_id.clone(),
                    kind,
                }
            })?;
            let direct_client =
                ProviderHttpClient::new(provider_config.clone()).map_err(|kind| {
                    ProviderClientPoolError::ProviderTransport {
                        provider_id: provider_id.clone(),
                        kind,
                    }
                })?;
            pool.clients.insert(provider_id.clone(), direct_client);

            for account in &provider.accounts {
                let proxy_url =
                    config
                        .resolve_account_proxy_url(account)
                        .map_err(|_error: ConfigError| {
                            ProviderClientPoolError::ProxyResolution {
                                provider_id: provider_id.clone(),
                                account_name: account.name.clone(),
                            }
                        })?;
                let Some(proxy_url) = proxy_url else {
                    continue;
                };
                let account_client =
                    ProviderHttpClient::new_with_proxy(provider_config.clone(), &proxy_url)
                        .map_err(|kind| ProviderClientPoolError::AccountTransport {
                            provider_id: provider_id.clone(),
                            account_name: account.name.clone(),
                            kind,
                        })?;
                pool.account_clients
                    .insert((provider_id.clone(), account.name.clone()), account_client);
            }
        }
        Ok(pool)
    }

    /// Return the account-specific client when present, otherwise the direct
    /// provider client.  The returned handle is cheap to clone and does not
    /// expose or mutate the pool topology.
    pub fn get_client(
        &self,
        provider_id: &str,
        account_name: Option<&str>,
    ) -> Result<ProviderHttpClient, ProviderClientPoolError> {
        if let Some(account_name) = account_name
            && let Some(client) = self
                .account_clients
                .get(&(provider_id.to_owned(), account_name.to_owned()))
        {
            return Ok(client.clone());
        }
        self.clients.get(provider_id).cloned().ok_or_else(|| {
            ProviderClientPoolError::ProviderNotFound {
                provider_id: provider_id.to_owned(),
            }
        })
    }

    /// Return the legacy default provider client when it is configured.
    pub fn get_default_client(&self) -> Option<ProviderHttpClient> {
        self.clients.get(DEFAULT_PROVIDER_ID).cloned()
    }

    /// Return provider IDs in stable order.
    pub fn providers(&self) -> Vec<String> {
        self.clients.keys().cloned().collect()
    }

    /// Return the operator-facing topology snapshot without secrets or URLs.
    pub fn snapshot(&self) -> ProviderClientPoolSnapshot {
        let mut providers: BTreeMap<String, usize> = self
            .clients
            .keys()
            .map(|provider_id| (provider_id.clone(), 1))
            .collect();
        for (provider_id, _account_name) in self.account_clients.keys() {
            *providers.entry(provider_id.clone()).or_default() += 1;
        }
        let account_clients = self
            .account_clients
            .keys()
            .map(|(provider_id, account_name)| AccountClientIdentity {
                provider_id: provider_id.clone(),
                account_name: account_name.clone(),
            })
            .collect::<Vec<_>>();
        ProviderClientPoolSnapshot {
            build_count: self.clients.len() + self.account_clients.len(),
            providers,
            account_client_count: account_clients.len(),
            account_clients,
        }
    }
}
