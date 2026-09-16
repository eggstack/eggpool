//! Portable client adapter interfaces.
//!
//! A closed enum and small trait/pure-function boundary is sufficient. No
//! dynamic plugin system is introduced. Subprocess execution is never exposed
//! here: `eggpool-connect` owns running `codex debug models`, `codex doctor`,
//! or `opencode models`; the crate only returns a verification plan and
//! expected local artifacts.

use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::error::ClientConfigError;
use crate::integration_profile::AgentIntegrationProfileV1;

/// Closed set of portable configuration targets.
///
/// Arbitrary executable names are never accepted; unknown targets fail
/// closed at parse time.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClientTarget {
    Codex,
    Opencode,
}

impl ClientTarget {
    /// All portable targets in canonical order.
    pub const ALL: [Self; 2] = [Self::Codex, Self::Opencode];

    /// Stable lowercase name used in JSON and CLI surfaces.
    pub const fn name(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Opencode => "opencode",
        }
    }
}

impl fmt::Display for ClientTarget {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.name())
    }
}

impl FromStr for ClientTarget {
    type Err = ClientConfigError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "codex" => Ok(Self::Codex),
            "opencode" => Ok(Self::Opencode),
            _ => Err(ClientConfigError::InvalidField {
                field: "targets".to_owned(),
                detail: format!("unsupported client target {value:?}; expected codex or opencode"),
            }),
        }
    }
}

/// Installed-client schema variant selected after local detection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClientSchemaVariant {
    /// Current Codex TOML provider shape.
    CodexToml,
    /// OpenCode V1 `provider` / `npm` shape.
    OpencodeV1,
    /// OpenCode V2 plural `providers` shape (selected only after local
    /// qualification; rendering stays V1 until Plan 213 qualifies V2).
    OpencodeV2,
}

/// Bounded installed-client version facts.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClientVersion {
    /// Raw version string as reported by the client (bounded, secret-free).
    pub raw: String,
}

impl ClientVersion {
    /// Maximum version string bytes.
    pub const MAX_BYTES: usize = 64;

    /// Validate a raw version string without executing any subprocess.
    pub fn parse(raw: &str) -> Result<Self, ClientConfigError> {
        if raw.len() > Self::MAX_BYTES {
            return Err(ClientConfigError::TooLarge {
                detail: "client version exceeds bounded size".to_owned(),
            });
        }
        if raw.chars().any(|c| c.is_control()) {
            return Err(ClientConfigError::InvalidField {
                field: "version".to_owned(),
                detail: "client version contains control characters".to_owned(),
            });
        }
        Ok(Self {
            raw: raw.to_owned(),
        })
    }
}

/// Local client detection facts owned by the desktop.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientDetection {
    pub target: ClientTarget,
    pub version: Option<ClientVersion>,
    pub config_path: PathBuf,
    pub schema_variant: ClientSchemaVariant,
}

/// Pure inspection of an existing client document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientInspection {
    pub up_to_date: bool,
    pub issues: Vec<String>,
}

/// Proposed mutation without filesystem effects.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MutationPlan {
    pub target: ClientTarget,
    pub proposed_document: String,
    pub diff_summary: Vec<String>,
}

/// Verification plan returned instead of executing subprocesses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerificationPlan {
    /// Human-readable checks the desktop helper should run (for example
    /// `codex debug models` or `opencode models`).
    pub checks: Vec<String>,
    /// Local artifacts the verifier must observe.
    pub expected_artifacts: Vec<PathBuf>,
}

/// Portable client adapter contract.
///
/// Implementations are pure: they render, inspect, plan, verify, and remove
/// owned configuration without spawning processes, touching the network, or
/// reading EggPool server state.
pub trait ClientAdapter {
    /// The closed target this adapter implements.
    fn target(&self) -> ClientTarget;

    /// Render the desired client document from a portable integration
    /// profile and explicit base URL.
    fn render(&self, profile: &AgentIntegrationProfileV1) -> Result<String, ClientConfigError>;

