//! Immutable static wire-profile registry.
//!
//! The profile table is intentionally data-only.  TOML may select one of the
//! codec identifiers owned by Rust, but it cannot name a Rust type, import a
//! module, or carry credentials.  Mutable preference and retry state belong
//! to the later M7 runtime boundary.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::codec::WireCodecId;

const BUILTIN_WIRE_PROFILES: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../src/eggpool/providers/_wire_profiles.toml"
));

/// Stable upstream wire-surface identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WireSurface {
    OpenaiChatCompletions,
    OpenaiResponses,
    AnthropicMessages,
    GeminiInteractions,
    GeminiGenerateContent,
}

/// Compatibility name for callers that use profile IDs as surface names.
pub type WireProfileId = WireSurface;

/// Compatibility name matching the Python wire vocabulary.
pub type WireSurfaceName = WireSurface;

impl WireSurface {
    pub const ALL: [Self; 5] = [
        Self::OpenaiChatCompletions,
        Self::OpenaiResponses,
        Self::AnthropicMessages,
        Self::GeminiInteractions,
        Self::GeminiGenerateContent,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::OpenaiChatCompletions => "openai_chat_completions",
            Self::OpenaiResponses => "openai_responses",
            Self::AnthropicMessages => "anthropic_messages",
            Self::GeminiInteractions => "gemini_interactions",
            Self::GeminiGenerateContent => "gemini_generate_content",
        }
    }
}

impl TryFrom<&str> for WireSurface {
    type Error = WireRegistryError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "openai_chat_completions" => Ok(Self::OpenaiChatCompletions),
            "openai_responses" => Ok(Self::OpenaiResponses),
            "anthropic_messages" => Ok(Self::AnthropicMessages),
            "gemini_interactions" => Ok(Self::GeminiInteractions),
            "gemini_generate_content" => Ok(Self::GeminiGenerateContent),
            _ => Err(WireRegistryError::UnknownProfile(value.to_owned())),
        }
    }
}

/// One built-in profile definition from `_wire_profiles.toml`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WireProfileDefinition {
    pub surface: WireSurface,
    pub request_codec: WireCodecId,
    pub response_codec: WireCodecId,
    pub stream_codec: WireCodecId,
}

impl WireProfileDefinition {
    pub fn codec_family(&self) -> Result<CodecFamily, WireRegistryError> {
        let family = self.request_codec.family();
        if self.response_codec.family() != family || self.stream_codec.family() != family {
            return Err(WireRegistryError::MixedCodecFamily {
                profile: self.surface,
            });
        }
        Ok(family)
    }
}

/// The semantic codec family selected by a static profile.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodecFamily {
    OpenaiChat,
    OpenaiResponses,
    AnthropicMessages,
    GeminiInteractions,
    GeminiGenerateContent,
}

/// Low-authority provider/model metadata from the packaged registry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WireHint {
    pub provider_id: String,
    pub model_id: String,
    pub preferred_surface: WireSurface,
    pub verified_on: String,
    pub source: String,
}

