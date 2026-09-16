//! Remote integration-profile DTO.
//!
//! Plan 211 will serve this object over HTTP; this crate defines its portable
//! schema so the server and `eggpool-connect` compile against one source of
//! truth. The object is deterministic, bounded, secret-free, and sufficient
//! for Codex/OpenCode rendering without another private server API.

use serde::{Deserialize, Serialize};

use crate::error::ClientConfigError;
use crate::hash::sha256_hex;
use crate::profile::validate_base_url;
use crate::projection::AgentModelProjection;

/// Current remote integration-profile schema version.
pub const INTEGRATION_PROFILE_SCHEMA_VERSION: u32 = 1;
/// Maximum models in one integration profile.
pub const MAX_INTEGRATION_MODELS: usize = 1000;
/// Maximum encoded profile bytes.
pub const MAX_INTEGRATION_PROFILE_BYTES: usize = 1024 * 1024;

/// Top-level integration capabilities (portable, secret-free).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IntegrationCapabilities {
    pub responses: bool,
    pub websockets: bool,
}

impl Default for IntegrationCapabilities {
    fn default() -> Self {
        Self {
            responses: true,
            websockets: false,
        }
    }
}

/// Versioned portable integration profile.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentIntegrationProfileV1 {
    pub schema_version: u32,
    /// Deterministic hex revision derived from sanitized canonical content.
    pub revision: String,
    pub base_url: String,
    pub models: Vec<AgentModelProjection>,
    pub capabilities: IntegrationCapabilities,
}

impl AgentIntegrationProfileV1 {
    /// Build a validated profile with deterministic ordering and revision.
    pub fn new(
        base_url: &str,
        models: Vec<AgentModelProjection>,
        capabilities: IntegrationCapabilities,
    ) -> Result<Self, ClientConfigError> {
        if models.len() > MAX_INTEGRATION_MODELS {
            return Err(ClientConfigError::CatalogTooLarge {
                count: models.len(),
            });
        }
        let mut sorted = models;
        sorted.sort_by(|left, right| left.public_id.cmp(&right.public_id));
        validate_base_url(base_url)?;
        if capabilities.websockets {
            return Err(ClientConfigError::InvalidField {
                field: "capabilities.websockets".to_owned(),
                detail: "portable profiles must not advertise websockets".to_owned(),
            });
        }
        let revision = Self::compute_revision(base_url, &sorted, &capabilities);
        let profile = Self {
            schema_version: INTEGRATION_PROFILE_SCHEMA_VERSION,
            revision,
            base_url: base_url.to_owned(),
            models: sorted,
            capabilities,
        };
        profile.validate()?;
        Ok(profile)
    }

    /// Deterministic revision over sanitized canonical content.
    fn compute_revision(
        base_url: &str,
        models: &[AgentModelProjection],
        capabilities: &IntegrationCapabilities,
    ) -> String {
        let canonical = serde_json::json!({
            "schema_version": INTEGRATION_PROFILE_SCHEMA_VERSION,
            "base_url": base_url,
            "models": models,
            "capabilities": capabilities,
        });
        let rendered = serde_json::to_string(&canonical).unwrap_or_else(|_| "{}".to_owned());
        sha256_hex(rendered.as_bytes())
    }

