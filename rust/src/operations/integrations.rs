//! Agent integration context, renderers, and safe snippet delivery.
//!
//! Portable Codex/OpenCode policy (projection, rendering, catalog validation,
//! TOML mutation, connection profiles, tokens) lives in the small
//! `eggpool-client-config` crate. This module is the EggPool adapter: it
//! converts `Config`/catalog/database facts into those portable types,
//! resolves server keys/endpoints, owns local lifecycle paths, delivery, and
//! server-only projection loading. It must not grow a second divergent
//! Codex/OpenCode engine.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fmt,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use eggpool_client_config as client_config;
use serde_json::{Map, Value, json};
use thiserror::Error;
use tokio::{io::AsyncWriteExt, process::Command, time::timeout};

use crate::{
    Config, ConfigError,
    db::{
        AccountRepository, CatalogModel, CatalogRepository, Database, DatabaseError,
        ProviderModelMetadata,
    },
    operations::config_mutation::{self, MutationError},
};

pub use client_config::{
    AgentModelCapabilities, AgentModelProjection, AgentReasoningCapabilities, IntegrationModel,
    ModelLimits,
};

const MAX_SNIPPET_BYTES: usize = 4 * 1024 * 1024;
const CLIPBOARD_TIMEOUT: Duration = Duration::from_secs(5);
const DEPRECATED_MODEL_ID: &str = "__deprecated__";
const INTEGRATION_SCHEMA_VERSION: u32 = client_config::OWNERSHIP_SCHEMA_VERSION;