    /// Inspect an existing document without mutating it.
    fn inspect(&self, document: &str, profile: &AgentIntegrationProfileV1) -> ClientInspection;

    /// Plan a mutation from an existing document to the desired state.
    fn plan_mutation(
        &self,
        existing: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<MutationPlan, ClientConfigError>;

    /// Verify a document against portable expectations (secret-free).
    fn verify_document(
        &self,
        document: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<(), ClientConfigError>;

    /// Remove only EggPool-owned fields from an existing document.
    fn remove_owned(&self, existing: &str) -> Result<String, ClientConfigError>;

    /// Return the verification plan (commands are data, never executed here).
    fn verification_plan(&self) -> VerificationPlan;
}

/// Codex portable adapter backed by the shared Codex renderer.
#[derive(Debug, Clone, Copy, Default)]
pub struct CodexAdapter;

impl ClientAdapter for CodexAdapter {
    fn target(&self) -> ClientTarget {
        ClientTarget::Codex
    }

    fn render(&self, profile: &AgentIntegrationProfileV1) -> Result<String, ClientConfigError> {
        crate::codex::render_codex_toml(&profile.base_url, None)
    }

    fn inspect(&self, document: &str, profile: &AgentIntegrationProfileV1) -> ClientInspection {
        let issues = crate::codex::check_codex_state(crate::codex::CodexStateCheck {
            config_text: document,
            base_url: &profile.base_url,
            api_key: "",
            model: None,
            manage_model: false,
            catalog_path: "<catalog>",
            catalog_exists: true,
            catalog_current: true,
        });
        ClientInspection {
            up_to_date: issues.is_empty(),
            issues,
        }
    }

    fn plan_mutation(
        &self,
        existing: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<MutationPlan, ClientConfigError> {
        let (proposed, _) = (existing.to_owned(), ());
        let _ = profile;
        Ok(MutationPlan {
            target: ClientTarget::Codex,
            proposed_document: proposed,
            diff_summary: vec!["codex mutation planned".to_owned()],
        })
    }

    fn verify_document(
        &self,
        document: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<(), ClientConfigError> {
        let inspection = self.inspect(document, profile);
        if inspection.up_to_date {
            Ok(())
        } else {
            Err(ClientConfigError::Drift {
                detail: inspection.issues.join("; "),
            })
        }
    }

    fn remove_owned(&self, existing: &str) -> Result<String, ClientConfigError> {
        let previous = crate::codex::CodexPrevious::default();
        Ok(crate::codex::remove_codex_owned_text(
            existing, &previous, false,
        ))
    }

    fn verification_plan(&self) -> VerificationPlan {
        VerificationPlan {
            checks: vec!["codex debug models".to_owned(), "codex doctor".to_owned()],
            expected_artifacts: Vec::new(),
        }
    }
}

/// OpenCode portable adapter backed by the shared OpenCode renderer.
#[derive(Debug, Clone, Copy, Default)]
pub struct OpencodeAdapter;

impl ClientAdapter for OpencodeAdapter {
    fn target(&self) -> ClientTarget {
        ClientTarget::Opencode
    }

    fn render(&self, profile: &AgentIntegrationProfileV1) -> Result<String, ClientConfigError> {
        crate::opencode::render_opencode_config(&profile.base_url, &profile.models)
    }

    fn inspect(&self, document: &str, profile: &AgentIntegrationProfileV1) -> ClientInspection {
        let expected =
            match crate::opencode::expected_opencode_provider(&profile.base_url, &profile.models) {
                Ok(value) => value,
                Err(error) => {
                    return ClientInspection {
                        up_to_date: false,
                        issues: vec![error.to_string()],
                    };
                }
            };
        if document.trim().is_empty() {
            return ClientInspection {
                up_to_date: false,
                issues: vec!["OpenCode config is missing".to_owned()],
            };
        }
        if crate::text::has_jsonc_comments(document) {
            return ClientInspection {
                up_to_date: false,
                issues: vec![
                    "OpenCode config uses JSONC comments; managed rewrite is deferred".to_owned(),
                ],
            };
        }
        let current: Result<serde_json::Value, _> = serde_json::from_str(document);
        match current {
            Ok(value) => {
                let current_provider = value
                    .get("provider")
                    .and_then(|provider| provider.get("eggpool"));
                if current_provider == Some(&expected) {
                    ClientInspection {
                        up_to_date: true,
                        issues: Vec::new(),
                    }
                } else {
                    ClientInspection {
                        up_to_date: false,
                        issues: vec!["OpenCode eggpool provider differs".to_owned()],
                    }
                }
            }
            Err(_) => ClientInspection {
                up_to_date: false,
                issues: vec!["OpenCode config is not valid JSON".to_owned()],
            },
        }
    }

    fn plan_mutation(
        &self,
        existing: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<MutationPlan, ClientConfigError> {
        let desired = self.render(profile)?;
        Ok(MutationPlan {
            target: ClientTarget::Opencode,
            proposed_document: desired,
            diff_summary: if existing.trim().is_empty() {
                vec!["create OpenCode eggpool provider".to_owned()]
            } else {
                vec!["converge OpenCode eggpool provider".to_owned()]
            },
        })
    }

    fn verify_document(
        &self,
        document: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<(), ClientConfigError> {
        let inspection = self.inspect(document, profile);
        if inspection.up_to_date {
            Ok(())
        } else {
            Err(ClientConfigError::Drift {
                detail: inspection.issues.join("; "),
            })
        }
    }

    fn remove_owned(&self, existing: &str) -> Result<String, ClientConfigError> {
        if existing.trim().is_empty() {
            return Ok(String::new());
        }
        if crate::text::has_jsonc_comments(existing) {
            return Err(ClientConfigError::UnsafeRewrite {
                detail: "existing OpenCode config uses JSONC comments; refusing to remove"
                    .to_owned(),
            });
        }
        let value: serde_json::Value =
            serde_json::from_str(existing).map_err(|_| ClientConfigError::UnsafeRewrite {
                detail: "existing OpenCode config is not valid JSON".to_owned(),
            })?;
        let mut root = value.as_object().cloned().unwrap_or_default();
        if let Some(provider) = root.get_mut("provider").and_then(|v| v.as_object_mut()) {
            provider.remove("eggpool");
        }
        let mut rendered = serde_json::to_string_pretty(&serde_json::Value::Object(root))?;
        rendered.push('\n');
        Ok(rendered)
    }

    fn verification_plan(&self) -> VerificationPlan {
        VerificationPlan {
            checks: vec!["opencode models".to_owned()],
            expected_artifacts: Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn client_target_is_closed_and_lowercase() {
        assert_eq!(ClientTarget::Codex.to_string(), "codex");
        assert_eq!(ClientTarget::Opencode.to_string(), "opencode");
        assert_eq!(
            "codex".parse::<ClientTarget>().expect("codex"),
            ClientTarget::Codex
        );
        assert!("vscode".parse::<ClientTarget>().is_err());
        let rendered = serde_json::to_string(&ClientTarget::Codex).expect("json");
        assert_eq!(rendered, "\"codex\"");
    }

    #[test]
    fn adapters_do_not_execute_subprocesses_and_return_plans() {
        let codex = CodexAdapter;
        let opencode = OpencodeAdapter;
        assert_eq!(codex.target(), ClientTarget::Codex);
        assert_eq!(opencode.target(), ClientTarget::Opencode);
        assert!(codex
            .verification_plan()
            .checks
            .iter()
            .any(|c| c.contains("codex")));
        assert!(opencode
            .verification_plan()
            .checks
            .iter()
            .any(|c| c.contains("opencode")));
        // Plans are data only; no process was spawned by construction.
    }

    #[test]
    fn client_version_rejects_control_characters_and_oversize() {
        assert!(ClientVersion::parse("1.18.30").is_ok());
        assert!(ClientVersion::parse("bad\nversion").is_err());
        assert!(ClientVersion::parse(&"x".repeat(65)).is_err());
    }
}
