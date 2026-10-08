//! Provider-profile data contract.
//!
//! The types here describe provider *facts*: identity, base URL, declared
//! protocols, per-surface paths, structural authentication shape, model
//! discovery, verification policy, and reviewed model-to-wire hints. They
//! describe nothing about accounts, credentials, routing, quota, health,
//! retries, persistence, or transport.
//!
//! Every field is metadata that a consumer can act on without the EggPool
//! daemon. Authentication entries carry a mode, a header *name*, and a scheme;
//! they can never carry a credential value, and the parser has no field in
//! which a secret reference could be written.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::surface::WireSurface;

/// Default Chat Completions path, matching the owning runtime's configuration
/// default for a provider that declares the `openai` protocol.
pub const DEFAULT_CHAT_COMPLETIONS_PATH: &str = "/chat/completions";
/// Default Anthropic Messages path, matching the owning runtime's
/// configuration default for a provider that declares `anthropic`.
pub const DEFAULT_ANTHROPIC_MESSAGES_PATH: &str = "/messages";
use crate::verification::{ProfileVerification, ProviderVerificationPolicy};

/// Authentication shape a surface or provider requires.
///
/// This is a structural fact. The credential itself is resolved and applied by
/// the owning runtime and never appears in provider-profile data.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderAuthMode {
    /// `Authorization: Bearer <credential>`.
    #[default]
    Bearer,
    /// The credential is sent as a bare header value, conventionally `x-api-key`.
    ApiKey,
    /// The credential is sent verbatim in the `Authorization` header.
    RawAuthorization,
    /// The surface requires no credential.
    None,
}

impl ProviderAuthMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Bearer => "bearer",
            Self::ApiKey => "api_key",
            Self::RawAuthorization => "raw_authorization",
            Self::None => "none",
        }
    }

    /// Parse an auth mode, returning `None` for an unknown value.
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "bearer" => Some(Self::Bearer),
            "api_key" => Some(Self::ApiKey),
            "raw_authorization" => Some(Self::RawAuthorization),
            "none" => Some(Self::None),
            _ => None,
        }
    }

    /// Whether this mode consumes a credential at all.
    pub fn requires_credential(self) -> bool {
        !matches!(self, Self::None)
    }
}

impl std::str::FromStr for ProviderAuthMode {
    type Err = ProfileValidationError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::parse(value).ok_or(ProfileValidationError::AuthMode(value.to_owned()))
    }
}

/// Structural authentication facts for a provider or one of its surfaces.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderAuthProfile {
    pub mode: ProviderAuthMode,
    /// Header *name* that carries the credential. Never a value.
    pub header: String,
    /// Authorization scheme, empty when the mode sends a bare header value.
    pub scheme: String,
    /// Additional distinct credential headers some providers require.
    pub additional: Vec<ProviderAdditionalAuthProfile>,
}

impl Default for ProviderAuthProfile {
    fn default() -> Self {
        Self {
            mode: ProviderAuthMode::Bearer,
            header: "Authorization".into(),
            scheme: "Bearer".into(),
            additional: Vec::new(),
        }
    }
}

/// One additional structural credential header.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderAdditionalAuthProfile {
    pub mode: ProviderAuthMode,
    pub header: String,
    pub scheme: String,
}

impl Default for ProviderAdditionalAuthProfile {
    fn default() -> Self {
        Self {
            mode: ProviderAuthMode::Bearer,
            header: "Authorization".into(),
            scheme: "Bearer".into(),
        }
    }
}

impl ProviderAuthProfile {
    /// Lower-cased names of every credential header this profile declares.
    pub fn credential_header_names(&self) -> BTreeSet<String> {
        std::iter::once(self.header.to_ascii_lowercase())
            .chain(
                self.additional
                    .iter()
                    .map(|item| item.header.to_ascii_lowercase()),
            )
            .collect()
    }

    fn validate(&self) -> Result<(), ProfileValidationError> {
        if !valid_header_name(&self.header)
            || self.scheme.is_empty()
            || self.scheme.chars().any(char::is_whitespace)
        {
            return Err(ProfileValidationError::AuthProfile(
                "header must be a valid HTTP token and scheme a non-empty whitespace-free token"
                    .into(),
            ));
        }
        if self.mode == ProviderAuthMode::None && !self.additional.is_empty() {
            return Err(ProfileValidationError::AuthProfile(
                "additional credential headers are declared while mode is none".into(),
            ));
        }
        let mut seen = BTreeSet::from([self.header.to_ascii_lowercase()]);
        for item in &self.additional {
            if item.mode == ProviderAuthMode::None
                || !valid_header_name(&item.header)
                || item.scheme.is_empty()
                || item.scheme.chars().any(char::is_whitespace)
                || !seen.insert(item.header.to_ascii_lowercase())
            {
                return Err(ProfileValidationError::AuthProfile(
                    "additional credential header is invalid or duplicated".into(),
                ));
            }
        }
        Ok(())
    }
}

