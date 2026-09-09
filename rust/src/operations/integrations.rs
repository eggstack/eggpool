//! Agent integration context, renderers, and safe snippet delivery.
//!
//! The renderers are intentionally small and deterministic.  Catalog access,
//! server-key resolution, and transcoder compatibility mutation are kept in
//! this shared boundary so individual targets cannot grow divergent policy.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fmt,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde_json::{Map, Value, json};
use thiserror::Error;
use tokio::{io::AsyncWriteExt, process::Command, time::timeout};

use crate::{
    Config, ConfigError,
    catalog::CapabilityStatus,
    db::{
        AccountRepository, CatalogModel, CatalogRepository, Database, DatabaseError,
        ProviderModelMetadata,
    },
    operations::config_mutation::{self, MutationError},
};

const MAX_SNIPPET_BYTES: usize = 4 * 1024 * 1024;
const CLIPBOARD_TIMEOUT: Duration = Duration::from_secs(5);
const DEPRECATED_MODEL_ID: &str = "__deprecated__";

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
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModelLimits {
    pub context_tokens: Option<u64>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct IntegrationModel {
    pub model_id: String,
    pub base_model_id: String,
    pub provider_id: Option<String>,
    pub display_name: String,
    pub capabilities: Value,
    pub source_metadata: Value,
    pub limits: ModelLimits,
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
        // Keep the generic delivery path fail-closed.  Codex uses an env-key
        // reference rather than embedding the value, but its generated file
        // is still a credential-bearing integration artifact and follows the
        // same explicit-print contract as the other targets.
        true
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
    let mut lines = vec!["model_provider = \"eggpool\"".to_owned()];
    if let Some(model) = model {
        lines.push(format!("model = {}", toml_string(model)?));
    }
    lines.extend([
        String::new(),
        "[model_providers.eggpool]".to_owned(),
        "name = \"EggPool\"".to_owned(),
        format!("base_url = {}", toml_string(&context.base_url)?),
        "wire_api = \"responses\"".to_owned(),
        "env_key = \"EGGPOOL_API_KEY\"".to_owned(),
    ]);
    Ok(lines.join("\n"))
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
    let mut models = BTreeMap::new();
    for model in &context.models {
        let mut entry = Map::new();
        let display = if let Some(provider) = &model.provider_id {
            if model.display_name.ends_with(&format!("/{provider}")) {
                model.display_name.clone()
            } else {
                format!("{}/{}", model.display_name, provider)
            }
        } else {
            model.display_name.clone()
        };
        if display != model.model_id {
            entry.insert("name".to_owned(), Value::String(display));
        }
        let mut limit = Map::new();
        if let Some(value) = model.limits.context_tokens.filter(|value| *value > 0) {
            limit.insert("context".to_owned(), json!(value));
        }
        if let Some(value) = model.limits.input_tokens.filter(|value| *value > 0) {
            limit.insert("input".to_owned(), json!(value));
        }
        if let Some(value) = model.limits.output_tokens.filter(|value| *value > 0) {
            limit.insert("output".to_owned(), json!(value));
        }
        if !limit.is_empty() {
            entry.insert("limit".to_owned(), Value::Object(limit));
        }
        if let Some(thinking) = model
            .capabilities
            .get("thinking")
            .and_then(Value::as_object)
        {
            if thinking.get("status").and_then(Value::as_str)
                == Some(CapabilityStatus::Supported.as_str())
            {
                entry.insert("reasoning".to_owned(), Value::Bool(true));
                if let Some(efforts) = thinking.get("supported_efforts").and_then(Value::as_array) {
                    let mut variants = Map::new();
                    for effort in efforts.iter().filter_map(Value::as_str) {
                        variants.insert(effort.to_owned(), json!({"reasoningEffort": effort}));
                    }
                    if !variants.is_empty() {
                        entry.insert("variants".to_owned(), Value::Object(variants));
                    }
                }
            }
        }
        models.insert(model.model_id.clone(), Value::Object(entry));
    }
    let options = Map::from_iter([
        (
            "baseURL".to_owned(),
            Value::String(context.base_url.clone()),
        ),
        ("apiKey".to_owned(), Value::String(context.api_key.clone())),
    ]);
    let eggpool = Map::from_iter([
        (
            "npm".to_owned(),
            Value::String("@ai-sdk/openai-compatible".to_owned()),
        ),
        ("name".to_owned(), Value::String("EggPool".to_owned())),
        ("options".to_owned(), Value::Object(options)),
        ("models".to_owned(), serde_json::to_value(models)?),
    ]);
    let provider = Map::from_iter([("eggpool".to_owned(), Value::Object(eggpool))]);
    let root = Map::from_iter([
        (
            "$schema".to_owned(),
            Value::String("https://opencode.ai/config.json".to_owned()),
        ),
        ("provider".to_owned(), Value::Object(provider)),
    ]);
    Ok(serde_json::to_string_pretty(&root)?)
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
        {
            if let Some(base) = model.capabilities.as_object_mut() {
                base.insert("supports_vision".to_owned(), Value::Bool(true));
            }
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

fn toml_string(value: &str) -> Result<String, IntegrationError> {
    Ok(serde_json::to_string(value)?)
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
    fn overrides_normalize_base_url() {
        let context =
            apply_overrides(context(), None, Some("https://example.invalid/v1/")).expect("url");
        assert_eq!(context.base_url, "https://example.invalid/v1");
        assert_eq!(context.base_url_root, "https://example.invalid");
        assert!(apply_overrides(context, None, Some("not a url")).is_err());
    }
}
