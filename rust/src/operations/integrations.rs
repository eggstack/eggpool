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
const MAX_CODEX_CATALOG_MODELS: usize = 1000;
const MAX_CODEX_CATALOG_BYTES: usize = 1024 * 1024;
const CLIPBOARD_TIMEOUT: Duration = Duration::from_secs(5);
const DEPRECATED_MODEL_ID: &str = "__deprecated__";
const INTEGRATION_SCHEMA_VERSION: u32 = 1;
const OPENCODE_RESPONSES_NPM: &str = "@ai-sdk/openai";
const EGGPOOL_API_KEY_ENV: &str = "EGGPOOL_API_KEY";

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
    #[error("model catalog exceeds bounded size ({count} models)")]
    CatalogTooLarge { count: usize },
    #[error("client config drift detected: {detail}")]
    Drift { detail: String },
    #[error("refusing to rewrite {path}: {detail}")]
    UnsafeRewrite { path: PathBuf, detail: String },
    #[error("lifecycle flag is not supported for {target}")]
    UnsupportedLifecycle { target: String },
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

/// Normalized reasoning facts for coding-agent renderers.
///
/// Derived only from validated catalog/model-info facts. Unknown stays
/// absent rather than optimistic.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentReasoningCapabilities {
    pub efforts: Vec<String>,
    pub default_effort: Option<String>,
    pub summaries: bool,
}

/// Provider-neutral coding-agent projection derived from existing
/// catalog/model-info/routing facts.
///
/// This is the single integration projection used by the Codex catalog and
/// OpenCode renderers. It never infers capability from model-ID substrings,
/// never leaks provider-private source metadata, and keeps `websockets`
/// false until a real EggPool Responses WebSocket path exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentModelCapabilities {
    pub context_tokens: Option<u64>,
    pub max_output_tokens: Option<u64>,
    pub input_text: bool,
    pub input_images: Option<bool>,
    pub reasoning: Option<AgentReasoningCapabilities>,
    pub function_tools: Option<bool>,
    pub freeform_tools: Option<bool>,
    pub deferred_tool_search: Option<bool>,
    pub responses: bool,
    pub websockets: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentModelProjection {
    pub public_id: String,
    pub display_name: String,
    pub capabilities: AgentModelCapabilities,
}

impl AgentModelCapabilities {
    fn conservative() -> Self {
        Self {
            context_tokens: None,
            max_output_tokens: None,
            input_text: true,
            input_images: None,
            reasoning: None,
            function_tools: None,
            freeform_tools: None,
            deferred_tool_search: None,
            responses: true,
            websockets: false,
        }
    }
}

/// Derive a provider-neutral projection from one validated integration model.
///
/// Rules: unknown stays unknown (None), never inferred from the model ID,
/// `websockets` stays false, remote compaction is not advertised here.
pub fn project_model(model: &IntegrationModel) -> AgentModelProjection {
    let mut capabilities = AgentModelCapabilities::conservative();
    capabilities.context_tokens = model.limits.context_tokens.filter(|value| *value > 0);
    capabilities.max_output_tokens = model.limits.output_tokens.filter(|value| *value > 0);

    if let Some(object) = model.capabilities.as_object() {
        if let Some(vision) = object.get("supports_vision").and_then(Value::as_bool) {
            capabilities.input_images = Some(vision);
        }
        if let Some(tools) = object.get("supports_tools").and_then(Value::as_bool) {
            capabilities.function_tools = Some(tools);
        }
        // Freeform (`custom`) and deferred (`tool_search`) stay unknown
        // unless the catalog explicitly proves them. Unknown is conservative:
        // renderers must not advertise them optimistically.
        if let Some(freeform) = object
            .get("supports_freeform_tools")
            .and_then(Value::as_bool)
        {
            capabilities.freeform_tools = Some(freeform);
        }
        if let Some(deferred) = object
            .get("supports_deferred_tool_search")
            .and_then(Value::as_bool)
        {
            capabilities.deferred_tool_search = Some(deferred);
        }
        if let Some(thinking) = object.get("thinking").and_then(Value::as_object)
            && thinking.get("status").and_then(Value::as_str)
                == Some(CapabilityStatus::Supported.as_str())
        {
            let mut efforts: Vec<String> = thinking
                .get("supported_efforts")
                .and_then(Value::as_array)
                .map(|values| {
                    values
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect::<BTreeSet<_>>()
                        .into_iter()
                        .collect()
                })
                .unwrap_or_default();
            efforts.sort();
            // `supported_efforts` is advisory; an empty list still means
            // reasoning is supported without enumerated efforts.
            capabilities.reasoning = Some(AgentReasoningCapabilities {
                efforts,
                default_effort: None,
                summaries: false,
            });
        }
    }

    AgentModelProjection {
        public_id: model.model_id.clone(),
        display_name: model.display_name.clone(),
        capabilities,
    }
}

