//! Compact operator/provider health snapshot (Plan 202).
//!
//! This module owns the presentation-light status aggregation boundary. It
//! combines active-generation account identity, live routing health, cached
//! catalog probe evidence, and runtime diagnostics into one bounded,
//! secret-free snapshot. It performs no outbound provider requests, mutates
//! no circuit-breaker state, and never surfaces credentials, prompts, raw
//! bodies, or raw upstream error strings.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::health::{AccountHealthSnapshot, BackoffReason};

/// Stable machine contract version for `eggpool status --json` and
/// `GET /api/status`.
pub const STATUS_SCHEMA_VERSION: u32 = 1;

/// Upper bound for provider rows rendered or serialized in one snapshot.
/// Provider cardinality is small in EggPool deployments; this is a safety
/// ceiling, not a pagination contract.
pub const MAX_STATUS_PROVIDERS: usize = 256;

/// Maximum bytes retained for a provider identifier in status output.
pub const MAX_PROVIDER_ID_CHARS: usize = 96;

/// Maximum bytes retained for a bounded reason code.
pub const MAX_REASON_CODE_CHARS: usize = 64;

/// Observation-freshness fallback when the caller has no configured
/// `models.stale_after_s`. Matches the shipped default catalog window.
pub const DEFAULT_OBSERVATION_STALE_AFTER_SECS: u64 = 7200;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProxyStatus {
    Ready,
    Degraded,
    Unready,
    /// CLI-only: the local server could not be reached, so no server
    /// snapshot exists. Never returned by the server endpoint.
    Unavailable,
}

impl ProxyStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Degraded => "degraded",
            Self::Unready => "unready",
            Self::Unavailable => "unavailable",
        }
    }
}

impl std::fmt::Display for ProxyStatus {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderStatus {
    Ready,
    Degraded,
    Unavailable,
    Disabled,
    Unknown,
}

impl ProviderStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Degraded => "degraded",
            Self::Unavailable => "unavailable",
            Self::Disabled => "disabled",
            Self::Unknown => "unknown",
        }
    }
}

impl std::fmt::Display for ProviderStatus {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderObservation {
    Verified,
    Failed,
    Stale,
    Never,
}

impl ProviderObservation {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Verified => "verified",
            Self::Failed => "failed",
            Self::Stale => "stale",
            Self::Never => "never",
        }
    }
}

