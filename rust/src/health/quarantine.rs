//! Exact-key bounded model quarantine state machine.

use std::{
    collections::BTreeMap,
    fmt,
    sync::{Arc, Mutex},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::MAX_NONTERMINAL_BACKOFF_SECONDS;

pub const DEFAULT_SUSPECTED_TTL: f64 = 120.0;
pub const DEFAULT_QUARANTINED_TTL: f64 = 300.0;
pub const DEFAULT_PROMOTION_THRESHOLD: u32 = 2;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QuarantineState {
    Healthy,
    Suspected,
    Quarantined,
    TerminalWithdrawn,
}

impl QuarantineState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Healthy => "healthy",
            Self::Suspected => "suspected",
            Self::Quarantined => "quarantined",
            Self::TerminalWithdrawn => "terminal_withdrawn",
        }
    }
}

impl TryFrom<&str> for QuarantineState {
    type Error = ();

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Ok(match value {
            "healthy" => Self::Healthy,
            "suspected" => Self::Suspected,
            "quarantined" => Self::Quarantined,
            "terminal_withdrawn" => Self::TerminalWithdrawn,
            _ => return Err(()),
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceProvenance {
    RuntimeHttp,
    ProviderCatalog,
    ModelInfo,
    ManualOverride,
    OperatorAction,
    MigrationLegacy,
}

impl EvidenceProvenance {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::RuntimeHttp => "runtime_http",
            Self::ProviderCatalog => "provider_catalog",
            Self::ModelInfo => "model_info",
            Self::ManualOverride => "manual_override",
            Self::OperatorAction => "operator_action",
            Self::MigrationLegacy => "migration_legacy",
        }
    }

    pub const fn is_authoritative(self) -> bool {
        matches!(
            self,
            Self::ProviderCatalog | Self::ManualOverride | Self::OperatorAction
        )
    }
}

impl TryFrom<&str> for EvidenceProvenance {
    type Error = ();

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Ok(match value {
            "runtime_http" => Self::RuntimeHttp,
            "provider_catalog" => Self::ProviderCatalog,
            "model_info" => Self::ModelInfo,
            "manual_override" => Self::ManualOverride,
            "operator_action" => Self::OperatorAction,
            "migration_legacy" => Self::MigrationLegacy,
            _ => return Err(()),
        })
    }
}

/// Stable exact scope identity. The digest matches Python's 32-character
/// SHA-256 prefix over the colon-delimited identity fields.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct QuarantineKey {
    pub provider_id: String,
    pub account_id: String,
    pub canonical_model_id: String,
    pub upstream_model_id: Option<String>,
    pub upstream_protocol: String,
    pub digest: String,
}

impl QuarantineKey {
    pub fn new(
        provider_id: &str,
        account_id: &str,
        canonical_model_id: &str,
        upstream_model_id: Option<&str>,
        upstream_protocol: &str,
    ) -> Self {
        let identity = format!(
            "{provider_id}:{account_id}:{canonical_model_id}:{}:{upstream_protocol}",
            upstream_model_id.unwrap_or_default()
        );
        let digest = Sha256::digest(identity.as_bytes())
            .iter()
            .take(16)
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Self {
            provider_id: provider_id.to_owned(),
            account_id: account_id.to_owned(),
            canonical_model_id: canonical_model_id.to_owned(),
            upstream_model_id: upstream_model_id.map(str::to_owned),
            upstream_protocol: upstream_protocol.to_owned(),
            digest,
        }
    }
}

impl fmt::Display for QuarantineKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.digest)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuarantineEntry {
    pub key: QuarantineKey,
    pub state: QuarantineState,
    pub evidence_provenance: EvidenceProvenance,
    pub reason: String,
    pub first_observed: f64,
    pub last_observed: f64,
    pub observation_count: u32,
    pub expiry: Option<f64>,
    pub cleared_at: Option<f64>,
    pub clear_reason: Option<String>,
    pub last_status_code: Option<u16>,
    pub last_error_class: Option<String>,
}

#[derive(Clone)]
pub struct ModelQuarantine {
    entries: Arc<Mutex<BTreeMap<QuarantineKey, QuarantineEntry>>>,
    pub suspected_ttl: f64,
    pub quarantined_ttl: f64,
    pub promotion_threshold: u32,
}

impl fmt::Debug for ModelQuarantine {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ModelQuarantine")
            .field(
                "entry_count",
                &self.entries.lock().expect("quarantine lock").len(),
            )
            .field("suspected_ttl", &self.suspected_ttl)
            .field("quarantined_ttl", &self.quarantined_ttl)
            .field("promotion_threshold", &self.promotion_threshold)
            .finish()
    }
}

impl Default for ModelQuarantine {
    fn default() -> Self {
        Self {
            entries: Arc::new(Mutex::new(BTreeMap::new())),
            suspected_ttl: DEFAULT_SUSPECTED_TTL,
            quarantined_ttl: DEFAULT_QUARANTINED_TTL,
            promotion_threshold: DEFAULT_PROMOTION_THRESHOLD,
        }
    }
}

