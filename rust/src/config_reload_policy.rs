//! Pure, fail-closed configuration reload policy.
//!
//! The Python policy in `eggpool.config_reload_policy` is the compatibility
//! oracle.  This module owns only semantic config comparison and redacted
//! diagnostics; it does not read files, build generations, touch the
//! database, or publish runtime state.

use std::{collections::BTreeSet, fmt, string::String, vec::Vec};

use serde::{Serialize, Serializer, ser::SerializeStruct};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::config::Config;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReloadDisposition {
    Live,
    RestartRequired,
    Ignored,
}

impl fmt::Display for ReloadDisposition {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Live => "live",
            Self::RestartRequired => "restart_required",
            Self::Ignored => "ignored",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ConfigChange {
    pub path: String,
    pub disposition: ReloadDisposition,
    pub section: String,
    pub old_display: String,
    pub new_display: String,
    pub secret: bool,
}

impl fmt::Display for ConfigChange {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} ({}) {} -> {}",
            self.path, self.disposition, self.old_display, self.new_display
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigDiff {
    pub changes: Vec<ConfigChange>,
}

impl Serialize for ConfigDiff {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("ConfigDiff", 3)?;
        state.serialize_field("changes", &self.changes)?;
        state.serialize_field("live", &self.live())?;
        state.serialize_field("restart_required", &self.restart_required())?;
        state.end()
    }
}

impl ConfigDiff {
    pub fn is_noop(&self) -> bool {
        self.changes.is_empty()
    }

    pub fn live(&self) -> Vec<&ConfigChange> {
        self.changes
            .iter()
            .filter(|change| change.disposition == ReloadDisposition::Live)
            .collect()
    }

    pub fn restart_required(&self) -> Vec<&ConfigChange> {
        self.changes
            .iter()
            .filter(|change| change.disposition == ReloadDisposition::RestartRequired)
            .collect()
    }

    pub fn has_restart_required(&self) -> bool {
        self.changes
            .iter()
            .any(|change| change.disposition == ReloadDisposition::RestartRequired)
    }