/// Collapse multiple projections for one public ID into conservative
/// guaranteed capabilities.
///
/// Aggregation: context/output are the minimum known guaranteed values,
/// boolean required features and input modalities are intersections,
/// reasoning efforts are set intersections, and unknown on any required
/// candidate remains unknown/conservative. Never publishes the union of
/// heterogeneous targets.
pub fn aggregate_projections(
    public_id: &str,
    display_name: &str,
    projections: &[AgentModelCapabilities],
) -> AgentModelCapabilities {
    if projections.is_empty() {
        return AgentModelCapabilities::conservative();
    }
    if projections.len() == 1 {
        return projections[0].clone();
    }
    let context_tokens = projections
        .iter()
        .filter_map(|capability| capability.context_tokens)
        .min();
    // If any candidate lacks a known context window, the guarantee is unknown.
    let context_tokens = if projections
        .iter()
        .any(|capability| capability.context_tokens.is_none())
    {
        // Keep the minimum known value when at least one candidate knows it,
        // but only if every candidate knows it. Otherwise the guarantee is
        // unknown rather than optimistic.
        if projections
            .iter()
            .all(|capability| capability.context_tokens.is_some())
        {
            context_tokens
        } else {
            None
        }
    } else {
        context_tokens
    };
    let max_output_tokens = if projections
        .iter()
        .all(|capability| capability.max_output_tokens.is_some())
    {
        projections
            .iter()
            .filter_map(|capability| capability.max_output_tokens)
            .min()
    } else {
        None
    };
    let input_images = {
        let mut values = BTreeSet::new();
        for capability in projections {
            match capability.input_images {
                Some(value) => {
                    values.insert(value);
                }
                None => {
                    values.insert(true);
                    values.insert(false);
                }
            }
        }
        // Intersection: only advertise image input when every candidate
        // guarantees it; only deny when every candidate denies it.
        if values.len() == 1 {
            Some(*values.iter().next().expect("single value"))
        } else {
            None
        }
    };
    let function_tools = {
        let mut values = BTreeSet::new();
        for capability in projections {
            match capability.function_tools {
                Some(value) => {
                    values.insert(value);
                }
                None => {
                    values.insert(true);
                    values.insert(false);
                }
            }
        }
        if values.len() == 1 {
            Some(*values.iter().next().expect("single value"))
        } else {
            None
        }
    };
    let intersect_optional_bool =
        |select: fn(&AgentModelCapabilities) -> Option<bool>| -> Option<bool> {
            let mut values = BTreeSet::new();
            for capability in projections {
                match select(capability) {
                    Some(value) => {
                        values.insert(value);
                    }
                    None => {
                        values.insert(true);
                        values.insert(false);
                    }
                }
            }
            if values.len() == 1 {
                Some(*values.iter().next().expect("single value"))
            } else {
                None
            }
        };
    let freeform_tools = intersect_optional_bool(|capability| capability.freeform_tools);
    let deferred_tool_search =
        intersect_optional_bool(|capability| capability.deferred_tool_search);
    let reasoning = {
        if projections
            .iter()
            .all(|capability| capability.reasoning.is_some())
        {
            let mut intersection: Option<BTreeSet<String>> = None;
            for capability in projections {
                let efforts = capability
                    .reasoning
                    .as_ref()
                    .map(|reasoning| reasoning.efforts.iter().cloned().collect::<BTreeSet<_>>())
                    .unwrap_or_default();
                intersection = Some(match intersection {
                    Some(current) => current.intersection(&efforts).cloned().collect(),
                    None => efforts,
                });
            }
            let mut efforts: Vec<String> = intersection.unwrap_or_default().into_iter().collect();
            efforts.sort();
            Some(AgentReasoningCapabilities {
                efforts,
                default_effort: None,
                summaries: false,
            })
        } else {
            None
        }
    };
    let _ = (public_id, display_name);
    AgentModelCapabilities {
        context_tokens,
        max_output_tokens,
        input_text: true,
        input_images,
        reasoning,
        function_tools,
        freeform_tools,
        deferred_tool_search,
        responses: true,
        websockets: false,
    }
}

/// Project every public model/alias in deterministic order.
pub fn project_context_models(context: &IntegrationContext) -> Vec<AgentModelProjection> {
    let mut grouped: BTreeMap<&str, Vec<&IntegrationModel>> = BTreeMap::new();
    for model in &context.models {
        grouped
            .entry(model.model_id.as_str())
            .or_default()
            .push(model);
    }
    let mut projections = Vec::new();
    for (public_id, models) in grouped {
        let display_name = models
            .first()
            .map(|model| model.display_name.as_str())
            .unwrap_or(public_id);
        let capabilities: Vec<AgentModelCapabilities> = models
            .iter()
            .map(|model| project_model(model).capabilities)
            .collect();
        let merged = aggregate_projections(public_id, display_name, &capabilities);
        projections.push(AgentModelProjection {
            public_id: public_id.to_owned(),
            display_name: display_name.to_owned(),
            capabilities: merged,
        });
    }
    projections.sort_by(|left, right| left.public_id.cmp(&right.public_id));
    projections
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
    build_codex_toml_snippet_with_catalog(context, model, None)
}

pub fn build_codex_toml_snippet_with_catalog(
    context: &IntegrationContext,
    model: Option<&str>,
    catalog_path: Option<&str>,
) -> Result<String, IntegrationError> {
    let mut lines = vec!["model_provider = \"eggpool\"".to_owned()];
    if let Some(model) = model {
        lines.push(format!("model = {}", toml_string(model)?));
    }
    if let Some(catalog_path) = catalog_path {
        lines.push(format!(
            "model_catalog_json = {}",
            toml_string(catalog_path)?
        ));
    }
    lines.extend([
        String::new(),
        "[model_providers.eggpool]".to_owned(),
        "name = \"EggPool\"".to_owned(),
        format!("base_url = {}", toml_string(&context.base_url)?),
        "wire_api = \"responses\"".to_owned(),
        "supports_websockets = false".to_owned(),
        format!("env_key = {}", toml_string(EGGPOOL_API_KEY_ENV)?),
    ]);
    Ok(lines.join("\n"))
}