/// A static request header a provider requires on a surface.
///
/// The value is a non-secret protocol constant (`anthropic-version`, for
/// example). There is deliberately no environment-reference field: secret
/// material is resolved by the owning runtime, never by shared profile data,
/// and the parser rejects any attempt to add one.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderStaticHeaderProfile {
    pub name: String,
    pub value: Option<String>,
}

/// Path, priority, and optional per-surface overrides for one wire surface.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderWireSurfaceProfile {
    /// Path appended to the provider base URL. `{model}` is the only
    /// permitted placeholder.
    pub path_template: String,
    /// Optional distinct streaming path.
    pub stream_path_template: Option<String>,
    /// Negotiation priority; lower values are preferred.
    pub priority: u32,
    /// Surface-specific authentication override.
    pub auth: Option<ProviderAuthProfile>,
    /// Surface-specific static headers.
    pub headers: Vec<ProviderStaticHeaderProfile>,
    /// Historical `POST /v1/responses/compact` support.
    pub supports_remote_compaction_v1: bool,
    /// Upstream compact path; required when v1 support is advertised.
    pub compact_path_template: Option<String>,
    /// Current Codex v2 `compaction_trigger` support.
    pub supports_remote_compaction_v2: bool,
}

impl Default for ProviderWireSurfaceProfile {
    fn default() -> Self {
        Self {
            path_template: String::new(),
            stream_path_template: None,
            priority: 100,
            auth: None,
            headers: Vec::new(),
            supports_remote_compaction_v1: false,
            compact_path_template: None,
            supports_remote_compaction_v2: false,
        }
    }
}

/// Model-discovery endpoint facts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderModelsEndpointProfile {
    pub method: String,
    pub path: String,
    pub body: Option<toml::Value>,
    pub query: BTreeMap<String, String>,
    /// Whether discovery must succeed before the provider is considered ready.
    pub required: bool,
}

impl Default for ProviderModelsEndpointProfile {
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

/// A reviewed exact model-to-wire hint.
///
/// `fixed` is the only field that may hard-pin a model to a surface. Every
/// bundled hint is advisory (`fixed = false`) so a runtime retains its ability
/// to learn or recover when an upstream contract changes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderModelWirePreference {
    pub preferred_surface: WireSurface,
    pub fixed: bool,
}

/// A bundled catalog seed for providers that cannot serve live discovery.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderStaticModelProfile {
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

/// One provider's shared, secret-free connection contract.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderProfile {
    /// Stable provider identity. Must match the registry key.
    pub id: String,
    /// Absolute `http(s)` base URL, without a trailing slash.
    pub base_url: String,
    /// Declared upstream protocols (`openai`, `anthropic`).
    pub protocols: Vec<String>,

    // Presentation metadata used by bootstrap selectors. It carries no
    // connection or credential facts.
    pub display_name: Option<String>,
    pub status: Option<String>,
    pub category: Option<String>,
    pub region: Option<String>,
    pub recommended: Option<bool>,
    pub notes: Option<String>,

    // Connection contract.
    pub auth: ProviderAuthProfile,
    pub wire_surfaces: BTreeMap<WireSurface, ProviderWireSurfaceProfile>,
    pub headers: Vec<ProviderStaticHeaderProfile>,
    pub models_endpoint: Option<ProviderModelsEndpointProfile>,
    pub verify: ProfileVerification,
    pub model_wire: BTreeMap<String, ProviderModelWirePreference>,
    pub static_models: Vec<ProviderStaticModelProfile>,
    /// Runtime capability overrides carried verbatim from the bundled data.
    ///
    /// These are operating policies of the owning runtime, not connection
    /// metadata: the shared contract preserves them so the canonical document
    /// stays the single source of bundled truth, and never interprets them.
    #[serde(rename = "model_capabilities")]
    pub runtime_capabilities: BTreeMap<String, toml::Value>,

    // Legacy path/method facts retained verbatim so the bundled data keeps
    // projecting onto the owning runtime's configuration shape. They are only
    // used when the equivalent structured fact is absent.
    pub openai_path: Option<String>,
    pub anthropic_path: Option<String>,
    pub responses_path: Option<String>,
    pub models_method: Option<String>,
    pub models_path: Option<String>,
}