    /// Stable, deduplicated top-level sections in path order.
    pub fn changed_sections(&self) -> Vec<String> {
        self.changes
            .iter()
            .filter_map(|change| change.path.split('.').next())
            .map(str::to_owned)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ConfigPolicyError {
    #[error("configuration semantic projection failed")]
    Serialization,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("configuration digest mismatch")]
pub struct DigestMismatch {
    pub expected: String,
    pub actual: String,
}

/// Return the stable semantic digest of a validated config.
///
/// Serde struct field order is fixed by the Rust schema and all dynamic maps
/// use `BTreeMap`, so TOML comments and formatting do not affect this digest.
pub fn semantic_digest(config: &Config) -> Result<String, ConfigPolicyError> {
    let bytes = serde_json::to_vec(config).map_err(|_| ConfigPolicyError::Serialization)?;
    let digest = Sha256::digest(bytes);
    Ok(format!("{digest:x}"))
}

/// Verify the optional caller expectation before any reload-side mutation.
pub fn verify_expected_digest(expected: Option<&str>, actual: &str) -> Result<(), DigestMismatch> {
    if expected.is_some_and(|expected| expected != actual) {
        return Err(DigestMismatch {
            expected: expected.unwrap_or_default().to_owned(),
            actual: actual.to_owned(),
        });
    }
    Ok(())
}

/// Compute a redacted semantic diff.  Validated `Config` values contain only
/// string-keyed JSON-compatible values, so serialization failure is treated
/// as an internal policy error rather than silently producing an empty diff.
pub fn compute_diff(old: &Config, new: &Config) -> Result<ConfigDiff, ConfigPolicyError> {
    let old_value = serde_json::to_value(old).map_err(|_| ConfigPolicyError::Serialization)?;
    let new_value = serde_json::to_value(new).map_err(|_| ConfigPolicyError::Serialization)?;
    let mut changes = Vec::new();

    diff_dynamic_map(&old_value, &new_value, "providers", &mut changes, true);
    diff_accounts(&old_value, &new_value, &mut changes);

    for path in FIELD_DISPOSITIONS.iter().map(|(path, _)| *path) {
        if matches!(path, "providers" | "accounts") {
            continue;
        }
        let Some(old_field) = value_at(&old_value, path) else {
            continue;
        };
        let Some(new_field) = value_at(&new_value, path) else {
            continue;
        };
        if old_field == new_field {
            continue;
        }
        let (old_display, new_display) = if path == "model_routers" {
            (
                display_router_count(old_field),
                display_router_count(new_field),
            )
        } else {
            (
                display_value(old_field, is_secret_path(path), path),
                display_value(new_field, is_secret_path(path), path),
            )
        };
        changes.push(change(path, old_display, new_display, is_secret_path(path)));
    }

    changes.sort_by(|left, right| left.path.cmp(&right.path));
    Ok(ConfigDiff { changes })
}

fn diff_dynamic_map(
    old: &Value,
    new: &Value,
    root: &str,
    changes: &mut Vec<ConfigChange>,
    structured: bool,
) {
    let old_map = old.get(root).and_then(Value::as_object);
    let new_map = new.get(root).and_then(Value::as_object);
    let keys = old_map
        .into_iter()
        .flat_map(|map| map.keys())
        .chain(new_map.into_iter().flat_map(|map| map.keys()))
        .map(String::as_str)
        .collect::<BTreeSet<_>>();

    for key in keys {
        let path = format!("{root}.{key}");
        let old_entry = old_map.and_then(|map| map.get(key));
        let new_entry = new_map.and_then(|map| map.get(key));
        match (old_entry, new_entry) {
            (None, Some(_)) => changes.push(change(path, "<missing>", "", false)),
            (Some(_), None) => changes.push(change(path, "", "<missing>", false)),
            (Some(old_entry), Some(new_entry)) if old_entry != new_entry => {
                if structured {
                    diff_structured(&path, old_entry, new_entry, changes);
                } else {
                    let old_display = display_value(old_entry, false, &path);
                    let new_display = display_value(new_entry, false, &path);
                    changes.push(change(path, old_display, new_display, false));
                }
            }
            _ => {}
        }
    }
}

fn diff_accounts(old: &Value, new: &Value, changes: &mut Vec<ConfigChange>) {
    let mut old_accounts = std::collections::BTreeMap::new();
    let mut new_accounts = std::collections::BTreeMap::new();
    collect_accounts(old, &mut old_accounts);
    collect_accounts(new, &mut new_accounts);
    let keys = old_accounts
        .keys()
        .chain(new_accounts.keys())
        .map(String::as_str)
        .collect::<BTreeSet<_>>();

    for key in keys {
        let path = format!("accounts.{key}");
        match (old_accounts.get(key), new_accounts.get(key)) {
            (None, Some(_)) => changes.push(change(path, "<missing>", "", false)),
            (Some(_), None) => changes.push(change(path, "", "<missing>", false)),
            (Some(old_account), Some(new_account)) if old_account != new_account => {
                diff_structured(&path, old_account, new_account, changes)
            }
            _ => {}
        }
    }
}

fn collect_accounts<'a>(
    value: &'a Value,
    accounts: &mut std::collections::BTreeMap<String, &'a Value>,
) {
    let Some(providers) = value.get("providers").and_then(Value::as_object) else {
        return;
    };
    for (provider_id, provider) in providers {
        let Some(items) = provider.get("accounts").and_then(Value::as_array) else {
            continue;
        };
        for account in items {
            let Some(name) = account.get("name").and_then(Value::as_str) else {
                continue;
            };
            accounts.insert(format!("{provider_id}/{name}"), account);
        }
    }
}

fn diff_structured(path: &str, old: &Value, new: &Value, changes: &mut Vec<ConfigChange>) {
    let (Some(old_map), Some(new_map)) = (old.as_object(), new.as_object()) else {
        changes.push(change(
            path,
            display_value(old, false, path),
            display_value(new, false, path),
            false,
        ));
        return;
    };
    let keys = old_map
        .keys()
        .chain(new_map.keys())
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    for key in keys {
        let child_path = format!("{path}.{key}");
        let old_value = old_map.get(key);
        let new_value = new_map.get(key);
        match (old_value, new_value) {
            (None, Some(_)) => changes.push(change(child_path, "<missing>", "", false)),
            (Some(_), None) => changes.push(change(child_path, "", "<missing>", false)),
            (Some(old_value), Some(new_value)) if old_value != new_value => {
                let secret = is_secret_path(&child_path);
                let old_display = display_value(old_value, secret, &child_path);
                let new_display = display_value(new_value, secret, &child_path);
                changes.push(change(child_path, old_display, new_display, secret));
            }
            _ => {}
        }
    }
}

fn change(
    path: impl Into<String>,
    old_display: impl Into<String>,
    new_display: impl Into<String>,
    secret: bool,
) -> ConfigChange {
    let path = path.into();
    ConfigChange {
        disposition: disposition_for(&path),
        section: path.split('.').next().unwrap_or(&path).to_owned(),
        old_display: old_display.into(),
        new_display: new_display.into(),
        path,
        secret,
    }
}

fn value_at<'a>(value: &'a Value, path: &str) -> Option<&'a Value> {
    path.split('.')
        .try_fold(value, |cursor, segment| cursor.get(segment))
}