impl ModelQuarantine {
    pub fn key(
        &self,
        provider_id: &str,
        account_id: &str,
        canonical_model_id: &str,
        upstream_model_id: Option<&str>,
        upstream_protocol: &str,
    ) -> QuarantineKey {
        QuarantineKey::new(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
    }

    pub fn is_model_quarantined(&self, key: &QuarantineKey, now: f64) -> bool {
        let entries = self.entries.lock().expect("quarantine lock");
        let Some(entry) = entries.get(key) else {
            return false;
        };
        matches!(
            entry.state,
            QuarantineState::Suspected | QuarantineState::Quarantined
        ) && entry.expiry.is_none_or(|expiry| now < expiry)
    }

    pub fn is_model_quarantined_for(
        &self,
        provider_id: &str,
        account_id: &str,
        canonical_model_id: &str,
        upstream_model_id: Option<&str>,
        upstream_protocol: &str,
        now: f64,
    ) -> bool {
        self.is_model_quarantined(
            &self.key(
                provider_id,
                account_id,
                canonical_model_id,
                upstream_model_id,
                upstream_protocol,
            ),
            now,
        )
    }

    pub fn record_observation(
        &self,
        key: QuarantineKey,
        provenance: EvidenceProvenance,
        reason: impl Into<String>,
        status_code: Option<u16>,
        error_class: Option<String>,
        now: f64,
    ) -> QuarantineEntry {
        let reason = reason.into();
        let mut entries = self.entries.lock().expect("quarantine lock");
        let expired = entries
            .get(&key)
            .is_some_and(|entry| entry.expiry.is_some_and(|expiry| now >= expiry));
        if expired {
            entries.remove(&key);
        }
        if entries
            .get(&key)
            .is_some_and(|entry| entry.state == QuarantineState::Healthy)
        {
            entries.remove(&key);
        }
        if let Some(entry) = entries.get_mut(&key) {
            if matches!(entry.state, QuarantineState::TerminalWithdrawn) {
                return entry.clone();
            }
            entry.last_observed = now;
            entry.observation_count = entry.observation_count.saturating_add(1);
            entry.reason = reason;
            entry.evidence_provenance = provenance;
            entry.last_status_code = status_code;
            entry.last_error_class = error_class;
            if entry.state == QuarantineState::Suspected
                && entry.observation_count >= self.promotion_threshold
            {
                entry.state = QuarantineState::Quarantined;
                entry.expiry =
                    Some(now + self.quarantined_ttl.min(MAX_NONTERMINAL_BACKOFF_SECONDS));
            }
            return entry.clone();
        }
        let entry = QuarantineEntry {
            key: key.clone(),
            state: QuarantineState::Suspected,
            evidence_provenance: provenance,
            reason,
            first_observed: now,
            last_observed: now,
            observation_count: 1,
            expiry: Some(now + self.suspected_ttl.min(MAX_NONTERMINAL_BACKOFF_SECONDS)),
            cleared_at: None,
            clear_reason: None,
            last_status_code: status_code,
            last_error_class: error_class,
        };
        entries.insert(key, entry.clone());
        entry
    }

    pub fn clear_exact_key(&self, key: &QuarantineKey, reason: &str, now: f64) -> bool {
        self.clear_key(key, reason, now, false)
    }

    pub fn set_terminal_withdrawn(
        &self,
        key: QuarantineKey,
        reason: &str,
        provenance: EvidenceProvenance,
        now: f64,
    ) -> Result<QuarantineEntry, &'static str> {
        if !provenance.is_authoritative() {
            return Err("runtime evidence cannot create terminal withdrawal");
        }
        let entry = QuarantineEntry {
            key: key.clone(),
            state: QuarantineState::TerminalWithdrawn,
            evidence_provenance: provenance,
            reason: reason.to_owned(),
            first_observed: now,
            last_observed: now,
            observation_count: 1,
            expiry: None,
            cleared_at: None,
            clear_reason: None,
            last_status_code: None,
            last_error_class: None,
        };
        self.entries
            .lock()
            .expect("quarantine lock")
            .insert(key, entry.clone());
        Ok(entry)
    }

    pub fn clear_authoritative_reappearance(&self, key: &QuarantineKey, now: f64) -> bool {
        self.clear_key(key, "catalog_reappearance", now, true)
    }

    pub fn manual_clear(&self, key: &QuarantineKey, now: f64) -> bool {
        self.clear_key(key, "operator_clear", now, true)
    }

    pub fn get_entry(&self, key: &QuarantineKey) -> Option<QuarantineEntry> {
        self.entries
            .lock()
            .expect("quarantine lock")
            .get(key)
            .cloned()
    }

    pub fn list_entries(&self, now: f64, include_expired: bool) -> Vec<QuarantineEntry> {
        self.entries
            .lock()
            .expect("quarantine lock")
            .values()
            .filter(|entry| {
                entry.state != QuarantineState::Healthy
                    && (include_expired || entry.expiry.is_none_or(|expiry| now < expiry))
            })
            .cloned()
            .collect()
    }