#[derive(Debug, Error)]
pub enum IntegrationError {
    #[error("configuration error: {0}")]
    Config(#[from] ConfigError),
    #[error("configuration mutation failed: {0}")]
    Mutation(#[from] MutationError),
    #[error("catalog read failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("integration JSON serialization failed")]
    Json(#[from] serde_json::Error),
    #[error("portable client configuration failed: {0}")]
    ClientConfig(#[from] client_config::ClientConfigError),
    #[error("--base-url must be an absolute HTTP(S) URL")]
    InvalidBaseUrl,
    #[error(
        "--model is required for {target} in write mode because the catalog has {count} models"
    )]
    ModelRequired { target: String, count: usize },
    #[error("output file already exists: {path}")]
    OutputExists { path: PathBuf },
    #[error("integration output failed")]
    Io(#[source] io::Error),
    #[error("generated snippet is too large")]
    TooLarge,
    #[error("model catalog exceeds bounded size ({count} models)")]
    CatalogTooLarge { count: usize },
    #[error("client config drift detected: {detail}")]
    Drift { detail: String },
    #[error("refusing to rewrite {path}: {detail}")]
    UnsafeRewrite { path: PathBuf, detail: String },
    #[error("lifecycle flag is not supported for {target}")]
    UnsupportedLifecycle { target: String },
}

/// Derive a provider-neutral projection from one validated integration model.
///
/// Thin adapter over [`client_config::project_model`]; the portable crate is
/// the single authoritative implementation.
pub fn project_model(model: &IntegrationModel) -> AgentModelProjection {
    client_config::project_model(model)
}

/// Collapse multiple projections for one public ID into conservative
/// guaranteed capabilities.
///
/// Thin adapter over [`client_config::aggregate_projections`].
pub fn aggregate_projections(
    public_id: &str,
    display_name: &str,
    projections: &[AgentModelCapabilities],
) -> AgentModelCapabilities {
    client_config::aggregate_projections(public_id, display_name, projections)
}

/// Project every public model/alias in deterministic order.
///
/// Thin adapter: converts the application context into the portable slice
/// form and delegates to [`client_config::project_models`].
pub fn project_context_models(context: &IntegrationContext) -> Vec<AgentModelProjection> {
    client_config::project_models(&context.models)
}

#[derive(Clone, PartialEq)]
pub struct IntegrationContext {
    pub config_path: PathBuf,
    pub api_key: String,
    pub base_url: String,
    pub base_url_root: String,
    pub host: String,
    pub port: u16,
    pub models: Vec<IntegrationModel>,
    pub collapse_models: bool,
    pub config_mutated: bool,
    pub transcoder_mutated: bool,
}

impl fmt::Debug for IntegrationContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("IntegrationContext")
            .field("config_path", &self.config_path)
            .field("api_key", &config_mutation::redact_key(&self.api_key))
            .field("base_url", &self.base_url)
            .field("base_url_root", &self.base_url_root)
            .field("host", &self.host)
            .field("port", &self.port)
            .field("models", &self.models)
            .field("collapse_models", &self.collapse_models)
            .field("config_mutated", &self.config_mutated)
            .field("transcoder_mutated", &self.transcoder_mutated)
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Target {
    Opencode,
    ClaudeCode,
    Aider,
    Codex,
    QwenCode,
    Kilo,
    Continue,
    Cline,
    RooCode,
    Goose,
    Openhands,
}

impl Target {
    pub const ALL: [Self; 11] = [
        Self::Opencode,
        Self::ClaudeCode,
        Self::Aider,
        Self::Codex,
        Self::QwenCode,
        Self::Kilo,
        Self::Continue,
        Self::Cline,
        Self::RooCode,
        Self::Goose,
        Self::Openhands,
    ];

    pub const fn name(self) -> &'static str {
        match self {
            Self::Opencode => "opencode",
            Self::ClaudeCode => "claude-code",
            Self::Aider => "aider",
            Self::Codex => "codex",
            Self::QwenCode => "qwen-code",
            Self::Kilo => "kilo",
            Self::Continue => "continue",
            Self::Cline => "cline",
            Self::RooCode => "roo-code",
            Self::Goose => "goose",
            Self::Openhands => "openhands",
        }
    }

    pub const fn requires_model(self) -> bool {
        matches!(self, Self::Continue | Self::Goose | Self::Openhands)
    }

    pub const fn contains_secret(self) -> bool {
        // The generic delivery path remains fail-closed for targets whose
        // rendered artifact embeds the resolved key. Codex carries only the
        // environment-variable name and must remain printable by default.
        // OpenCode uses `{env:EGGPOOL_API_KEY}` interpolation and likewise
        // carries no resolved secret.
        !matches!(self, Self::Codex | Self::Opencode)
    }
}

#[derive(Debug, Clone)]
pub struct SnippetOptions {
    pub print_secret: bool,
    pub no_clipboard: bool,
    pub force: bool,
    pub output: Option<PathBuf>,
    pub write: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub stdout: Option<String>,
    pub messages: Vec<String>,
}

/// Build the full shared context.  This is the only target-facing path that
/// may resolve the key, read persisted catalog state, or enable transcoding.
pub async fn build_integration_context(
    config_path: &Path,
) -> Result<IntegrationContext, IntegrationError> {
    build_context(config_path, true, true).await
}

/// Build only the endpoint/key context used by Claude Code.  Its frozen
/// command contract does not inspect catalog state or alter transcoder config.
pub fn build_endpoint_context(config_path: &Path) -> Result<IntegrationContext, IntegrationError> {
    let config = Config::from_toml(config_path)?;
    let (api_key, config_mutated) = config_mutation::resolve_server_key(config_path)?;
    let port = config.server.port;
    let host = detect_lan_ip();
    Ok(IntegrationContext {
        config_path: config_path.to_owned(),
        api_key,
        base_url: format!("http://{host}:{port}/v1"),
        base_url_root: format!("http://{host}:{port}"),
        host,
        port,
        models: Vec::new(),
        collapse_models: config.models.collapse_models,
        config_mutated,
        transcoder_mutated: false,
    })
}

async fn build_context(
    config_path: &Path,
    load_catalog: bool,
    enable_transcoder: bool,
) -> Result<IntegrationContext, IntegrationError> {
    let config = Config::from_toml(config_path)?;
    let (api_key, config_mutated) = config_mutation::resolve_server_key(config_path)?;
    let port = config.server.port;
    let host = detect_lan_ip();
    let mut models = if load_catalog {
        match load_catalog_models(&config).await {
            Ok(models) => models,
            Err(error) => {
                tracing::warn!(error = %error, "catalog unavailable while rendering integration config");
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };
    // Static models are configuration facts and remain available when the
    // persisted catalog has not been created yet or cannot be read.
    merge_static_models(&mut models, &config);
    apply_model_overrides(&mut models, &config);
    models.sort_by(|left, right| left.model_id.cmp(&right.model_id));
    let should_enable_transcoder = enable_transcoder
        && !config.transcoder.enabled
        && config.providers.values().any(|provider| {
            provider.accounts.iter().any(|account| account.enabled)
                && provider
                    .protocols
                    .iter()
                    .any(|protocol| protocol == "anthropic")
                && !provider
                    .protocols
                    .iter()
                    .any(|protocol| protocol == "openai")
        });
    let transcoder_mutated = if should_enable_transcoder {
        config_mutation::set_transcoder_enabled(config_path, true)?
    } else {
        false
    };
    Ok(IntegrationContext {
        config_path: config_path.to_owned(),
        api_key,
        base_url: format!("http://{host}:{port}/v1"),
        base_url_root: format!("http://{host}:{port}"),
        host,
        port,
        models,
        collapse_models: config.models.collapse_models,
        config_mutated,
        transcoder_mutated,
    })
}

/// Apply the shared `--host`/`--base-url` endpoint overrides.
pub fn apply_overrides(
    mut context: IntegrationContext,
    host: Option<&str>,
    base_url: Option<&str>,
) -> Result<IntegrationContext, IntegrationError> {
    if let Some(base_url) = base_url {
        let uri = base_url
            .parse::<http::Uri>()
            .map_err(|_| IntegrationError::InvalidBaseUrl)?;
        let valid_scheme = uri
            .scheme_str()
            .is_some_and(|scheme| matches!(scheme, "http" | "https"));
        if !valid_scheme || uri.authority().is_none() || base_url.chars().any(char::is_whitespace) {
            return Err(IntegrationError::InvalidBaseUrl);
        }
        let normalized = base_url.trim_end_matches('/').to_owned();
        let root = normalized
            .strip_suffix("/v1")
            .unwrap_or(&normalized)
            .to_owned();
        context.base_url = normalized;
        context.base_url_root = root;
        if let Some(host) = host {
            context.host = host.to_owned();
        }
    } else if let Some(host) = host {
        context.host = host.to_owned();
        context.base_url = format!("http://{host}:{}/v1", context.port);
        context.base_url_root = format!("http://{host}:{}", context.port);
    }
    Ok(context)
}

pub fn resolve_model(
    target: Target,
    requested: Option<&str>,
    context: &IntegrationContext,
    write_mode: bool,
) -> Result<Option<String>, IntegrationError> {
    if let Some(model) = requested {
        return Ok(Some(model.to_owned()));
    }
    if context.models.len() == 1 {
        return Ok(Some(context.models[0].model_id.clone()));
    }
    if write_mode && target.requires_model() {
        return Err(IntegrationError::ModelRequired {
            target: target.name().to_owned(),
            count: context.models.len(),
        });
    }
    Ok(None)
}

pub fn render_target(
    target: Target,
    context: &IntegrationContext,
    model: Option<&str>,
) -> Result<String, IntegrationError> {
    match target {
        Target::Opencode => build_opencode_config_json(context),
        Target::ClaudeCode => build_claude_code_json(context),
        Target::Aider => Ok(build_aider_env_snippet(context, model)),
        Target::Codex => Ok(build_codex_toml_snippet(context, model)?),
        Target::QwenCode => Ok(build_qwen_code_provider_snippet(context, model)?),
        Target::Kilo => Ok(build_kilo_snippet(context, model)?),
        Target::Continue => Ok(build_continue_yaml_snippet(context, model)),
        Target::Cline => Ok(build_cline_snippet(context, model)?),
        Target::RooCode => Ok(build_roo_code_snippet(context, model)?),
        Target::Goose => Ok(build_goose_env_snippet(context, model)),
        Target::Openhands => Ok(build_openhands_env_snippet(context, model)),
    }
}

pub async fn deliver(
    snippet: &str,
    target: Target,
    options: &SnippetOptions,
    default_path: Option<&Path>,
    paste_hint: Option<&str>,
) -> Result<Delivery, IntegrationError> {
    if snippet.len() > MAX_SNIPPET_BYTES {
        return Err(IntegrationError::TooLarge);
    }
    let secret = target.contains_secret();
    if options.output.is_some() || options.write {
        let path = options
            .output
            .clone()
            .or_else(|| default_path.map(Path::to_owned))
            .ok_or_else(|| {
                IntegrationError::Io(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "--write requires --output or a known default path",
                ))
            })?;
        let backup = write_snippet(&path, snippet, options.force)?;
        let mut messages = vec![format!("Wrote config to {}", path.display())];
        if let Some(backup) = backup {
            messages.insert(0, format!("Backup created: {}", backup.display()));
        }
        return Ok(Delivery {
            stdout: None,
            messages,
        });
    }

    let copied = if options.no_clipboard {
        false
    } else {
        copy_to_clipboard(snippet).await
    };
    let mut messages = Vec::new();
    if copied {
        messages.push("Copied config to clipboard.".to_owned());
        if secret && !options.print_secret {
            messages.push(
                "Secret included in clipboard. Use --print-secret to also print to stdout."
                    .to_owned(),
            );
            return Ok(Delivery {
                stdout: None,
                messages,
            });
        }
    }
    let stdout = if options.print_secret || !secret {
        Some(snippet.to_owned())
    } else {
        messages.push(
            "Secret not printed to stdout. Use --print-secret to include it, or use `eggpool getkey` to retrieve it."
                .to_owned(),
        );
        None
    };
    if let Some(hint) = paste_hint {
        messages.push(hint.to_owned());
    }
    Ok(Delivery { stdout, messages })
}

pub fn default_path(target: Target) -> Option<PathBuf> {
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    match target {
        Target::Aider => Some(PathBuf::from(".env.eggpool")),
        Target::Continue => Some(home.join(".continue/eggpool.yaml")),
        Target::Cline => Some(PathBuf::from("cline-eggpool.json")),
        Target::RooCode => Some(PathBuf::from("roo-code-eggpool.json")),
        _ => None,
    }
}

pub fn paste_hint(target: Target) -> Option<&'static str> {
    match target {
        Target::Aider => Some("Source the file: source .env.eggpool"),
        Target::Continue => Some("Paste into ~/.continue/config.yaml under the models: key."),
        Target::Cline => {
            Some("Paste values into Cline extension settings (OpenAI Compatible provider).")
        }
        Target::RooCode => {
            Some("Paste values into Roo Code extension settings (OpenAI Compatible provider).")
        }
        Target::Goose => Some("Export these variables before running Goose."),
        Target::Openhands => Some("Pass these environment variables to the OpenHands runtime."),
        Target::Codex => Some(
            "Set EGGPOOL_API_KEY in the environment used to launch Codex; retrieve the current key with `eggpool getkey`.",
        ),
        _ => None,
    }
}

pub fn build_aider_env_snippet(context: &IntegrationContext, model: Option<&str>) -> String {
    let mut lines = vec![
        format!("export OPENAI_API_KEY={}", shell_quote(&context.api_key)),
        format!("export OPENAI_API_BASE={}", shell_quote(&context.base_url)),
    ];
    if let Some(model) = model {
        lines.push(format!("aider --model {}", shell_quote(model)));
    }
    lines.join("\n")
}

pub fn build_codex_toml_snippet(
    context: &IntegrationContext,
    model: Option<&str>,
) -> Result<String, IntegrationError> {
    Ok(client_config::render_codex_toml(&context.base_url, model)?)
}

pub fn build_codex_toml_snippet_with_catalog(
    context: &IntegrationContext,
    model: Option<&str>,
    catalog_path: Option<&str>,
) -> Result<String, IntegrationError> {
    Ok(client_config::render_codex_toml_with_catalog(
        &context.base_url,
        model,
        catalog_path,
    )?)
}

/// Render a current Codex model catalog from the provider-neutral projection.
///
/// Thin adapter over [`client_config::build_codex_catalog_json`]; the
/// portable crate owns deterministic ordering, bounds, sanitization, and the
/// strict-parser contract. The catalog must never carry credentials.
pub fn build_codex_catalog_json(context: &IntegrationContext) -> Result<String, IntegrationError> {
    let projections = project_context_models(context);
    let rendered =
        client_config::build_codex_catalog_json(&projections).map_err(|error| match error {
            client_config::ClientConfigError::CatalogTooLarge { count } => {
                IntegrationError::CatalogTooLarge { count }
            }
            client_config::ClientConfigError::DocumentTooLarge => IntegrationError::TooLarge,
            other => IntegrationError::ClientConfig(other),
        })?;
    // The catalog must never carry credentials or raw source metadata.
    debug_assert!(!rendered.contains(&context.api_key));
    Ok(rendered)
}

/// Validate that a generated catalog meets the strict-parser contract.
///
/// Thin adapter over [`client_config::validate_codex_catalog_json`].
pub fn validate_codex_catalog_json(
    catalog_json: &str,
    api_key: &str,
) -> Result<usize, IntegrationError> {
    Ok(client_config::validate_codex_catalog_json(
        catalog_json,
        api_key,
    )?)
}

fn build_claude_code_json(context: &IntegrationContext) -> Result<String, IntegrationError> {
    Ok(format!(
        "{{\n  \"api_key\": {},\n  \"base_url\": {}\n}}",
        json_string(&context.api_key)?,
        json_string(&context.base_url)?,
    ))
}

fn build_qwen_code_provider_snippet(
    context: &IntegrationContext,
    model: Option<&str>,
) -> Result<String, IntegrationError> {
    let mut provider = BTreeMap::from([
        ("api_key", Value::String(context.api_key.clone())),
        ("base_url", Value::String(context.base_url.clone())),
        ("name", Value::String("EggPool".to_owned())),
        ("type", Value::String("openai".to_owned())),
    ]);
    if let Some(model) = model {
        provider.insert("model", Value::String(model.to_owned()));
    }
    Ok(serde_json::to_string_pretty(&provider)?)
}

fn build_kilo_snippet(
    context: &IntegrationContext,
    model: Option<&str>,
) -> Result<String, IntegrationError> {
    let mut models = BTreeMap::new();
    for entry in &context.models {
        let mut model_entry: BTreeMap<String, Value> = BTreeMap::new();
        if let Some(value) = entry.limits.context_tokens.filter(|value| *value > 0) {
            model_entry.insert("context_length".to_owned(), json!(value));
        }
        models.insert(
            entry.model_id.clone(),
            Value::Object(model_entry.into_iter().collect()),
        );
    }
    if let Some(model) = model {
        models
            .entry(model.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
    }
    let provider = BTreeMap::from([
        ("apiBase", Value::String(context.base_url.clone())),
        ("apiKey", Value::String(context.api_key.clone())),
        ("models", serde_json::to_value(models)?),
        ("name", Value::String("EggPool".to_owned())),
    ]);
    Ok(serde_json::to_string_pretty(&BTreeMap::from([(
        "openai_compatible",
        serde_json::to_value(provider)?,
    )]))?)
}

fn build_continue_yaml_snippet(context: &IntegrationContext, model: Option<&str>) -> String {
    let mut lines = vec![
        "models:".to_owned(),
        format!("  - title: {}", yaml_string("EggPool")),
        format!("    provider: {}", yaml_string("openai")),
    ];
    if let Some(model) =
        model.or_else(|| (context.models.len() == 1).then(|| context.models[0].model_id.as_str()))
    {
        lines.push(format!("    model: {}", yaml_string(model)));
    }
    lines.extend([
        format!("    apiBase: {}", yaml_string(&context.base_url)),
        format!("    apiKey: {}", yaml_string(&context.api_key)),
    ]);
    lines.join("\n")
}

fn build_cline_snippet(
    context: &IntegrationContext,
    model: Option<&str>,
) -> Result<String, IntegrationError> {
    let mut profile = BTreeMap::from([
        ("apiProvider", Value::String("openai-compatible".to_owned())),
        ("openAiApiKey", Value::String(context.api_key.clone())),
        ("openAiBaseUrl", Value::String(context.base_url.clone())),
    ]);
    if let Some(model) =
        model.or_else(|| (context.models.len() == 1).then(|| context.models[0].model_id.as_str()))
    {
        profile.insert("openAiModelId", Value::String(model.to_owned()));
    }
    Ok(serde_json::to_string_pretty(&profile)?)
}

fn build_roo_code_snippet(
    context: &IntegrationContext,
    model: Option<&str>,
) -> Result<String, IntegrationError> {
    build_cline_snippet(context, model)
}

fn build_goose_env_snippet(context: &IntegrationContext, model: Option<&str>) -> String {
    let mut lines = vec![
        format!(
            "export GOOSE_PROVIDER__BASE_URL={}",
            shell_quote(&context.base_url)
        ),
        format!(
            "export GOOSE_PROVIDER__API_KEY={}",
            shell_quote(&context.api_key)
        ),
    ];
    if let Some(model) = model {
        lines.push(format!(
            "export GOOSE_PROVIDER__MODEL={}",
            shell_quote(model)
        ));
    }
    lines.join("\n")
}

fn build_openhands_env_snippet(context: &IntegrationContext, model: Option<&str>) -> String {
    let mut lines = vec![
        format!("export LLM_BASE_URL={}", shell_quote(&context.base_url)),
        format!("export LLM_API_KEY={}", shell_quote(&context.api_key)),
    ];
    if let Some(model) = model {
        lines.push(format!("export LLM_MODEL={}", shell_quote(model)));
    }
    lines.join("\n")
}

fn build_opencode_config_json(context: &IntegrationContext) -> Result<String, IntegrationError> {
    let projections = project_context_models(context);
    Ok(client_config::render_opencode_config(
        &context.base_url,
        &projections,
    )?)
}

// ---------------------------------------------------------------------------
// Managed lifecycle (Plan 200 workstreams 6-8)
// ---------------------------------------------------------------------------

/// Lifecycle actions shared by managed Codex/OpenCode installation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecycleAction {
    Check,
    Apply,
    Sync,
    Remove,
    DryRun,
}

#[derive(Debug, Clone)]
pub struct LifecycleOptions {
    pub action: LifecycleAction,
    pub dry_run: bool,
    pub force: bool,
    pub model: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LifecycleReport {
    pub target: String,
    pub action: String,
    pub up_to_date: bool,
    pub changed: bool,
    pub messages: Vec<String>,
    pub diff: Option<String>,
}

impl LifecycleReport {
    fn new(target: &str, action: LifecycleAction, dry_run: bool) -> Self {
        let action_name = match (action, dry_run) {
            (_, true) if action != LifecycleAction::Check => {
                format!("{} --dry-run", action_name(action))
            }
            _ => action_name(action).to_owned(),
        };
        Self {
            target: target.to_owned(),
            action: action_name,
            up_to_date: true,
            changed: false,
            messages: Vec::new(),
            diff: None,
        }
    }
}

fn action_name(action: LifecycleAction) -> &'static str {
    match action {
        LifecycleAction::Check => "check",
        LifecycleAction::Apply => "apply",
        LifecycleAction::Sync => "sync",
        LifecycleAction::Remove => "remove",
        LifecycleAction::DryRun => "dry-run",
    }
}

type OwnershipManifest = client_config::OwnershipManifest;

fn sha256_hex(bytes: &[u8]) -> String {
    client_config::sha256_hex(bytes)
}

/// Resolve the EggPool-owned state directory for integration artifacts.
pub fn integration_state_dir() -> PathBuf {
    crate::operations::paths::RuntimePaths::resolve()
        .state_dir
        .join("integrations")
}

pub fn codex_catalog_path_for_state(state_dir: &Path) -> PathBuf {
    state_dir.join("codex").join("eggpool-codex-models.json")
}

pub fn codex_manifest_path_for_state(state_dir: &Path) -> PathBuf {
    state_dir.join("codex").join("manifest.json")
}

pub fn opencode_manifest_path_for_state(state_dir: &Path) -> PathBuf {
    state_dir.join("opencode").join("manifest.json")
}

/// Resolve the Codex client config path, respecting `CODEX_HOME`.
pub fn codex_config_path() -> PathBuf {
    if let Some(home) = env::var_os("CODEX_HOME")
        && !home.is_empty()
    {
        return PathBuf::from(home).join("config.toml");
    }
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join(".codex").join("config.toml")
}

/// Resolve the OpenCode client config path.
///
/// Uses `OPENCODE_CONFIG` when set, otherwise the global
/// `~/.config/opencode/opencode.json` location.
pub fn opencode_config_path() -> PathBuf {
    if let Some(custom) = env::var_os("OPENCODE_CONFIG")
        && !custom.is_empty()
    {
        return PathBuf::from(custom);
    }
    if let Some(xdg) = env::var_os("XDG_CONFIG_HOME")
        && !xdg.is_empty()
    {
        return PathBuf::from(xdg).join("opencode").join("opencode.json");
    }
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join(".config").join("opencode").join("opencode.json")
}

fn load_manifest(path: &Path) -> Option<OwnershipManifest> {
    let bytes = fs::read(path).ok()?;
    serde_json::from_slice(&bytes).ok()
}

fn save_manifest(path: &Path, manifest: &OwnershipManifest) -> Result<(), IntegrationError> {
    let bytes = serde_json::to_vec_pretty(manifest)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(IntegrationError::Io)?;
    }
    atomic_write(path, &bytes, None)?;
    Ok(())
}

type CodexPrevious = client_config::codex::CodexPrevious;

fn apply_codex_text_mutation(
    existing: &str,
    base_url: &str,
    catalog_path: &str,
    model: Option<&str>,
    manage_model: bool,
) -> (String, CodexPrevious) {
    client_config::apply_codex_text_mutation(existing, base_url, catalog_path, model, manage_model)
}

fn remove_codex_owned_text(existing: &str, previous: &CodexPrevious, owned_model: bool) -> String {
    client_config::remove_codex_owned_text(existing, previous, owned_model)
}

fn codex_expected_snippet(
    context: &IntegrationContext,
    model: Option<&str>,
    catalog_path: &Path,
) -> Result<String, IntegrationError> {
    build_codex_toml_snippet_with_catalog(context, model, Some(&catalog_path.to_string_lossy()))
}

fn check_codex_state(
    config_text: &str,
    context: &IntegrationContext,
    model: Option<&str>,
    manage_model: bool,
    catalog_path: &Path,
    catalog_json: &str,
) -> Vec<String> {
    // Filesystem observations stay in the adapter; pure TOML policy lives in
    // the portable crate.
    let catalog_exists = catalog_path.exists();
    let catalog_current = fs::read_to_string(catalog_path).ok().as_deref() == Some(catalog_json);
    client_config::check_codex_state(client_config::CodexStateCheck {
        config_text,
        base_url: &context.base_url,
        api_key: &context.api_key,
        model,
        manage_model,
        catalog_path: &catalog_path.to_string_lossy(),
        catalog_exists,
        catalog_current,
    })
}

/// Run the managed Codex lifecycle.
///
/// `state_dir` and `config_path` are injectable for deterministic tests;
/// pass `None` to use the XDG/`CODEX_HOME` resolution.
pub fn codex_lifecycle(
    context: &IntegrationContext,
    options: &LifecycleOptions,
    state_dir: Option<&Path>,
    config_path_override: Option<&Path>,
) -> Result<LifecycleReport, IntegrationError> {
    let default_state = integration_state_dir();
    let state_dir = state_dir.unwrap_or(&default_state);
    let default_config = codex_config_path();
    let config_path = config_path_override.unwrap_or(&default_config);
    let catalog_path = codex_catalog_path_for_state(state_dir);
    let manifest_path = codex_manifest_path_for_state(state_dir);

    let catalog_json = build_codex_catalog_json(context)?;
    validate_codex_catalog_json(&catalog_json, &context.api_key)?;
    let manage_model = options.model.is_some();
    let expected_snippet =
        codex_expected_snippet(context, options.model.as_deref(), &catalog_path)?;

    let existing_text = fs::read_to_string(config_path).unwrap_or_default();
    let manifest = load_manifest(&manifest_path);

    let mut report = LifecycleReport::new("codex", options.action, options.dry_run);

    // Drift detection: if EggPool previously wrote this config and the file
    // changed unexpectedly since, refuse to clobber user edits.
    if matches!(
        options.action,
        LifecycleAction::Apply | LifecycleAction::Sync | LifecycleAction::Remove
    ) && !options.dry_run
        && let Some(manifest) = &manifest
        && manifest.client_config_path == config_path
    {
        let current_hash = sha256_hex(existing_text.as_bytes());
        if current_hash != manifest.post_edit_hash && !options.force {
            return Err(IntegrationError::Drift {
                detail: format!(
                    "Codex config {} changed since EggPool's last write; refusing to overwrite without --force",
                    config_path.display()
                ),
            });
        }
    }

    match options.action {
        LifecycleAction::Check => {
            let issues = check_codex_state(
                &existing_text,
                context,
                options.model.as_deref(),
                manage_model
                    || manifest.as_ref().is_some_and(|manifest| {
                        manifest.owned_fields.contains(&"model".to_owned())
                    }),
                &catalog_path,
                &catalog_json,
            );
            if issues.is_empty() {
                report.messages.push(format!(
                    "Codex config {} and catalog {} are current.",
                    config_path.display(),
                    catalog_path.display()
                ));
            } else {
                report.up_to_date = false;
                for issue in &issues {
                    report.messages.push(format!("drift: {issue}"));
                }
                report.diff = Some(expected_snippet);
            }
            Ok(report)
        }
        LifecycleAction::DryRun => {
            let (proposed, _) = apply_codex_text_mutation(
                &existing_text,
                &context.base_url,
                &catalog_path.to_string_lossy(),
                options.model.as_deref(),
                manage_model,
            );
            report.diff = Some(proposed.clone());
            report.messages.push(format!(
                "Would write catalog {} ({} bytes, {} models).",
                catalog_path.display(),
                catalog_json.len(),
                project_context_models(context).len()
            ));
            report.messages.push(format!(
                "Would update Codex config {}.",
                config_path.display()
            ));
            report.up_to_date = existing_text == proposed
                && catalog_path.exists()
                && fs::read_to_string(&catalog_path).ok().as_deref() == Some(&catalog_json);
            Ok(report)
        }
        LifecycleAction::Apply | LifecycleAction::Sync => {
            let (proposed, previous) = apply_codex_text_mutation(
                &existing_text,
                &context.base_url,
                &catalog_path.to_string_lossy(),
                options.model.as_deref(),
                manage_model,
            );
            if options.dry_run {
                report.diff = Some(proposed);
                report
                    .messages
                    .push("Dry run: no files written.".to_owned());
                return Ok(report);
            }
            let catalog_changed =
                fs::read_to_string(&catalog_path).ok().as_deref() != Some(&catalog_json);
            let config_changed = existing_text != proposed;
            if let Some(parent) = catalog_path.parent() {
                fs::create_dir_all(parent).map_err(IntegrationError::Io)?;
            }
            atomic_write(&catalog_path, catalog_json.as_bytes(), None)?;
            if config_changed {
                if let Some(parent) = config_path.parent() {
                    fs::create_dir_all(parent).map_err(IntegrationError::Io)?;
                }
                atomic_write(config_path, proposed.as_bytes(), None)?;
            }
            let mut owned_fields = vec![
                "model_provider".to_owned(),
                "model_catalog_json".to_owned(),
                "model_providers.eggpool".to_owned(),
            ];
            if manage_model
                || manifest
                    .as_ref()
                    .is_some_and(|manifest| manifest.owned_fields.contains(&"model".to_owned()))
            {
                owned_fields.push("model".to_owned());
            }
            let mut previous_values = manifest
                .as_ref()
                .map(|manifest| manifest.previous_values.clone())
                .unwrap_or_default();
            // Only capture pre-EggPool values on first ownership; later syncs
            // preserve the original restoration evidence.
            if manifest.is_none() {
                if let Some(value) = previous.model_provider {
                    previous_values.insert("model_provider".to_owned(), Value::String(value));
                }
                if let Some(value) = previous.model_catalog_json {
                    previous_values.insert("model_catalog_json".to_owned(), Value::String(value));
                }
                if let Some(value) = previous.model {
                    previous_values.insert("model".to_owned(), Value::String(value));
                }
            }
            let manifest = OwnershipManifest {
                schema_version: INTEGRATION_SCHEMA_VERSION,
                eggpool_version: crate::version::PACKAGE_VERSION.to_owned(),
                target: "codex".to_owned(),
                client_config_path: config_path.to_owned(),
                pre_edit_hash: manifest
                    .as_ref()
                    .map(|manifest| manifest.pre_edit_hash.clone())
                    .unwrap_or_else(|| sha256_hex(existing_text.as_bytes())),
                post_edit_hash: sha256_hex(proposed.as_bytes()),
                owned_fields,
                previous_values,
                generated_catalog_path: Some(catalog_path.clone()),
                generated_catalog_hash: Some(sha256_hex(catalog_json.as_bytes())),
                base_url: context.base_url.clone(),
            };
            save_manifest(&manifest_path, &manifest)?;
            report.changed = catalog_changed || config_changed;
            report.up_to_date = true;
            if config_changed {
                report
                    .messages
                    .push(format!("Updated Codex config {}.", config_path.display()));
            }
            if catalog_changed {
                report
                    .messages
                    .push(format!("Wrote Codex catalog {}.", catalog_path.display()));
            }
            if !report.changed {
                report
                    .messages
                    .push("Codex config and catalog already current.".to_owned());
            }
            report.messages.push(
                "Set EGGPOOL_API_KEY in the environment used to launch Codex; retrieve the current key with `eggpool getkey`.".to_owned(),
            );
            Ok(report)
        }
        LifecycleAction::Remove => {
            let Some(manifest) = manifest else {
                report
                    .messages
                    .push("No EggPool-owned Codex state to remove.".to_owned());
                return Ok(report);
            };
            if manifest.client_config_path != config_path {
                return Err(IntegrationError::Drift {
                    detail: "Codex ownership manifest points at a different config path".to_owned(),
                });
            }
            if options.dry_run {
                report.diff = Some(format!(
                    "Would remove catalog {} and restore owned Codex fields in {}.",
                    catalog_path.display(),
                    config_path.display()
                ));
                return Ok(report);
            }
            let previous = CodexPrevious {
                model_provider: manifest
                    .previous_values
                    .get("model_provider")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                model_catalog_json: manifest
                    .previous_values
                    .get("model_catalog_json")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                model: manifest
                    .previous_values
                    .get("model")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                provider_table: None,
            };
            let owned_model = manifest.owned_fields.contains(&"model".to_owned());
            let restored = remove_codex_owned_text(&existing_text, &previous, owned_model);
            if restored != existing_text && !existing_text.is_empty() {
                atomic_write(config_path, restored.as_bytes(), None)?;
                report.changed = true;
                report.messages.push(format!(
                    "Restored owned fields in {}.",
                    config_path.display()
                ));
            } else if existing_text.is_empty() {
                report
                    .messages
                    .push("Codex config already absent.".to_owned());
            } else {
                report
                    .messages
                    .push("No owned Codex fields to restore.".to_owned());
            }
            if catalog_path.exists() {
                fs::remove_file(&catalog_path).map_err(IntegrationError::Io)?;
                report.changed = true;
                report.messages.push(format!(
                    "Removed generated catalog {}.",
                    catalog_path.display()
                ));
            }
            if manifest_path.exists() {
                fs::remove_file(&manifest_path).map_err(IntegrationError::Io)?;
            }
            Ok(report)
        }
    }
}

fn opencode_expected_provider(context: &IntegrationContext) -> Result<Value, IntegrationError> {
    let projections = project_context_models(context);
    Ok(client_config::expected_opencode_provider(
        &context.base_url,
        &projections,
    )?)
}

fn has_jsonc_comments(raw: &str) -> bool {
    client_config::has_jsonc_comments(raw)
}

/// Run the managed OpenCode lifecycle.
///
/// OpenCode configs support JSONC (JSON with comments). A normal serde JSON
/// round trip would destroy comments, so existing files with comments are
/// never silently rewritten: `--apply` is limited to absent/new files in that
/// case and existing-file users stay on generated output until a safe
/// preserving mutator is available.
pub fn opencode_lifecycle(
    context: &IntegrationContext,
    options: &LifecycleOptions,
    state_dir: Option<&Path>,
    config_path_override: Option<&Path>,
) -> Result<LifecycleReport, IntegrationError> {
    let default_state = integration_state_dir();
    let state_dir = state_dir.unwrap_or(&default_state);
    let default_config = opencode_config_path();
    let config_path = config_path_override.unwrap_or(&default_config);
    let manifest_path = opencode_manifest_path_for_state(state_dir);

    let expected_provider = opencode_expected_provider(context)?;
    let mut report = LifecycleReport::new("opencode", options.action, options.dry_run);
    let existing_raw = fs::read_to_string(config_path).unwrap_or_default();
    let manifest = load_manifest(&manifest_path);

    if matches!(
        options.action,
        LifecycleAction::Apply | LifecycleAction::Sync | LifecycleAction::Remove
    ) && !options.dry_run
        && let Some(manifest) = &manifest
        && manifest.client_config_path == config_path
    {
        let current_hash = sha256_hex(existing_raw.as_bytes());
        if current_hash != manifest.post_edit_hash && !options.force {
            return Err(IntegrationError::Drift {
                detail: format!(
                    "OpenCode config {} changed since EggPool's last write; refusing to overwrite without --force",
                    config_path.display()
                ),
            });
        }
    }

    match options.action {
        LifecycleAction::Check => {
            if existing_raw.is_empty() {
                report.up_to_date = false;
                report.messages.push(format!(
                    "OpenCode config {} is missing; apply would create it.",
                    config_path.display()
                ));
                return Ok(report);
            }
            if has_jsonc_comments(&existing_raw) {
                report.up_to_date = false;
                report.messages.push(
                    "OpenCode config uses JSONC comments; managed rewrite is deferred to generated output.".to_owned(),
                );
                return Ok(report);
            }
            let value: Value =
                serde_json::from_str(&existing_raw).map_err(|_| IntegrationError::Drift {
                    detail: "OpenCode config is not valid JSON".to_owned(),
                })?;
            let current = value
                .get("provider")
                .and_then(|provider| provider.get("eggpool"));
            if current == Some(&expected_provider) {
                report.messages.push(format!(
                    "OpenCode config {} is current.",
                    config_path.display()
                ));
            } else {
                report.up_to_date = false;
                report
                    .messages
                    .push("OpenCode eggpool provider differs.".to_owned());
                report.diff = Some(serde_json::to_string_pretty(&expected_provider)?);
            }
            Ok(report)
        }
        LifecycleAction::DryRun => {
            report.diff = Some(serde_json::to_string_pretty(&expected_provider)?);
            report.messages.push(format!(
                "Would converge OpenCode provider eggpool in {}.",
                config_path.display()
            ));
            Ok(report)
        }
        LifecycleAction::Apply | LifecycleAction::Sync => {
            if !existing_raw.is_empty() && has_jsonc_comments(&existing_raw) {
                return Err(IntegrationError::UnsafeRewrite {
                    path: config_path.to_owned(),
                    detail: "existing OpenCode config uses JSONC comments; refusing to rewrite (use --dry-run to review generated output)".to_owned(),
                });
            }
            let mut root = if existing_raw.trim().is_empty() {
                Map::new()
            } else {
                serde_json::from_str::<Value>(&existing_raw)
                    .map_err(|_| IntegrationError::UnsafeRewrite {
                        path: config_path.to_owned(),
                        detail: "existing OpenCode config is not valid JSON; refusing to rewrite"
                            .to_owned(),
                    })?
                    .as_object()
                    .cloned()
                    .ok_or_else(|| IntegrationError::UnsafeRewrite {
                        path: config_path.to_owned(),
                        detail: "existing OpenCode config root is not an object".to_owned(),
                    })?
            };
            if !root.contains_key("$schema") {
                root.insert(
                    "$schema".to_owned(),
                    Value::String("https://opencode.ai/config.json".to_owned()),
                );
            }
            let mut provider = root
                .get("provider")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let changed = provider.get("eggpool") != Some(&expected_provider);
            provider.insert("eggpool".to_owned(), expected_provider.clone());
            root.insert("provider".to_owned(), Value::Object(provider));
            let proposed = serde_json::to_string_pretty(&Value::Object(root))? + "\n";
            if options.dry_run {
                report.diff = Some(proposed);
                report
                    .messages
                    .push("Dry run: no files written.".to_owned());
                return Ok(report);
            }
            if existing_raw == proposed {
                report.messages.push(format!(
                    "OpenCode config {} already current.",
                    config_path.display()
                ));
                return Ok(report);
            }
            if let Some(parent) = config_path.parent() {
                fs::create_dir_all(parent).map_err(IntegrationError::Io)?;
            }
            atomic_write(config_path, proposed.as_bytes(), None)?;
            let manifest = OwnershipManifest {
                schema_version: INTEGRATION_SCHEMA_VERSION,
                eggpool_version: crate::version::PACKAGE_VERSION.to_owned(),
                target: "opencode".to_owned(),
                client_config_path: config_path.to_owned(),
                pre_edit_hash: manifest
                    .as_ref()
                    .map(|manifest| manifest.pre_edit_hash.clone())
                    .unwrap_or_else(|| sha256_hex(existing_raw.as_bytes())),
                post_edit_hash: sha256_hex(proposed.as_bytes()),
                owned_fields: vec!["provider.eggpool".to_owned()],
                previous_values: Map::new(),
                generated_catalog_path: None,
                generated_catalog_hash: None,
                base_url: context.base_url.clone(),
            };
            save_manifest(&manifest_path, &manifest)?;
            report.changed = changed || existing_raw.is_empty();
            report.up_to_date = true;
            report.messages.push(format!(
                "Updated OpenCode config {}.",
                config_path.display()
            ));
            report
                .messages
                .push("Set EGGPOOL_API_KEY in the environment used to launch OpenCode.".to_owned());
            Ok(report)
        }
        LifecycleAction::Remove => {
            let Some(manifest) = manifest else {
                report
                    .messages
                    .push("No EggPool-owned OpenCode state to remove.".to_owned());
                return Ok(report);
            };
            if manifest.client_config_path != config_path {
                return Err(IntegrationError::Drift {
                    detail: "OpenCode ownership manifest points at a different config path"
                        .to_owned(),
                });
            }
            if existing_raw.is_empty() {
                if manifest_path.exists() {
                    fs::remove_file(&manifest_path).map_err(IntegrationError::Io)?;
                }
                report
                    .messages
                    .push("OpenCode config already absent.".to_owned());
                return Ok(report);
            }
            if has_jsonc_comments(&existing_raw) {
                return Err(IntegrationError::UnsafeRewrite {
                    path: config_path.to_owned(),
                    detail: "existing OpenCode config uses JSONC comments; refusing to remove"
                        .to_owned(),
                });
            }
            let value: Value = serde_json::from_str(&existing_raw).map_err(|_| {
                IntegrationError::UnsafeRewrite {
                    path: config_path.to_owned(),
                    detail: "existing OpenCode config is not valid JSON".to_owned(),
                }
            })?;
            let mut root = value.as_object().cloned().unwrap_or_default();
            if let Some(provider) = root.get_mut("provider").and_then(Value::as_object_mut) {
                provider.remove("eggpool");
            }
            let proposed = if root
                .get("provider")
                .and_then(Value::as_object)
                .is_some_and(Map::is_empty)
            {
                root.remove("provider");
                serde_json::to_string_pretty(&Value::Object(root))? + "\n"
            } else {
                serde_json::to_string_pretty(&Value::Object(root))? + "\n"
            };
            if options.dry_run {
                report.diff = Some(proposed);
                return Ok(report);
            }
            atomic_write(config_path, proposed.as_bytes(), None)?;
            if manifest_path.exists() {
                fs::remove_file(&manifest_path).map_err(IntegrationError::Io)?;
            }
            report.changed = true;
            report.messages.push(format!(
                "Removed EggPool provider from {}.",
                config_path.display()
            ));
            Ok(report)
        }
    }
}

async fn load_catalog_models(config: &Config) -> Result<Vec<IntegrationModel>, IntegrationError> {
    let database = Database::open(crate::db::DatabaseConfig::from(&config.database)).await?;
    let result = read_catalog_models(&database, config).await;
    let close = database.close().await;
    match (result, close) {
        (Err(error), _) => Err(error),
        (Ok(_), Err(error)) => Err(error.into()),
        (Ok(models), Ok(())) => Ok(models),
    }
}

async fn read_catalog_models(
    database: &Database,
    config: &Config,
) -> Result<Vec<IntegrationModel>, IntegrationError> {
    let catalog = CatalogRepository::new(database);
    let globals = catalog.list_models().await?;
    let providers = catalog.list_provider_models().await?;
    let supports = catalog.list_account_model_support().await?;
    let accounts = AccountRepository::new(database).list_all().await?;
    let enabled_accounts: BTreeMap<i64, String> = accounts
        .into_iter()
        .filter(|account| account.enabled)
        .map(|account| (account.id, account.provider_id))
        .collect();
    let active_pairs: BTreeSet<(String, String)> = supports
        .into_iter()
        .filter(|support| support.enabled)
        .filter_map(|support| {
            enabled_accounts
                .get(&support.account_id)
                .map(|provider| (support.model_id, provider.clone()))
        })
        .collect();
    let mut models = if config.models.collapse_models {
        globals
            .iter()
            .filter(|row| row.model_id != DEPRECATED_MODEL_ID && supported_protocol(&row.protocol))
            .map(|row| model_from_global(row, config))
            .collect::<Vec<_>>()
    } else {
        let selected = providers
            .iter()
            .filter(|row| supported_protocol(row.protocol.as_deref().unwrap_or_default()))
            .filter(|row| {
                active_pairs.is_empty()
                    || active_pairs.contains(&(row.model_id.clone(), row.provider_id.clone()))
            })
            .map(|row| model_from_provider(row, config))
            .collect::<Vec<_>>();
        if selected.is_empty() {
            providers
                .iter()
                .filter(|row| supported_protocol(row.protocol.as_deref().unwrap_or_default()))
                .map(|row| model_from_provider(row, config))
                .collect()
        } else {
            selected
        }
    };
    merge_static_models(&mut models, config);
    apply_model_overrides(&mut models, config);
    models.sort_by(|left, right| left.model_id.cmp(&right.model_id));
    Ok(models)
}

fn model_from_global(row: &CatalogModel, config: &Config) -> IntegrationModel {
    let capabilities = json_object(&row.capabilities);
    let source_metadata = json_object(&row.source_metadata);
    IntegrationModel {
        model_id: row.model_id.clone(),
        base_model_id: row.model_id.clone(),
        provider_id: None,
        display_name: display_name(&row.model_id, row.display_name.as_deref(), &source_metadata),
        limits: resolve_limits(config, None, &row.model_id, &capabilities, &source_metadata),
        capabilities,
        source_metadata,
    }
}

fn model_from_provider(row: &ProviderModelMetadata, config: &Config) -> IntegrationModel {
    let capabilities = json_object(&row.capabilities);
    let source_metadata = json_object(&row.source_metadata);
    let display = display_name(&row.model_id, row.display_name.as_deref(), &source_metadata);
    IntegrationModel {
        model_id: format!("{}/{}", row.model_id, row.provider_id),
        base_model_id: row.model_id.clone(),
        provider_id: Some(row.provider_id.clone()),
        display_name: display,
        limits: resolve_limits(
            config,
            Some(&row.provider_id),
            &row.model_id,
            &capabilities,
            &source_metadata,
        ),
        capabilities,
        source_metadata,
    }
}

fn merge_static_models(models: &mut Vec<IntegrationModel>, config: &Config) {
    let existing: BTreeSet<String> = models.iter().map(|model| model.model_id.clone()).collect();
    let mut seen = existing;
    for (provider_id, provider) in &config.providers {
        if !provider.accounts.iter().any(|account| account.enabled) {
            continue;
        }
        for static_model in &provider.static_models {
            let model_id = if config.models.collapse_models {
                static_model.id.clone()
            } else {
                format!("{}/{}", static_model.id, provider_id)
            };
            if !seen.insert(model_id.clone()) {
                continue;
            }
            let mut capabilities = Map::new();
            if let Some(value) = static_model.supports_tools {
                capabilities.insert("supports_tools".to_owned(), json!(value));
            }
            if let Some(value) = static_model.supports_vision {
                capabilities.insert("supports_vision".to_owned(), json!(value));
            }
            if let Some(value) = static_model.max_context_tokens {
                capabilities.insert("max_context_tokens".to_owned(), json!(value));
            }
            if let Some(value) = static_model.max_input_tokens {
                capabilities.insert("max_input_tokens".to_owned(), json!(value));
            }
            if let Some(value) = static_model.max_output_tokens {
                capabilities.insert("max_output_tokens".to_owned(), json!(value));
            }
            let mut metadata = Map::new();
            for (key, value) in &static_model.source_metadata {
                metadata.insert(
                    key.clone(),
                    serde_json::to_value(value).unwrap_or(Value::Null),
                );
            }
            metadata.insert(
                "source".to_owned(),
                Value::String("static_config".to_owned()),
            );
            let capabilities = Value::Object(capabilities);
            let source_metadata = Value::Object(metadata);
            models.push(IntegrationModel {
                model_id,
                base_model_id: static_model.id.clone(),
                provider_id: Some(provider_id.clone()),
                display_name: static_model
                    .display_name
                    .clone()
                    .unwrap_or_else(|| static_model.id.clone()),
                limits: resolve_limits(
                    config,
                    Some(provider_id),
                    &static_model.id,
                    &capabilities,
                    &source_metadata,
                ),
                capabilities,
                source_metadata,
            });
        }
    }
}

fn apply_model_overrides(models: &mut [IntegrationModel], config: &Config) {
    for model in models {
        let override_config = model
            .provider_id
            .as_deref()
            .and_then(|provider| config.providers.get(provider))
            .and_then(|provider| provider.model_capabilities.get(&model.base_model_id))
            .or_else(|| config.model_capabilities.get(&model.base_model_id));
        let Some(override_config) = override_config else {
            continue;
        };
        let Ok(value) = serde_json::to_value(override_config) else {
            continue;
        };
        if let (Some(base), Some(overrides)) =
            (model.capabilities.as_object_mut(), value.as_object())
        {
            for (key, value) in overrides {
                if !value.is_null() {
                    base.insert(key.clone(), value.clone());
                }
            }
        }
        if override_config
            .multimodal
            .as_ref()
            .and_then(|media| media.image_input.as_ref())
            .is_some_and(|image| image.base64 == Some(true) || image.url == Some(true))
            && let Some(base) = model.capabilities.as_object_mut()
        {
            base.insert("supports_vision".to_owned(), Value::Bool(true));
        }
    }
}

fn resolve_limits(
    config: &Config,
    provider_id: Option<&str>,
    model_id: &str,
    capabilities: &Value,
    source_metadata: &Value,
) -> ModelLimits {
    let provider_override = provider_id
        .and_then(|provider| config.providers.get(provider))
        .and_then(|provider| provider.model_overrides.get(model_id));
    let global_override = config.model_overrides.get(model_id);
    ModelLimits {
        context_tokens: first_limit(
            provider_override.and_then(|value| value.max_context_tokens),
            global_override.and_then(|value| value.max_context_tokens),
            capabilities,
            source_metadata,
            &[
                "max_context_tokens",
                "context_window",
                "context_length",
                "max_position_embeddings",
            ],
        ),
        input_tokens: first_limit(
            provider_override.and_then(|value| value.max_input_tokens),
            global_override.and_then(|value| value.max_input_tokens),
            capabilities,
            source_metadata,
            &["max_input_tokens", "input_token_limit"],
        ),
        output_tokens: first_limit(
            provider_override.and_then(|value| value.max_output_tokens),
            global_override.and_then(|value| value.max_output_tokens),
            capabilities,
            source_metadata,
            &[
                "max_output_tokens",
                "output_token_limit",
                "max_completion_tokens",
            ],
        ),
    }
}

fn first_limit(
    provider: Option<u64>,
    global: Option<u64>,
    capabilities: &Value,
    metadata: &Value,
    keys: &[&str],
) -> Option<u64> {
    provider
        .or(global)
        .or_else(|| {
            keys.iter()
                .find_map(|key| positive_u64(capabilities.get(*key)))
        })
        .or_else(|| keys.iter().find_map(|key| positive_u64(metadata.get(*key))))
}

fn positive_u64(value: Option<&Value>) -> Option<u64> {
    value
        .and_then(Value::as_u64)
        .filter(|value| *value > 0)
        .or_else(|| {
            value
                .and_then(Value::as_str)
                .and_then(|value| value.trim().parse().ok())
                .filter(|value: &u64| *value > 0)
        })
}

fn json_object(raw: &str) -> Value {
    serde_json::from_str(raw)
        .ok()
        .filter(Value::is_object)
        .unwrap_or_else(|| Value::Object(Map::new()))
}

fn display_name(model_id: &str, display_name: Option<&str>, metadata: &Value) -> String {
    display_name
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            metadata
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_else(|| model_id.to_owned())
}

fn supported_protocol(protocol: &str) -> bool {
    matches!(protocol, "openai" | "anthropic")
}

fn detect_lan_ip() -> String {
    std::net::UdpSocket::bind("0.0.0.0:0")
        .and_then(|socket| {
            socket.connect("10.255.255.255:1")?;
            socket.local_addr()
        })
        .map(|address| address.ip().to_string())
        .unwrap_or_else(|_| "127.0.0.1".to_owned())
}

fn json_string(value: &str) -> Result<String, IntegrationError> {
    Ok(serde_json::to_string(value)?)
}

fn yaml_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_owned())
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&byte))
    {
        return value.to_owned();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

fn write_snippet(
    path: &Path,
    snippet: &str,
    force: bool,
) -> Result<Option<PathBuf>, IntegrationError> {
    if path.exists() && !force {
        return Err(IntegrationError::OutputExists {
            path: path.to_owned(),
        });
    }
    let mut backup = None;
    let existing_mode = fs::metadata(path)
        .ok()
        .map(|metadata| metadata.permissions());
    if path.exists() {
        let existing = fs::read_to_string(path).map_err(IntegrationError::Io)?;
        let expected = format!("{snippet}\n");
        if existing != expected {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |duration| duration.as_secs());
            let stem = path
                .file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or("config");
            let extension = path
                .extension()
                .and_then(|value| value.to_str())
                .map_or(String::new(), |value| format!(".{value}"));
            let parent = path.parent().unwrap_or_else(|| Path::new("."));
            let mut candidate = parent.join(format!("{stem}.eggpool.bak.{timestamp}{extension}"));
            let mut suffix = 1_u32;
            while candidate.exists() {
                candidate = parent.join(format!(
                    "{stem}.eggpool.bak.{timestamp}-{suffix}{extension}"
                ));
                suffix += 1;
            }
            atomic_write(&candidate, existing.as_bytes(), None)?;
            backup = Some(candidate);
        }
    }
    atomic_write(path, format!("{snippet}\n").as_bytes(), existing_mode)?;
    Ok(backup)
}