fn is_secret_path(path: &str) -> bool {
    path.rsplit('.').next().is_some_and(is_secret_name)
}

fn is_secret_name(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    matches!(
        name.as_str(),
        "api_key" | "api_key_env" | "proxy_url" | "proxy_url_env"
    ) || name.contains("token")
        || name.contains("password")
        || name.contains("secret")
        || name.contains("credential")
}

fn display_router_count(value: &Value) -> String {
    let count = value.as_object().map_or(0, serde_json::Map::len);
    format!(
        "{count} configured {}",
        if count == 1 { "router" } else { "routers" }
    )
}

fn display_value(value: &Value, secret: bool, context: &str) -> String {
    if secret {
        return "<changed>".to_owned();
    }
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(value) => if *value { "True" } else { "False" }.to_owned(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => sanitize_text_for_audit(value, context),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(|value| display_value(value, false, context))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => {
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            let rendered = keys
                .into_iter()
                .map(|key| {
                    let child = format!("{context}.{key}");
                    let secret =
                        is_secret_name(key) || (key == "url" && context.starts_with("proxies"));
                    format!("{key}={}", display_value(&values[key], secret, &child))
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{rendered}}}")
        }
    }
}

/// Scrub common credential-shaped text before it reaches operator output.
pub fn sanitize_text_for_audit(value: &str, context: &str) -> String {
    let mut output = value.to_owned();
    for marker in [
        "Bearer ",
        "Basic ",
        "secret=",
        "secret:",
        "token=",
        "token:",
        "password=",
        "password:",
        "api_key=",
        "api_key:",
        "api-key=",
        "api-key:",
    ] {
        while let Some(start) = find_ascii_case_insensitive(&output, marker) {
            let value_start = start + marker.len();
            let end = output[value_start..]
                .find(char::is_whitespace)
                .map_or(output.len(), |offset| value_start + offset);
            output.replace_range(start..end, "<redacted>");
        }
    }
    for prefix in [
        "sk-proj-", "sk-or-", "sk-ant-", "sk-", "key-", "glpat-", "ghp_", "xai-",
    ] {
        while let Some(start) = find_ascii_case_insensitive(&output, prefix) {
            let value_start = start + prefix.len();
            let end = output[value_start..]
                .find(|character: char| {
                    !(character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.'))
                })
                .map_or(output.len(), |offset| value_start + offset);
            if end == value_start {
                break;
            }
            output.replace_range(start..end, "<redacted>");
        }
    }
    if let Some(scheme_end) = output.find("://") {
        let credentials_start = scheme_end + 3;
        if let Some(at_offset) = output[credentials_start..].find('@') {
            let at = credentials_start + at_offset;
            if output[credentials_start..at].contains(':') {
                output.replace_range(credentials_start..at, "<redacted>");
            }
        }
    }
    let _ = context;
    output
}

fn find_ascii_case_insensitive(value: &str, needle: &str) -> Option<usize> {
    value.char_indices().map(|(index, _)| index).find(|index| {
        value[*index..]
            .to_ascii_lowercase()
            .starts_with(&needle.to_ascii_lowercase())
    })
}

const DYNAMIC_RULES: &[(&str, ReloadDisposition)] = &[
    ("providers.<provider_id>", ReloadDisposition::Live),
    (
        "accounts.<provider_id>/<account_name>",
        ReloadDisposition::Live,
    ),
    ("model_overrides.<model_id>", ReloadDisposition::Live),
    ("model_capabilities.<model_id>", ReloadDisposition::Live),
    ("transcoder.<field>", ReloadDisposition::Live),
    ("cache.<field>", ReloadDisposition::RestartRequired),
    ("models.<field>", ReloadDisposition::Live),
];

// R001's exported Python policy is copied as a reviewable, sorted Rust table.
const FIELD_DISPOSITIONS: &[(&str, ReloadDisposition)] = &[
    ("accounts", ReloadDisposition::Live),
    ("backup.directory", ReloadDisposition::RestartRequired),
    ("backup.enabled", ReloadDisposition::Live),
    ("backup.include_env", ReloadDisposition::RestartRequired),
    ("backup.interval_s", ReloadDisposition::Live),
    ("backup.retain_count", ReloadDisposition::Live),
    ("backup.startup_delay_s", ReloadDisposition::Live),
    ("dashboard.enabled", ReloadDisposition::RestartRequired),
    ("dashboard.public", ReloadDisposition::RestartRequired),
    (
        "dashboard.refresh_interval_s",
        ReloadDisposition::RestartRequired,
    ),
    ("dashboard.retain_event_days", ReloadDisposition::Live),
    (
        "dashboard.retain_request_stats_days",
        ReloadDisposition::Live,
    ),
    (
        "dashboard.store_request_content",
        ReloadDisposition::RestartRequired,
    ),
    ("dashboard.theme", ReloadDisposition::RestartRequired),
    ("dashboard.themes_dir", ReloadDisposition::RestartRequired),
    (
        "database.busy_timeout_ms",
        ReloadDisposition::RestartRequired,
    ),
    (
        "database.journal_size_limit",
        ReloadDisposition::RestartRequired,
    ),
    ("database.path", ReloadDisposition::RestartRequired),
    ("database.synchronous", ReloadDisposition::RestartRequired),
    ("database.wal", ReloadDisposition::RestartRequired),
    (
        "database.worker_threads",
        ReloadDisposition::RestartRequired,
    ),
    (
        "limits.five_hour_microdollars",
        ReloadDisposition::RestartRequired,
    ),
    (
        "limits.monthly_microdollars",
        ReloadDisposition::RestartRequired,
    ),
    (
        "limits.weekly_microdollars",
        ReloadDisposition::RestartRequired,
    ),
    (
        "maintenance.contention_defer_above_lock_wait_p95_ms",
        ReloadDisposition::Live,
    ),
    ("maintenance.max_batches_per_tick", ReloadDisposition::Live),
    ("maintenance.max_deferral_age_s", ReloadDisposition::Live),
    ("maintenance.max_rows_per_batch", ReloadDisposition::Live),
    ("maintenance.max_tick_duration_ms", ReloadDisposition::Live),
    (
        "maintenance.p0_max_batches_per_tick",
        ReloadDisposition::Live,
    ),
    ("maintenance.p0_max_rows_per_batch", ReloadDisposition::Live),
    (
        "maintenance.p0_max_tick_duration_ms",
        ReloadDisposition::Live,
    ),
    ("metrics.aggregate_only", ReloadDisposition::RestartRequired),
    (
        "metrics.cleanup_interval_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "metrics.cleanup_max_rows_per_pass",
        ReloadDisposition::RestartRequired,
    ),
    ("metrics.detailed_span_sample_rate", ReloadDisposition::Live),
    (
        "metrics.dispatch_spans.sample_rate",
        ReloadDisposition::Live,
    ),
    (
        "metrics.dispatch_spans.window_size",
        ReloadDisposition::RestartRequired,
    ),
    (
        "metrics.event_loop_lag_enabled",
        ReloadDisposition::RestartRequired,
    ),
    ("metrics.flush_interval_s", ReloadDisposition::Live),
    (
        "metrics.max_buffered_events",
        ReloadDisposition::RestartRequired,
    ),
    (
        "metrics.operational_event_retain_days",
        ReloadDisposition::Live,
    ),
    (
        "metrics.rollup_retain_days",
        ReloadDisposition::RestartRequired,
    ),
    (
        "metrics.routing_decision_retain_days",
        ReloadDisposition::Live,
    ),
    (
        "metrics.timeseries_bucket_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "metrics.trace_sample_rate",
        ReloadDisposition::RestartRequired,
    ),
    ("metrics.write_mode", ReloadDisposition::RestartRequired),
    ("model_capabilities", ReloadDisposition::Live),
    ("model_info.aliases", ReloadDisposition::RestartRequired),
    (
        "model_info.conflict_ttl_s",
        ReloadDisposition::RestartRequired,
    ),
    ("model_info.enabled", ReloadDisposition::Live),
    (
        "model_info.include_in_models_endpoint",
        ReloadDisposition::RestartRequired,
    ),
    ("model_info.known_ttl_s", ReloadDisposition::RestartRequired),
    (
        "model_info.max_models_per_cycle",
        ReloadDisposition::RestartRequired,
    ),
    ("model_info.overrides", ReloadDisposition::RestartRequired),
    (
        "model_info.partial_ttl_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "model_info.refresh_interval_s",
        ReloadDisposition::RestartRequired,
    ),
    ("model_info.sources", ReloadDisposition::RestartRequired),
    (
        "model_info.sparse_new_accelerated_days",
        ReloadDisposition::RestartRequired,
    ),
    (
        "model_info.sparse_new_initial_ttl_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "model_info.sparse_new_later_ttl_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "model_info.startup_refresh",
        ReloadDisposition::RestartRequired,
    ),
    (
        "model_info.store_raw_observations",
        ReloadDisposition::RestartRequired,
    ),
    ("model_overrides", ReloadDisposition::Live),
    ("model_routers", ReloadDisposition::Live),
    ("models.allow_stale_catalog", ReloadDisposition::Live),
    (
        "models.catalog_withdrawal_policy",
        ReloadDisposition::RestartRequired,
    ),
    ("models.collapse_models", ReloadDisposition::Live),
    ("models.expose_mode", ReloadDisposition::Live),
    ("models.ping_retain_days", ReloadDisposition::Live),
    ("models.refresh_interval_s", ReloadDisposition::Live),
    ("models.stale_after_s", ReloadDisposition::Live),
    ("models.startup_refresh", ReloadDisposition::RestartRequired),
    (
        "network.connect_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "network.keepalive_expiry_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "network.max_connections",
        ReloadDisposition::RestartRequired,
    ),
    ("network.max_keepalive", ReloadDisposition::RestartRequired),
    ("network.read_timeout_s", ReloadDisposition::RestartRequired),
    (
        "pricing.catalogs.aliases",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.api_key",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.base_url",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.enabled",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.max_entries",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.options",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.priority",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.opencode_zen.ttl_seconds",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.api_key",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.base_url",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.enabled",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.max_entries",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.options",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.priority",
        ReloadDisposition::RestartRequired,
    ),
    (
        "pricing.catalogs.openrouter.ttl_seconds",
        ReloadDisposition::RestartRequired,
    ),
    ("pricing.fallback", ReloadDisposition::RestartRequired),
    ("providers", ReloadDisposition::Live),
    ("proxies", ReloadDisposition::RestartRequired),
    (
        "readiness_probe.enabled",
        ReloadDisposition::RestartRequired,
    ),
    (
        "readiness_probe.freshness_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "readiness_probe.initial_probe",
        ReloadDisposition::RestartRequired,
    ),
    (
        "readiness_probe.interval_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "readiness_probe.timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    ("routing.fairness_epsilon", ReloadDisposition::Live),
    ("routing.fairness_mode", ReloadDisposition::Live),
    ("routing.fairness_scope", ReloadDisposition::Live),
    ("routing.health_penalty", ReloadDisposition::Live),
    ("routing.inflight_penalty", ReloadDisposition::Live),
    ("routing.local_quota_mode", ReloadDisposition::Live),
    ("routing.max_retries_before_stream", ReloadDisposition::Live),
    ("routing.near_tie_epsilon", ReloadDisposition::Live),
    (
        "routing.quota_exhausted_cooldown_seconds",
        ReloadDisposition::Live,
    ),
    ("routing.randomize_near_ties", ReloadDisposition::Live),
    ("routing.strategy", ReloadDisposition::Live),
    (
        "routing.trace.flush_interval_s",
        ReloadDisposition::RestartRequired,
    ),
    ("routing.trace.guard_cooldown_s", ReloadDisposition::Live),
    (
        "routing.trace.guard_oldest_event_age_s",
        ReloadDisposition::Live,
    ),
    (
        "routing.trace.guard_queue_occupancy_threshold",
        ReloadDisposition::Live,
    ),
    (
        "routing.trace.include_score_components",
        ReloadDisposition::Live,
    ),
    (
        "routing.trace.max_batch_size",
        ReloadDisposition::RestartRequired,
    ),
    ("routing.trace.mode", ReloadDisposition::Live),
    (
        "routing.trace.queue_capacity",
        ReloadDisposition::RestartRequired,
    ),
    ("routing.trace.sample_rate", ReloadDisposition::Live),
    (
        "routing.trace.shutdown_flush_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "routing.trace.skip_above_lock_wait_p95_ms",
        ReloadDisposition::Live,
    ),
    (
        "routing.unknown_request_reservation_microdollars",
        ReloadDisposition::Live,
    ),
    (
        "routing.wire_negotiation.cache_max_entries",
        ReloadDisposition::Live,
    ),
    ("routing.wire_negotiation.enabled", ReloadDisposition::Live),
    (
        "routing.wire_negotiation.learned_preference_ttl_s",
        ReloadDisposition::Live,
    ),
    (
        "routing.wire_negotiation.max_concurrent_per_provider",
        ReloadDisposition::Live,
    ),
    (
        "routing.wire_negotiation.min_negotiation_interval_s",
        ReloadDisposition::Live,
    ),
    (
        "routing.wire_negotiation.rejection_cooldown_s",
        ReloadDisposition::Live,
    ),
    ("security.allowed_hosts", ReloadDisposition::RestartRequired),
    ("security.cors_origins", ReloadDisposition::RestartRequired),
    (
        "security.persist_redacted_error_detail",
        ReloadDisposition::Live,
    ),
    (
        "security.redact_headers",
        ReloadDisposition::RestartRequired,
    ),
    (
        "security.trusted_proxies",
        ReloadDisposition::RestartRequired,
    ),
    ("server.access_log", ReloadDisposition::RestartRequired),
    ("server.api_key", ReloadDisposition::RestartRequired),
    ("server.api_key_env", ReloadDisposition::RestartRequired),
    ("server.host", ReloadDisposition::RestartRequired),
    ("server.log_level", ReloadDisposition::RestartRequired),
    ("server.max_request_body_bytes", ReloadDisposition::Live),
    ("server.port", ReloadDisposition::RestartRequired),
    ("server.threads", ReloadDisposition::RestartRequired),
    ("transcoder", ReloadDisposition::Live),
    ("update_checker.enabled", ReloadDisposition::RestartRequired),
    ("upstream.base_url", ReloadDisposition::RestartRequired),
    (
        "upstream.connect_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "upstream.keepalive_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "upstream.max_connections",
        ReloadDisposition::RestartRequired,
    ),
    ("upstream.max_keepalive", ReloadDisposition::RestartRequired),
    (
        "upstream.pool_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "upstream.read_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
    (
        "upstream.write_timeout_s",
        ReloadDisposition::RestartRequired,
    ),
];

pub fn field_dispositions() -> &'static [(&'static str, ReloadDisposition)] {
    FIELD_DISPOSITIONS
}

pub fn dynamic_rules() -> &'static [(&'static str, ReloadDisposition)] {
    DYNAMIC_RULES
}