impl ProviderProfile {
    /// Display label for selectors; falls back to the provider id.
    pub fn label(&self) -> &str {
        self.display_name.as_deref().unwrap_or(&self.id)
    }

    /// Whether the profile is offered as a recommended option.
    pub fn is_recommended(&self) -> bool {
        self.recommended.unwrap_or(false)
    }

    /// Model-discovery endpoint for this profile.
    ///
    /// The structured endpoint wins; otherwise the legacy method/path pair is
    /// used, and finally the runtime's conventional `GET /models` default. The
    /// resolution order matches the owning runtime's configuration
    /// normalization so both agree on where discovery happens.
    pub fn resolved_models_endpoint(&self) -> ProviderModelsEndpointProfile {
        if let Some(endpoint) = &self.models_endpoint {
            return endpoint.clone();
        }
        ProviderModelsEndpointProfile {
            method: self
                .models_method
                .clone()
                .unwrap_or_else(|| "GET".into())
                .to_ascii_uppercase(),
            path: self.models_path.clone().unwrap_or_else(|| "/models".into()),
            body: None,
            query: BTreeMap::new(),
            required: self.verify.require_models,
        }
    }

    /// Effective wire surfaces, deriving them from the legacy path fields when
    /// the profile declares no explicit `wire_surfaces` table.
    ///
    /// The derivation mirrors the owning runtime's configuration
    /// normalization exactly: an explicit table always wins, and the legacy
    /// fields are consulted only for a profile that declares none.
    pub fn resolved_wire_surfaces(&self) -> BTreeMap<WireSurface, ProviderWireSurfaceProfile> {
        if !self.wire_surfaces.is_empty() {
            return self.wire_surfaces.clone();
        }
        WireSurface::ALL
            .into_iter()
            .filter_map(|surface| {
                self.surface_path_template(surface).map(|path_template| {
                    (
                        surface,
                        ProviderWireSurfaceProfile {
                            path_template: path_template.to_owned(),
                            ..ProviderWireSurfaceProfile::default()
                        },
                    )
                })
            })
            .collect()
    }

    /// Path template this profile serves for `surface`, derived exactly as
    /// [`Self::resolved_wire_surfaces`] derives it.
    ///
    /// A declared `openai`/`anthropic` protocol implies the conventional
    /// default path when the profile states none, matching the owning
    /// runtime's configuration defaults.
    pub fn surface_path_template(&self, surface: WireSurface) -> Option<&str> {
        if let Some(entry) = self.wire_surfaces.get(&surface) {
            return Some(entry.path_template.as_str());
        }
        let declares = |protocol: &str| self.protocols.iter().any(|value| value == protocol);
        let path = match surface {
            WireSurface::OpenaiChatCompletions if declares("openai") => self
                .openai_path
                .as_deref()
                .unwrap_or(DEFAULT_CHAT_COMPLETIONS_PATH),
            WireSurface::OpenaiResponses => self.responses_path.as_deref()?,
            WireSurface::AnthropicMessages if declares("anthropic") => self
                .anthropic_path
                .as_deref()
                .unwrap_or(DEFAULT_ANTHROPIC_MESSAGES_PATH),
            _ => return None,
        };
        if path.is_empty() {
            return None;
        }
        Some(path)
    }

    /// Resolve a surface's authentication: the surface override, else the
    /// provider default.
    pub fn surface_auth(&self, surface: WireSurface) -> Option<&ProviderAuthProfile> {
        self.wire_surfaces
            .get(&surface)
            .and_then(|entry| entry.auth.as_ref())
            .or(Some(&self.auth))
    }

    /// Compose the absolute URL for a surface from the provider base URL and
    /// the surface path template. `{model}` is left for the caller to fill.
    pub fn surface_url(&self, surface: WireSurface) -> Option<String> {
        let path_template = self.surface_path_template(surface)?;
        Some(compose_url(&self.base_url, path_template))
    }

    /// Exact advisory surface hint for a model id.
    ///
    /// A model id with no reviewed hint is unresolved. The lookup never
    /// guesses from prefixes or model families, and an unknown id stays
    /// unknown.
    pub fn model_wire_preference(&self, model_id: &str) -> Option<&ProviderModelWirePreference> {
        self.model_wire.get(model_id)
    }

