//! Catalog identity, support, mutation, and schema-54 hydration.

use std::{
    collections::{BTreeMap, BTreeSet},
    time::{SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use thiserror::Error;

use crate::{
    Config,
    db::{CatalogRepository, Database, DatabaseError},
};

const DEPRECATED_MODEL_ID: &str = "__deprecated__";
const SUPPORTED_PROTOCOLS: [&str; 2] = ["openai", "anthropic"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum CapabilityStatus {
    Supported,
    Unsupported,
    Unknown,
    Mixed,
    Conflicting,
}

impl CapabilityStatus {
    fn parse(value: Option<&Value>) -> Self {
        match value.and_then(Value::as_str) {
            Some("supported") => Self::Supported,
            Some("unsupported") => Self::Unsupported,
            Some("mixed") => Self::Mixed,
            Some("conflicting") => Self::Conflicting,
            _ => Self::Unknown,
        }
    }
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Supported => "supported",
            Self::Unsupported => "unsupported",
            Self::Unknown => "unknown",
            Self::Mixed => "mixed",
            Self::Conflicting => "conflicting",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ThinkingCapability {
    pub status: CapabilityStatus,
    pub source: String,
    pub native_protocols: Vec<String>,
    pub supported_efforts: Vec<String>,
    pub toggle: CapabilityStatus,
    pub effort: CapabilityStatus,
    pub budget: CapabilityStatus,
    pub budget_tokens_min: Option<u64>,
    pub budget_tokens_max: Option<u64>,
}

impl Default for ThinkingCapability {
    fn default() -> Self {
        Self {
            status: CapabilityStatus::Unknown,
            source: "unknown".into(),
            native_protocols: Vec::new(),
            supported_efforts: Vec::new(),
            toggle: CapabilityStatus::Unknown,
            effort: CapabilityStatus::Unknown,
            budget: CapabilityStatus::Unknown,
            budget_tokens_min: None,
            budget_tokens_max: None,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelCapabilities {
    pub supports_tools: Option<bool>,
    pub supports_vision: Option<bool>,
    pub thinking: ThinkingCapability,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EffectiveModelLimits {
    pub context_tokens: Option<u64>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub enforce: bool,
    pub context_source: String,
    pub input_source: String,
    pub output_source: String,
}

impl Default for EffectiveModelLimits {
    fn default() -> Self {
        Self {
            context_tokens: None,
            input_tokens: None,
            output_tokens: None,
            enforce: true,
            context_source: "unknown".into(),
            input_source: "unknown".into(),
            output_source: "unknown".into(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProtocolResolutionStatus {
    Resolved,
    Unresolved,
}

impl ProtocolResolutionStatus {
    fn parse(value: &str) -> Result<Self, CatalogCacheError> {
        match value {
            "resolved" => Ok(Self::Resolved),
            "unresolved" => Ok(Self::Unresolved),
            other => Err(CatalogCacheError::InvalidResolutionStatus(other.into())),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProviderModelIdentity {
    pub model_id: String,
    pub provider_id: String,
    pub display_name: Option<String>,
    pub protocol: Option<String>,
    pub protocol_source: Option<String>,
    pub resolution_status: ProtocolResolutionStatus,
    pub capabilities: ModelCapabilities,
    pub limits: EffectiveModelLimits,
    pub source_metadata: Value,
    pub first_seen_at: i64,
    pub last_seen_at: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelIdentity {
    pub model_id: String,
    pub display_name: Option<String>,
    pub protocol: Option<String>,
    pub protocol_source: Option<String>,
    pub resolution_status: ProtocolResolutionStatus,
    pub capabilities: ModelCapabilities,
    pub limits: EffectiveModelLimits,
    pub source_metadata: Value,
    pub first_seen_at: i64,
    pub last_seen_at: i64,
    pub first_provider_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelInput {
    pub model_id: String,
    pub display_name: Option<String>,
    pub protocol: Option<String>,
    pub protocol_source: Option<String>,
    pub resolution_status: ProtocolResolutionStatus,
    pub capabilities: ModelCapabilities,
    pub limits: EffectiveModelLimits,
    pub source_metadata: Value,
}

impl ModelInput {
    pub fn new(model_id: impl Into<String>) -> Self {
        Self {
            model_id: model_id.into(),
            display_name: None,
            protocol: None,
            protocol_source: None,
            resolution_status: ProtocolResolutionStatus::Unresolved,
            capabilities: ModelCapabilities::default(),
            limits: EffectiveModelLimits::default(),
            source_metadata: Value::Object(Map::new()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountFreshness {
    pub last_successful_refresh_at: i64,
    pub source: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AccountCatalogOutcome {
    SuccessAuthoritative,
    SuccessEmpty,
    SuccessPartial,
    Failed,
    Skipped,
}

impl AccountCatalogOutcome {
    fn parse(value: &str) -> Result<Self, CatalogCacheError> {
        match value {
            "success_authoritative" => Ok(Self::SuccessAuthoritative),
            "success_empty" => Ok(Self::SuccessEmpty),
            "success_partial" => Ok(Self::SuccessPartial),
            "failed" => Ok(Self::Failed),
            "skipped" => Ok(Self::Skipped),
            other => Err(CatalogCacheError::InvalidOutcome(other.into())),
        }
    }
}

impl AccountCatalogOutcome {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::SuccessAuthoritative => "success_authoritative",
            Self::SuccessEmpty => "success_empty",
            Self::SuccessPartial => "success_partial",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountCatalogUpdateResult {
    pub account_name: String,
    pub provider_id: String,
    pub authoritative: bool,
    pub allow_withdrawals: bool,
    pub added_support: usize,
    pub updated_support: usize,
    pub preserved_support: usize,
    pub withdrawn_support: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CacheSnapshot {
    pub model_ids: Vec<String>,
    pub provider_model_keys: Vec<(String, String)>,
    pub account_support: BTreeMap<String, Vec<String>>,
    pub account_providers: BTreeMap<String, String>,
    pub freshness: BTreeMap<String, AccountFreshness>,
    pub outcomes: BTreeMap<String, AccountCatalogOutcome>,
    pub account_provider_keys: BTreeMap<String, Vec<(String, String)>>,
}

#[derive(Debug, Error)]
pub enum CatalogCacheError {
    #[error("catalog database hydration failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("catalog row has invalid mandatory model identity {0:?}")]
    InvalidModelId(String),
    #[error("catalog row has invalid mandatory provider identity {0:?}")]
    InvalidProviderId(String),
    #[error("catalog row has unsupported protocol {0:?}")]
    InvalidProtocol(String),
    #[error("catalog row has unknown resolution status {0:?}")]
    InvalidResolutionStatus(String),
    #[error("catalog row references unknown account id {0}")]
    UnknownAccount(i64),
    #[error(
        "catalog refresh row for account {account_id} has provider {row_provider:?}, expected {expected_provider:?}"
    )]
    RefreshProviderMismatch {
        account_id: i64,
        row_provider: String,
        expected_provider: String,
    },
    #[error("catalog refresh timestamp is invalid: {0:?}")]
    InvalidTimestamp(String),
    #[error("catalog refresh row has unknown outcome {0:?}")]
    InvalidOutcome(String),
    #[error("catalog update references unknown account {0:?}")]
    UnknownAccountName(String),
}

/// Long-lived catalog cache. Maps use ordered keys so snapshots are stable;
/// routing reads never need to query SQLite.
#[derive(Debug, Clone, Default)]
pub struct ModelCatalogCache {
    models: BTreeMap<String, ModelIdentity>,
    provider_models: BTreeMap<(String, String), ProviderModelIdentity>,
    account_support: BTreeMap<String, BTreeSet<String>>,
    account_providers: BTreeMap<String, String>,
    account_provider_keys: BTreeMap<String, BTreeSet<(String, String)>>,
    freshness: BTreeMap<String, AccountFreshness>,
    outcomes: BTreeMap<String, AccountCatalogOutcome>,
    config: Option<Config>,
}

impl ModelCatalogCache {
    pub fn set_config(&mut self, config: &Config) {
        self.config = Some(config.clone());
    }

    pub async fn hydrate_from_db(
        &mut self,
        database: &Database,
    ) -> Result<usize, CatalogCacheError> {
        let accounts = crate::db::AccountRepository::new(database)
            .list_all()
            .await?;
        let catalog = CatalogRepository::new(database);
        let model_rows = catalog.list_models().await?;
        let provider_rows = catalog.list_provider_models().await?;
        let support_rows = catalog.list_account_model_support().await?;
        let refresh_rows = catalog.list_refresh_state().await?;
        let mut account_by_id = BTreeMap::new();
        for account in accounts {
            if account.id <= 0
                || account.name.trim().is_empty()
                || account.provider_id.trim().is_empty()
            {
                return Err(CatalogCacheError::InvalidProviderId(account.provider_id));
            }
            account_by_id.insert(
                account.id,
                (account.name, account.provider_id, account.enabled),
            );
        }
        for row in model_rows {
            if row.model_id == DEPRECATED_MODEL_ID {
                continue;
            }
            self.models.insert(
                row.model_id.clone(),
                ModelIdentity {
                    model_id: required_id(&row.model_id)?,
                    display_name: row.display_name,
                    protocol: parse_protocol(&row.protocol)?,
                    protocol_source: row.protocol_source,
                    resolution_status: ProtocolResolutionStatus::parse(&row.resolution_status)?,
                    capabilities: parse_capabilities(&row.capabilities),
                    limits: parse_limits(&row.capabilities, &row.source_metadata),
                    source_metadata: advisory_json(&row.source_metadata),
                    first_seen_at: parse_timestamp(&row.first_seen_at)?,
                    last_seen_at: parse_timestamp(&row.last_seen_at)?,
                    first_provider_id: required_id(&row.provider_id)?,
                },
            );
        }
        for row in provider_rows {
            if row.model_id == DEPRECATED_MODEL_ID {
                continue;
            }
            let identity = ProviderModelIdentity {
                model_id: required_id(&row.model_id)?,
                provider_id: required_provider(&row.provider_id)?,
                display_name: row.display_name,
                protocol: parse_optional_protocol(row.protocol.as_deref())?,
                protocol_source: row.protocol_source,
                resolution_status: ProtocolResolutionStatus::parse(&row.resolution_status)?,
                capabilities: parse_capabilities(&row.capabilities),
                limits: parse_limits(&row.capabilities, &row.source_metadata),
                source_metadata: advisory_json(&row.source_metadata),
                first_seen_at: parse_timestamp(&row.first_seen_at)?,
                last_seen_at: parse_timestamp(&row.last_seen_at)?,
            };
            self.provider_models.insert(
                (identity.model_id.clone(), identity.provider_id.clone()),
                identity,
            );
        }
        for (id, (name, provider, enabled)) in &account_by_id {
            if *enabled {
                self.account_providers
                    .insert(name.clone(), provider.clone());
            }
            let _ = id;
        }
        for row in support_rows {
            if row.model_id == DEPRECATED_MODEL_ID || !row.enabled {
                continue;
            }
            let Some((name, _, enabled)) = account_by_id.get(&row.account_id) else {
                return Err(CatalogCacheError::UnknownAccount(row.account_id));
            };
            if *enabled {
                self.account_support
                    .entry(row.model_id)
                    .or_default()
                    .insert(name.clone());
            }
        }
        for row in refresh_rows {
            let Some((name, provider, _)) = account_by_id.get(&row.account_id) else {
                return Err(CatalogCacheError::UnknownAccount(row.account_id));
            };
            if provider != &row.provider_id {
                return Err(CatalogCacheError::RefreshProviderMismatch {
                    account_id: row.account_id,
                    row_provider: row.provider_id,
                    expected_provider: provider.clone(),
                });
            }
            let timestamp = parse_timestamp(&row.last_successful_refresh_at)?;
            if timestamp > 0 {
                self.freshness.insert(
                    name.clone(),
                    AccountFreshness {
                        last_successful_refresh_at: timestamp,
                        source: "durable".into(),
                    },
                );
                self.outcomes.insert(
                    name.clone(),
                    AccountCatalogOutcome::parse(&row.last_outcome)?,
                );
            }
        }
        self.hydrate_legacy_freshness();
        Ok(self.models.len())
    }

    fn hydrate_legacy_freshness(&mut self) {
        let durable: BTreeSet<String> = self.freshness.keys().cloned().collect();
        for (model_id, accounts) in &self.account_support {
            for account in accounts {
                if durable.contains(account) {
                    continue;
                }
                let provider = self.account_providers.get(account);
                let timestamp = provider
                    .and_then(|provider_id| {
                        self.provider_models
                            .get(&(model_id.clone(), provider_id.clone()))
                            .map(|row| row.last_seen_at)
                    })
                    .unwrap_or_else(|| self.models.get(model_id).map_or(0, |row| row.last_seen_at));
                if timestamp > 0 {
                    self.freshness
                        .entry(account.clone())
                        .or_insert(AccountFreshness {
                            last_successful_refresh_at: timestamp,
                            source: "legacy_model_timestamp".into(),
                        });
                }
            }
        }
    }

    pub fn update_from_account(
        &mut self,
        account_name: &str,
        provider_id: &str,
        models: &[ModelInput],
        authoritative: bool,
        allow_withdrawals: bool,
    ) -> Result<AccountCatalogUpdateResult, CatalogCacheError> {
        self.update_from_account_inner(
            account_name,
            provider_id,
            models,
            authoritative,
            allow_withdrawals,
            true,
        )
    }

    /// Apply provider observations without claiming that a live refresh
    /// succeeded.  Static configuration is durable routing knowledge, but it
    /// is not freshness evidence and must not advance catalog refresh state.
    pub fn seed_from_account(
        &mut self,
        account_name: &str,
        provider_id: &str,
        models: &[ModelInput],
    ) -> Result<AccountCatalogUpdateResult, CatalogCacheError> {
        self.update_from_account_inner(account_name, provider_id, models, false, false, false)
    }

    fn update_from_account_inner(
        &mut self,
        account_name: &str,
        provider_id: &str,
        models: &[ModelInput],
        authoritative: bool,
        allow_withdrawals: bool,
        record_refresh: bool,
    ) -> Result<AccountCatalogUpdateResult, CatalogCacheError> {
        if !self.account_providers.contains_key(account_name) {
            return Err(CatalogCacheError::UnknownAccountName(account_name.into()));
        }
        // Validate the complete observation before changing any support or
        // provider metadata.  An authoritative response is allowed to
        // withdraw support only after it has been established as a valid
        // catalog observation; malformed input must remain non-destructive
        // just like failed/partial/empty refreshes.
        for model in models {
            validate_model_input(model)?;
        }
        self.account_providers
            .insert(account_name.into(), provider_id.into());
        let now = unix_now();
        let destructive = authoritative && allow_withdrawals;
        let incoming: BTreeSet<(String, String)> = models
            .iter()
            .map(|model| (model.model_id.clone(), provider_id.into()))
            .collect();
        let prior = self
            .account_provider_keys
            .get(account_name)
            .cloned()
            .unwrap_or_default();
        let mut preserved = 0;
        if !destructive {
            preserved = self
                .account_support
                .values()
                .filter(|accounts| accounts.contains(account_name))
                .count();
        }
        let mut withdrawn = 0;
        if destructive {
            for accounts in self.account_support.values_mut() {
                if accounts.remove(account_name) {
                    withdrawn += 1;
                }
            }
            for stale in prior.difference(&incoming) {
                if !self
                    .account_provider_keys
                    .iter()
                    .any(|(other, keys)| other != account_name && keys.contains(stale))
                {
                    self.provider_models.remove(stale);
                }
            }
            self.account_provider_keys
                .insert(account_name.into(), incoming.clone());
        } else {
            let mut keys = prior;
            keys.extend(incoming.iter().cloned());
            self.account_provider_keys.insert(account_name.into(), keys);
        }
        let mut added = 0;
        let mut updated = 0;
        for model in models {
            let key = (model.model_id.clone(), provider_id.into());
            let merged = self.merge_provider_input(model, provider_id, now, destructive);
            self.provider_models.insert(key.clone(), merged.clone());
            let global = self.models.get_mut(&model.model_id);
            match global {
                None => {
                    self.models
                        .insert(model.model_id.clone(), global_identity(&merged));
                }
                Some(global) => {
                    if global.protocol.is_none() && merged.protocol.is_some() {
                        global.protocol = merged.protocol.clone();
                        global.protocol_source = merged.protocol_source.clone();
                    }
                    global.last_seen_at = now;
                }
            }
            let accounts = self
                .account_support
                .entry(model.model_id.clone())
                .or_default();
            if accounts.insert(account_name.into()) {
                added += 1;
            } else {
                updated += 1;
            }
        }
        if record_refresh {
            self.freshness.insert(
                account_name.into(),
                AccountFreshness {
                    last_successful_refresh_at: now,
                    source: "runtime_success".into(),
                },
            );
            self.outcomes.insert(
                account_name.into(),
                if destructive {
                    AccountCatalogOutcome::SuccessAuthoritative
                } else {
                    AccountCatalogOutcome::SuccessPartial
                },
            );
        }
        Ok(AccountCatalogUpdateResult {
            account_name: account_name.into(),
            provider_id: provider_id.into(),
            authoritative,
            allow_withdrawals,
            added_support: added,
            updated_support: updated,
            preserved_support: preserved,
            withdrawn_support: withdrawn,
        })
    }

    /// Register a configured account before its first catalog response.
    /// This mirrors the Python cache's account/provider association step and
    /// deliberately does not create model support or freshness state.
    pub fn set_account_provider(
        &mut self,
        account_name: impl Into<String>,
        provider_id: impl Into<String>,
    ) {
        self.account_providers
            .insert(account_name.into(), provider_id.into());
    }

    fn merge_provider_input(
        &self,
        input: &ModelInput,
        provider_id: &str,
        now: i64,
        destructive: bool,
    ) -> ProviderModelIdentity {
        let key = (input.model_id.clone(), provider_id.into());
        let old = self.provider_models.get(&key);
        let mut result = ProviderModelIdentity {
            model_id: input.model_id.clone(),
            provider_id: provider_id.into(),
            display_name: input.display_name.clone(),
            protocol: input.protocol.clone(),
            protocol_source: input.protocol_source.clone(),
            resolution_status: input.resolution_status,
            capabilities: input.capabilities.clone(),
            limits: input.limits.clone(),
            source_metadata: input.source_metadata.clone(),
            first_seen_at: old.map_or(now, |row| row.first_seen_at),
            last_seen_at: now,
        };
        if let Some(old) = old {
            let static_fields = old.protocol_source.as_deref() == Some("static_config");
            if static_fields
                && !matches!(
                    result.protocol_source.as_deref(),
                    Some("config" | "static_config")
                )
            {
                result.protocol = old.protocol.clone();
                result.protocol_source = old.protocol_source.clone();
                if result.capabilities.supports_tools.is_none() {
                    result.capabilities.supports_tools = old.capabilities.supports_tools;
                }
                if result.capabilities.supports_vision.is_none() {
                    result.capabilities.supports_vision = old.capabilities.supports_vision;
                }
                if result.limits.context_tokens.is_none() {
                    result.limits.context_tokens = old.limits.context_tokens;
                    result.limits.context_source = old.limits.context_source.clone();
                }
                if result.limits.input_tokens.is_none() {
                    result.limits.input_tokens = old.limits.input_tokens;
                    result.limits.input_source = old.limits.input_source.clone();
                }
                if result.limits.output_tokens.is_none() {
                    result.limits.output_tokens = old.limits.output_tokens;
                    result.limits.output_source = old.limits.output_source.clone();
                }
            }
            if !destructive && old.protocol.is_some() && result.protocol.is_none() {
                result.protocol = old.protocol.clone();
                result.protocol_source = old.protocol_source.clone();
            }
        }
        result
    }

    pub fn seed_static_models(&mut self, config: &Config) -> Result<usize, CatalogCacheError> {
        let mut seeded = 0;
        for (provider_id, provider) in &config.providers {
            for static_model in &provider.static_models {
                let mut input = ModelInput::new(static_model.id.clone());
                input.display_name = static_model.display_name.clone();
                input.protocol = static_model.protocol.clone();
                input.protocol_source = input.protocol.as_ref().map(|_| "static_config".into());
                input.resolution_status = if input.protocol.is_some() {
                    ProtocolResolutionStatus::Resolved
                } else {
                    ProtocolResolutionStatus::Unresolved
                };
                input.capabilities.supports_tools = static_model.supports_tools;
                input.capabilities.supports_vision = static_model.supports_vision;
                input.limits.context_tokens = static_model.max_context_tokens;
                input.limits.input_tokens = static_model.max_input_tokens;
                input.limits.output_tokens = static_model.max_output_tokens;
                input.limits.context_source = "static_config".into();
                input.limits.input_source = "static_config".into();
                input.limits.output_source = "static_config".into();
                for account in &provider.accounts {
                    self.account_providers
                        .insert(account.name.clone(), provider_id.clone());
                    if !self.account_support.contains_key(&input.model_id) {
                        self.account_support
                            .entry(input.model_id.clone())
                            .or_default();
                    }
                    self.account_provider_keys
                        .entry(account.name.clone())
                        .or_default();
                    self.seed_from_account(
                        &account.name,
                        provider_id,
                        std::slice::from_ref(&input),
                    )?;
                    seeded += 1;
                }
            }
        }
        Ok(seeded)
    }

    pub fn prune_unused(&mut self) -> usize {
        let referenced: BTreeSet<String> = self
            .account_support
            .iter()
            .filter(|(_, accounts)| !accounts.is_empty())
            .map(|(model, _)| model.clone())
            .chain(self.provider_models.keys().map(|(model, _)| model.clone()))
            .collect();
        let stale: Vec<String> = self
            .models
            .keys()
            .filter(|model| !referenced.contains(*model))
            .cloned()
            .collect();
        for model in &stale {
            self.models.remove(model);
            self.account_support.remove(model);
        }
        stale.len()
    }

    pub fn has_model(&self, model_id: &str) -> bool {
        self.models.contains_key(model_id)
    }
    pub fn get_model(&self, model_id: &str) -> Option<&ModelIdentity> {
        self.models.get(model_id)
    }
    pub fn get_all_models(&self) -> Vec<&ModelIdentity> {
        self.models.values().collect()
    }
    pub fn get_provider_model(
        &self,
        model_id: &str,
        provider_id: &str,
    ) -> Option<&ProviderModelIdentity> {
        self.provider_models
            .get(&(model_id.into(), provider_id.into()))
    }
    pub fn get_provider_model_entry(
        &self,
        model_id: &str,
        provider_id: &str,
    ) -> Option<&ProviderModelIdentity> {
        self.get_provider_model(model_id, provider_id)
    }
    pub fn get_provider_model_entries(&self) -> Vec<&ProviderModelIdentity> {
        self.provider_models.values().collect()
    }
    pub fn supporting_accounts(&self, model_id: &str) -> Vec<&str> {
        self.account_support
            .get(model_id)
            .into_iter()
            .flatten()
            .map(String::as_str)
            .collect()
    }
    pub fn get_supporting_accounts_for_model(&self, model_id: &str) -> Vec<&str> {
        self.supporting_accounts(model_id)
    }
    pub fn provider_for_account(&self, account_name: &str) -> Option<&str> {
        self.account_providers.get(account_name).map(String::as_str)
    }
    pub fn account_supports_model(&self, account_name: &str, model_id: &str) -> bool {
        self.account_support.contains_key(model_id)
            && self
                .account_support
                .get(model_id)
                .is_some_and(|accounts| accounts.contains(account_name))
    }
    pub fn is_account_model_available(&self, account_name: &str, model_id: &str) -> bool {
        self.account_supports_model(account_name, model_id)
    }
    pub fn account_model_is_fresh(&self, account_name: &str, ttl_seconds: i64, now: i64) -> bool {
        self.freshness.get(account_name).is_some_and(|freshness| {
            now.saturating_sub(freshness.last_successful_refresh_at) <= ttl_seconds.max(0)
        })
    }

    /// Return the models last associated with one account.  This is used by
    /// the refresh event boundary and never performs durable I/O.
    pub fn models_for_account(&self, account_name: &str) -> BTreeSet<String> {
        self.account_support
            .iter()
            .filter(|(_, accounts)| accounts.contains(account_name))
            .map(|(model_id, _)| model_id.clone())
            .collect()
    }

    /// Return the provider/model keys last advertised by one account.
    pub fn provider_keys_for_account(&self, account_name: &str) -> BTreeSet<(String, String)> {
        self.account_provider_keys
            .get(account_name)
            .cloned()
            .unwrap_or_default()
    }
    pub fn freshness(&self, account_name: &str) -> Option<&AccountFreshness> {
        self.freshness.get(account_name)
    }
    pub fn account_outcome(&self, account_name: &str) -> Option<AccountCatalogOutcome> {
        self.outcomes.get(account_name).copied()
    }
    pub fn record_outcome(
        &mut self,
        account_name: impl Into<String>,
        outcome: AccountCatalogOutcome,
    ) {
        self.outcomes.insert(account_name.into(), outcome);
    }
    pub fn snapshot(&self) -> CacheSnapshot {
        CacheSnapshot {
            model_ids: self.models.keys().cloned().collect(),
            provider_model_keys: self.provider_models.keys().cloned().collect(),
            account_support: self
                .account_support
                .iter()
                .map(|(model, accounts)| (model.clone(), accounts.iter().cloned().collect()))
                .collect(),
            account_providers: self.account_providers.clone(),
            freshness: self.freshness.clone(),
            outcomes: self.outcomes.clone(),
            account_provider_keys: self
                .account_provider_keys
                .iter()
                .map(|(account, keys)| (account.clone(), keys.iter().cloned().collect()))
                .collect(),
        }
    }
    pub fn exposed_model_ids(&self) -> Vec<String> {
        self.models
            .keys()
            .filter(|model| model.as_str() != DEPRECATED_MODEL_ID)
            .cloned()
            .collect()
    }
    pub fn parse_model_provider(
        model_id: &str,
        known_providers: &BTreeSet<String>,
    ) -> (String, Option<String>) {
        parse_model_provider(model_id, known_providers)
    }
    pub fn get_effective_limits(
        &self,
        model_id: &str,
        provider_id: Option<&str>,
    ) -> Option<EffectiveModelLimits> {
        if let Some(provider) = provider_id {
            return self
                .provider_models
                .get(&(model_id.into(), provider.into()))
                .map(|entry| self.apply_overrides(&entry.limits, model_id, Some(provider)));
        }
        let rows: Vec<&ProviderModelIdentity> = self
            .provider_models
            .iter()
            .filter(|((candidate_model, _), _)| candidate_model == model_id)
            .map(|(_, entry)| entry)
            .collect();
        if rows.is_empty() {
            return self
                .models
                .get(model_id)
                .map(|entry| self.apply_overrides(&entry.limits, model_id, None));
        }
        let merged = EffectiveModelLimits {
            context_tokens: rows
                .iter()
                .filter_map(|row| row.limits.context_tokens)
                .min(),
            input_tokens: rows.iter().filter_map(|row| row.limits.input_tokens).min(),
            output_tokens: rows.iter().filter_map(|row| row.limits.output_tokens).min(),
            enforce: rows.iter().all(|row| row.limits.enforce),
            context_source: "conservative".into(),
            input_source: "conservative".into(),
            output_source: "conservative".into(),
        };
        Some(self.apply_overrides(&merged, model_id, None))
    }
    pub fn get_provider_capabilities(
        &self,
        model_id: &str,
        provider_id: &str,
    ) -> Option<ModelCapabilities> {
        self.provider_models
            .get(&(model_id.into(), provider_id.into()))
            .map(|entry| {
                self.apply_capability_overrides(&entry.capabilities, model_id, Some(provider_id))
            })
    }

    /// Return conservative capability state for an unsuffixed model. Provider
    /// rows remain available through `get_provider_capabilities` when callers
    /// need the exact host contract.
    pub fn get_effective_capabilities(
        &self,
        model_id: &str,
        provider_id: Option<&str>,
    ) -> Option<ModelCapabilities> {
        if let Some(provider) = provider_id {
            return self.get_provider_capabilities(model_id, provider);
        }
        let rows: Vec<ModelCapabilities> = self
            .provider_models
            .iter()
            .filter(|((candidate_model, _), _)| candidate_model == model_id)
            .map(|((_, provider), entry)| {
                self.apply_capability_overrides(&entry.capabilities, model_id, Some(provider))
            })
            .collect();
        if rows.is_empty() {
            return self
                .models
                .get(model_id)
                .map(|entry| self.apply_capability_overrides(&entry.capabilities, model_id, None));
        }
        let supports_tools = if rows
            .iter()
            .all(|row| row.supports_tools == rows[0].supports_tools)
        {
            rows[0].supports_tools
        } else {
            None
        };
        let supports_vision = if rows
            .iter()
            .all(|row| row.supports_vision == rows[0].supports_vision)
        {
            rows[0].supports_vision
        } else {
            None
        };
        let statuses: BTreeSet<CapabilityStatus> =
            rows.iter().map(|row| row.thinking.status).collect();
        let status = if statuses.len() == 1 {
            rows[0].thinking.status
        } else {
            CapabilityStatus::Mixed
        };
        Some(ModelCapabilities {
            supports_tools,
            supports_vision,
            thinking: ThinkingCapability {
                status,
                source: "aggregate".into(),
                native_protocols: rows
                    .iter()
                    .flat_map(|row| row.thinking.native_protocols.iter().cloned())
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect(),
                supported_efforts: rows
                    .iter()
                    .flat_map(|row| row.thinking.supported_efforts.iter().cloned())
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect(),
                toggle: CapabilityStatus::Unknown,
                effort: CapabilityStatus::Unknown,
                budget: CapabilityStatus::Unknown,
                budget_tokens_min: rows
                    .iter()
                    .filter_map(|row| row.thinking.budget_tokens_min)
                    .max(),
                budget_tokens_max: rows
                    .iter()
                    .filter_map(|row| row.thinking.budget_tokens_max)
                    .min(),
            },
        })
    }

    fn apply_capability_overrides(
        &self,
        base: &ModelCapabilities,
        model_id: &str,
        provider_id: Option<&str>,
    ) -> ModelCapabilities {
        let mut result = base.clone();
        let override_config = provider_id
            .and_then(|provider| {
                self.config
                    .as_ref()
                    .and_then(|config| config.providers.get(provider))
                    .and_then(|provider| provider.model_capabilities.get(model_id))
            })
            .or_else(|| {
                self.config
                    .as_ref()
                    .and_then(|config| config.model_capabilities.get(model_id))
            });
        if let Some(override_config) = override_config {
            if let Some(thinking) = &override_config.thinking {
                if let Some(status) = thinking.status.as_deref() {
                    result.thinking.status = parse_status_str(status);
                }
                if let Some(source) = thinking.source.as_deref() {
                    result.thinking.source = source.into();
                }
                if let Some(toggle) = thinking.toggle.as_deref() {
                    result.thinking.toggle = parse_status_str(toggle);
                }
                if let Some(effort) = thinking.effort.as_deref() {
                    result.thinking.effort = parse_status_str(effort);
                }
                if let Some(budget) = thinking.budget.as_deref() {
                    result.thinking.budget = parse_status_str(budget);
                }
            }
            if let Some(media) = &override_config.multimodal {
                if let Some(image) = &media.image_input {
                    if image.base64 == Some(true) || image.url == Some(true) {
                        result.supports_vision = Some(true);
                    }
                }
            }
        }
        result
    }
    fn apply_overrides(
        &self,
        base: &EffectiveModelLimits,
        model_id: &str,
        provider_id: Option<&str>,
    ) -> EffectiveModelLimits {
        let mut result = base.clone();
        let provider_override = provider_id.and_then(|provider| {
            self.config
                .as_ref()
                .and_then(|config| config.providers.get(provider))
                .and_then(|provider| provider.model_overrides.get(model_id))
        });
        let global_override = self
            .config
            .as_ref()
            .and_then(|config| config.model_overrides.get(model_id));
        if let Some(value) = provider_override
            .and_then(|config| config.max_context_tokens)
            .or_else(|| global_override.and_then(|config| config.max_context_tokens))
        {
            result.context_tokens = Some(value);
            result.context_source =
                if provider_override.is_some_and(|config| config.max_context_tokens.is_some()) {
                    "provider_override"
                } else {
                    "global_override"
                }
                .into();
        }
        if let Some(value) = provider_override
            .and_then(|config| config.max_input_tokens)
            .or_else(|| global_override.and_then(|config| config.max_input_tokens))
        {
            result.input_tokens = Some(value);
            result.input_source =
                if provider_override.is_some_and(|config| config.max_input_tokens.is_some()) {
                    "provider_override"
                } else {
                    "global_override"
                }
                .into();
        }
        if let Some(value) = provider_override
            .and_then(|config| config.max_output_tokens)
            .or_else(|| global_override.and_then(|config| config.max_output_tokens))
        {
            result.output_tokens = Some(value);
            result.output_source =
                if provider_override.is_some_and(|config| config.max_output_tokens.is_some()) {
                    "provider_override"
                } else {
                    "global_override"
                }
                .into();
        }
        if let Some(config) = provider_override.or(global_override) {
            result.enforce = config.enforce_context_limit;
        }
        result
    }
}

fn required_id(value: &str) -> Result<String, CatalogCacheError> {
    if value.trim().is_empty() {
        Err(CatalogCacheError::InvalidModelId(value.into()))
    } else {
        Ok(value.into())
    }
}
fn required_provider(value: &str) -> Result<String, CatalogCacheError> {
    if value.trim().is_empty() {
        Err(CatalogCacheError::InvalidProviderId(value.into()))
    } else {
        Ok(value.into())
    }
}
fn parse_protocol(value: &str) -> Result<Option<String>, CatalogCacheError> {
    if value.trim().is_empty() {
        return Ok(None);
    }
    if SUPPORTED_PROTOCOLS.contains(&value) {
        Ok(Some(value.into()))
    } else {
        Err(CatalogCacheError::InvalidProtocol(value.into()))
    }
}
fn parse_optional_protocol(value: Option<&str>) -> Result<Option<String>, CatalogCacheError> {
    value.map_or(Ok(None), parse_protocol)
}
fn validate_model_input(input: &ModelInput) -> Result<(), CatalogCacheError> {
    required_id(&input.model_id)?;
    if let Some(protocol) = &input.protocol {
        if !SUPPORTED_PROTOCOLS.contains(&protocol.as_str()) {
            return Err(CatalogCacheError::InvalidProtocol(protocol.clone()));
        }
    }
    Ok(())
}
fn advisory_json(raw: &str) -> Value {
    serde_json::from_str(raw)
        .ok()
        .filter(Value::is_object)
        .unwrap_or_else(|| Value::Object(Map::new()))
}
fn parse_status_str(value: &str) -> CapabilityStatus {
    CapabilityStatus::parse(Some(&Value::String(value.into())))
}
fn parse_capabilities(raw: &str) -> ModelCapabilities {
    let value = advisory_json(raw);
    let object = value.as_object();
    let mut result = ModelCapabilities {
        supports_tools: object
            .and_then(|o| o.get("supports_tools"))
            .and_then(Value::as_bool),
        supports_vision: object
            .and_then(|o| o.get("supports_vision"))
            .and_then(Value::as_bool),
        thinking: ThinkingCapability::default(),
    };
    if let Some(thinking) = object
        .and_then(|o| o.get("thinking"))
        .and_then(Value::as_object)
    {
        result.thinking.status = CapabilityStatus::parse(thinking.get("status"));
        result.thinking.source = thinking
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .into();
        result.thinking.toggle = CapabilityStatus::parse(thinking.get("toggle"));
        result.thinking.effort = CapabilityStatus::parse(thinking.get("effort"));
        result.thinking.budget = CapabilityStatus::parse(thinking.get("budget"));
        result.thinking.budget_tokens_min =
            thinking.get("budget_tokens_min").and_then(Value::as_u64);
        result.thinking.budget_tokens_max =
            thinking.get("budget_tokens_max").and_then(Value::as_u64);
        if let Some(values) = thinking.get("native_protocols").and_then(Value::as_array) {
            result.thinking.native_protocols = values
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect();
        }
        if let Some(values) = thinking.get("supported_efforts").and_then(Value::as_array) {
            result.thinking.supported_efforts = values
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect();
        }
    }
    result
}
fn parse_limits(capabilities: &str, source_metadata: &str) -> EffectiveModelLimits {
    let caps = advisory_json(capabilities);
    let metadata = advisory_json(source_metadata);
    let find = |keys: &[&str]| {
        keys.iter()
            .find_map(|key| caps.get(*key).or_else(|| metadata.get(*key)))
            .and_then(parse_positive_u64)
    };
    EffectiveModelLimits {
        context_tokens: find(&[
            "max_context_tokens",
            "context_window",
            "context_length",
            "max_position_embeddings",
        ]),
        input_tokens: find(&["max_input_tokens", "input_token_limit"]),
        output_tokens: find(&[
            "max_output_tokens",
            "output_token_limit",
            "max_completion_tokens",
        ]),
        ..Default::default()
    }
}
fn parse_positive_u64(value: &Value) -> Option<u64> {
    value.as_u64().filter(|value| *value > 0).or_else(|| {
        value
            .as_str()
            .and_then(|value| value.trim().parse().ok())
            .filter(|value: &u64| *value > 0)
    })
}
fn global_identity(row: &ProviderModelIdentity) -> ModelIdentity {
    ModelIdentity {
        model_id: row.model_id.clone(),
        display_name: row.display_name.clone(),
        protocol: row.protocol.clone(),
        protocol_source: row.protocol_source.clone(),
        resolution_status: row.resolution_status,
        capabilities: row.capabilities.clone(),
        limits: row.limits.clone(),
        source_metadata: row.source_metadata.clone(),
        first_seen_at: row.first_seen_at,
        last_seen_at: row.last_seen_at,
        first_provider_id: row.provider_id.clone(),
    }
}
fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64)
}

/// Python-compatible final-slash provider qualifier parsing.
pub fn parse_model_provider(
    model_id: &str,
    known_providers: &BTreeSet<String>,
) -> (String, Option<String>) {
    let normalized = model_id.trim();
    let Some((base, candidate)) = normalized.rsplit_once('/') else {
        return (normalized.into(), None);
    };
    if base.is_empty() || candidate.is_empty() || !known_providers.contains(candidate) {
        return (normalized.into(), None);
    }
    (base.into(), Some(candidate.into()))
}

fn parse_timestamp(value: &str) -> Result<i64, CatalogCacheError> {
    let value = value.trim();
    if let Ok(epoch) = value.parse::<i64>() {
        return Ok(epoch);
    }
    let parts: Vec<i64> = value
        .split(['-', ' ', ':'])
        .filter_map(|part| part.parse().ok())
        .collect();
    if parts.len() < 6 {
        return Err(CatalogCacheError::InvalidTimestamp(value.into()));
    }
    let (year, month, day, hour, minute, second) =
        (parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]);
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return Err(CatalogCacheError::InvalidTimestamp(value.into()));
    }
    Ok(days_from_civil(year, month, day) * 86_400 + hour * 3_600 + minute * 60 + second)
}

// Howard Hinnant's proleptic-Gregorian conversion, kept local to avoid a new
// date dependency in the migration candidate.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month_prime = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}