/// Exact policy lookup, with only the Python-approved blanket dynamic rules.
/// Everything else is restart-required.
pub fn disposition_for(path: &str) -> ReloadDisposition {
    if let Some((_, disposition)) = FIELD_DISPOSITIONS.iter().find(|(known, _)| *known == path) {
        return *disposition;
    }
    for prefix in [
        "providers.",
        "accounts.",
        "model_overrides.",
        "model_capabilities.",
        "transcoder.",
    ] {
        if path.starts_with(prefix) {
            return ReloadDisposition::Live;
        }
    }
    ReloadDisposition::RestartRequired
}

const SCHEMA_COLLAPSE: &[&str] = &[
    "accounts",
    "model_capabilities",
    "model_info.aliases",
    "model_info.overrides",
    "model_info.sources",
    "model_overrides",
    "model_routers",
    "providers",
    "proxies",
    "transcoder",
];

/// Return the stable leaf projection used by the schema-coverage guard.
pub fn schema_paths() -> Result<Vec<String>, ConfigPolicyError> {
    let value =
        serde_json::to_value(Config::default()).map_err(|_| ConfigPolicyError::Serialization)?;
    let mut paths = Vec::new();
    collect_schema_paths(&value, "", &mut paths);
    paths.sort();
    Ok(paths)
}

fn collect_schema_paths(value: &Value, prefix: &str, paths: &mut Vec<String>) {
    if !prefix.is_empty() && SCHEMA_COLLAPSE.contains(&prefix) {
        paths.push(prefix.to_owned());
        return;
    }
    match value {
        Value::Object(values) if values.is_empty() => {
            if !prefix.is_empty() {
                paths.push(prefix.to_owned());
            }
        }
        Value::Object(values) => {
            for (key, value) in values {
                let path = if prefix.is_empty() {
                    key.clone()
                } else {
                    format!("{prefix}.{key}")
                };
                collect_schema_paths(value, &path, paths);
            }
        }
        Value::Array(_) => paths.push(prefix.to_owned()),
        _ => paths.push(prefix.to_owned()),
    }
}