fn atomic_write(
    path: &Path,
    bytes: &[u8],
    mode: Option<fs::Permissions>,
) -> Result<(), IntegrationError> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(IntegrationError::Io)?;
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| {
            IntegrationError::Io(io::Error::new(
                io::ErrorKind::InvalidInput,
                "output path has no file name",
            ))
        })?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos());
    let temporary = parent.join(format!(".{name}.tmp-{}-{nonce}", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary).map_err(IntegrationError::Io)?;
    let result = (|| {
        file.write_all(bytes).map_err(IntegrationError::Io)?;
        file.sync_all().map_err(IntegrationError::Io)?;
        if let Some(mode) = mode {
            fs::set_permissions(&temporary, mode).map_err(IntegrationError::Io)?;
        }
        fs::rename(&temporary, path).map_err(IntegrationError::Io)?;
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
        Ok::<(), IntegrationError>(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

async fn copy_to_clipboard(text: &str) -> bool {
    let commands: [(&str, &[&str]); 3] = [
        ("pbcopy", &[]),
        ("xclip", &["-selection", "clipboard"]),
        ("xsel", &["--clipboard", "--input"]),
    ];
    for (program, args) in commands {
        if which(program).is_none() {
            continue;
        }
        let Ok(mut child) = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        else {
            continue;
        };
        let Some(mut stdin) = child.stdin.take() else {
            continue;
        };
        let result = timeout(CLIPBOARD_TIMEOUT, async {
            stdin.write_all(text.as_bytes()).await?;
            drop(stdin);
            child.wait().await
        })
        .await;
        if matches!(result, Ok(Ok(status)) if status.success()) {
            return true;
        }
        if result.is_err() {
            let _ = child.start_kill();
        }
    }
    false
}

fn which(name: &str) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    env::split_paths(&path)
        .map(|directory| directory.join(name))
        .find(|candidate| candidate.is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn context() -> IntegrationContext {
        IntegrationContext {
            config_path: PathBuf::from("/dev/null"),
            api_key: "ep_test_key_123".to_owned(),
            base_url: "http://192.168.1.100:11300/v1".to_owned(),
            base_url_root: "http://192.168.1.100:11300".to_owned(),
            host: "192.168.1.100".to_owned(),
            port: 11300,
            models: vec![IntegrationModel {
                model_id: "gpt-4o/openai".to_owned(),
                base_model_id: "gpt-4o".to_owned(),
                provider_id: Some("openai".to_owned()),
                display_name: "GPT-4o".to_owned(),
                capabilities: Value::Object(Map::new()),
                source_metadata: Value::Object(Map::new()),
                limits: ModelLimits {
                    context_tokens: Some(128_000),
                    ..Default::default()
                },
            }],
            collapse_models: false,
            config_mutated: false,
            transcoder_mutated: false,
        }
    }

    #[test]
    fn all_parser_targets_have_renderers() {
        let context = context();
        for target in Target::ALL {
            let model = if target.requires_model() {
                Some("gpt-4o/openai")
            } else {
                None
            };
            let snippet = render_target(target, &context, model)
                .unwrap_or_else(|_| panic!("{}", target.name()));
            assert!(!snippet.is_empty(), "{}", target.name());
        }
    }

    #[test]
    fn shell_and_structured_renderers_escape_values() {
        let mut context = context();
        context.api_key = "ep_'key\\tail".to_owned();
        context.base_url = "http://host/\"v1".to_owned();
        let aider = build_aider_env_snippet(&context, Some("model with space"));
        assert!(aider.contains(r#"'ep_'"'"'key\tail'"#));
        let codex = build_codex_toml_snippet(&context, Some("model\"\\x")).expect("toml");
        assert!(codex.contains("model = \"model\\\"\\\\x\""));
    }

    #[test]
    fn codex_renderer_matches_the_http_responses_provider_contract() {
        let context = context();
        let snippet = build_codex_toml_snippet(&context, Some("router-alias")).expect("toml");
        let parsed: toml::Value = snippet.parse().expect("valid TOML");
        let provider = parsed
            .get("model_providers")
            .and_then(|value| value.get("eggpool"))
            .and_then(toml::Value::as_table)
            .expect("EggPool provider table");

        assert_eq!(
            parsed.get("model_provider").and_then(toml::Value::as_str),
            Some("eggpool")
        );
        assert_eq!(
            parsed.get("model").and_then(toml::Value::as_str),
            Some("router-alias")
        );
        assert_eq!(
            provider.get("name").and_then(toml::Value::as_str),
            Some("EggPool")
        );
        assert_eq!(
            provider.get("base_url").and_then(toml::Value::as_str),
            Some("http://192.168.1.100:11300/v1")
        );
        assert_eq!(
            provider.get("wire_api").and_then(toml::Value::as_str),
            Some("responses")
        );
        assert_eq!(
            provider
                .get("supports_websockets")
                .and_then(toml::Value::as_bool),
            Some(false)
        );
        assert_eq!(
            provider.get("env_key").and_then(toml::Value::as_str),
            Some("EGGPOOL_API_KEY")
        );
        assert!(!snippet.contains(&context.api_key));
        assert!(!snippet.contains("chat_completions"));
        assert!(!snippet.contains("OPENAI_API_KEY"));
        assert!(!snippet.contains("models/") && !snippet.contains("/catalog"));
    }

    #[test]
    fn codex_renderer_omits_model_when_none_is_selected() {
        let context = context();
        let snippet = build_codex_toml_snippet(&context, None).expect("toml");
        let parsed: toml::Value = snippet.parse().expect("valid TOML");

        assert_eq!(
            parsed.get("model_provider").and_then(toml::Value::as_str),
            Some("eggpool")
        );
        assert!(parsed.get("model").is_none());
        assert!(
            parsed
                .get("model_providers")
                .and_then(|value| value.get("eggpool"))
                .is_some()
        );
    }

    #[test]
    fn only_rendered_secret_targets_require_explicit_secret_printing() {
        assert!(!Target::Codex.contains_secret());
        assert!(!Target::Opencode.contains_secret());
        assert!(Target::QwenCode.contains_secret());
        assert!(Target::Aider.contains_secret());
    }

    #[test]
    fn overrides_normalize_base_url() {
        let context =
            apply_overrides(context(), None, Some("https://example.invalid/v1/")).expect("url");
        assert_eq!(context.base_url, "https://example.invalid/v1");
        assert_eq!(context.base_url_root, "https://example.invalid");
        assert!(apply_overrides(context, None, Some("not a url")).is_err());
    }

    // Portable projection/catalog semantics are owned and tested by
    // `eggpool-client-config` (projection, codex, token, integration_profile
    // modules). These adapter tests verify the EggPool thin wrappers preserve
    // local `configsetup` behavior through that shared crate.

    #[test]
    fn adapter_projection_delegates_to_portable_crate() {
        let model = IntegrationModel {
            model_id: "gpt-4o-super-vision-tool-9000".to_owned(),
            base_model_id: "gpt-4o-super-vision-tool-9000".to_owned(),
            provider_id: None,
            display_name: "Tricky Name".to_owned(),
            capabilities: Value::Object(Map::new()),
            source_metadata: Value::Object(Map::new()),
            limits: ModelLimits::default(),
        };
        let projection = project_model(&model);
        // Unknown stays unknown; no name inference; websockets false.
        assert_eq!(projection.capabilities.input_images, None);
        assert!(!projection.capabilities.websockets);
        let context = context();
        let projections = project_context_models(&context);
        assert_eq!(projections.len(), 1);
    }

    #[test]
    fn adapter_catalog_delegates_to_portable_crate() {
        let context = context();
        let catalog = build_codex_catalog_json(&context).expect("catalog");
        let count = validate_codex_catalog_json(&catalog, &context.api_key).expect("valid");
        assert_eq!(count, 1);
        assert!(!catalog.contains(&context.api_key));
    }

    #[test]
    fn codex_toml_with_catalog_points_at_generated_artifact() {
        let context = context();
        let snippet = build_codex_toml_snippet_with_catalog(
            &context,
            None,
            Some("/tmp/eggpool-codex-models.json"),
        )
        .expect("toml");
        let parsed: toml::Value = snippet.parse().expect("valid TOML");
        assert_eq!(
            parsed
                .get("model_catalog_json")
                .and_then(toml::Value::as_str),
            Some("/tmp/eggpool-codex-models.json")
        );
        assert!(!snippet.contains(&context.api_key));
    }

    #[test]
    fn opencode_renderer_uses_responses_runtime_and_env_key() {
        let context = context();
        let rendered = build_opencode_config_json(&context).expect("opencode");
        assert!(!rendered.contains(&context.api_key));
        assert!(rendered.contains(client_config::OPENCODE_RESPONSES_NPM));
        assert!(!rendered.contains("@ai-sdk/openai-compatible"));
        assert!(rendered.contains("{env:EGGPOOL_API_KEY}"));
        let value: Value = serde_json::from_str(&rendered).expect("json");
        let provider = value
            .get("provider")
            .and_then(|provider| provider.get("eggpool"))
            .expect("eggpool provider");
        assert_eq!(
            provider.get("npm").and_then(Value::as_str),
            Some(client_config::OPENCODE_RESPONSES_NPM)
        );
        let models = provider
            .get("models")
            .and_then(Value::as_object)
            .expect("models");
        let entry = models.get("gpt-4o/openai").expect("model entry");
        assert_eq!(
            entry
                .get("limit")
                .and_then(|limit| limit.get("context"))
                .and_then(Value::as_u64),
            Some(128_000)
        );
    }

    #[test]
    fn codex_lifecycle_apply_check_sync_remove_is_idempotent_with_drift_refusal() {
        let directory = tempfile::tempdir().expect("tempdir");
        let state_dir = directory.path().join("state");
        let config_path = directory.path().join("codex").join("config.toml");
        let context = context();

        let apply_options = LifecycleOptions {
            action: LifecycleAction::Apply,
            dry_run: false,
            force: false,
            model: None,
        };
        let first = codex_lifecycle(
            &context,
            &apply_options,
            Some(&state_dir),
            Some(&config_path),
        )
        .expect("apply");
        assert!(first.changed);
        let second = codex_lifecycle(
            &context,
            &apply_options,
            Some(&state_dir),
            Some(&config_path),
        )
        .expect("re-apply");
        assert!(!second.changed);

        let check_options = LifecycleOptions {
            action: LifecycleAction::Check,
            dry_run: false,
            force: false,
            model: None,
        };
        let check = codex_lifecycle(
            &context,
            &check_options,
            Some(&state_dir),
            Some(&config_path),
        )
        .expect("check");
        assert!(check.up_to_date);

        // External user edit after apply must cause a safe refusal.
        let mut tampered = fs::read_to_string(&config_path).expect("config");
        tampered.push_str("\n[user_custom]\nvalue = 1\n");
        fs::write(&config_path, tampered).expect("tamper");
        let sync_options = LifecycleOptions {
            action: LifecycleAction::Sync,
            dry_run: false,
            force: false,
            model: None,
        };
        assert!(
            codex_lifecycle(
                &context,
                &sync_options,
                Some(&state_dir),
                Some(&config_path)
            )
            .is_err()
        );
        // --force converges again.
        let forced = LifecycleOptions {
            action: LifecycleAction::Sync,
            dry_run: false,
            force: true,
            model: None,
        };
        codex_lifecycle(&context, &forced, Some(&state_dir), Some(&config_path))
            .expect("forced sync");

        let remove_options = LifecycleOptions {
            action: LifecycleAction::Remove,
            dry_run: false,
            force: false,
            model: None,
        };
        let remove = codex_lifecycle(
            &context,
            &remove_options,
            Some(&state_dir),
            Some(&config_path),
        )
        .expect("remove");
        assert!(remove.changed);
        assert!(!codex_catalog_path_for_state(&state_dir).exists());
    }

    #[test]
    fn opencode_lifecycle_creates_merges_and_refuses_jsonc_rewrite() {
        let directory = tempfile::tempdir().expect("tempdir");
        let state_dir = directory.path().join("state");
        let config_path = directory.path().join("opencode.json");
        let context = context();

        let apply = LifecycleOptions {
            action: LifecycleAction::Apply,
            dry_run: false,
            force: false,
            model: None,
        };
        let report = opencode_lifecycle(&context, &apply, Some(&state_dir), Some(&config_path))
            .expect("apply");
        assert!(report.changed);
        let raw = fs::read_to_string(&config_path).expect("config");
        assert!(!raw.contains(&context.api_key));
        assert!(raw.contains("{env:EGGPOOL_API_KEY}"));

        // Existing JSONC with comments must never be silently rewritten.
        fs::write(&config_path, "{\n// user comment\n\"provider\": {}\n}\n").expect("jsonc");
        let check = LifecycleOptions {
            action: LifecycleAction::Check,
            dry_run: false,
            force: false,
            model: None,
        };
        let report = opencode_lifecycle(&context, &check, Some(&state_dir), Some(&config_path))
            .expect("check jsonc");
        assert!(!report.up_to_date);
        assert!(
            opencode_lifecycle(&context, &apply, Some(&state_dir), Some(&config_path)).is_err()
        );
    }
}
