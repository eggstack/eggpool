//! Immutable provider/account client topology.
//!
//! The pool is built from one validated configuration snapshot.  It owns one
//! direct client per provider and one additional client for each configured
//! account with a resolved proxy.  Routing and account eligibility are
//! deliberately outside this boundary.

use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use arc_swap::ArcSwapOption;
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
    /// The generation has explicitly closed this pool.
    #[error("provider client pool is closed")]
    Closed,
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

/// Result of the idempotent generation-owned pool close operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProviderClientPoolCloseReport {
    pub closed_now: bool,
    pub close_count: usize,
}

#[derive(Debug)]
struct ProviderClientPoolInner {
    topology: ArcSwapOption<ClientTopology>,
    close_count: AtomicUsize,
}

#[derive(Debug, Default)]
struct ClientTopology {
    clients: BTreeMap<String, ProviderHttpClient>,
    account_clients: BTreeMap<String, BTreeMap<String, ProviderHttpClient>>,
}

/// Immutable provider/account client topology for one configuration snapshot.
#[derive(Clone)]
pub struct ProviderClientPool {
    inner: Arc<ProviderClientPoolInner>,
}

impl std::fmt::Debug for ProviderClientPool {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProviderClientPool")
            .field("providers", &self.providers())
            .field("closed", &self.is_closed())
            .finish()
    }
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
            inner: Arc::new(ProviderClientPoolInner {
                topology: ArcSwapOption::from(Some(Arc::new(ClientTopology::default()))),
                close_count: AtomicUsize::new(0),
            }),
        }
    }

    /// Build the complete topology before it is exposed to the server.
    ///
    /// Construction is all-or-nothing: if a later provider or account fails,
    /// the partially built local pool is dropped before the error returns.
    pub fn from_config(config: &Config) -> Result<Self, ProviderClientPoolError> {
        let mut topology = ClientTopology::default();
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
            topology.clients.insert(provider_id.clone(), direct_client);

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
                topology
                    .account_clients
                    .entry(provider_id.clone())
                    .or_default()
                    .insert(account.name.clone(), account_client);
            }
        }
        let pool = Self::new();
        pool.inner.topology.store(Some(Arc::new(topology)));
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
        let topology = self
            .inner
            .topology
            .load_full()
            .ok_or(ProviderClientPoolError::Closed)?;
        if let Some(account_name) = account_name
            && let Some(client) = topology
                .account_clients
                .get(provider_id)
                .and_then(|clients| clients.get(account_name))
        {
            return Ok(client.clone());
        }
        topology.clients.get(provider_id).cloned().ok_or_else(|| {
            ProviderClientPoolError::ProviderNotFound {
                provider_id: provider_id.to_owned(),
            }
        })
    }

    /// Return the legacy default provider client when it is configured.
    pub fn get_default_client(&self) -> Option<ProviderHttpClient> {
        self.inner
            .topology
            .load_full()?
            .clients
            .get(DEFAULT_PROVIDER_ID)
            .cloned()
    }

    /// Return provider IDs in stable order.
    pub fn providers(&self) -> Vec<String> {
        let Some(topology) = self.inner.topology.load_full() else {
            return Vec::new();
        };
        topology.clients.keys().cloned().collect()
    }

    /// Return the operator-facing topology snapshot without secrets or URLs.
    pub fn snapshot(&self) -> ProviderClientPoolSnapshot {
        let Some(topology) = self.inner.topology.load_full() else {
            return ProviderClientPoolSnapshot {
                build_count: 0,
                providers: BTreeMap::new(),
                account_client_count: 0,
                account_clients: Vec::new(),
            };
        };
        let clients = &topology.clients;
        let account_clients = &topology.account_clients;
        let mut providers: BTreeMap<String, usize> = clients
            .keys()
            .map(|provider_id| (provider_id.clone(), 1))
            .collect();
        for (provider_id, accounts) in account_clients {
            *providers.entry(provider_id.clone()).or_default() += accounts.len();
        }
        let account_clients = account_clients
            .iter()
            .flat_map(|(provider_id, accounts)| {
                accounts
                    .keys()
                    .map(move |account_name| AccountClientIdentity {
                        provider_id: provider_id.clone(),
                        account_name: account_name.clone(),
                    })
            })
            .collect::<Vec<_>>();
        ProviderClientPoolSnapshot {
            build_count: clients.len() + account_clients.len(),
            providers,
            account_client_count: account_clients.len(),
            account_clients,
        }
    }

    /// Mark this generation's transports closed and drop the pool-owned
    /// client handles.  A request that already cloned an individual client
    /// may finish under the R004 lease boundary; no new submission can enter
    /// through this pool.  Repeated calls are harmless and report the same
    /// monotonic close count.
    pub fn close(&self) -> ProviderClientPoolCloseReport {
        let closed_now = self.inner.topology.swap(None).is_some();
        if closed_now {
            self.inner.close_count.fetch_add(1, Ordering::AcqRel);
        }
        ProviderClientPoolCloseReport {
            closed_now,
            close_count: self.inner.close_count.load(Ordering::Acquire),
        }
    }

    pub fn is_closed(&self) -> bool {
        self.inner.topology.load().is_none()
    }

    pub fn close_count(&self) -> usize {
        self.inner.close_count.load(Ordering::Acquire)
    }
}