    /// Strict validation: schema, bounds, ordering, secret-free facts.
    pub fn validate(&self) -> Result<(), ClientConfigError> {
        if self.schema_version != INTEGRATION_PROFILE_SCHEMA_VERSION {
            return Err(ClientConfigError::UnsupportedSchema {
                detail: format!(
                    "unsupported integration profile schema {}; expected {INTEGRATION_PROFILE_SCHEMA_VERSION}",
                    self.schema_version
                ),
            });
        }
        validate_base_url(&self.base_url)?;
        if self.models.len() > MAX_INTEGRATION_MODELS {
            return Err(ClientConfigError::CatalogTooLarge {
                count: self.models.len(),
            });
        }
        let mut last: Option<&str> = None;
        for model in &self.models {
            if model.public_id.is_empty() || model.public_id.len() > 256 {
                return Err(ClientConfigError::InvalidField {
                    field: "models.public_id".to_owned(),
                    detail: "public ID has invalid length".to_owned(),
                });
            }
            if model.public_id.chars().any(|c| c.is_control()) {
                return Err(ClientConfigError::InvalidField {
                    field: "models.public_id".to_owned(),
                    detail: "public ID contains control characters".to_owned(),
                });
            }
            if let Some(previous) = last {
                if model.public_id.as_str() < previous {
                    return Err(ClientConfigError::InvalidField {
                        field: "models".to_owned(),
                        detail: "models are not in deterministic order".to_owned(),
                    });
                }
            }
            last = Some(model.public_id.as_str());
            if model.capabilities.websockets {
                return Err(ClientConfigError::InvalidField {
                    field: "models.capabilities.websockets".to_owned(),
                    detail: "portable models must not advertise websockets".to_owned(),
                });
            }
        }
        let expected = Self::compute_revision(&self.base_url, &self.models, &self.capabilities);
        if self.revision != expected {
            return Err(ClientConfigError::Drift {
                detail: "integration profile revision does not match canonical content".to_owned(),
            });
        }
        let encoded = serde_json::to_string(self)?;
        if encoded.len() > MAX_INTEGRATION_PROFILE_BYTES {
            return Err(ClientConfigError::DocumentTooLarge);
        }
        // Secret-free by construction: no key, credential, path, or source
        // metadata fields exist on this type. Enforce encoded hygiene.
        if encoded.contains("EGGPOOL_API_KEY_VALUE") || encoded.contains("source_metadata") {
            return Err(ClientConfigError::Drift {
                detail: "integration profile must not carry credentials or source metadata"
                    .to_owned(),
            });
        }
        Ok(())
    }

    /// Canonical JSON for transport (deterministic key order via struct).
    pub fn canonical_json(&self) -> Result<String, ClientConfigError> {
        self.validate()?;
        Ok(serde_json::to_string(self)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::projection::{AgentModelCapabilities, AgentModelProjection};

    fn models() -> Vec<AgentModelProjection> {
        vec![
            AgentModelProjection {
                public_id: "b-model".to_owned(),
                display_name: "B".to_owned(),
                capabilities: AgentModelCapabilities::conservative(),
            },
            AgentModelProjection {
                public_id: "a-model".to_owned(),
                display_name: "A".to_owned(),
                capabilities: AgentModelCapabilities::conservative(),
            },
        ]
    }

    #[test]
    fn integration_profile_is_deterministic_with_stable_revision() {
        let first = AgentIntegrationProfileV1::new(
            "https://pool.example/v1",
            models(),
            IntegrationCapabilities::default(),
        )
        .expect("profile");
        let second = AgentIntegrationProfileV1::new(
            "https://pool.example/v1",
            models(),
            IntegrationCapabilities::default(),
        )
        .expect("profile");
        assert_eq!(first.revision, second.revision);
        assert_eq!(first.models[0].public_id, "a-model");
        assert_eq!(
            first.canonical_json().expect("json"),
            second.canonical_json().expect("json")
        );
        first.validate().expect("valid");
    }

    #[test]
    fn integration_profile_rejects_websockets_and_bounds() {
        let caps = IntegrationCapabilities {
            websockets: true,
            ..Default::default()
        };
        assert!(AgentIntegrationProfileV1::new("https://pool.example/v1", models(), caps).is_err());

        let many: Vec<AgentModelProjection> = (0..MAX_INTEGRATION_MODELS + 1)
            .map(|i| AgentModelProjection {
                public_id: format!("model-{i:05}"),
                display_name: format!("Model {i}"),
                capabilities: AgentModelCapabilities::conservative(),
            })
            .collect();
        assert!(AgentIntegrationProfileV1::new(
            "https://pool.example/v1",
            many,
            IntegrationCapabilities::default()
        )
        .is_err());
    }

    #[test]
    fn integration_profile_is_secret_free() {
        let profile = AgentIntegrationProfileV1::new(
            "https://pool.example/v1",
            models(),
            IntegrationCapabilities::default(),
        )
        .expect("profile");
        let json = profile.canonical_json().expect("json");
        assert!(!json.contains("source_metadata"));
        assert!(!json.contains("EGGPOOL_API_KEY_VALUE"));
        let debug = format!("{profile:?}");
        assert!(!debug.contains("source_metadata"));
    }
}
