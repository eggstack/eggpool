//! Typed TOML configuration compatibility for the side-by-side candidate.
//!
//! The Python implementation remains authoritative.  This module deliberately
//! models the supported configuration contract rather than mirroring Pydantic's
//! class hierarchy: Serde handles shape/defaults and `Config::validate` handles
//! cross-field rules that cannot be expressed locally on a field.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;
use sha2::{Digest, Sha256};
use thiserror::Error;

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 11_300;
const DEFAULT_PROVIDER_ID: &str = "opencode-go";
const DEFAULT_UPSTREAM_URL: &str = "https://opencode.ai/zen/go/v1";
const MAX_REQUEST_BODY_BYTES: u64 = 10 * 1024 * 1024;

fn default_database_path() -> String {
    let root = env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".local/share"));
    root.join("eggpool/usage.sqlite3")
        .to_string_lossy()
        .into_owned()
}

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("Config file not found: {path}")]
    FileNotFound { path: String },
    #[error("Invalid TOML in configuration file: {path}")]
    Parse { path: String },
    #[error("Configuration validation failed: {detail}")]
    Validation { detail: String },
    #[error("Cannot read configuration file: {path}")]
    Read { path: String },
}

impl ConfigError {
    fn validation(detail: impl Into<String>) -> Self {
        Self::Validation {
            detail: detail.into(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub api_key: Option<String>,
    pub api_key_env: String,
    pub log_level: String,
    pub access_log: bool,
    pub threads: u32,
    pub max_request_body_bytes: u64,
}
impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            host: DEFAULT_HOST.into(),
            port: DEFAULT_PORT,
            api_key: None,
            api_key_env: "SERVER_API_KEY".into(),
            log_level: "INFO".into(),
            access_log: false,
            threads: 1,
            max_request_body_bytes: MAX_REQUEST_BODY_BYTES,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct UpstreamConfig {
    pub base_url: String,
    pub connect_timeout_s: f64,
    pub read_timeout_s: f64,
    pub write_timeout_s: f64,
    pub pool_timeout_s: f64,
    pub max_connections: u32,
    pub max_keepalive: u32,
    pub keepalive_timeout_s: f64,
}
impl Default for UpstreamConfig {
    fn default() -> Self {
        Self {
            base_url: DEFAULT_UPSTREAM_URL.into(),
            connect_timeout_s: 5.0,
            read_timeout_s: 300.0,
            write_timeout_s: 30.0,
            pool_timeout_s: 30.0,
            max_connections: 16,
            max_keepalive: 4,
            keepalive_timeout_s: 30.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct DatabaseConfig {
    pub path: String,
    pub busy_timeout_ms: u32,
    pub wal: bool,
    pub synchronous: String,
    pub worker_threads: u8,
    pub journal_size_limit: Option<u64>,
}
impl Default for DatabaseConfig {
    fn default() -> Self {
        Self {
            path: default_database_path(),
            busy_timeout_ms: 5000,
            wal: true,
            synchronous: "NORMAL".into(),
            worker_threads: 1,
            journal_size_limit: None,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ReadinessProbeConfig {
    pub enabled: bool,
    pub interval_s: f64,
    pub freshness_s: f64,
    pub timeout_s: f64,
    pub initial_probe: bool,
}
impl Default for ReadinessProbeConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            interval_s: 10.0,
            freshness_s: 30.0,
            timeout_s: 5.0,
            initial_probe: true,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelsConfig {
    pub refresh_interval_s: u64,
    pub expose_mode: String,
    pub startup_refresh: bool,
    pub stale_after_s: u64,
    pub allow_stale_catalog: bool,
    pub ping_retain_days: u64,
    pub collapse_models: bool,
    pub catalog_withdrawal_policy: String,
}
impl Default for ModelsConfig {
    fn default() -> Self {
        Self {
            refresh_interval_s: 300,
            expose_mode: "union".into(),
            startup_refresh: true,
            stale_after_s: 7200,
            allow_stale_catalog: true,
            ping_retain_days: 7,
            collapse_models: false,
            catalog_withdrawal_policy: "preserve_until_health".into(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct RoutingTraceConfig {
    pub mode: String,
    pub sample_rate: f64,
    pub include_score_components: bool,
    pub skip_above_lock_wait_p95_ms: f64,
    pub queue_capacity: u32,
    pub flush_interval_s: f64,
    pub max_batch_size: u32,
    pub shutdown_flush_timeout_s: f64,
    pub guard_queue_occupancy_threshold: f64,
    pub guard_oldest_event_age_s: f64,
    pub guard_cooldown_s: f64,
}
impl Default for RoutingTraceConfig {
    fn default() -> Self {
        Self {
            mode: "off".into(),
            sample_rate: 0.0,
            include_score_components: false,
            skip_above_lock_wait_p95_ms: 200.0,
            queue_capacity: 1000,
            flush_interval_s: 1.0,
            max_batch_size: 50,
            shutdown_flush_timeout_s: 5.0,
            guard_queue_occupancy_threshold: 0.8,
            guard_oldest_event_age_s: 30.0,
            guard_cooldown_s: 5.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct WireNegotiationConfig {
    pub enabled: bool,
    pub max_concurrent_per_provider: u8,
    pub min_negotiation_interval_s: f64,
    pub rejection_cooldown_s: f64,
    pub learned_preference_ttl_s: f64,
    pub cache_max_entries: u32,
}
impl Default for WireNegotiationConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_concurrent_per_provider: 1,
            min_negotiation_interval_s: 1.0,
            rejection_cooldown_s: 300.0,
            learned_preference_ttl_s: 86_400.0,
            cache_max_entries: 2048,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct RoutingConfig {
    pub strategy: String,
    pub near_tie_epsilon: f64,
    pub max_retries_before_stream: u32,
    pub unknown_request_reservation_microdollars: u64,
    pub inflight_penalty: u64,
    pub health_penalty: u64,
    pub randomize_near_ties: bool,
    pub quota_exhausted_cooldown_seconds: f64,
    pub local_quota_mode: String,
    pub fairness_mode: String,
    pub fairness_epsilon: Option<f64>,
    pub fairness_scope: String,
    pub wire_negotiation: WireNegotiationConfig,
    pub trace: RoutingTraceConfig,
}
impl Default for RoutingConfig {
    fn default() -> Self {
        Self {
            strategy: "quota_fair".into(),
            near_tie_epsilon: 0.1,
            max_retries_before_stream: 3,
            unknown_request_reservation_microdollars: 1_000_000,
            inflight_penalty: 100_000,
            health_penalty: 500_000,
            randomize_near_ties: true,
            quota_exhausted_cooldown_seconds: 300.0,
            local_quota_mode: "score_only".into(),
            fairness_mode: "round_robin".into(),
            fairness_epsilon: None,
            fairness_scope: "provider_model_protocol".into(),
            wire_negotiation: WireNegotiationConfig::default(),
            trace: RoutingTraceConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct PricingCatalogEntry {
    pub enabled: bool,
    pub priority: u32,
    pub ttl_seconds: u64,
    pub max_entries: u64,
    pub base_url: Option<String>,
    pub api_key: Option<String>,
    pub options: BTreeMap<String, toml::Value>,
}
impl Default for PricingCatalogEntry {
    fn default() -> Self {
        Self {
            enabled: false,
            priority: 100,
            ttl_seconds: 86_400,
            max_entries: 4096,
            base_url: None,
            api_key: None,
            options: BTreeMap::new(),
        }
    }
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct PricingCatalogsConfig {
    pub openrouter: PricingCatalogEntry,
    pub opencode_zen: PricingCatalogEntry,
    pub aliases: Vec<BTreeMap<String, toml::Value>>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct PricingConfig {
    pub catalogs: PricingCatalogsConfig,
    pub fallback: String,
}
impl Default for PricingConfig {
    fn default() -> Self {
        Self {
            catalogs: PricingCatalogsConfig::default(),
            fallback: "generic_estimate".into(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct LimitsConfig {
    pub five_hour_microdollars: u64,
    pub weekly_microdollars: u64,
    pub monthly_microdollars: u64,
}
impl Default for LimitsConfig {
    fn default() -> Self {
        Self {
            five_hour_microdollars: 12_000_000,
            weekly_microdollars: 30_000_000,
            monthly_microdollars: 60_000_000,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct DispatchSpansConfig {
    pub sample_rate: f64,
    pub window_size: u32,
}
impl Default for DispatchSpansConfig {
    fn default() -> Self {
        Self {
            sample_rate: 0.0,
            window_size: 200,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct MetricsConfig {
    pub write_mode: String,
    pub flush_interval_s: u64,
    pub max_buffered_events: u64,
    pub timeseries_bucket_s: u64,
    pub trace_sample_rate: f64,
    pub aggregate_only: bool,
    pub rollup_retain_days: u64,
    pub operational_event_retain_days: u64,
    pub routing_decision_retain_days: u64,
    pub cleanup_interval_s: u64,
    pub cleanup_max_rows_per_pass: u64,
    pub event_loop_lag_enabled: bool,
    pub dispatch_spans: DispatchSpansConfig,
    pub detailed_span_sample_rate: Option<f64>,
}
impl Default for MetricsConfig {
    fn default() -> Self {
        Self {
            write_mode: "low_wear".into(),
            flush_interval_s: 120,
            max_buffered_events: 250,
            timeseries_bucket_s: 300,
            trace_sample_rate: 0.05,
            aggregate_only: true,
            rollup_retain_days: 90,
            operational_event_retain_days: 90,
            routing_decision_retain_days: 90,
            cleanup_interval_s: 86_400,
            cleanup_max_rows_per_pass: 5000,
            event_loop_lag_enabled: false,
            dispatch_spans: DispatchSpansConfig::default(),
            detailed_span_sample_rate: None,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct DashboardConfig {
    pub enabled: bool,
    pub public: bool,
    pub theme: String,
    pub themes_dir: Option<String>,
    pub retain_request_stats_days: u64,
    pub retain_event_days: u64,
    pub store_request_content: bool,
    pub refresh_interval_s: u64,
}
impl Default for DashboardConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            public: false,
            theme: "Cyber Red".into(),
            themes_dir: None,
            retain_request_stats_days: 30,
            retain_event_days: 90,
            store_request_content: false,
            refresh_interval_s: 60,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct SecurityConfig {
    pub allowed_hosts: Vec<String>,
    pub cors_origins: Vec<String>,
    pub trusted_proxies: Vec<String>,
    pub redact_headers: Vec<String>,
    pub persist_redacted_error_detail: bool,
}
impl Default for SecurityConfig {
    fn default() -> Self {
        Self {
            allowed_hosts: Vec::new(),
            cors_origins: Vec::new(),
            trusted_proxies: Vec::new(),
            redact_headers: vec!["authorization".into(), "x-api-key".into()],
            persist_redacted_error_detail: false,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProxyConfig {
    pub url: Option<String>,
    pub url_env: Option<String>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct AccountConfig {
    pub name: String,
    pub api_key: Option<String>,
    pub api_key_env: String,
    pub enabled: bool,
    pub weight: f64,
    pub five_hour_offset_microdollars: i64,
    pub weekly_offset_microdollars: i64,
    pub monthly_offset_microdollars: i64,
    pub proxy: Option<String>,
    pub proxy_url: Option<String>,
    pub proxy_url_env: Option<String>,
}
impl Default for AccountConfig {
    fn default() -> Self {
        Self {
            name: String::new(),
            api_key: None,
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            five_hour_offset_microdollars: 0,
            weekly_offset_microdollars: 0,
            monthly_offset_microdollars: 0,
            proxy: None,
            proxy_url: None,
            proxy_url_env: None,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderAdditionalAuthConfig {
    pub mode: String,
    pub header: String,
    pub scheme: String,
}
impl Default for ProviderAdditionalAuthConfig {
    fn default() -> Self {
        Self {
            mode: "bearer".into(),
            header: "Authorization".into(),
            scheme: "Bearer".into(),
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderAuthConfig {
    pub mode: String,
    pub header: String,
    pub scheme: String,
    pub additional: Vec<ProviderAdditionalAuthConfig>,
}
impl Default for ProviderAuthConfig {
    fn default() -> Self {
        Self {
            mode: "bearer".into(),
            header: "Authorization".into(),
            scheme: "Bearer".into(),
            additional: Vec::new(),
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderStaticHeaderConfig {
    pub name: String,
    pub value: Option<String>,
    pub value_env: Option<String>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderWireSurfaceConfig {
    pub path_template: String,
    pub stream_path_template: Option<String>,
    pub priority: u32,
    pub auth: Option<ProviderAuthConfig>,
    pub headers: Vec<ProviderStaticHeaderConfig>,
}
impl Default for ProviderWireSurfaceConfig {
    fn default() -> Self {
        Self {
            path_template: String::new(),
            stream_path_template: None,
            priority: 100,
            auth: None,
            headers: Vec::new(),
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelWirePreference {
    pub preferred_surface: String,
    pub fixed: bool,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderModelsEndpointConfig {
    pub method: String,
    pub path: String,
    pub body: Option<toml::Value>,
    pub query: BTreeMap<String, String>,
    pub required: bool,
}
impl Default for ProviderModelsEndpointConfig {
    fn default() -> Self {
        Self {
            method: "GET".into(),
            path: "/models".into(),
            body: None,
            query: BTreeMap::new(),
            required: true,
        }
    }
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderStaticModelConfig {
    pub id: String,
    pub display_name: Option<String>,
    pub protocol: Option<String>,
    pub max_context_tokens: Option<u64>,
    pub max_input_tokens: Option<u64>,
    pub max_output_tokens: Option<u64>,
    pub supports_tools: Option<bool>,
    pub supports_vision: Option<bool>,
    pub source_metadata: BTreeMap<String, toml::Value>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderVerifyConfig {
    pub probe_model: Option<String>,
    pub probe_protocol: String,
    pub require_models: bool,
}
impl Default for ProviderVerifyConfig {
    fn default() -> Self {
        Self {
            probe_model: None,
            probe_protocol: "openai".into(),
            require_models: true,
        }
    }
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderStreamTimeoutConfig {
    pub first_byte_timeout_s: Option<f64>,
    pub idle_timeout_s: Option<f64>,
    pub max_lifetime_s: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderConfig {
    pub id: String,
    pub base_url: String,
    pub protocols: Vec<String>,
    pub kind: Option<String>,
    pub openai_path: String,
    pub anthropic_path: String,
    pub responses_path: Option<String>,
    pub models_method: String,
    pub models_path: String,
    pub connect_timeout_s: f64,
    pub read_timeout_s: f64,
    pub write_timeout_s: f64,
    pub pool_timeout_s: f64,
    pub stream_completion_policy: String,
    pub stream_timeouts: ProviderStreamTimeoutConfig,
    pub max_connections: u32,
    pub max_keepalive: u32,
    pub keepalive_timeout_s: f64,
    pub routing_priority: u32,
    pub accounts: Vec<AccountConfig>,
    pub model_overrides: BTreeMap<String, ModelOverrideConfig>,
    pub model_capabilities: BTreeMap<String, ModelCapabilitiesOverrideConfig>,
    pub wire_surfaces: BTreeMap<String, ProviderWireSurfaceConfig>,
    pub model_wire: BTreeMap<String, ModelWirePreference>,
    pub auth: ProviderAuthConfig,
    pub headers: Vec<ProviderStaticHeaderConfig>,
    pub models_endpoint: Option<ProviderModelsEndpointConfig>,
    pub static_models: Vec<ProviderStaticModelConfig>,
    pub verify: ProviderVerifyConfig,
}
impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            id: String::new(),
            base_url: String::new(),
            protocols: vec!["openai".into()],
            kind: None,
            openai_path: "/chat/completions".into(),
            anthropic_path: "/messages".into(),
            responses_path: None,
            models_method: "GET".into(),
            models_path: "/models".into(),
            connect_timeout_s: 5.0,
            read_timeout_s: 300.0,
            write_timeout_s: 30.0,
            pool_timeout_s: 30.0,
            stream_completion_policy: "strict".into(),
            stream_timeouts: ProviderStreamTimeoutConfig::default(),
            max_connections: 32,
            max_keepalive: 8,
            keepalive_timeout_s: 30.0,
            routing_priority: 0,
            accounts: Vec::new(),
            model_overrides: BTreeMap::new(),
            model_capabilities: BTreeMap::new(),
            wire_surfaces: BTreeMap::new(),
            model_wire: BTreeMap::new(),
            auth: ProviderAuthConfig::default(),
            headers: Vec::new(),
            models_endpoint: None,
            static_models: Vec::new(),
            verify: ProviderVerifyConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelLimitOverrideConfig {
    pub max_context_tokens: Option<u64>,
    pub max_input_tokens: Option<u64>,
    pub max_output_tokens: Option<u64>,
    pub enforce_context_limit: bool,
}
impl Default for ModelLimitOverrideConfig {
    fn default() -> Self {
        Self {
            max_context_tokens: None,
            max_input_tokens: None,
            max_output_tokens: None,
            enforce_context_limit: true,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelOverrideConfig {
    pub max_context_tokens: Option<u64>,
    pub max_input_tokens: Option<u64>,
    pub max_output_tokens: Option<u64>,
    pub enforce_context_limit: bool,
    pub protocol: Option<String>,
    pub input_price_per_1k: Option<f64>,
    pub output_price_per_1k: Option<f64>,
    pub cache_read_per_million_microdollars: Option<u64>,
    pub cache_write_per_million_microdollars: Option<u64>,
}
impl Default for ModelOverrideConfig {
    fn default() -> Self {
        Self {
            max_context_tokens: None,
            max_input_tokens: None,
            max_output_tokens: None,
            enforce_context_limit: true,
            protocol: None,
            input_price_per_1k: None,
            output_price_per_1k: None,
            cache_read_per_million_microdollars: None,
            cache_write_per_million_microdollars: None,
        }
    }
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct ThinkingCapabilityOverrideConfig {
    pub status: Option<String>,
    pub source: Option<String>,
    pub native_protocols: Option<Vec<String>>,
    pub budget_tokens_min: Option<u64>,
    pub budget_tokens_max: Option<u64>,
    pub supported_efforts: Option<Vec<String>>,
    pub effort_to_budget_tokens: Option<BTreeMap<String, u64>>,
    pub toggle: Option<String>,
    pub effort: Option<String>,
    pub budget: Option<String>,
    pub notes: Option<String>,
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct MediaCapabilityOverrideConfig {
    pub base64: Option<bool>,
    pub url: Option<bool>,
    pub max_source_bytes: Option<u64>,
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct MultimodalCapabilityOverrideConfig {
    pub image_input: Option<MediaCapabilityOverrideConfig>,
    pub document_input: Option<MediaCapabilityOverrideConfig>,
    pub audio_input: Option<MediaCapabilityOverrideConfig>,
    pub non_text_tool_result: Option<bool>,
    pub max_serialized_request_bytes: Option<u64>,
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct ModelCapabilitiesOverrideConfig {
    pub thinking: Option<ThinkingCapabilityOverrideConfig>,
    pub transcoding: Option<toml::Value>,
    pub multimodal: Option<MultimodalCapabilityOverrideConfig>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct NetworkConfig {
    pub connect_timeout_s: f64,
    pub read_timeout_s: f64,
    pub max_connections: u32,
    pub max_keepalive: u32,
    pub keepalive_expiry_s: f64,
}
impl Default for NetworkConfig {
    fn default() -> Self {
        Self {
            connect_timeout_s: 10.0,
            read_timeout_s: 30.0,
            max_connections: 8,
            max_keepalive: 2,
            keepalive_expiry_s: 90.0,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct MaintenanceBudgetConfig {
    pub max_rows_per_batch: u32,
    pub max_batches_per_tick: u32,
    pub max_tick_duration_ms: f64,
    pub contention_defer_above_lock_wait_p95_ms: f64,
    pub max_deferral_age_s: f64,
    pub p0_max_rows_per_batch: u32,
    pub p0_max_batches_per_tick: u32,
    pub p0_max_tick_duration_ms: f64,
}
impl Default for MaintenanceBudgetConfig {
    fn default() -> Self {
        Self {
            max_rows_per_batch: 500,
            max_batches_per_tick: 4,
            max_tick_duration_ms: 500.0,
            contention_defer_above_lock_wait_p95_ms: 200.0,
            max_deferral_age_s: 3600.0,
            p0_max_rows_per_batch: 1000,
            p0_max_batches_per_tick: 2,
            p0_max_tick_duration_ms: 1000.0,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct BackupConfig {
    pub enabled: bool,
    pub interval_s: u64,
    pub retain_count: u64,
    pub startup_delay_s: u64,
    pub directory: Option<String>,
    pub include_env: bool,
}
impl Default for BackupConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            interval_s: 86_400,
            retain_count: 14,
            startup_delay_s: 300,
            directory: None,
            include_env: true,
        }
    }
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct UpdateCheckerConfig {
    pub enabled: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelInfoSourceConfig {
    pub enabled: bool,
    pub priority: u32,
    pub ttl_seconds: u64,
    pub base_url: Option<String>,
    pub api_key: Option<String>,
    pub api_key_env: Option<String>,
    pub max_entries: u64,
    pub options: BTreeMap<String, toml::Value>,
}
impl Default for ModelInfoSourceConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            priority: 100,
            ttl_seconds: 86_400,
            base_url: None,
            api_key: None,
            api_key_env: None,
            max_entries: 4096,
            options: BTreeMap::new(),
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelInfoSourcesConfig {
    pub provider_catalog: ModelInfoSourceConfig,
    pub openrouter: ModelInfoSourceConfig,
    pub artificial_analysis: ModelInfoSourceConfig,
    pub huggingface: ModelInfoSourceConfig,
}
impl Default for ModelInfoSourcesConfig {
    fn default() -> Self {
        Self {
            provider_catalog: ModelInfoSourceConfig {
                priority: 0,
                ttl_seconds: 300,
                ..Default::default()
            },
            openrouter: Default::default(),
            artificial_analysis: ModelInfoSourceConfig {
                enabled: false,
                priority: 50,
                ..Default::default()
            },
            huggingface: ModelInfoSourceConfig {
                enabled: false,
                priority: 200,
                ttl_seconds: 604_800,
                ..Default::default()
            },
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelInfoAliasConfig {
    pub provider_id: String,
    pub model_id: String,
    pub source: String,
    pub source_model_id: String,
    pub confidence: String,
    pub notes: Option<String>,
}
impl Default for ModelInfoAliasConfig {
    fn default() -> Self {
        Self {
            provider_id: String::new(),
            model_id: String::new(),
            source: String::new(),
            source_model_id: String::new(),
            confidence: "curated".into(),
            notes: None,
        }
    }
}
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct ModelInfoOverrideConfig {
    pub summary: Option<String>,
    pub family: Option<String>,
    pub display_name: Option<String>,
    pub notes: Option<String>,
    pub hide_benchmark_sources: bool,
    pub status_override: Option<String>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelInfoConfig {
    pub enabled: bool,
    pub startup_refresh: bool,
    pub refresh_interval_s: u64,
    pub known_ttl_s: u64,
    pub partial_ttl_s: u64,
    pub sparse_new_initial_ttl_s: u64,
    pub sparse_new_later_ttl_s: u64,
    pub sparse_new_accelerated_days: u64,
    pub conflict_ttl_s: u64,
    pub max_models_per_cycle: u64,
    pub include_in_models_endpoint: bool,
    pub store_raw_observations: bool,
    pub sources: ModelInfoSourcesConfig,
    pub aliases: Vec<ModelInfoAliasConfig>,
    pub overrides: BTreeMap<String, ModelInfoOverrideConfig>,
}
impl Default for ModelInfoConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            startup_refresh: true,
            refresh_interval_s: 21_600,
            known_ttl_s: 86_400,
            partial_ttl_s: 43_200,
            sparse_new_initial_ttl_s: 3600,
            sparse_new_later_ttl_s: 21_600,
            sparse_new_accelerated_days: 7,
            conflict_ttl_s: 7200,
            max_models_per_cycle: 50,
            include_in_models_endpoint: true,
            store_raw_observations: true,
            sources: Default::default(),
            aliases: Vec::new(),
            overrides: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderControlPolicyConfig {
    pub unsupported_control: String,
    pub unknown_contract: String,
    pub allow_compatibility_retry: bool,
}
impl Default for ProviderControlPolicyConfig {
    fn default() -> Self {
        Self {
            unsupported_control: "reject".into(),
            unknown_contract: "allow_with_warning".into(),
            allow_compatibility_retry: false,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct CapabilityPolicy {
    pub unsupported_thinking: String,
    pub unknown_thinking: String,
    pub mixed_collapsed_thinking: String,
}
impl Default for CapabilityPolicy {
    fn default() -> Self {
        Self {
            unsupported_thinking: "reject".into(),
            unknown_thinking: "reject".into(),
            mixed_collapsed_thinking: "filter".into(),
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct TranscoderFeatures {
    pub tools: bool,
    pub vision: bool,
    pub thinking: bool,
    pub structured_outputs: bool,
    pub anthropic_primitives: bool,
}
impl Default for TranscoderFeatures {
    fn default() -> Self {
        Self {
            tools: true,
            vision: false,
            thinking: true,
            structured_outputs: false,
            anthropic_primitives: false,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ThinkingBudgetDefaults {
    pub low: u64,
    pub medium: u64,
    pub high: u64,
}
impl Default for ThinkingBudgetDefaults {
    fn default() -> Self {
        Self {
            low: 1024,
            medium: 4096,
            high: 16_384,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct OpenaiReasoningFields {
    pub non_stream: Vec<String>,
    pub stream_delta: Vec<String>,
    pub emit_compat_aliases: bool,
}
impl Default for OpenaiReasoningFields {
    fn default() -> Self {
        Self {
            non_stream: vec!["reasoning_content".into()],
            stream_delta: vec!["reasoning".into()],
            emit_compat_aliases: false,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct TranscoderPolicy {
    pub enabled: bool,
    pub loss_policy: String,
    pub prefer_native: bool,
    pub features: TranscoderFeatures,
    pub capability_policy: CapabilityPolicy,
    pub thinking_budget_defaults: ThinkingBudgetDefaults,
    pub budget_resolution_policy: String,
    pub openai_reasoning_fields: OpenaiReasoningFields,
    pub provider_control_policy: ProviderControlPolicyConfig,
}
impl Default for TranscoderPolicy {
    fn default() -> Self {
        Self {
            enabled: true,
            loss_policy: "warn".into(),
            prefer_native: true,
            features: Default::default(),
            capability_policy: Default::default(),
            thinking_budget_defaults: Default::default(),
            budget_resolution_policy: "lenient".into(),
            openai_reasoning_fields: Default::default(),
            provider_control_policy: Default::default(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct ModelRouteConfig {
    pub model: String,
    pub description: String,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ModelRouterConfig {
    pub selector_model: String,
    pub default_model: String,
    pub routes: BTreeMap<String, ModelRouteConfig>,
    pub sticky: bool,
    pub affinity_ttl_s: f64,
    pub selector_timeout_s: f64,
    pub max_input_bytes: u64,
    pub repair_attempts: u8,
}
impl Default for ModelRouterConfig {
    fn default() -> Self {
        Self {
            selector_model: String::new(),
            default_model: String::new(),
            routes: BTreeMap::new(),
            sticky: true,
            affinity_ttl_s: 43_200.0,
            selector_timeout_s: 2.0,
            max_input_bytes: 2048,
            repair_attempts: 1,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct Config {
    pub server: ServerConfig,
    pub upstream: UpstreamConfig,
    pub database: DatabaseConfig,
    pub models: ModelsConfig,
    pub model_routers: BTreeMap<String, ModelRouterConfig>,
    pub routing: RoutingConfig,
    pub limits: LimitsConfig,
    pub pricing: PricingConfig,
    pub dashboard: DashboardConfig,
    pub security: SecurityConfig,
    pub metrics: MetricsConfig,
    pub maintenance: MaintenanceBudgetConfig,
    pub backup: BackupConfig,
    pub readiness_probe: ReadinessProbeConfig,
    pub network: NetworkConfig,
    pub proxies: BTreeMap<String, ProxyConfig>,
    pub accounts: Vec<AccountConfig>,
    pub providers: BTreeMap<String, ProviderConfig>,
    pub model_overrides: BTreeMap<String, ModelOverrideConfig>,
    pub model_capabilities: BTreeMap<String, ModelCapabilitiesOverrideConfig>,
    pub transcoder: TranscoderPolicy,
    pub model_info: ModelInfoConfig,
    pub update_checker: UpdateCheckerConfig,
}

pub type AppConfig = Config;

impl Config {
    /// Resolve an account's explicit outbound proxy using the Python
    /// precedence contract.  Environment values are trimmed at the boundary;
    /// inline values are retained exactly and are validated by the transport
    /// constructor.  Secret values never appear in the returned errors.
    pub fn resolve_account_proxy_url(
        &self,
        account: &AccountConfig,
    ) -> Result<Option<String>, ConfigError> {
        if let Some(proxy_url) = &account.proxy_url {
            return Ok(Some(proxy_url.clone()));
        }
        if let Some(env_name) = &account.proxy_url_env {
            return Self::resolve_proxy_url_env(env_name, &account.name).map(Some);
        }
        let Some(proxy_name) = &account.proxy else {
            return Ok(None);
        };
        let Some(proxy) = self.proxies.get(proxy_name) else {
            return Err(ConfigError::validation(
                "account references an unknown proxy",
            ));
        };
        if let Some(proxy_url) = &proxy.url {
            return Ok(Some(proxy_url.clone()));
        }
        let Some(env_name) = &proxy.url_env else {
            return Err(ConfigError::validation("proxy url_env is not set"));
        };
        Self::resolve_proxy_url_env(env_name, &account.name).map(Some)
    }

    fn resolve_proxy_url_env(env_name: &str, account_name: &str) -> Result<String, ConfigError> {
        let value = env::var(env_name).ok();
        Self::resolve_proxy_url_env_value(env_name, account_name, value)
    }

    fn resolve_proxy_url_env_value(
        env_name: &str,
        account_name: &str,
        value: Option<String>,
    ) -> Result<String, ConfigError> {
        let value = value.ok_or_else(|| {
            ConfigError::validation(format!(
                "Account {account_name:?} references proxy env var {env_name:?}, but it is not set"
            ))
        })?;
        if value.is_empty() {
            return Err(ConfigError::validation(format!(
                "Account {account_name:?} references proxy env var {env_name:?}, but it is not set"
            )));
        }
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return Err(ConfigError::validation(format!(
                "Account {account_name:?} references proxy env var {env_name:?}, but it is whitespace-only"
            )));
        }
        Ok(trimmed.to_owned())
    }

    pub fn from_toml(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let path = path.as_ref();
        if !path.exists() {
            return Err(ConfigError::FileNotFound {
                path: path.display().to_string(),
            });
        }
        let content = fs::read_to_string(path).map_err(|_| ConfigError::Read {
            path: path.display().to_string(),
        })?;
        let value: toml::Value = content.parse().map_err(|_| ConfigError::Parse {
            path: path.display().to_string(),
        })?;
        let mut config: Self = value.try_into().map_err(|_| {
            ConfigError::validation("configuration contains an unsupported field, type, or value")
        })?;
        config.validate()?;
        if config.server.port == 0 {
            return Err(ConfigError::validation(
                "server.port must be between 1 and 65535 in production configuration",
            ));
        }
        Ok(config)
    }

    pub fn validate(&mut self) -> Result<(), ConfigError> {
        if self.providers.is_empty() && !self.accounts.is_empty() {
            let accounts = std::mem::take(&mut self.accounts);
            self.providers.insert(
                DEFAULT_PROVIDER_ID.into(),
                ProviderConfig {
                    id: DEFAULT_PROVIDER_ID.into(),
                    base_url: self.upstream.base_url.clone(),
                    protocols: vec!["openai".into(), "anthropic".into()],
                    accounts,
                    ..Default::default()
                },
            );
        }
        for proxy in self.proxies.values() {
            let count = [proxy.url.as_ref(), proxy.url_env.as_ref()]
                .into_iter()
                .filter(|value| value.is_some_and(|value| !value.is_empty()))
                .count();
            if count != 1 {
                return Err(ConfigError::validation(
                    "Proxy config must set exactly one of url or url_env",
                ));
            }
        }
        let mut account_names = BTreeSet::new();
        for (provider_id, provider) in &mut self.providers {
            if provider.id != *provider_id {
                return Err(ConfigError::validation(format!(
                    "Provider key does not match declared id for {provider_id}"
                )));
            }
            validate_provider(provider, &self.proxies, &mut account_names)?;
        }
        validate_model_routers(&self.model_routers)?;
        for override_config in self.model_overrides.values() {
            validate_model_limits(
                override_config.max_context_tokens,
                override_config.max_input_tokens,
                override_config.max_output_tokens,
            )?;
        }
        validate_enum(
            &self.models.expose_mode,
            &["union", "intersection", "healthy_union"],
            "models.expose_mode",
        )?;
        validate_enum(
            &self.models.catalog_withdrawal_policy,
            &["preserve_until_health", "confirmed_once", "confirmed_twice"],
            "models.catalog_withdrawal_policy",
        )?;
        validate_enum(&self.routing.strategy, &["quota_fair"], "routing.strategy")?;
        validate_enum(
            &self.routing.local_quota_mode,
            &["score_only", "hard_cap"],
            "routing.local_quota_mode",
        )?;
        validate_enum(
            &self.routing.fairness_mode,
            &["off", "round_robin", "random"],
            "routing.fairness_mode",
        )?;
        validate_enum(
            &self.routing.fairness_scope,
            &[
                "provider_model_protocol",
                "provider_model",
                "priority_model_protocol",
            ],
            "routing.fairness_scope",
        )?;
        validate_enum(
            &self.pricing.fallback,
            &["generic_estimate", "off"],
            "pricing.fallback",
        )?;
        validate_enum(
            &self.transcoder.loss_policy,
            &["warn", "reject"],
            "transcoder.loss_policy",
        )?;
        validate_enum(
            &self.transcoder.budget_resolution_policy,
            &["lenient", "strict"],
            "transcoder.budget_resolution_policy",
        )?;
        if self.dashboard.store_request_content {
            return Err(ConfigError::validation(
                "dashboard.store_request_content must be false; request content must not be persisted",
            ));
        }
        if self.server.threads == 0 || self.server.threads > 64 {
            return Err(ConfigError::validation(
                "server.threads must be between 1 and 64",
            ));
        }
        if self.server.max_request_body_bytes == 0 {
            return Err(ConfigError::validation(
                "server.max_request_body_bytes must be greater than zero",
            ));
        }
        validate_positive_pair(
            self.upstream.max_keepalive,
            self.upstream.max_connections,
            "upstream",
        )?;
        validate_positive_pair(
            self.network.max_keepalive,
            self.network.max_connections,
            "network",
        )?;
        if self.readiness_probe.freshness_s <= self.readiness_probe.interval_s {
            return Err(ConfigError::validation(
                "readiness_probe.freshness_s must be greater than readiness_probe.interval_s",
            ));
        }
        if self.database.busy_timeout_ms == 0 {
            return Err(ConfigError::validation(
                "database.busy_timeout_ms must be greater than zero",
            ));
        }
        if self.limits.five_hour_microdollars == 0
            || self.limits.weekly_microdollars == 0
            || self.limits.monthly_microdollars == 0
        {
            return Err(ConfigError::validation("limits must be greater than zero"));
        }
        Ok(())
    }

    pub fn all_accounts(&self) -> Vec<&AccountConfig> {
        self.providers
            .values()
            .flat_map(|p| p.accounts.iter())
            .collect()
    }

    pub fn validate_account_credentials(&self) -> Result<(), ConfigError> {
        for (provider_id, provider) in &self.providers {
            if provider.auth.mode == "none"
                && provider
                    .wire_surfaces
                    .values()
                    .all(|s| s.auth.as_ref().is_none_or(|a| a.mode == "none"))
            {
                continue;
            }
            for account in &provider.accounts {
                if !account.enabled {
                    continue;
                }
                let value = account
                    .api_key
                    .clone()
                    .or_else(|| env::var(&account.api_key_env).ok());
                let Some(key) = value else {
                    return Err(ConfigError::validation(format!(
                        "Provider {provider_id:?} account {:?}: credential is not set",
                        account.name
                    )));
                };
                if key.chars().any(|c| matches!(c, '\r' | '\n' | '\0')) {
                    return Err(ConfigError::validation(format!(
                        "Provider {provider_id:?} account {:?}: credential contains CR, LF, or NUL",
                        account.name
                    )));
                }
                if key.trim().is_empty() {
                    return Err(ConfigError::validation(format!(
                        "Account {:?} has a whitespace-only API key",
                        account.name
                    )));
                }
            }
        }
        Ok(())
    }

    pub fn runtime_path(path: &str) -> PathBuf {
        expand_path(path)
    }

    /// Resolve the server key using the same inline-before-environment rule as
    /// the Python server. The value is used only by inbound authentication.
    pub fn resolved_server_api_key(&self) -> Option<String> {
        self.server
            .api_key
            .clone()
            .or_else(|| env::var(&self.server.api_key_env).ok())
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
    }
}

fn validate_positive_pair(
    keepalive: u32,
    connections: u32,
    section: &str,
) -> Result<(), ConfigError> {
    if keepalive == 0 || connections == 0 {
        return Err(ConfigError::validation(format!(
            "{section} connection limits must be greater than zero"
        )));
    }
    if keepalive > connections {
        return Err(ConfigError::validation(format!(
            "{section}.max_keepalive must not exceed max_connections"
        )));
    }
    Ok(())
}

fn validate_enum(value: &str, allowed: &[&str], field: &str) -> Result<(), ConfigError> {
    if !allowed.contains(&value) {
        return Err(ConfigError::validation(format!("{field} is invalid")));
    }
    Ok(())
}

fn validate_provider(
    provider: &mut ProviderConfig,
    proxies: &BTreeMap<String, ProxyConfig>,
    names: &mut BTreeSet<String>,
) -> Result<(), ConfigError> {
    if provider.id.is_empty()
        || !provider
            .id
            .bytes()
            .enumerate()
            .all(|(index, byte)| byte.is_ascii_alphanumeric() || (index > 0 && byte == b'-'))
        || provider.id.ends_with('-')
    {
        return Err(ConfigError::validation(
            "Provider id must be alphanumeric with optional hyphens",
        ));
    }
    let authority = provider
        .base_url
        .split_once("://")
        .and_then(|(_, rest)| rest.split('/').next())
        .unwrap_or_default();
    if provider.base_url.trim() != provider.base_url
        || provider.base_url.chars().any(char::is_whitespace)
        || !(provider.base_url.starts_with("http://") || provider.base_url.starts_with("https://"))
        || authority.is_empty()
        || authority.starts_with(':')
        || provider.base_url.contains('@')
        || provider.base_url.contains('?')
        || provider.base_url.contains('#')
    {
        return Err(ConfigError::validation(
            "Provider base_url must be an absolute credential-free HTTP(S) URL without whitespace, query, or fragment",
        ));
    }
    if provider.protocols.is_empty()
        || provider
            .protocols
            .iter()
            .any(|p| p != "openai" && p != "anthropic")
    {
        return Err(ConfigError::validation(
            "Provider protocols must contain only openai or anthropic",
        ));
    }
    validate_positive_pair(provider.max_keepalive, provider.max_connections, "provider")?;
    for value in [
        provider.connect_timeout_s,
        provider.read_timeout_s,
        provider.write_timeout_s,
        provider.pool_timeout_s,
    ] {
        if !value.is_finite() || value <= 0.0 {
            return Err(ConfigError::validation(
                "provider timeout must be greater than zero",
            ));
        }
    }
    if !provider.keepalive_timeout_s.is_finite() || provider.keepalive_timeout_s < 0.0 {
        return Err(ConfigError::validation(
            "provider.keepalive_timeout_s must be non-negative",
        ));
    }
    validate_enum(
        &provider.stream_completion_policy,
        &["strict", "compatible", "permissive_observe"],
        "provider.stream_completion_policy",
    )?;
    for value in [
        provider.stream_timeouts.first_byte_timeout_s,
        provider.stream_timeouts.idle_timeout_s,
    ] {
        if value.is_some_and(|value| !value.is_finite() || value <= 0.0 || value > 86_400.0) {
            return Err(ConfigError::validation(
                "provider stream timeout is invalid",
            ));
        }
    }
    if provider
        .stream_timeouts
        .max_lifetime_s
        .is_some_and(|value| !value.is_finite() || !(0.0..=86_400.0).contains(&value))
    {
        return Err(ConfigError::validation(
            "provider max_lifetime_s is invalid",
        ));
    }
    provider.models_method = provider.models_method.to_ascii_uppercase();
    if provider.models_method != "GET" && provider.models_method != "POST" {
        return Err(ConfigError::validation(
            "provider models_method must be GET or POST",
        ));
    }
    if provider.models_endpoint.is_none() && !provider.models_path.is_empty() {
        provider.models_endpoint = Some(ProviderModelsEndpointConfig {
            method: provider.models_method.clone(),
            path: provider.models_path.clone(),
            ..Default::default()
        });
    }
    if provider.wire_surfaces.is_empty() {
        if provider.protocols.iter().any(|p| p == "openai") && !provider.openai_path.is_empty() {
            provider.wire_surfaces.insert(
                "openai_chat_completions".into(),
                ProviderWireSurfaceConfig {
                    path_template: provider.openai_path.clone(),
                    ..Default::default()
                },
            );
        }
        if provider.responses_path.is_some() {
            provider.wire_surfaces.insert(
                "openai_responses".into(),
                ProviderWireSurfaceConfig {
                    path_template: provider.responses_path.clone().unwrap_or_default(),
                    ..Default::default()
                },
            );
        }
        if provider.protocols.iter().any(|p| p == "anthropic")
            && !provider.anthropic_path.is_empty()
        {
            provider.wire_surfaces.insert(
                "anthropic_messages".into(),
                ProviderWireSurfaceConfig {
                    path_template: provider.anthropic_path.clone(),
                    ..Default::default()
                },
            );
        }
    }
    validate_auth(&provider.auth)?;
    validate_headers(&provider.headers, Some(&provider.auth), "provider")?;
    for (surface, candidate) in &provider.wire_surfaces {
        validate_path(&candidate.path_template)?;
        if let Some(path) = &candidate.stream_path_template {
            validate_path(path)?;
        }
        if let Some(auth) = &candidate.auth {
            validate_auth(auth)?;
        }
        validate_headers(
            &candidate.headers,
            candidate.auth.as_ref().or(Some(&provider.auth)),
            surface,
        )?;
    }
    let mut static_model_ids = BTreeSet::new();
    for model in &provider.static_models {
        if model.id.trim().is_empty()
            || !static_model_ids.insert(model.id.clone())
            || model
                .protocol
                .as_ref()
                .is_some_and(|protocol| protocol != "openai" && protocol != "anthropic")
            || model.max_context_tokens.is_some_and(|context| context == 0)
            || model.max_input_tokens.is_some_and(|limit| limit == 0)
            || model.max_output_tokens.is_some_and(|limit| limit == 0)
            || model
                .max_input_tokens
                .zip(model.max_context_tokens)
                .is_some_and(|(limit, context)| limit > context)
            || model
                .max_output_tokens
                .zip(model.max_context_tokens)
                .is_some_and(|(limit, context)| limit > context)
        {
            return Err(ConfigError::validation(
                "provider static model declaration is invalid",
            ));
        }
    }
    for override_config in provider.model_overrides.values() {
        validate_model_limits(
            override_config.max_context_tokens,
            override_config.max_input_tokens,
            override_config.max_output_tokens,
        )?;
        for value in [
            override_config.input_price_per_1k,
            override_config.output_price_per_1k,
        ] {
            if value.is_some_and(|value| !value.is_finite() || value < 0.0) {
                return Err(ConfigError::validation("model price must be non-negative"));
            }
        }
    }
    for capabilities in provider.model_capabilities.values() {
        if let Some(thinking) = &capabilities.thinking {
            if thinking
                .budget_tokens_min
                .zip(thinking.budget_tokens_max)
                .is_some_and(|(minimum, maximum)| minimum > maximum)
                || thinking.budget_tokens_min.is_some_and(|value| value == 0)
                || thinking.budget_tokens_max.is_some_and(|value| value == 0)
                || thinking
                    .effort_to_budget_tokens
                    .as_ref()
                    .is_some_and(|map| map.values().any(|value| *value == 0))
            {
                return Err(ConfigError::validation(
                    "thinking capability override is invalid",
                ));
            }
        }
    }
    for (model, preference) in &provider.model_wire {
        if model.trim().is_empty()
            || !provider
                .wire_surfaces
                .contains_key(&preference.preferred_surface)
        {
            return Err(ConfigError::validation(
                "provider model_wire references an unavailable wire surface",
            ));
        }
    }
    for account in &provider.accounts {
        if account.name.trim().is_empty() {
            return Err(ConfigError::validation("account name must not be empty"));
        }
        if !names.insert(account.name.clone()) {
            return Err(ConfigError::validation(format!(
                "Duplicate account name: {:?}",
                account.name
            )));
        }
        if account.weight <= 0.0 {
            return Err(ConfigError::validation(format!(
                "Account {:?} has non-positive weight",
                account.name
            )));
        }
        let proxy_count = [
            account.proxy.as_ref(),
            account.proxy_url.as_ref(),
            account.proxy_url_env.as_ref(),
        ]
        .into_iter()
        .filter(|v| v.is_some())
        .count();
        if proxy_count > 1 {
            return Err(ConfigError::validation(format!(
                "Account {:?} must set at most one proxy source",
                account.name
            )));
        }
        if let Some(name) = &account.proxy {
            if !proxies.contains_key(name) {
                return Err(ConfigError::validation(
                    "account references an unknown proxy",
                ));
            }
        }
        let needs_key = provider.auth.mode != "none"
            || provider
                .wire_surfaces
                .values()
                .any(|s| s.auth.as_ref().is_some_and(|a| a.mode != "none"));
        if needs_key && account.api_key.is_none() && account.api_key_env.trim().is_empty() {
            return Err(ConfigError::validation(format!(
                "Account {:?} must set api_key or api_key_env",
                account.name
            )));
        }
    }
    Ok(())
}

fn validate_model_limits(
    context: Option<u64>,
    input: Option<u64>,
    output: Option<u64>,
) -> Result<(), ConfigError> {
    if context.is_some_and(|value| value == 0)
        || input.is_some_and(|value| value == 0)
        || output.is_some_and(|value| value == 0)
        || input
            .zip(context)
            .is_some_and(|(input, context)| input > context)
        || output
            .zip(context)
            .is_some_and(|(output, context)| output > context)
    {
        return Err(ConfigError::validation(
            "model input/output limits are invalid",
        ));
    }
    Ok(())
}

fn validate_auth(auth: &ProviderAuthConfig) -> Result<(), ConfigError> {
    if !["bearer", "api_key", "raw_authorization", "none"].contains(&auth.mode.as_str())
        || !valid_header_name(&auth.header)
        || auth.scheme.is_empty()
        || auth.scheme.chars().any(char::is_whitespace)
    {
        return Err(ConfigError::validation(
            "invalid provider authentication configuration",
        ));
    }
    let mut seen = BTreeSet::from([auth.header.to_ascii_lowercase()]);
    if auth.mode == "none" && !auth.additional.is_empty() {
        return Err(ConfigError::validation(
            "provider auth has additional entries but mode is none",
        ));
    }
    for item in &auth.additional {
        if !["bearer", "api_key", "raw_authorization"].contains(&item.mode.as_str())
            || !valid_header_name(&item.header)
            || item.scheme.is_empty()
            || item.scheme.chars().any(char::is_whitespace)
            || !seen.insert(item.header.to_ascii_lowercase())
        {
            return Err(ConfigError::validation(
                "invalid or duplicate provider authentication header",
            ));
        }
    }
    Ok(())
}
fn validate_headers(
    headers: &[ProviderStaticHeaderConfig],
    auth: Option<&ProviderAuthConfig>,
    owner: &str,
) -> Result<(), ConfigError> {
    let mut seen = BTreeSet::new();
    let auth_names: BTreeSet<String> = auth
        .into_iter()
        .flat_map(|a| {
            std::iter::once(a.header.to_ascii_lowercase())
                .chain(a.additional.iter().map(|x| x.header.to_ascii_lowercase()))
        })
        .collect();
    for h in headers {
        if !valid_header_name(&h.name)
            || (h.value.is_none() == h.value_env.is_none())
            || !seen.insert(h.name.to_ascii_lowercase())
            || auth_names.contains(&h.name.to_ascii_lowercase())
        {
            return Err(ConfigError::validation(format!(
                "{owner} contains an invalid, duplicate, or authentication-conflicting static header"
            )));
        }
        if h.value
            .as_ref()
            .is_some_and(|v| v.chars().any(|c| matches!(c, '\r' | '\n' | '\0')))
        {
            return Err(ConfigError::validation(
                "HTTP header values must not contain CR, LF, or NUL",
            ));
        }
    }
    Ok(())
}
fn valid_header_name(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
}
fn validate_path(value: &str) -> Result<(), ConfigError> {
    let without_model = value.replace("{model}", "");
    if value.is_empty()
        || value.trim() != value
        || !value.starts_with('/')
        || value.contains('?')
        || value.contains('#')
        || without_model.contains('{')
        || without_model.contains('}')
    {
        return Err(ConfigError::validation(
            "wire path template must be a non-empty relative path",
        ));
    }
    Ok(())
}

fn validate_model_routers(
    routers: &BTreeMap<String, ModelRouterConfig>,
) -> Result<(), ConfigError> {
    for (virtual_id, router) in routers {
        if virtual_id.trim().is_empty()
            || virtual_id.contains('/')
            || virtual_id.len() > 128
            || router.routes.is_empty()
        {
            return Err(ConfigError::validation(
                "invalid model router virtual ID or empty routes",
            ));
        }
        if router.selector_model.trim().is_empty()
            || router.default_model.trim().is_empty()
            || router.selector_model.len() > 128
            || router.default_model.len() > 128
        {
            return Err(ConfigError::validation(
                "invalid model router model reference",
            ));
        }
        if routers.contains_key(&router.selector_model)
            || router
                .routes
                .values()
                .any(|r| routers.contains_key(&r.model))
        {
            return Err(ConfigError::validation(
                "model routers cannot target virtual models",
            ));
        }
        if !router
            .routes
            .values()
            .any(|r| r.model == router.default_model)
        {
            return Err(ConfigError::validation(
                "model router default_model must match a route model",
            ));
        }
        for (label, route) in &router.routes {
            if label.trim().is_empty()
                || label.len() > 128
                || route.model.trim().is_empty()
                || route.description.trim().is_empty()
                || route.description.len() > 512
            {
                return Err(ConfigError::validation("invalid model router route"));
            }
        }
    }
    Ok(())
}

pub fn resolve_config_path(cli_value: Option<&Path>) -> PathBuf {
    let config_env = env::var("EGGPOOL_CONFIG").ok();
    let xdg_config_home = env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    resolve_config_path_with(
        cli_value,
        config_env.as_deref(),
        xdg_config_home.as_deref(),
        &env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
    )
}

/// Pure form of config resolution used by differential tests and callers that
/// already own an environment snapshot.
pub fn resolve_config_path_with(
    cli_value: Option<&Path>,
    config_env: Option<&str>,
    xdg_config_home: Option<&Path>,
    cwd: &Path,
) -> PathBuf {
    if let Some(value) = cli_value.filter(|value| !value.as_os_str().is_empty()) {
        return expand_path_from(&value.to_string_lossy(), cwd);
    }
    if let Some(value) = config_env.filter(|value| !value.trim().is_empty()) {
        return expand_path_from(value, cwd);
    }
    let xdg = xdg_config_home
        .map(Path::to_path_buf)
        .unwrap_or_else(|| home_dir().join(".config"));
    let installed = xdg.join("eggpool/config.toml");
    if installed.exists() {
        return absolute(installed);
    }
    absolute(cwd.join("config.toml"))
}

pub fn default_config_dir() -> PathBuf {
    env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".config"))
        .join("eggpool")
}

pub fn default_data_dir() -> PathBuf {
    env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".local/share"))
        .join("eggpool")
}

pub fn default_config_path() -> PathBuf {
    default_config_dir().join("config.toml")
}

pub fn default_env_path() -> PathBuf {
    default_config_dir().join(".env")
}

pub fn default_state_dir() -> PathBuf {
    env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".local/state"))
        .join("eggpool")
}

pub fn resolve_env_path(config_path: Option<&Path>) -> Option<PathBuf> {
    if let Some(value) = env::var_os("EGGPOOL_ENV").filter(|value| !value.is_empty()) {
        let path = expand_path(&value.to_string_lossy());
        return path.exists().then_some(path);
    }
    if let Some(config_path) = config_path {
        let path = config_path.parent()?.join(".env");
        if path.exists() {
            return Some(absolute(path));
        }
    }
    let path = default_env_path();
    path.exists().then_some(absolute(path))
}

fn expand_path(value: &str) -> PathBuf {
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    expand_path_from(value, &cwd)
}

fn expand_path_from(value: &str, cwd: &Path) -> PathBuf {
    let value = value.strip_prefix("~/").map_or_else(
        || value.to_owned(),
        |tail| home_dir().join(tail).to_string_lossy().into_owned(),
    );
    let path = PathBuf::from(value);
    if path.is_absolute() {
        path
    } else {
        cwd.join(path)
    }
}
fn absolute(path: PathBuf) -> PathBuf {
    if path.is_absolute() {
        path
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

pub fn content_digest(path: &Path) -> Result<String, ConfigError> {
    let bytes = fs::read(path).map_err(|_| ConfigError::Read {
        path: path.display().to_string(),
    })?;
    let digest = Sha256::digest(bytes);
    Ok(format!("{digest:x}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_the_python_contract() {
        let config = Config::default();
        assert_eq!(config.server.host, "127.0.0.1");
        assert_eq!(config.server.port, 11300);
        assert_eq!(config.server.api_key_env, "SERVER_API_KEY");
        assert_eq!(config.upstream.base_url, DEFAULT_UPSTREAM_URL);
        assert_eq!(config.database.busy_timeout_ms, 5000);
        assert!(config.database.wal);
        assert_eq!(config.models.refresh_interval_s, 300);
        assert_eq!(config.limits.monthly_microdollars, 60_000_000);
        assert!(config.transcoder.enabled);
        assert!(config.model_info.enabled);
    }

    #[test]
    fn config_resolution_preserves_documented_precedence() {
        let root = env::temp_dir().join(format!("eggpool-f003-{}", std::process::id()));
        let xdg = root.join("xdg");
        std::fs::create_dir_all(xdg.join("eggpool")).expect("create test roots");
        std::fs::write(xdg.join("eggpool/config.toml"), "").expect("write xdg config");
        let cli = root.join("cli.toml");
        let env_path = root.join("env.toml");
        let from_cli = resolve_config_path_with(
            Some(&cli),
            Some(env_path.to_str().unwrap()),
            Some(&xdg),
            &root,
        );
        assert_eq!(from_cli, cli);
        let from_env =
            resolve_config_path_with(None, Some(env_path.to_str().unwrap()), Some(&xdg), &root);
        assert_eq!(from_env, env_path);
        let from_xdg = resolve_config_path_with(None, None, Some(&xdg), &root);
        assert_eq!(from_xdg, xdg.join("eggpool/config.toml"));
        let empty_xdg = root.join("empty-xdg");
        let from_cwd = resolve_config_path_with(None, None, Some(&empty_xdg), &root);
        assert_eq!(from_cwd, root.join("config.toml"));
        std::fs::remove_dir_all(root).expect("remove test roots");
    }

    #[test]
    fn validation_rejects_proxy_header_url_and_router_contract_errors() {
        let mut config = Config::default();
        config.proxies.insert(
            "bad".into(),
            ProxyConfig {
                url: Some("".into()),
                url_env: None,
            },
        );
        assert!(config.validate().is_err());

        let mut config = Config::default();
        config.providers.insert(
            "edge".into(),
            ProviderConfig {
                id: "edge".into(),
                base_url: "https://example.test".into(),
                headers: vec![ProviderStaticHeaderConfig {
                    name: "Authorization".into(),
                    value: Some("x".into()),
                    value_env: None,
                }],
                ..Default::default()
            },
        );
        assert!(config.validate().is_err());

        let mut config = Config::default();
        config.model_routers.insert(
            "virtual".into(),
            ModelRouterConfig {
                selector_model: "virtual".into(),
                default_model: "real".into(),
                routes: BTreeMap::from([(
                    "r".into(),
                    ModelRouteConfig {
                        model: "real".into(),
                        description: "route".into(),
                    },
                )]),
                ..Default::default()
            },
        );
        assert!(config.validate().is_err());
    }

    #[test]
    fn account_proxy_resolution_matches_precedence_and_env_trimming() {
        let mut config = Config::default();
        config.proxies.insert(
            "shared".into(),
            ProxyConfig {
                url: Some("http://named.example:8080".into()),
                url_env: None,
            },
        );
        assert_eq!(
            config
                .resolve_account_proxy_url(&AccountConfig {
                    name: "inline".into(),
                    proxy_url: Some("  socks5://inline.example:1080  ".into()),
                    ..Default::default()
                })
                .unwrap(),
            Some("  socks5://inline.example:1080  ".into())
        );
        assert_eq!(
            config
                .resolve_account_proxy_url(&AccountConfig {
                    name: "named".into(),
                    proxy: Some("shared".into()),
                    ..Default::default()
                })
                .unwrap(),
            Some("http://named.example:8080".into())
        );
        assert_eq!(
            config
                .resolve_account_proxy_url(&AccountConfig {
                    name: "direct".into(),
                    ..Default::default()
                })
                .unwrap(),
            None
        );
        assert_eq!(
            Config::resolve_proxy_url_env_value(
                "ACCOUNT_PROXY",
                "account",
                Some(" \tsocks5://proxy.example:1080 \n".into())
            )
            .unwrap(),
            "socks5://proxy.example:1080"
        );
        for (value, expected) in [
            (None, "not set"),
            (Some(String::new()), "not set"),
            (Some(" \t\n ".into()), "whitespace-only"),
        ] {
            let error = Config::resolve_proxy_url_env_value("MISSING", "account", value)
                .expect_err("invalid proxy environment value");
            assert!(error.to_string().contains(expected));
        }
    }
}