    pub fn prune_expired(&self, now: f64) -> usize {
        let mut entries = self.entries.lock().expect("quarantine lock");
        let keys: Vec<QuarantineKey> = entries
            .iter()
            .filter(|(_, entry)| {
                entry.state != QuarantineState::Healthy
                    && entry.expiry.is_some_and(|expiry| now >= expiry)
            })
            .map(|(key, _)| key.clone())
            .collect();
        let count = keys.len();
        for key in keys {
            entries.remove(&key);
        }
        count
    }

    /// Hydrate one validated durable entry without allowing stale persistence
    /// to demote newer runtime-cleared or terminal state.
    pub fn hydrate_entry(&self, entry: QuarantineEntry, now: f64) {
        if entry.state != QuarantineState::TerminalWithdrawn
            && entry.expiry.is_some_and(|expiry| now >= expiry)
        {
            return;
        }
        let mut entries = self.entries.lock().expect("quarantine lock");
        let Some(existing) = entries.get(&entry.key) else {
            entries.insert(entry.key.clone(), entry);
            return;
        };
        if matches!(
            existing.state,
            QuarantineState::Healthy | QuarantineState::TerminalWithdrawn
        ) {
            return;
        }
        let rank = |state: QuarantineState| match state {
            QuarantineState::Healthy => 0,
            QuarantineState::Suspected => 1,
            QuarantineState::Quarantined => 2,
            QuarantineState::TerminalWithdrawn => 3,
        };
        if rank(existing.state) > rank(entry.state)
            || (rank(existing.state) == rank(entry.state)
                && existing.observation_count >= entry.observation_count)
        {
            return;
        }
        entries.insert(entry.key.clone(), entry);
    }

    fn clear_key(&self, key: &QuarantineKey, reason: &str, now: f64, allow_terminal: bool) -> bool {
        let mut entries = self.entries.lock().expect("quarantine lock");
        let Some(entry) = entries.get_mut(key) else {
            return false;
        };
        if entry.state == QuarantineState::Healthy
            || (!allow_terminal && entry.state == QuarantineState::TerminalWithdrawn)
        {
            return false;
        }
        if entry.expiry.is_some_and(|expiry| now >= expiry)
            && entry.state != QuarantineState::TerminalWithdrawn
        {
            entries.remove(key);
            return false;
        }
        entry.state = QuarantineState::Healthy;
        entry.expiry = None;
        entry.cleared_at = Some(now);
        entry.clear_reason = Some(reason.to_owned());
        true
    }
}

/// Validate a durable row represented by the typed repository record.
pub fn entry_from_row(
    row: &crate::health::repository::ModelQuarantineRecord,
) -> Result<QuarantineEntry, String> {
    let state = QuarantineState::try_from(row.state.as_str())
        .map_err(|_| "invalid quarantine state".to_owned())?;
    let provenance = EvidenceProvenance::try_from(row.evidence_provenance.as_str())
        .map_err(|_| "invalid quarantine evidence provenance".to_owned())?;
    for (name, value) in [
        ("provider_id", &row.provider_id),
        ("account_id", &row.account_id),
        ("canonical_model_id", &row.canonical_model_id),
        ("upstream_protocol", &row.upstream_protocol),
    ] {
        if value.is_empty() {
            return Err(format!("invalid quarantine {name}"));
        }
    }
    if row.observation_count == 0
        || !row.first_observed_epoch.is_finite()
        || !row.last_observed_epoch.is_finite()
        || row.expiry_epoch.is_some_and(|value| !value.is_finite())
    {
        return Err("invalid quarantine timestamp/count".to_owned());
    }
    match state {
        QuarantineState::Suspected | QuarantineState::Quarantined if row.expiry_epoch.is_none() => {
            return Err("nonterminal quarantine requires expiry".to_owned());
        }
        QuarantineState::TerminalWithdrawn if row.expiry_epoch.is_some() => {
            return Err("terminal quarantine cannot expire".to_owned());
        }
        _ => {}
    }
    if row.upstream_model_id.as_ref().is_some_and(String::is_empty) {
        return Err("invalid quarantine upstream_model_id".to_owned());
    }
    Ok(QuarantineEntry {
        key: QuarantineKey::new(
            &row.provider_id,
            &row.account_id,
            &row.canonical_model_id,
            row.upstream_model_id.as_deref(),
            &row.upstream_protocol,
        ),
        state,
        evidence_provenance: provenance,
        reason: row.reason.clone(),
        first_observed: row.first_observed_epoch,
        last_observed: row.last_observed_epoch,
        observation_count: row.observation_count,
        expiry: row.expiry_epoch,
        cleared_at: row.cleared_at_epoch,
        clear_reason: row.clear_reason.clone(),
        last_status_code: row.last_status_code,
        last_error_class: row.last_error_class.clone(),
    })
}
