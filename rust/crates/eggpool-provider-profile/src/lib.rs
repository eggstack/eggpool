//! Neutral, secret-free, sans-I/O provider-profile contract.
//!
//! This crate is the shared owner of provider *metadata*: base URLs, declared
//! wire surfaces and their paths, structural authentication shape, model
//! discovery, credential-verification policy, and reviewed exact model-to-wire
//! hints. EggPool consumes the same canonical data that sibling consumers do,
//! so a downstream integration never has to duplicate or guess provider facts.
//!
//! # Boundaries
//!
//! - **Secret-free.** Profiles describe authentication *structure* — mode,
//!   header name, scheme. There is no field for a credential value and no
//!   field for an environment or credential reference; the parser rejects both
//!   (see [`ProviderStaticHeaderProfile`]).
//! - **Sans-I/O.** No clock, no environment reads, no filesystem access at
//!   runtime, no network, no async runtime, no database, no logging. Parsing
//!   is a pure function of the document text.
//! - **Not a runtime.** Accounts, credentials, routing, quota, health,
//!   quarantine, retries, persistence, live catalog refresh, transport, and
//!   wire negotiation stay with the owning runtime. This crate names facts; it
//!   never executes them.
//! - **No second wire vocabulary.** Surfaces are the identities owned by
//!   [`eggpool_wire`], so a hint here selects exactly the codec surface a
//!   consumer will encode with.
//! - **First-party documentation is the authority.** The embedded profiles are
//!   reviewed transcriptions of provider documentation, not an independent
//!   source of truth.
//!
//! # Usage
//!
//! ```
//! use eggpool_provider_profile::{ProviderProfileRegistry, WireSurface};
//!
//! let registry = ProviderProfileRegistry::embedded().expect("embedded profiles parse");
//! let opencode = registry.require("opencode-go").expect("OpenCode Go is bundled");
//!
//! assert_eq!(opencode.base_url, "https://opencode.ai/zen/go/v1");
//! assert_eq!(
//!     opencode.surface_url(WireSurface::AnthropicMessages).as_deref(),
//!     Some("https://opencode.ai/zen/go/v1/messages")
//! );
//!
//! // A model with no reviewed hint stays unresolved.
//! assert!(opencode.model_wire_preference("not-a-reviewed-model").is_none());
//! ```

#![forbid(unsafe_code)]

pub mod profile;
pub mod surface;
pub mod verification;

use std::collections::BTreeMap;

use serde::Deserialize;
use thiserror::Error;

pub use profile::{
    ProfileValidationError, ProviderAdditionalAuthProfile, ProviderAuthMode, ProviderAuthProfile,
    ProviderModelWirePreference, ProviderModelsEndpointProfile, ProviderProfile,
    ProviderStaticHeaderProfile, ProviderStaticModelProfile, ProviderWireSurfaceProfile,
    compose_url, valid_header_name, validate_path, validate_profile,
};
pub use surface::WireSurface;
pub use verification::{
    CatalogAuthentication, CatalogEvidence, CredentialProof, CredentialStatus, ProfileVerification,
    ProviderVerificationPolicy, VerificationError,
};

/// The canonical embedded provider-profile document.
///
/// This is the single bundled source of provider metadata in the repository:
/// EggPool and every sibling consumer project the same bytes.
pub const EMBEDDED_PROVIDER_PROFILES_TOML: &str = include_str!("../assets/_provider_profiles.toml");

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDocument {
    providers: BTreeMap<String, ProviderProfile>,
}

/// Parsed, validated provider profiles keyed by provider id.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderProfileRegistry {
    profiles: BTreeMap<String, ProviderProfile>,
}

impl ProviderProfileRegistry {
    /// Parse the canonical embedded profiles.
    ///
    /// Malformed bundled data fails here, before any runtime can act on it.
    pub fn embedded() -> Result<Self, ProviderProfileError> {
        Self::from_toml(EMBEDDED_PROVIDER_PROFILES_TOML)
    }

    /// Parse and validate a provider-profile document without touching the
    /// filesystem or environment.
    pub fn from_toml(text: &str) -> Result<Self, ProviderProfileError> {
        let document: RawDocument =
            toml::from_str(text).map_err(|error| ProviderProfileError::Parse(error.to_string()))?;
        let mut profiles = BTreeMap::new();
        for (id, profile) in document.providers {
            validate_profile(&id, &profile).map_err(|source| ProviderProfileError::Invalid {
                provider: id.clone(),
                source,
            })?;
            profiles.insert(id, profile);
        }
        if profiles.is_empty() {
            return Err(ProviderProfileError::Parse(
                "providers must contain at least one entry".into(),
            ));
        }
        Ok(Self { profiles })
    }

    /// Look up one profile.
    pub fn get(&self, id: &str) -> Option<&ProviderProfile> {
        self.profiles.get(id)
    }

    /// Look up one profile, failing closed when it is absent.
    pub fn require(&self, id: &str) -> Result<&ProviderProfile, ProviderProfileError> {
        self.profiles
            .get(id)
            .ok_or_else(|| ProviderProfileError::Unknown(id.to_owned()))
    }

    pub fn profiles(&self) -> impl Iterator<Item = &ProviderProfile> {
        self.profiles.values()
    }

    pub fn ids(&self) -> impl Iterator<Item = &str> {
        self.profiles.keys().map(String::as_str)
    }

    pub fn len(&self) -> usize {
        self.profiles.len()
    }

    pub fn is_empty(&self) -> bool {
        self.profiles.is_empty()
    }

    /// Resolve an exact model-to-wire hint for one provider.
    ///
    /// Returns `None` for an unknown provider *and* for an unknown model id.
    /// The lookup is exact: no prefix or model-family guessing.
    pub fn model_wire_preference(
        &self,
        provider_id: &str,
        model_id: &str,
    ) -> Option<&ProviderModelWirePreference> {
        self.profiles
            .get(provider_id)?
            .model_wire_preference(model_id)
    }
}

/// Failure to parse or validate a provider-profile document.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ProviderProfileError {
    #[error("provider-profile document is invalid: {0}")]
    Parse(String),
    #[error("provider {0:?} is not present in the registry")]
    Unknown(String),
    #[error("provider {provider} is invalid: {source}")]
    Invalid {
        provider: String,
        #[source]
        source: ProfileValidationError,
    },
}
