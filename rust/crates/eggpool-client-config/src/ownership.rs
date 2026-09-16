//! Portable ownership-state types.
//!
//! Ownership manifests record which client fields EggPool manages, the
//! pre/post hashes for drift detection, and the generated catalog identity.
//! Filesystem IO stays in the application; this module owns the schema,
//! owned-field definitions, and pure validation.

use std::collections::BTreeSet;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Current ownership-manifest schema version.
pub const OWNERSHIP_SCHEMA_VERSION: u32 = 1;

/// EggPool-owned Codex fields.
pub const OWNED_CODEX_FIELDS: &[&str] = &[
    "model_provider",
    "model_catalog_json",
    "model_providers.eggpool",
];

/// EggPool-owned OpenCode fields.
pub const OWNED_OPENCODE_FIELDS: &[&str] = &["provider.eggpool"];

/// Portable ownership manifest (local lifecycle state, not a shared profile).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OwnershipManifest {
    pub schema_version: u32,
    pub eggpool_version: String,
    pub target: String,
    pub client_config_path: PathBuf,
    pub pre_edit_hash: String,
    pub post_edit_hash: String,
    pub owned_fields: Vec<String>,
    pub previous_values: Map<String, Value>,
    pub generated_catalog_path: Option<PathBuf>,
    pub generated_catalog_hash: Option<String>,
    pub base_url: String,
}

impl OwnershipManifest {
    /// Validate the manifest shape without touching the filesystem.
    pub fn validate(&self) -> Result<(), crate::error::ClientConfigError> {
        if self.schema_version != OWNERSHIP_SCHEMA_VERSION {
            return Err(crate::error::ClientConfigError::UnsupportedSchema {
                detail: format!(
                    "unsupported ownership schema {}; expected {OWNERSHIP_SCHEMA_VERSION}",
                    self.schema_version
                ),
            });
        }
        if self.target != "codex" && self.target != "opencode" {
            return Err(crate::error::ClientConfigError::InvalidField {
                field: "target".to_owned(),
                detail: "ownership target must be codex or opencode".to_owned(),
            });
        }
        if self.owned_fields.is_empty() {
            return Err(crate::error::ClientConfigError::InvalidField {
                field: "owned_fields".to_owned(),
                detail: "ownership must declare at least one field".to_owned(),
            });
        }
        let expected: BTreeSet<&str> = if self.target == "codex" {
            OWNED_CODEX_FIELDS.iter().copied().collect()
        } else {
            OWNED_OPENCODE_FIELDS.iter().copied().collect()
        };
        for field in &self.owned_fields {
            // `model` is an explicitly requested optional Codex field.
            if self.target == "codex" && field == "model" {
                continue;
            }
            if !expected.contains(field.as_str()) {
                return Err(crate::error::ClientConfigError::InvalidField {
                    field: "owned_fields".to_owned(),
                    detail: format!("unexpected owned field {field:?} for {}", self.target),
                });
            }
        }
        for hash in [&self.pre_edit_hash, &self.post_edit_hash] {
            if hash.len() != 64 || !hash.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err(crate::error::ClientConfigError::InvalidField {
                    field: "hash".to_owned(),
                    detail: "ownership hash must be lowercase hex SHA-256".to_owned(),
                });
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest() -> OwnershipManifest {
        OwnershipManifest {
            schema_version: OWNERSHIP_SCHEMA_VERSION,
            eggpool_version: "0.8.0".to_owned(),
            target: "codex".to_owned(),
            client_config_path: PathBuf::from("/tmp/config.toml"),
            pre_edit_hash: "a".repeat(64),
            post_edit_hash: "b".repeat(64),
            owned_fields: vec![
                "model_provider".to_owned(),
                "model_catalog_json".to_owned(),
                "model_providers.eggpool".to_owned(),
            ],
            previous_values: Map::new(),
            generated_catalog_path: None,
            generated_catalog_hash: None,
            base_url: "http://127.0.0.1:11300/v1".to_owned(),
        }
    }

    #[test]
    fn ownership_validates_closed_targets_and_hashes() {
        manifest().validate().expect("valid");
        let mut bad = manifest();
        bad.target = "vscode".to_owned();
        assert!(bad.validate().is_err());
        let mut bad = manifest();
        bad.post_edit_hash = "short".to_owned();
        assert!(bad.validate().is_err());
    }
}