impl std::fmt::Display for ProviderObservation {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProxyHealthSummary {
    pub status: ProxyStatus,
    pub ready: bool,
    pub available: bool,
    pub version: String,
    pub base_url: String,
    pub uptime_seconds: Option<f64>,
    pub model_count: usize,
    pub routable_accounts: usize,
    pub enabled_accounts: usize,
    pub reason_code: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProviderHealthSummary {
    pub provider_id: String,
    pub status: ProviderStatus,
    pub enabled_accounts: usize,
    pub total_accounts: usize,
    pub routable_accounts: usize,
    pub backoff_accounts: usize,
    pub unavailable_accounts: usize,
    pub model_count: Option<usize>,
    pub last_probe_age_seconds: Option<u64>,
    pub last_probe_latency_ms: Option<u64>,
    pub last_probe_status_code: Option<u16>,
    pub last_observation: ProviderObservation,
    pub reason_code: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RuntimeHealthSummary {
    pub generation: Option<u64>,
    pub digest_prefix: String,
    pub reload: String,
    pub tasks: String,
    pub db: String,
    pub retiring: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProxyStatusSnapshot {
    pub schema_version: u32,
    pub observed_at: String,
    pub proxy: ProxyHealthSummary,
    pub providers: Vec<ProviderHealthSummary>,
    pub runtime: RuntimeHealthSummary,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadinessSnapshot {
    pub ready: bool,
    pub reason_code: Option<String>,
}

/// One account's contribution to provider aggregation. Health and ping
/// evidence live in different clock domains, so the caller supplies
/// pre-computed routability plus bounded ping evidence.
#[derive(Debug, Clone)]
pub struct AccountStatusInput {
    pub account_name: String,
    pub provider_id: String,
    pub enabled: bool,
    pub has_usable_credentials: bool,
    pub routable: bool,
    pub in_backoff: bool,
    pub backoff_reason: Option<BackoffReason>,
    pub circuit_open: bool,
    pub has_success_observation: bool,
    pub has_failure_observation: bool,
    pub has_model_quarantine: bool,
    pub auth_terminal: bool,
}

/// Bounded cached probe evidence for one account.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PingEvidence {
    pub observed: bool,
    pub success: bool,
    pub age_seconds: Option<u64>,
    pub latency_ms: Option<u64>,
    pub status_code: Option<u16>,
    pub model_count: i64,
}

impl PingEvidence {
    pub const fn never() -> Self {
        Self {
            observed: false,
            success: false,
            age_seconds: None,
            latency_ms: None,
            status_code: None,
            model_count: 0,
        }
    }
}

/// Per-provider rollup inputs.
#[derive(Debug, Clone)]
pub struct ProviderStatusInput {
    pub provider_id: String,
    pub accounts: Vec<AccountStatusInput>,
    pub ping: PingEvidence,
    pub catalog_model_count: Option<usize>,
    pub stale_after_secs: u64,
}

pub fn truncate_id(value: &str) -> String {
    let mut out: String = value.chars().take(MAX_PROVIDER_ID_CHARS).collect();
    if out.len() > MAX_PROVIDER_ID_CHARS {
        out.truncate(MAX_PROVIDER_ID_CHARS);
    }
    out
}

fn bounded_reason(value: impl Into<String>) -> String {
    let value: String = value.into();
    value.chars().take(MAX_REASON_CODE_CHARS).collect()
}

pub const fn backoff_reason_code(reason: BackoffReason) -> &'static str {
    match reason {
        BackoffReason::AuthenticationFailed => "authentication_failed",
        BackoffReason::RateLimited | BackoffReason::QuotaExhausted => "rate_limited",
        _ => "provider_backoff",
    }
}

/// Classify one account snapshot into routability inputs without mutating
/// health state. `now` must be in the health manager's clock domain.
pub fn account_input_from_snapshot(
    account_name: &str,
    provider_id: &str,
    enabled: bool,
    has_usable_credentials: bool,
    snapshot: Option<&AccountHealthSnapshot>,
    now: f64,
    circuit_open: bool,
) -> AccountStatusInput {
    let Some(snapshot) = snapshot else {
        return AccountStatusInput {
            account_name: account_name.to_owned(),
            provider_id: provider_id.to_owned(),
            enabled,
            has_usable_credentials,
            // Unknown accounts remain routing-eligible by default, but
            // status must not claim verified readiness for them.
            routable: enabled && has_usable_credentials,
            in_backoff: false,
            backoff_reason: None,
            circuit_open: false,
            has_success_observation: false,
            has_failure_observation: false,
            has_model_quarantine: false,
            auth_terminal: false,
        };
    };
    let in_cooldown =
        now < snapshot.cooldown_until || snapshot.disabled_until.is_some_and(|until| now < until);
    let auth_terminal = snapshot.health_state == "authentication_failed"
        || snapshot.last_failure_category == Some(BackoffReason::AuthenticationFailed)
        || (!snapshot.disabled_reason.is_empty()
            && snapshot.health_state == BackoffReason::AuthenticationFailed.as_str());
    // An account is routable when static gating passes and no live
    // backoff/circuit/terminal condition currently excludes it.
    let live_blocked = in_cooldown
        || circuit_open
        || auth_terminal
        || !snapshot.is_healthy && snapshot.last_failure_category.is_some();
    let routable = enabled && has_usable_credentials && !live_blocked;
    let in_backoff = enabled && has_usable_credentials && (in_cooldown || circuit_open);
    let has_quarantine =
        !snapshot.terminal_models.is_empty() || !snapshot.disabled_models.is_empty();
    AccountStatusInput {
        account_name: account_name.to_owned(),
        provider_id: provider_id.to_owned(),
        enabled,
        has_usable_credentials,
        routable,
        in_backoff,
        backoff_reason: snapshot.last_failure_category,
        circuit_open,
        has_success_observation: snapshot.last_success.is_some(),
        has_failure_observation: snapshot.last_failure.is_some(),
        has_model_quarantine: has_quarantine,
        auth_terminal,
    }
}

/// Aggregate one provider's accounts plus its latest cached probe into a
/// stable provider summary. Never emits raw ping error text.
pub fn aggregate_provider(input: &ProviderStatusInput) -> ProviderHealthSummary {
    let provider_id = truncate_id(&input.provider_id);
    let total_accounts = input.accounts.len();
    let enabled_accounts = input
        .accounts
        .iter()
        .filter(|account| account.enabled)
        .count();
    let routable_accounts = input
        .accounts
        .iter()
        .filter(|account| account.enabled && account.routable)
        .count();
    let backoff_accounts = input
        .accounts
        .iter()
        .filter(|account| account.enabled && account.in_backoff)
        .count();
    let unavailable_accounts = input
        .accounts
        .iter()
        .filter(|account| account.enabled && account.has_usable_credentials && !account.routable)
        .count();

    if enabled_accounts == 0 {
        return ProviderHealthSummary {
            provider_id,
            status: ProviderStatus::Disabled,
            enabled_accounts,
            total_accounts,
            routable_accounts: 0,
            backoff_accounts: 0,
            unavailable_accounts,
            model_count: input.catalog_model_count,
            last_probe_age_seconds: input.ping.age_seconds,
            last_probe_latency_ms: input.ping.latency_ms,
            last_probe_status_code: input.ping.status_code,
            last_observation: observation_for(input),
            reason_code: Some(bounded_reason("no_enabled_accounts")),
        };
    }

    if routable_accounts == 0 {
        let reason = provider_failure_reason(input).unwrap_or("no_routable_accounts");
        return ProviderHealthSummary {
            provider_id,
            status: ProviderStatus::Unavailable,
            enabled_accounts,
            total_accounts,
            routable_accounts,
            backoff_accounts,
            unavailable_accounts,
            model_count: input.catalog_model_count,
            last_probe_age_seconds: input.ping.age_seconds,
            last_probe_latency_ms: input.ping.latency_ms,
            last_probe_status_code: input.ping.status_code,
            last_observation: observation_for(input),
            reason_code: Some(bounded_reason(reason)),
        };
    }

    // At least one viable route remains. Surface partial failure as degraded.
    let partial_failure = input.accounts.iter().any(|account| {
        account.enabled
            && (account.in_backoff
                || account.circuit_open
                || account.auth_terminal
                || (account.has_usable_credentials && !account.routable))
    });
    let has_quarantine = input
        .accounts
        .iter()
        .any(|account| account.enabled && account.routable && account.has_model_quarantine);
    let probe_failed_fresh = input.ping.observed
        && !input.ping.success
        && input
            .ping
            .age_seconds
            .is_some_and(|age| age <= input.stale_after_secs);

    if partial_failure || has_quarantine || probe_failed_fresh {
        let reason = if partial_failure {
            provider_failure_reason(input).unwrap_or("partial_account_failure")
        } else if has_quarantine {
            "model_quarantine"
        } else {
            "probe_failed"
        };
        return ProviderHealthSummary {
            provider_id,
            status: ProviderStatus::Degraded,
            enabled_accounts,
            total_accounts,
            routable_accounts,
            backoff_accounts,
            unavailable_accounts,
            model_count: input.catalog_model_count,
            last_probe_age_seconds: input.ping.age_seconds,
            last_probe_latency_ms: input.ping.latency_ms,
            last_probe_status_code: input.ping.status_code,
            last_observation: observation_for(input),
            reason_code: Some(bounded_reason(reason)),
        };
    }

    // All enabled accounts look routable. Require real upstream evidence
    // before claiming ready; a freshly registered health entry alone is not
    // proof of reachability.
    let verified = input
        .accounts
        .iter()
        .any(|account| account.enabled && account.routable && account.has_success_observation)
        || (input.ping.observed
            && input.ping.success
            && input
                .ping
                .age_seconds
                .is_some_and(|age| age <= input.stale_after_secs));
    if verified {
        return ProviderHealthSummary {
            provider_id,
            status: ProviderStatus::Ready,
            enabled_accounts,
            total_accounts,
            routable_accounts,
            backoff_accounts,
            unavailable_accounts,
            model_count: input.catalog_model_count,
            last_probe_age_seconds: input.ping.age_seconds,
            last_probe_latency_ms: input.ping.latency_ms,
            last_probe_status_code: input.ping.status_code,
            last_observation: ProviderObservation::Verified,
            reason_code: None,
        };
    }

    // Routable by gating but nothing has verified the upstream yet, or all
    // evidence is too stale to make a reachability claim.
    let stale = input.ping.observed
        && input
            .ping
            .age_seconds
            .is_some_and(|age| age > input.stale_after_secs);
    ProviderHealthSummary {
        provider_id,
        status: ProviderStatus::Unknown,
        enabled_accounts,
        total_accounts,
        routable_accounts,
        backoff_accounts,
        unavailable_accounts,
        model_count: input.catalog_model_count,
        last_probe_age_seconds: input.ping.age_seconds,
        last_probe_latency_ms: input.ping.latency_ms,
        last_probe_status_code: input.ping.status_code,
        last_observation: if stale {
            ProviderObservation::Stale
        } else {
            observation_for(input)
        },
        reason_code: Some(bounded_reason(if stale {
            "probe_stale"
        } else {
            "unobserved"
        })),
    }
}

fn observation_for(input: &ProviderStatusInput) -> ProviderObservation {
    let any_success = input
        .accounts
        .iter()
        .any(|account| account.enabled && account.routable && account.has_success_observation)
        || (input.ping.observed
            && input.ping.success
            && input
                .ping
                .age_seconds
                .is_some_and(|age| age <= input.stale_after_secs));
    if any_success {
        return ProviderObservation::Verified;
    }
    let stale = input.ping.observed
        && input
            .ping
            .age_seconds
            .is_some_and(|age| age > input.stale_after_secs);
    if stale {
        return ProviderObservation::Stale;
    }
    let any_failure = input
        .accounts
        .iter()
        .any(|account| account.enabled && account.has_failure_observation)
        || (input.ping.observed && !input.ping.success);
    if any_failure {
        return ProviderObservation::Failed;
    }
    ProviderObservation::Never
}

fn provider_failure_reason(input: &ProviderStatusInput) -> Option<&'static str> {
    let mut saw_circuit = false;
    let mut saw_auth = false;
    let mut saw_rate = false;
    let mut saw_backoff = false;
    for account in &input.accounts {
        if !account.enabled || account.routable {
            continue;
        }
        if !account.has_usable_credentials {
            continue;
        }
        if account.auth_terminal {
            saw_auth = true;
            continue;
        }
        if account.circuit_open {
            saw_circuit = true;
            continue;
        }
        match account.backoff_reason {
            Some(BackoffReason::AuthenticationFailed) => saw_auth = true,
            Some(BackoffReason::RateLimited) | Some(BackoffReason::QuotaExhausted) => {
                saw_rate = true;
            }
            Some(_) => saw_backoff = true,
            None => {
                if account.in_backoff {
                    saw_backoff = true;
                }
            }
        }
    }
    // Include degraded-scope partial failures as well.
    if !saw_auth && !saw_circuit && !saw_rate && !saw_backoff {
        for account in &input.accounts {
            if !account.enabled || account.routable {
                continue;
            }
            if account.auth_terminal {
                saw_auth = true;
            } else if account.circuit_open {
                saw_circuit = true;
            }
        }
        if !saw_auth && !saw_circuit {
            let partial = input.accounts.iter().any(|account| {
                account.enabled && account.has_usable_credentials && !account.routable
            });
            if partial {
                return Some("partial_account_failure");
            }
        }
    }
    if saw_auth {
        Some("authentication_failed")
    } else if saw_circuit {
        Some("circuit_open")
    } else if saw_rate {
        Some("rate_limited")
    } else if saw_backoff {
        Some("provider_backoff")
    } else if input.ping.observed && !input.ping.success {
        Some("probe_failed")
    } else {
        None
    }
}

/// Shared readiness evaluation used by both `/v1/readyz` and status.
/// Mirrors the current readyz decision tree without copying it.
pub fn evaluate_readiness(
    configured_accounts: usize,
    enabled_accounts: usize,
    has_loaded_credentials: bool,
    active_catalog_models: usize,
    persisted_models: usize,
    database_ok: bool,
    runtime_available: bool,
) -> ReadinessSnapshot {
    if !runtime_available {
        return ReadinessSnapshot {
            ready: false,
            reason_code: Some("runtime unavailable".to_owned()),
        };
    }
    if !database_ok {
        return ReadinessSnapshot {
            ready: false,
            reason_code: Some("database not writable".to_owned()),
        };
    }
    if configured_accounts == 0 {
        return ReadinessSnapshot {
            ready: false,
            reason_code: Some("no accounts configured".to_owned()),
        };
    }
    if enabled_accounts == 0 {
        return ReadinessSnapshot {
            ready: false,
            reason_code: Some("no enabled accounts".to_owned()),
        };
    }
    if !has_loaded_credentials {
        return ReadinessSnapshot {
            ready: false,
            reason_code: Some("no loaded credentials".to_owned()),
        };
    }
    if active_catalog_models == 0 || persisted_models == 0 {
        return ReadinessSnapshot {
            ready: false,
            reason_code: Some("no usable model catalog".to_owned()),
        };
    }
    ReadinessSnapshot {
        ready: true,
        reason_code: None,
    }
}

/// Overall proxy status. Disabled providers never degrade the proxy by
/// themselves; any degraded/unavailable enabled provider does.
pub fn aggregate_proxy(
    readiness: &ReadinessSnapshot,
    providers: &[ProviderHealthSummary],
    task_degraded: bool,
    reload_active: bool,
    retiring_abnormal: bool,
) -> (ProxyStatus, bool, Option<String>) {
    if !readiness.ready {
        let reason = readiness
            .reason_code
            .clone()
            .unwrap_or_else(|| "not ready".to_owned());
        return (ProxyStatus::Unready, false, Some(bounded_reason(reason)));
    }
    let degraded_provider = providers
        .iter()
        .filter(|provider| {
            !matches!(
                provider.status,
                ProviderStatus::Disabled | ProviderStatus::Unknown
            )
        })
        .find(|provider| {
            matches!(
                provider.status,
                ProviderStatus::Degraded | ProviderStatus::Unavailable
            )
        });
    if let Some(provider) = degraded_provider {
        let reason = provider
            .reason_code
            .clone()
            .unwrap_or_else(|| "provider degraded".to_owned());
        return (ProxyStatus::Degraded, true, Some(bounded_reason(reason)));
    }
    if task_degraded || reload_active || retiring_abnormal {
        let reason = if task_degraded {
            "background task degraded"
        } else if reload_active {
            "reload in progress"
        } else {
            "generation retiring"
        };
        return (ProxyStatus::Degraded, true, Some(bounded_reason(reason)));
    }
    (ProxyStatus::Ready, true, None)
}

/// Build the current wall-clock observation timestamp without leaking
/// monotonic health-clock values. Rendered as RFC 3339 UTC without an
/// external date dependency.
pub fn observed_at_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    format_epoch_rfc3339(secs)
}

fn format_epoch_rfc3339(epoch_secs: u64) -> String {
    let days = (epoch_secs / 86400) as i64;
    let time_of_day = (epoch_secs % 86400) as i64;
    let hour = time_of_day / 3600;
    let minute = (time_of_day % 3600) / 60;
    let second = time_of_day % 60;
    // Civil-from-days (Howard Hinnant) for Gregorian dates.
    let shifted = days + 719468;
    let era = shifted.div_euclid(146097);
    let day_of_era = shifted.rem_euclid(146097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_part = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_part + 2) / 5 + 1;
    let month = if month_part < 10 {
        month_part + 3
    } else {
        month_part - 9
    };
    let year = if month <= 2 { year + 1 } else { year };
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// Deterministic provider ordering plus cardinality bound.
pub fn sort_providers(providers: &mut Vec<ProviderHealthSummary>) {
    providers.sort_by(|left, right| left.provider_id.cmp(&right.provider_id));
    providers.truncate(MAX_STATUS_PROVIDERS);
}

/// Aggregate per-provider ping rows (latest per provider) into bounded
/// evidence. Raw error strings are consumed only as a failure bit.
pub fn ping_evidence_for_provider(
    latest: Option<&crate::db::Ping>,
    now_epoch_secs: u64,
) -> PingEvidence {
    let Some(ping) = latest else {
        return PingEvidence::never();
    };
    let success = ping.error.is_none()
        && ping
            .status_code
            .is_some_and(|code| (200..300).contains(&code));
    // Skipped catalog probes record (None, None, 0) with no error. They are
    // not observations of upstream reachability.
    if ping.status_code.is_none() && ping.error.is_none() {
        return PingEvidence {
            observed: false,
            success: false,
            age_seconds: probe_age_secs(&ping.probed_at, now_epoch_secs),
            latency_ms: ping.latency_ms.map(|value| value.max(0) as u64),
            status_code: None,
            model_count: ping.model_count,
        };
    }
    PingEvidence {
        observed: true,
        success,
        age_seconds: probe_age_secs(&ping.probed_at, now_epoch_secs),
        latency_ms: ping.latency_ms.map(|value| value.max(0) as u64),
        status_code: ping.status_code.and_then(|code| u16::try_from(code).ok()),
        model_count: ping.model_count,
    }
}

fn probe_age_secs(probed_at: &str, now_epoch_secs: u64) -> Option<u64> {
    parse_sqlite_timestamp(probed_at).and_then(|probed| now_epoch_secs.checked_sub(probed))
}

/// Parse `YYYY-MM-DD HH:MM:SS` (SQLite `CURRENT_TIMESTAMP`) as UTC epoch
/// seconds. Returns `None` for unparseable values rather than failing.
fn parse_sqlite_timestamp(value: &str) -> Option<u64> {
    let value = value.trim().replace('T', " ");
    let (date, time) = value.split_once(' ')?;
    let mut date_parts = date.split('-');
    let year: i64 = date_parts.next()?.parse().ok()?;
    let month: i64 = date_parts.next()?.parse().ok()?;
    let day: i64 = date_parts.next()?.parse().ok()?;
    let mut time_parts = time.split(':');
    let hour: i64 = time_parts.next()?.parse().ok()?;
    let minute: i64 = time_parts.next()?.parse().ok()?;
    let second: i64 = time_parts
        .next()
        .and_then(|part| part.split('.').next())
        .and_then(|part| part.parse().ok())?;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    // Days-from-civil (Howard Hinnant) for Gregorian dates.
    let year_adj = if month <= 2 { year - 1 } else { year };
    let era = year_adj.div_euclid(400);
    let year_of_era = year_adj.rem_euclid(400);
    let month_adj = (month + 9) % 12;
    let day_of_year = (153 * month_adj + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146097 + day_of_era - 719468;
    u64::try_from(days * 86400 + hour * 3600 + minute * 60 + second).ok()
}

/// Count distinct catalog models per provider from a cache snapshot.
pub fn catalog_counts_by_provider(
    provider_model_keys: &[(String, String)],
) -> BTreeMap<String, usize> {
    let mut models: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (model_id, provider_id) in provider_model_keys {
        models
            .entry(provider_id.clone())
            .or_default()
            .insert(model_id.clone());
    }
    models
        .into_iter()
        .map(|(provider, set)| (provider, set.len()))
        .collect()
}

pub fn stale_after_secs(configured: u64) -> u64 {
    if configured == 0 {
        DEFAULT_OBSERVATION_STALE_AFTER_SECS
    } else {
        configured
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn account(
        name: &str,
        provider: &str,
        enabled: bool,
        routable: bool,
        success: bool,
        failure: bool,
    ) -> AccountStatusInput {
        AccountStatusInput {
            account_name: name.to_owned(),
            provider_id: provider.to_owned(),
            enabled,
            has_usable_credentials: true,
            routable: enabled && routable,
            in_backoff: enabled && !routable,
            backoff_reason: if failure {
                Some(BackoffReason::RateLimited)
            } else {
                None
            },
            circuit_open: false,
            has_success_observation: success,
            has_failure_observation: failure,
            has_model_quarantine: false,
            auth_terminal: false,
        }
    }

    fn provider(
        id: &str,
        accounts: Vec<AccountStatusInput>,
        ping: PingEvidence,
    ) -> ProviderStatusInput {
        ProviderStatusInput {
            provider_id: id.to_owned(),
            accounts,
            ping,
            catalog_model_count: Some(3),
            stale_after_secs: 7200,
        }
    }

    #[test]
    fn disabled_provider_does_not_hide_configuration() {
        let summary = aggregate_provider(&provider(
            "local-test",
            vec![account("a1", "local-test", false, false, false, false)],
            PingEvidence::never(),
        ));
        assert_eq!(summary.status, ProviderStatus::Disabled);
        assert_eq!(summary.reason_code.as_deref(), Some("no_enabled_accounts"));
    }

    #[test]
    fn unobserved_routable_provider_is_unknown_not_ready() {
        let summary = aggregate_provider(&provider(
            "opencode-go",
            vec![account("a1", "opencode-go", true, true, false, false)],
            PingEvidence::never(),
        ));
        assert_eq!(summary.status, ProviderStatus::Unknown);
        assert_eq!(summary.reason_code.as_deref(), Some("unobserved"));
        assert_eq!(summary.last_observation, ProviderObservation::Never);
    }

    #[test]
    fn recent_successful_ping_is_ready() {
        let summary = aggregate_provider(&provider(
            "openrouter",
            vec![account("a1", "openrouter", true, true, false, false)],
            PingEvidence {
                observed: true,
                success: true,
                age_seconds: Some(120),
                latency_ms: Some(121),
                status_code: Some(200),
                model_count: 12,
            },
        ));
        assert_eq!(summary.status, ProviderStatus::Ready);
        assert_eq!(summary.last_observation, ProviderObservation::Verified);
    }

    #[test]
    fn request_success_beats_stale_failed_ping() {
        let summary = aggregate_provider(&provider(
            "minimax",
            vec![account("a1", "minimax", true, true, true, false)],
            PingEvidence {
                observed: true,
                success: false,
                age_seconds: Some(60),
                latency_ms: None,
                status_code: Some(500),
                model_count: 0,
            },
        ));
        // A fresh failed probe while a viable route remains is degraded,
        // never unavailable, and newer request success keeps the
        // observation honest without hiding the probe failure.
        assert_eq!(summary.status, ProviderStatus::Degraded);
        assert_eq!(summary.reason_code.as_deref(), Some("probe_failed"));
    }

    #[test]
    fn partial_account_failure_is_degraded() {
        let mut failing = account("b1", "minimax", true, false, false, true);
        failing.in_backoff = true;
        let summary = aggregate_provider(&provider(
            "minimax",
            vec![account("a1", "minimax", true, true, true, false), failing],
            PingEvidence::never(),
        ));
        assert_eq!(summary.status, ProviderStatus::Degraded);
        assert_eq!(summary.reason_code.as_deref(), Some("rate_limited"));
    }

    #[test]
    fn all_backoff_is_unavailable() {
        let mut first = account("a1", "x", true, false, false, true);
        first.in_backoff = true;
        let mut second = account("a2", "x", true, false, false, true);
        second.in_backoff = true;
        let summary =
            aggregate_provider(&provider("x", vec![first, second], PingEvidence::never()));
        assert_eq!(summary.status, ProviderStatus::Unavailable);
    }

    #[test]
    fn auth_terminal_maps_to_bounded_reason() {
        let mut blocked = account("a1", "x", true, false, false, true);
        blocked.auth_terminal = true;
        blocked.backoff_reason = Some(BackoffReason::AuthenticationFailed);
        let summary = aggregate_provider(&provider("x", vec![blocked], PingEvidence::never()));
        assert_eq!(summary.status, ProviderStatus::Unavailable);
        assert_eq!(
            summary.reason_code.as_deref(),
            Some("authentication_failed")
        );
    }

    #[test]
    fn model_quarantine_degrades_working_provider() {
        let mut quarantined = account("a1", "x", true, true, true, false);
        quarantined.has_model_quarantine = true;
        let summary = aggregate_provider(&provider("x", vec![quarantined], PingEvidence::never()));
        assert_eq!(summary.status, ProviderStatus::Degraded);
        assert_eq!(summary.reason_code.as_deref(), Some("model_quarantine"));
    }

    #[test]
    fn sqlite_timestamp_parses_and_rejects_garbage() {
        assert!(parse_sqlite_timestamp("2026-09-16 12:00:00").is_some());
        assert!(parse_sqlite_timestamp("not-a-timestamp").is_none());
        assert!(parse_sqlite_timestamp("2026-13-40 99:99:99").is_none());
    }
}