/// Render a current Codex model catalog from the provider-neutral projection.
///
/// The renderer is owned by the integrations boundary and built from the
/// implementation-time Codex model schema (audit baselines in Plan 200 plus
/// live schema research 2026-09-16). It emits valid Codex catalog JSON with
/// deterministic ordering, bounded size, no secrets, and no provider-private
/// source metadata. Unknown facts stay absent rather than optimistic, and
/// `websockets`/remote-compaction remain disabled.
pub fn build_codex_catalog_json(context: &IntegrationContext) -> Result<String, IntegrationError> {
    let projections = project_context_models(context);
    if projections.len() > MAX_CODEX_CATALOG_MODELS {
        return Err(IntegrationError::CatalogTooLarge {
            count: projections.len(),
        });
    }
    let mut models = Vec::new();
    for (index, projection) in projections.iter().enumerate() {
        let _ = index;
        models.push(codex_catalog_entry(projection));
    }
    let root = json!({ "models": models });
    let rendered = serde_json::to_string_pretty(&root)?;
    if rendered.len() > MAX_CODEX_CATALOG_BYTES {
        return Err(IntegrationError::TooLarge);
    }
    // The catalog must never carry credentials or raw source metadata.
    debug_assert!(!rendered.contains(&context.api_key));
    Ok(rendered)
}

fn codex_catalog_entry(projection: &AgentModelProjection) -> Value {
    let capabilities = &projection.capabilities;
    let mut entry = Map::new();
    entry.insert(
        "slug".to_owned(),
        Value::String(projection.public_id.clone()),
    );
    entry.insert(
        "display_name".to_owned(),
        Value::String(projection.display_name.clone()),
    );
    entry.insert(
        "description".to_owned(),
        Value::String(format!(
            "EggPool model {} via EggPool Responses gateway",
            projection.public_id
        )),
    );
    // Tool/shell capability: only advertise parallel tool calls when the
    // projection guarantees function tools across the route. The shell type
    // itself is the conservative `shell_command` used by current custom
    // Codex catalogs; EggPool does not claim `unified_exec` semantics.
    entry.insert(
        "shell_type".to_owned(),
        Value::String("shell_command".to_owned()),
    );
    entry.insert("visibility".to_owned(), Value::String("list".to_owned()));
    entry.insert("supported_in_api".to_owned(), Value::Bool(true));
    entry.insert("priority".to_owned(), json!(100));
    entry.insert("prefer_websockets".to_owned(), Value::Bool(false));
    entry.insert(
        "supports_parallel_tool_calls".to_owned(),
        Value::Bool(capabilities.function_tools == Some(true)),
    );
    entry.insert(
        "experimental_supported_tools".to_owned(),
        Value::Array(Vec::new()),
    );
    let mut modalities = vec![Value::String("text".to_owned())];
    if capabilities.input_images == Some(true) {
        modalities.push(Value::String("image".to_owned()));
    }
    entry.insert("input_modalities".to_owned(), Value::Array(modalities));
    entry.insert(
        "supports_image_detail_original".to_owned(),
        Value::Bool(false),
    );
    if let Some(context_window) = capabilities.context_tokens {
        entry.insert("context_window".to_owned(), json!(context_window));
        entry.insert("max_context_window".to_owned(), json!(context_window));
        // Current Codex derives its default auto-compaction threshold from
        // the resolved context window (normally 90%). Advertise the same
        // conservative threshold so local compaction matches routing reality.
        let auto_compact = context_window * 90 / 100;
        entry.insert("auto_compact_token_limit".to_owned(), json!(auto_compact));
    } else {
        entry.insert("auto_compact_token_limit".to_owned(), Value::Null);
    }
    entry.insert(
        "truncation_policy".to_owned(),
        json!({ "mode": "tokens", "limit": 10000 }),
    );
    if let Some(reasoning) = &capabilities.reasoning {
        entry.insert(
            "supports_reasoning_summaries".to_owned(),
            Value::Bool(false),
        );
        entry.insert(
            "default_reasoning_summary".to_owned(),
            Value::String("none".to_owned()),
        );
        if reasoning.efforts.is_empty() {
            // Current Codex (0.154.0, `codex debug models`) requires
            // `supported_reasoning_levels` to be present even when the model
            // has no reasoning support; an empty array is the conservative
            // representation of "no guaranteed reasoning levels".
            entry.insert(
                "supported_reasoning_levels".to_owned(),
                Value::Array(Vec::new()),
            );
        } else {
            let levels: Vec<Value> = reasoning
                .efforts
                .iter()
                .map(|effort| {
                    json!({
                        "effort": effort,
                        "description": format!("EggPool reasoning effort {effort}"),
                    })
                })
                .collect();
            entry.insert(
                "supported_reasoning_levels".to_owned(),
                Value::Array(levels),
            );
            if let Some(default) = reasoning
                .default_effort
                .as_ref()
                .filter(|default| reasoning.efforts.contains(default))
            {
                entry.insert(
                    "default_reasoning_level".to_owned(),
                    Value::String(default.clone()),
                );
            } else if let Some(first) = reasoning.efforts.first() {
                // Conservative default: first enumerated effort in sorted
                // order. The projection never invents efforts, only carries
                // validated catalog facts.
                entry.insert(
                    "default_reasoning_level".to_owned(),
                    Value::String(first.clone()),
                );
            }
        }
    } else {
        entry.insert(
            "supports_reasoning_summaries".to_owned(),
            Value::Bool(false),
        );
        entry.insert(
            "default_reasoning_summary".to_owned(),
            Value::String("none".to_owned()),
        );
        // See above: current Codex requires the array even without reasoning.
        entry.insert(
            "supported_reasoning_levels".to_owned(),
            Value::Array(Vec::new()),
        );
    }
    entry.insert("support_verbosity".to_owned(), Value::Bool(false));
    // Current Codex (0.154.0) rejects a catalog entry missing both
    // `base_instructions` and `model_messages.instructions_template`.
    // EggPool provides no custom base instructions; an empty string is the
    // conservative "no EggPool override" value qualified via
    // `codex debug models` (live qualification 2026-09-16). It must not be
    // used to fabricate provider instructions.
    entry.insert("base_instructions".to_owned(), Value::String(String::new()));
    Value::Object(entry)
}