    /// Consumer-facing verification policy for this profile.
    ///
    /// A credentialed profile is deferred: catalog discovery is not credential
    /// proof, and no bundled profile is evidence-qualified for an
    /// authenticated metadata verification.
    pub fn verification_policy(&self) -> ProviderVerificationPolicy {
        if self.auth.mode.requires_credential() {
            self.verify.policy_with_credential(None)
        } else {
            self.verify.policy_without_credential()
        }
    }
}

/// Join a base URL and a path with exactly one separator.
pub fn compose_url(base: &str, path: &str) -> String {
    format!(
        "{}/{}",
        base.trim_end_matches('/'),
        path.trim_start_matches('/')
    )
}

/// Rejected provider-profile facts.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ProfileValidationError {
    #[error("profile document declares provider {key:?} whose id is {id:?}")]
    IdKeyMismatch { key: String, id: String },
    #[error("provider id must not be empty")]
    EmptyId,
    #[error("base URL must be an absolute http(s) URL without a trailing slash")]
    BaseUrl,
    #[error("profile declares no supported protocol")]
    Protocols,
    #[error("authentication profile is invalid: {0}")]
    AuthProfile(String),
    #[error("authentication mode {0:?} is not supported")]
    AuthMode(String),
    #[error("static header is invalid: {0}")]
    StaticHeader(String),
    #[error("path template is invalid: {0}")]
    Path(String),
    #[error("models endpoint is invalid: {0}")]
    ModelsEndpoint(String),
    #[error("verification metadata is invalid: {0}")]
    Verification(&'static str),
    #[error("model {model:?} references surface {surface:?} the profile does not serve")]
    UnknownSurface { model: String, surface: String },
    #[error("static model declaration is invalid: {0}")]
    StaticModel(&'static str),
}

/// Fail-closed validation of one profile.
pub fn validate_profile(
    key: &str,
    profile: &ProviderProfile,
) -> Result<(), ProfileValidationError> {
    if profile.id.trim().is_empty() {
        return Err(ProfileValidationError::EmptyId);
    }
    if profile.id != key {
        return Err(ProfileValidationError::IdKeyMismatch {
            key: key.to_owned(),
            id: profile.id.clone(),
        });
    }
    if !is_absolute_base_url(&profile.base_url) {
        return Err(ProfileValidationError::BaseUrl);
    }
    if profile.protocols.is_empty()
        || profile
            .protocols
            .iter()
            .any(|protocol| !matches!(protocol.as_str(), "openai" | "anthropic"))
    {
        return Err(ProfileValidationError::Protocols);
    }

    profile.auth.validate()?;
    validate_static_headers(&profile.headers, Some(&profile.auth))?;
    if profile
        .verify
        .probe_protocol
        .as_deref()
        .is_some_and(|protocol| !matches!(protocol, "openai" | "anthropic"))
    {
        return Err(ProfileValidationError::Verification(
            "probe_protocol must be openai or anthropic",
        ));
    }
    if profile
        .verify
        .probe_model
        .as_deref()
        .is_some_and(|model| model.trim().is_empty())
    {
        return Err(ProfileValidationError::Verification(
            "probe_model must not be empty",
        ));
    }

    if let Some(endpoint) = &profile.models_endpoint {
        let method = endpoint.method.to_ascii_uppercase();
        if method != "GET" && method != "POST" {
            return Err(ProfileValidationError::ModelsEndpoint(
                "method must be GET or POST".into(),
            ));
        }
        validate_path(&endpoint.path)?;
    }
    // The structured endpoint and the legacy method/path pair describe the same
    // endpoint. Declaring both with different values is drift, not intent.
    if let (Some(endpoint), Some(path)) = (&profile.models_endpoint, &profile.models_path) {
        let method = profile
            .models_method
            .clone()
            .unwrap_or_else(|| endpoint.method.clone())
            .to_ascii_uppercase();
        if endpoint.path != *path || method != endpoint.method.to_ascii_uppercase() {
            return Err(ProfileValidationError::ModelsEndpoint(
                "models_endpoint disagrees with the legacy models_method/models_path pair".into(),
            ));
        }
    } else if let Some(path) = &profile.models_path {
        validate_path(path)?;
        if let Some(method) = &profile.models_method {
            let method = method.to_ascii_uppercase();
            if method != "GET" && method != "POST" {
                return Err(ProfileValidationError::ModelsEndpoint(
                    "legacy models_method must be GET or POST".into(),
                ));
            }
        }
    }
    for path in [
        profile.openai_path.as_deref(),
        profile.anthropic_path.as_deref(),
        profile.responses_path.as_deref(),
    ]
    .into_iter()
    .flatten()
    {
        validate_path(path)?;
    }

    for (surface, entry) in &profile.wire_surfaces {
        validate_path(&entry.path_template)?;
        if let Some(path) = &entry.stream_path_template {
            validate_path(path)?;
        }
        if let Some(path) = &entry.compact_path_template {
            validate_path(path)?;
        }
        if entry.supports_remote_compaction_v1 && entry.compact_path_template.is_none() {
            return Err(ProfileValidationError::Path(format!(
                "surface {surface:?} advertises remote compaction v1 without a compact path"
            )));
        }
        if entry.supports_remote_compaction_v2 && *surface != WireSurface::OpenaiResponses {
            return Err(ProfileValidationError::Path(format!(
                "surface {surface:?} advertises remote compaction v2 on a non-Responses surface"
            )));
        }
        if let Some(auth) = &entry.auth {
            auth.validate()?;
        }
        validate_static_headers(&entry.headers, entry.auth.as_ref().or(Some(&profile.auth)))?;
    }

    let surfaces = profile.resolved_wire_surfaces();
    for (model, preference) in &profile.model_wire {
        if model.trim().is_empty() || !surfaces.contains_key(&preference.preferred_surface) {
            return Err(ProfileValidationError::UnknownSurface {
                model: model.clone(),
                surface: preference.preferred_surface.as_str().to_owned(),
            });
        }
    }

    let mut static_models = BTreeSet::new();
    for model in &profile.static_models {
        if model.id.trim().is_empty()
            || !static_models.insert(model.id.clone())
            || model
                .protocol
                .as_deref()
                .is_some_and(|protocol| !matches!(protocol, "openai" | "anthropic"))
            || model.max_context_tokens == Some(0)
            || model.max_input_tokens == Some(0)
            || model.max_output_tokens == Some(0)
            || model
                .max_input_tokens
                .zip(model.max_context_tokens)
                .is_some_and(|(limit, context)| limit > context)
            || model
                .max_output_tokens
                .zip(model.max_context_tokens)
                .is_some_and(|(limit, context)| limit > context)
        {
            return Err(ProfileValidationError::StaticModel(
                "id, protocol, and limit declarations are invalid or duplicated",
            ));
        }
    }

    Ok(())
}

/// Whether a base URL is an absolute `http(s)` URL without a trailing slash.
fn is_absolute_base_url(value: &str) -> bool {
    (value.starts_with("https://") || value.starts_with("http://"))
        && !value.ends_with('/')
        && !value.chars().any(char::is_whitespace)
}

fn validate_static_headers(
    headers: &[ProviderStaticHeaderProfile],
    auth: Option<&ProviderAuthProfile>,
) -> Result<(), ProfileValidationError> {
    let credential_headers = auth
        .map(ProviderAuthProfile::credential_header_names)
        .unwrap_or_default();
    let mut seen = BTreeSet::new();
    for header in headers {
        if !valid_header_name(&header.name) || !seen.insert(header.name.to_ascii_lowercase()) {
            return Err(ProfileValidationError::StaticHeader(
                "name is invalid or duplicated".into(),
            ));
        }
        // A literal value on a credential header would be a credential in
        // shared data. Credential values are resolved by the owning runtime.
        if header.value.is_some() && credential_headers.contains(&header.name.to_ascii_lowercase())
        {
            return Err(ProfileValidationError::StaticHeader(format!(
                "{} is a credential header and must not carry a literal value",
                header.name
            )));
        }
        if header
            .value
            .as_deref()
            .is_some_and(|value| value.chars().any(|char| matches!(char, '\r' | '\n' | '\0')))
        {
            return Err(ProfileValidationError::StaticHeader(
                "value must not contain CR, LF, or NUL".into(),
            ));
        }
    }
    Ok(())
}

/// Validate a wire path template: an absolute path whose only placeholder is
/// `{model}`.
pub fn validate_path(value: &str) -> Result<(), ProfileValidationError> {
    let without_model = value.replace("{model}", "");
    if value.is_empty()
        || value.trim() != value
        || !value.starts_with('/')
        || value.contains('?')
        || value.contains('#')
        || without_model.contains('{')
        || without_model.contains('}')
    {
        return Err(ProfileValidationError::Path(format!(
            "{value:?} is not a relative provider path template"
        )));
    }
    Ok(())
}

/// HTTP token grammar for a header name.
pub fn valid_header_name(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
}