/// Provider-configured path and static dispatch metadata joined to a registry
/// definition.  It contains no credential or environment-resolved secret.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfiguredWireProfile {
    pub definition: WireProfileDefinition,
    pub path_template: String,
    pub stream_path_template: Option<String>,
    pub priority: u32,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum WireRegistryError {
    #[error("wire profile registry TOML is invalid: {0}")]
    Parse(String),
    #[error("wire profile registry has an unsupported or malformed profile id {0:?}")]
    UnknownProfile(String),
    #[error("wire profile {0:?} is missing a required definition")]
    MissingProfile(String),
    #[error("wire profile {profile:?} mixes incompatible codec families")]
    MixedCodecFamily { profile: WireSurface },
    #[error("wire hint references unknown profile {0:?}")]
    HintUnknownProfile(String),
    #[error("provider references unknown wire profile {0:?}")]
    ProviderUnknownProfile(String),
    #[error("provider model preference references unavailable wire profile {0:?}")]
    ModelPreferenceUnavailable(String),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawProfileDefinition {
    request_codec: WireCodecId,
    response_codec: WireCodecId,
    stream_codec: WireCodecId,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRegistry {
    profiles: BTreeMap<String, RawProfileDefinition>,
    #[serde(default)]
    hints: Vec<WireHintRaw>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireHintRaw {
    provider_id: String,
    model_id: String,
    preferred_surface: String,
    verified_on: String,
    source: String,
}

/// Closed, immutable registry of statically declared wire profiles.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireProfileRegistry {
    profiles: BTreeMap<WireSurface, WireProfileDefinition>,
    hints: Vec<WireHint>,
}

impl WireProfileRegistry {
    /// Parse the packaged Python-oracle registry at compile-time-selected path.
    pub fn embedded() -> Result<Self, WireRegistryError> {
        Self::from_toml(BUILTIN_WIRE_PROFILES)
    }

    /// Parse registry data without accessing the filesystem or environment.
    pub fn from_toml(text: &str) -> Result<Self, WireRegistryError> {
        let raw: RawRegistry =
            toml::from_str(text).map_err(|error| WireRegistryError::Parse(error.to_string()))?;
        if raw.profiles.is_empty() {
            return Err(WireRegistryError::Parse(
                "profiles must contain at least one entry".into(),
            ));
        }

        let mut profiles = BTreeMap::new();
        for (profile_id, raw_profile) in raw.profiles {
            let surface = WireSurface::try_from(profile_id.as_str())?;
            let definition = WireProfileDefinition {
                surface,
                request_codec: raw_profile.request_codec,
                response_codec: raw_profile.response_codec,
                stream_codec: raw_profile.stream_codec,
            };
            definition.codec_family()?;
            profiles.insert(surface, definition);
        }

        let mut hints = Vec::with_capacity(raw.hints.len());
        for raw_hint in raw.hints {
            let preferred_surface = WireSurface::try_from(raw_hint.preferred_surface.as_str())
                .map_err(|_| {
                    WireRegistryError::HintUnknownProfile(raw_hint.preferred_surface.clone())
                })?;
            if !profiles.contains_key(&preferred_surface) {
                return Err(WireRegistryError::HintUnknownProfile(
                    raw_hint.preferred_surface,
                ));
            }
            if raw_hint.provider_id.trim().is_empty()
                || raw_hint.model_id.trim().is_empty()
                || raw_hint.verified_on.trim().is_empty()
                || raw_hint.source.trim().is_empty()
            {
                return Err(WireRegistryError::Parse(
                    "wire hints require non-empty provider, model, verification, and source fields"
                        .into(),
                ));
            }
            hints.push(WireHint {
                provider_id: raw_hint.provider_id,
                model_id: raw_hint.model_id,
                preferred_surface,
                verified_on: raw_hint.verified_on,
                source: raw_hint.source,
            });
        }
        hints.sort_by(|left, right| {
            (
                &left.provider_id,
                &left.model_id,
                left.preferred_surface,
                &left.verified_on,
                &left.source,
            )
                .cmp(&(
                    &right.provider_id,
                    &right.model_id,
                    right.preferred_surface,
                    &right.verified_on,
                    &right.source,
                ))
        });

        Ok(Self { profiles, hints })
    }

    pub fn get(&self, surface: WireSurface) -> Option<&WireProfileDefinition> {
        self.profiles.get(&surface)
    }

    pub fn get_str(&self, profile_id: &str) -> Result<&WireProfileDefinition, WireRegistryError> {
        let surface = WireSurface::try_from(profile_id)?;
        self.get(surface)
            .ok_or_else(|| WireRegistryError::MissingProfile(profile_id.to_owned()))
    }

    pub fn profiles(&self) -> impl Iterator<Item = &WireProfileDefinition> {
        self.profiles.values()
    }

    pub fn profile_ids(&self) -> Vec<&'static str> {
        self.profiles
            .keys()
            .map(|surface| surface.as_str())
            .collect()
    }

    pub fn hints(&self) -> &[WireHint] {
        &self.hints
    }

    pub fn supports(&self, surface: WireSurface) -> bool {
        self.profiles.contains_key(&surface)
    }

    /// Join static registry definitions to provider-owned path metadata.
    pub fn configured_profiles(
        &self,
        surfaces: &BTreeMap<String, crate::config::ProviderWireSurfaceConfig>,
    ) -> Result<Vec<ConfiguredWireProfile>, WireRegistryError> {
        let mut result = Vec::with_capacity(surfaces.len());
        for (surface_id, config) in surfaces {
            let surface = WireSurface::try_from(surface_id.as_str())
                .map_err(|_| WireRegistryError::ProviderUnknownProfile(surface_id.clone()))?;
            let definition = self
                .get(surface)
                .ok_or_else(|| WireRegistryError::ProviderUnknownProfile(surface_id.clone()))?;
            result.push(ConfiguredWireProfile {
                definition: definition.clone(),
                path_template: config.path_template.clone(),
                stream_path_template: config.stream_path_template.clone(),
                priority: config.priority,
            });
        }
        result.sort_by_key(|profile| (profile.priority, profile.definition.surface));
        Ok(result)
    }

    pub fn validate_provider_references(
        &self,
        surfaces: &BTreeMap<String, crate::config::ProviderWireSurfaceConfig>,
        model_preferences: &BTreeMap<String, crate::config::ModelWirePreference>,
    ) -> Result<(), WireRegistryError> {
        for surface_id in surfaces.keys() {
            if !self.supports(
                WireSurface::try_from(surface_id.as_str())
                    .map_err(|_| WireRegistryError::ProviderUnknownProfile(surface_id.clone()))?,
            ) {
                return Err(WireRegistryError::ProviderUnknownProfile(
                    surface_id.clone(),
                ));
            }
        }
        for preference in model_preferences.values() {
            let surface =
                WireSurface::try_from(preference.preferred_surface.as_str()).map_err(|_| {
                    WireRegistryError::ModelPreferenceUnavailable(
                        preference.preferred_surface.clone(),
                    )
                })?;
            if !surfaces.contains_key(surface.as_str()) {
                return Err(WireRegistryError::ModelPreferenceUnavailable(
                    preference.preferred_surface.clone(),
                ));
            }
        }
        Ok(())
    }
}