/// Validate that a generated catalog meets the strict-parser contract.
///
/// Mirrors the required Codex fields without importing Codex: every entry
/// must carry a slug/display name, must not advertise websockets, and must
/// not contain credentials or raw source blobs.
pub fn validate_codex_catalog_json(
    catalog_json: &str,
    api_key: &str,
) -> Result<usize, IntegrationError> {
    let value: Value = serde_json::from_str(catalog_json)?;
    let models = value
        .get("models")
        .and_then(Value::as_array)
        .ok_or_else(|| IntegrationError::Drift {
            detail: "Codex catalog is missing the models array".to_owned(),
        })?;
    for model in models {
        let object = model.as_object().ok_or_else(|| IntegrationError::Drift {
            detail: "Codex catalog entry is not an object".to_owned(),
        })?;
        if object
            .get("slug")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .is_none()
        {
            return Err(IntegrationError::Drift {
                detail: "Codex catalog entry is missing slug".to_owned(),
            });
        }
        if object
            .get("display_name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .is_none()
        {
            return Err(IntegrationError::Drift {
                detail: "Codex catalog entry is missing display_name".to_owned(),
            });
        }
        if object.get("prefer_websockets").and_then(Value::as_bool) == Some(true) {
            return Err(IntegrationError::Drift {
                detail: "Codex catalog must not advertise websockets".to_owned(),
            });
        }
        // Current Codex (0.154.0, qualified 2026-09-16 via
        // `codex debug models`) rejects entries missing
        // `supported_reasoning_levels` or both `base_instructions` and
        // `model_messages.instructions_template`. The renderer always emits
        // the conservative empty values; validation enforces the contract so
        // future renderer regressions fail deterministically.
        if object
            .get("supported_reasoning_levels")
            .and_then(Value::as_array)
            .is_none()
        {
            return Err(IntegrationError::Drift {
                detail: "Codex catalog entry is missing supported_reasoning_levels".to_owned(),
            });
        }
        let has_base_instructions = object
            .get("base_instructions")
            .and_then(Value::as_str)
            .is_some();
        let has_template_instructions = object
            .get("model_messages")
            .and_then(|messages| messages.get("instructions_template"))
            .and_then(Value::as_str)
            .is_some();
        if !has_base_instructions && !has_template_instructions {
            return Err(IntegrationError::Drift {
                detail: "Codex catalog entry is missing base_instructions".to_owned(),
            });
        }
    }
    let rendered = catalog_json;
    if !api_key.is_empty() && rendered.contains(api_key) {
        return Err(IntegrationError::Drift {
            detail: "Codex catalog must not contain the server key".to_owned(),
        });
    }
    Ok(models.len())
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
    let mut models = BTreeMap::new();
    for projection in &projections {
        let mut entry = Map::new();
        if projection.display_name != projection.public_id {
            entry.insert(
                "name".to_owned(),
                Value::String(projection.display_name.clone()),
            );
        }
        let mut limit = Map::new();
        if let Some(value) = projection
            .capabilities
            .context_tokens
            .filter(|value| *value > 0)
        {
            limit.insert("context".to_owned(), json!(value));
        }
        if let Some(value) = projection
            .capabilities
            .max_output_tokens
            .filter(|value| *value > 0)
        {
            limit.insert("output".to_owned(), json!(value));
        }
        if !limit.is_empty() {
            entry.insert("limit".to_owned(), Value::Object(limit));
        }
        let mut input_modalities = vec![Value::String("text".to_owned())];
        if projection.capabilities.input_images == Some(true) {
            input_modalities.push(Value::String("image".to_owned()));
        }
        entry.insert(
            "modalities".to_owned(),
            json!({
                "input": input_modalities,
                "output": ["text"],
            }),
        );
        if let Some(reasoning) = &projection.capabilities.reasoning {
            entry.insert("reasoning".to_owned(), Value::Bool(true));
            if !reasoning.efforts.is_empty() {
                let mut variants = Map::new();
                for effort in &reasoning.efforts {
                    variants.insert(effort.to_owned(), json!({"reasoningEffort": effort}));
                }
                entry.insert("variants".to_owned(), Value::Object(variants));
            }
        }
        // Never claim image/tool capability the projection cannot guarantee.
        // `function_tools == Some(true)` is the only case where parallel
        // tool use is advertised; otherwise the entry simply omits the claim.
        models.insert(projection.public_id.clone(), Value::Object(entry));
    }
    let options = Map::from_iter([
        (
            "baseURL".to_owned(),
            Value::String(context.base_url.clone()),
        ),
        // Prefer OpenCode's supported environment-variable interpolation so
        // the generated file carries no resolved secret. Operators set
        // `EGGPOOL_API_KEY` in the environment that launches OpenCode.
        (
            "apiKey".to_owned(),
            Value::String(format!("{{env:{EGGPOOL_API_KEY_ENV}}}")),
        ),
    ]);
    let eggpool = Map::from_iter([
        (
            "npm".to_owned(),
            Value::String(OPENCODE_RESPONSES_NPM.to_owned()),
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

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct OwnershipManifest {
    schema_version: u32,
    eggpool_version: String,
    target: String,
    client_config_path: PathBuf,
    pre_edit_hash: String,
    post_edit_hash: String,
    owned_fields: Vec<String>,
    previous_values: Map<String, Value>,
    generated_catalog_path: Option<PathBuf>,
    generated_catalog_hash: Option<String>,
    base_url: String,
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex_bytes(&hasher.finalize())
}

fn hex_bytes(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(char::from_digit(u32::from(byte >> 4), 16).expect("hex"));
        output.push(char::from_digit(u32::from(byte & 0x0f), 16).expect("hex"));
    }
    output
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

fn toml_line_header(line: &str) -> Option<String> {
    let trimmed = line.trim();
    if let Some(value) = trimmed
        .strip_prefix("[[")
        .and_then(|value| value.strip_suffix("]]"))
    {
        return Some(value.trim().to_owned());
    }
    trimmed
        .strip_prefix('[')
        .and_then(|value| value.strip_suffix(']'))
        .map(|value| value.trim().to_owned())
}

fn toml_assignment_key(line: &str) -> Option<String> {
    let trimmed = line.trim_start();
    if trimmed.starts_with('#') || trimmed.starts_with('[') {
        return None;
    }
    let (key, _) = trimmed.split_once('=')?;
    let key = key.trim().trim_matches(['"', '\'']).trim().to_owned();
    (!key.is_empty()
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'))
    .then_some(key)
}

fn toml_string_value(line: &str) -> Option<String> {
    let (_, raw) = line.split_once('=')?;
    let raw = raw.trim();
    // Strip trailing comments that are not inside quotes (best-effort).
    let mut in_string = false;
    let mut escaped = false;
    let mut end = raw.len();
    for (index, character) in raw.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if character == '\\' && in_string {
            escaped = true;
            continue;
        }
        if character == '"' {
            in_string = !in_string;
            continue;
        }
        if character == '#' && !in_string {
            end = index;
            break;
        }
    }
    let value = raw[..end].trim().trim_matches('"').trim().to_owned();
    Some(value)
}

fn first_table_index(lines: &[String]) -> Option<usize> {
    lines
        .iter()
        .position(|line| toml_line_header(line).is_some())
}

fn find_root_key(lines: &[String], key: &str) -> Option<usize> {
    let end = first_table_index(lines).unwrap_or(lines.len());
    lines[..end]
        .iter()
        .position(|line| toml_assignment_key(line).as_deref() == Some(key))
}

fn set_root_key(lines: &mut Vec<String>, key: &str, rendered: &str) {
    let line = format!("{key} = {rendered}");
    if let Some(index) = find_root_key(lines, key) {
        lines[index] = line;
        return;
    }
    match first_table_index(lines) {
        Some(index) => lines.insert(index, line),
        None => {
            if !lines.is_empty() && !lines.last().is_some_and(String::is_empty) {
                lines.push(String::new());
            }
            lines.push(line);
        }
    }
}

fn remove_root_key(lines: &mut Vec<String>, key: &str) -> Option<String> {
    let index = find_root_key(lines, key)?;
    let previous = toml_string_value(&lines[index]);
    lines.remove(index);
    previous
}

fn find_table(lines: &[String], header: &str) -> Option<(usize, usize)> {
    let start = lines
        .iter()
        .position(|line| toml_line_header(line).as_deref() == Some(header))?;
    let mut end = start + 1;
    while end < lines.len() && toml_line_header(&lines[end]).is_none() {
        end += 1;
    }
    Some((start, end))
}

fn table_value(lines: &[String], header: &str, key: &str) -> Option<String> {
    let (start, end) = find_table(lines, header)?;
    lines[start + 1..end]
        .iter()
        .find(|line| toml_assignment_key(line).as_deref() == Some(key))
        .and_then(|line| toml_string_value(line))
}

#[derive(Debug, Clone, Default)]
struct CodexPrevious {
    model_provider: Option<String>,
    model_catalog_json: Option<String>,
    model: Option<String>,
    provider_table: Option<Vec<String>>,
}

/// Apply the narrow EggPool-owned Codex mutation to TOML text.
///
/// Owns only root `model_provider`, root `model_catalog_json`, optional root
/// `model` (when explicitly requested), and the `[model_providers.eggpool]`
/// table. All other content, comments, and formatting are preserved. Root
/// keys are placed before the first table so they remain root assignments.
fn apply_codex_text_mutation(
    existing: &str,
    base_url: &str,
    catalog_path: &str,
    model: Option<&str>,
    manage_model: bool,
) -> (String, CodexPrevious) {
    let mut lines: Vec<String> = if existing.is_empty() {
        Vec::new()
    } else {
        existing.lines().map(str::to_owned).collect()
    };
    let mut previous = CodexPrevious::default();
    if let Some(index) = find_root_key(&lines, "model_provider") {
        previous.model_provider = toml_string_value(&lines[index]);
    }
    if let Some(index) = find_root_key(&lines, "model_catalog_json") {
        previous.model_catalog_json = toml_string_value(&lines[index]);
    }
    if let Some(index) = find_root_key(&lines, "model") {
        previous.model = toml_string_value(&lines[index]);
    }
    if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
        previous.provider_table = Some(lines[start..end].to_vec());
    }

    let catalog_rendered =
        serde_json::to_string(catalog_path).unwrap_or_else(|_| "\"\"".to_owned());
    set_root_key(&mut lines, "model_provider", "\"eggpool\"");
    set_root_key(&mut lines, "model_catalog_json", &catalog_rendered);
    if manage_model {
        if let Some(model) = model {
            let rendered = serde_json::to_string(model).unwrap_or_else(|_| "\"\"".to_owned());
            set_root_key(&mut lines, "model", &rendered);
        } else if find_root_key(&lines, "model").is_some() {
            remove_root_key(&mut lines, "model");
            // When EggPool manages the model slot and no model is requested,
            // the rich catalog lets the user pick from the Codex UI/CLI.
        }
    }

    let base_rendered = serde_json::to_string(base_url).unwrap_or_else(|_| "\"\"".to_owned());
    let table_lines = vec![
        "[model_providers.eggpool]".to_owned(),
        "name = \"EggPool\"".to_owned(),
        format!("base_url = {base_rendered}"),
        "wire_api = \"responses\"".to_owned(),
        "supports_websockets = false".to_owned(),
        format!(
            "env_key = {}",
            serde_json::to_string(EGGPOOL_API_KEY_ENV).expect("env key")
        ),
    ];
    if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
        lines.splice(start..end, table_lines);
    } else {
        if !lines.is_empty() && !lines.last().is_some_and(String::is_empty) {
            lines.push(String::new());
        }
        lines.extend(table_lines);
    }

    let mut text = lines.join("\n");
    if !text.is_empty() && !text.ends_with('\n') {
        text.push('\n');
    }
    (text, previous)
}

fn remove_codex_owned_text(existing: &str, previous: &CodexPrevious, owned_model: bool) -> String {
    let mut lines: Vec<String> = if existing.is_empty() {
        Vec::new()
    } else {
        existing.lines().map(str::to_owned).collect()
    };
    // Restore previous root values where safely possible; otherwise remove
    // only the EggPool-owned assignment.
    match &previous.model_provider {
        Some(value) if !value.is_empty() && value != "eggpool" => {
            set_root_key(
                &mut lines,
                "model_provider",
                &serde_json::to_string(value).expect("toml"),
            );
        }
        _ => {
            remove_root_key(&mut lines, "model_provider");
        }
    }
    match &previous.model_catalog_json {
        Some(value) if !value.is_empty() => {
            set_root_key(
                &mut lines,
                "model_catalog_json",
                &serde_json::to_string(value).expect("toml"),
            );
        }
        _ => {
            remove_root_key(&mut lines, "model_catalog_json");
        }
    }
    if owned_model {
        match &previous.model {
            Some(value) if !value.is_empty() => {
                set_root_key(
                    &mut lines,
                    "model",
                    &serde_json::to_string(value).expect("toml"),
                );
            }
            _ => {
                remove_root_key(&mut lines, "model");
            }
        }
    }
    if let Some(table) = &previous.provider_table {
        if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
            lines.splice(start..end, table.clone());
        }
    } else if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
        lines.drain(start..end);
        // Trim a single orphan blank line left by table removal.
        if start < lines.len()
            && lines[start].trim().is_empty()
            && start > 0
            && lines[start - 1].trim().is_empty()
        {
            lines.remove(start);
        }
    }
    let mut text = lines.join("\n");
    if !text.is_empty() && !text.ends_with('\n') {
        text.push('\n');
    }
    text
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
    let mut issues = Vec::new();
    let lines: Vec<String> = if config_text.is_empty() {
        Vec::new()
    } else {
        config_text.lines().map(str::to_owned).collect()
    };
    if find_root_key(&lines, "model_provider")
        .and_then(|index| toml_string_value(&lines[index]))
        .as_deref()
        != Some("eggpool")
    {
        issues.push("root model_provider is not \"eggpool\"".to_owned());
    }
    let expected_catalog = catalog_path.to_string_lossy().into_owned();
    if find_root_key(&lines, "model_catalog_json")
        .and_then(|index| toml_string_value(&lines[index]))
        .as_deref()
        != Some(expected_catalog.as_str())
    {
        issues.push(format!(
            "root model_catalog_json does not point at {}",
            catalog_path.display()
        ));
    }
    if manage_model {
        let current =
            find_root_key(&lines, "model").and_then(|index| toml_string_value(&lines[index]));
        if current.as_deref() != model {
            issues.push("root model selection differs from requested model".to_owned());
        }
    }
    let table = "model_providers.eggpool";
    if table_value(&lines, table, "name").as_deref() != Some("EggPool") {
        issues.push("[model_providers.eggpool] name differs".to_owned());
    }
    if table_value(&lines, table, "base_url").as_deref() != Some(context.base_url.as_str()) {
        issues.push("[model_providers.eggpool] base_url differs".to_owned());
    }
    if table_value(&lines, table, "wire_api").as_deref() != Some("responses") {
        issues.push("[model_providers.eggpool] wire_api differs".to_owned());
    }
    if table_value(&lines, table, "supports_websockets").as_deref() != Some("false") {
        issues.push("[model_providers.eggpool] supports_websockets differs".to_owned());
    }
    if table_value(&lines, table, "env_key").as_deref() != Some(EGGPOOL_API_KEY_ENV) {
        issues.push("[model_providers.eggpool] env_key differs".to_owned());
    }
    if !catalog_path.exists() {
        issues.push(format!(
            "generated catalog {} is missing",
            catalog_path.display()
        ));
    } else if let Ok(existing) = fs::read_to_string(catalog_path)
        && existing != catalog_json
    {
        issues.push("generated catalog content is stale".to_owned());
    }
    if config_text.contains(&context.api_key) {
        issues.push("client config embeds the resolved server key".to_owned());
    }
    issues
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
    let rendered = build_opencode_config_json(context)?;
    let value: Value = serde_json::from_str(&rendered)?;
    value
        .get("provider")
        .and_then(|provider| provider.get("eggpool"))
        .cloned()
        .ok_or_else(|| IntegrationError::Drift {
            detail: "OpenCode renderer is missing the eggpool provider".to_owned(),
        })
}

fn has_jsonc_comments(raw: &str) -> bool {
    // Best-effort JSONC detection: look for `//` or `/*` outside strings.
    let mut in_string = false;
    let mut escaped = false;
    let bytes = raw.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if escaped {
            escaped = false;
            index += 1;
            continue;
        }
        if in_string {
            if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            index += 1;
            continue;
        }
        if byte == b'"' {
            in_string = true;
            index += 1;
            continue;
        }
        if byte == b'/'
            && index + 1 < bytes.len()
            && (bytes[index + 1] == b'/' || bytes[index + 1] == b'*')
        {
            return true;
        }
        index += 1;
    }
    false
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

    #[test]
    fn projection_derives_only_from_validated_facts() {
        let model = IntegrationModel {
            model_id: "alias".to_owned(),
            base_model_id: "alias".to_owned(),
            provider_id: None,
            display_name: "Alias".to_owned(),
            capabilities: json!({
                "supports_tools": true,
                "supports_vision": false,
                "thinking": {
                    "status": "supported",
                    "supported_efforts": ["high", "low", "high"],
                },
            }),
            source_metadata: json!({"secret": "must-not-leak"}),
            limits: ModelLimits {
                context_tokens: Some(200_000),
                output_tokens: Some(16_000),
                ..Default::default()
            },
        };
        let projection = project_model(&model);
        assert_eq!(projection.public_id, "alias");
        assert_eq!(projection.capabilities.context_tokens, Some(200_000));
        assert_eq!(projection.capabilities.max_output_tokens, Some(16_000));
        assert_eq!(projection.capabilities.input_images, Some(false));
        assert_eq!(projection.capabilities.function_tools, Some(true));
        assert!(projection.capabilities.responses);
        assert!(!projection.capabilities.websockets);
        let reasoning = projection.capabilities.reasoning.expect("reasoning");
        assert_eq!(reasoning.efforts, vec!["high".to_owned(), "low".to_owned()]);
    }

    #[test]
    fn projection_leaves_unknown_absent_and_never_infers_from_name() {
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
        assert_eq!(projection.capabilities.context_tokens, None);
        assert_eq!(projection.capabilities.input_images, None);
        assert_eq!(projection.capabilities.function_tools, None);
        assert_eq!(projection.capabilities.reasoning, None);
        assert!(!projection.capabilities.websockets);
    }

    #[test]
    fn aggregation_uses_conservative_intersection_not_union() {
        let left = AgentModelCapabilities {
            context_tokens: Some(200_000),
            max_output_tokens: Some(16_000),
            input_text: true,
            input_images: Some(true),
            reasoning: Some(AgentReasoningCapabilities {
                efforts: vec!["high".to_owned(), "low".to_owned(), "medium".to_owned()],
                default_effort: None,
                summaries: false,
            }),
            function_tools: Some(true),
            freeform_tools: None,
            deferred_tool_search: None,
            responses: true,
            websockets: false,
        };
        let right = AgentModelCapabilities {
            context_tokens: Some(100_000),
            max_output_tokens: None,
            input_text: true,
            input_images: Some(false),
            reasoning: Some(AgentReasoningCapabilities {
                efforts: vec!["low".to_owned(), "medium".to_owned()],
                default_effort: None,
                summaries: false,
            }),
            function_tools: Some(true),
            freeform_tools: None,
            deferred_tool_search: None,
            responses: true,
            websockets: false,
        };
        let merged = aggregate_projections("alias", "Alias", &[left, right]);
        // Minimum known context, unknown output stays unknown, modalities and
        // efforts intersect.
        assert_eq!(merged.context_tokens, Some(100_000));
        assert_eq!(merged.max_output_tokens, None);
        assert_eq!(merged.input_images, None);
        assert_eq!(merged.function_tools, Some(true));
        let reasoning = merged.reasoning.expect("reasoning");
        assert_eq!(
            reasoning.efforts,
            vec!["low".to_owned(), "medium".to_owned()]
        );
        assert!(!merged.websockets);
    }

    #[test]
    fn codex_catalog_emits_current_required_fields_for_unknown_models() {
        // Regression for live qualification 2026-09-16 against Codex CLI
        // 0.154.0 (`codex debug models`): entries without reasoning support
        // previously omitted `supported_reasoning_levels`, and all entries
        // omitted `base_instructions`, both of which current Codex requires.
        let context = context();
        let catalog = build_codex_catalog_json(&context).expect("catalog");
        let count = validate_codex_catalog_json(&catalog, &context.api_key).expect("valid");
        assert_eq!(count, 1);
        let value: Value = serde_json::from_str(&catalog).expect("json");
        let entry = value
            .get("models")
            .and_then(Value::as_array)
            .and_then(|models| models.first())
            .expect("entry");
        assert_eq!(
            entry
                .get("supported_reasoning_levels")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(0)
        );
        assert_eq!(
            entry.get("base_instructions").and_then(Value::as_str),
            Some("")
        );

        // Validation must reject catalogs missing the current required fields
        // so future renderer regressions fail deterministically.
        let mut missing_levels: Value = serde_json::from_str(&catalog).expect("json");
        for model in missing_levels
            .get_mut("models")
            .and_then(Value::as_array_mut)
            .expect("models")
        {
            model
                .as_object_mut()
                .expect("object")
                .remove("supported_reasoning_levels");
        }
        assert!(
            validate_codex_catalog_json(&missing_levels.to_string(), &context.api_key).is_err()
        );

        let mut missing_instructions: Value = serde_json::from_str(&catalog).expect("json");
        for model in missing_instructions
            .get_mut("models")
            .and_then(Value::as_array_mut)
            .expect("models")
        {
            model
                .as_object_mut()
                .expect("object")
                .remove("base_instructions");
        }
        assert!(
            validate_codex_catalog_json(&missing_instructions.to_string(), &context.api_key)
                .is_err()
        );
    }

    #[test]
    fn codex_catalog_is_deterministic_sanitized_and_bounded() {
        let mut context = context();
        context.models.push(IntegrationModel {
            model_id: "claude-sonnet/anthropic".to_owned(),
            base_model_id: "claude-sonnet".to_owned(),
            provider_id: Some("anthropic".to_owned()),
            display_name: "Claude Sonnet".to_owned(),
            capabilities: json!({
                "supports_vision": true,
                "supports_tools": true,
                "thinking": {"status": "supported", "supported_efforts": ["high"]},
            }),
            source_metadata: json!({"internal": "secret"}),
            limits: ModelLimits {
                context_tokens: Some(200_000),
                output_tokens: Some(8_000),
                ..Default::default()
            },
        });
        let first = build_codex_catalog_json(&context).expect("catalog");
        let second = build_codex_catalog_json(&context).expect("catalog");
        assert_eq!(first, second);
        let count = validate_codex_catalog_json(&first, &context.api_key).expect("valid");
        assert_eq!(count, 2);
        assert!(!first.contains(&context.api_key));
        assert!(!first.contains("internal"));
        assert!(!first.contains("secret"));
        let value: Value = serde_json::from_str(&first).expect("json");
        let models = value
            .get("models")
            .and_then(Value::as_array)
            .expect("models");
        assert_eq!(models.len(), 2);
        // Deterministic ordering by public ID.
        assert_eq!(
            models[0].get("slug").and_then(Value::as_str),
            Some("claude-sonnet/anthropic")
        );
        for model in models {
            assert_eq!(
                model.get("prefer_websockets").and_then(Value::as_bool),
                Some(false)
            );
            assert_eq!(
                model.get("visibility").and_then(Value::as_str),
                Some("list")
            );
            assert!(
                model
                    .get("slug")
                    .and_then(Value::as_str)
                    .is_some_and(|slug| !slug.is_empty())
            );
        }
        // Context/output limits survive parsing; auto-compact is 90%.
        let first_entry = &models[0];
        assert_eq!(
            first_entry.get("context_window").and_then(Value::as_u64),
            Some(200_000)
        );
        assert_eq!(
            first_entry
                .get("auto_compact_token_limit")
                .and_then(Value::as_u64),
            Some(180_000)
        );
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
        assert!(rendered.contains(OPENCODE_RESPONSES_NPM));
        assert!(!rendered.contains("@ai-sdk/openai-compatible"));
        assert!(rendered.contains("{env:EGGPOOL_API_KEY}"));
        let value: Value = serde_json::from_str(&rendered).expect("json");
        let provider = value
            .get("provider")
            .and_then(|provider| provider.get("eggpool"))
            .expect("eggpool provider");
        assert_eq!(
            provider.get("npm").and_then(Value::as_str),
            Some(OPENCODE_RESPONSES_NPM)
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
    fn codex_mutation_preserves_comments_and_unrelated_tables() {
        let existing = "# user comment\nmodel_provider = \"other\"\n\n[other_table]\nkey = 1\n";
        let (proposed, previous) = apply_codex_text_mutation(
            existing,
            "http://127.0.0.1:11300/v1",
            "/tmp/catalog.json",
            None,
            false,
        );
        assert!(proposed.contains("# user comment"));
        assert!(proposed.contains("[other_table]"));
        assert!(proposed.contains("model_provider = \"eggpool\""));
        assert_eq!(previous.model_provider.as_deref(), Some("other"));
        // Root keys are placed before the first table.
        let provider_pos = proposed.find("model_provider").expect("provider");
        let table_pos = proposed.find("[other_table]").expect("table");
        assert!(provider_pos < table_pos);
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
