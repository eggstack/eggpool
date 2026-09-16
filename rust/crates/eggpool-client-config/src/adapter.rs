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
    /// OpenCode V1 `provider` / `npm` / `options` shape.
    OpencodeV1,
    /// OpenCode V2 plural `providers` / `package` / `settings` shape with an
    /// `env` credential list (qualified by Plan 213 against current V2 docs).
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
    pub schema_variant: ClientSchemaVariant,
    pub proposed_document: String,
    pub diff_summary: Vec<String>,
    /// Exact previous raw text of the owned entry, if one existed (Codex:
    /// previous provider table/root values are summarized in `diff_summary`
    /// while full values travel with the ownership manifest; OpenCode: raw
    /// previous provider JSON for byte-exact restoration).
    pub previous_raw: Option<String>,
}

/// Options for [`ClientAdapter::plan_mutation`].
///
/// `catalog_path` is required for Codex (the generated catalog artifact the
/// config must point at); `client_version` seeds OpenCode variant selection
/// for empty configs (shape always wins when present).
#[derive(Debug, Clone, Default)]
pub struct PlanOptions<'a> {
    pub catalog_path: Option<&'a str>,
    pub model: Option<&'a str>,
    pub manage_model: bool,
    pub client_version: Option<&'a str>,
}

/// Previous owned values for [`ClientAdapter::remove_owned`].
#[derive(Debug, Clone)]
pub enum RemovalPrevious {
    Codex(crate::codex::CodexPrevious),
    Opencode {
        variant: ClientSchemaVariant,
        raw: Option<String>,
    },
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
        options: &PlanOptions<'_>,
    ) -> Result<MutationPlan, ClientConfigError>;

    /// Verify a document against portable expectations (secret-free).
    fn verify_document(
        &self,
        document: &str,
        profile: &AgentIntegrationProfileV1,
    ) -> Result<(), ClientConfigError>;

    /// Remove only EggPool-owned fields from an existing document,
    /// restoring `previous` captures where present.
    fn remove_owned(
        &self,
        existing: &str,
        previous: &RemovalPrevious,
    ) -> Result<String, ClientConfigError>;

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
        options: &PlanOptions<'_>,
    ) -> Result<MutationPlan, ClientConfigError> {
        let catalog_path = options
            .catalog_path
            .ok_or_else(|| ClientConfigError::InvalidField {
                field: "catalog_path".to_owned(),
                detail: "Codex mutation requires the generated catalog path".to_owned(),
            })?;
        let (proposed, previous) = crate::codex::apply_codex_text_mutation(
            existing,
            &profile.base_url,
            catalog_path,
            options.model,
            options.manage_model,
        );
        let mut diff_summary = vec![
            format!("update Codex provider (base_url {})", profile.base_url),
            format!("point model_catalog_json at {catalog_path}"),
        ];
        if previous
            .model_provider
            .as_deref()
            .is_some_and(|value| !value.is_empty() && value != "eggpool")
        {
            diff_summary
                .push("capture previous model_provider for ownership-aware remove".to_owned());
        }
        if previous.provider_table.is_some() {
            diff_summary.push(
                "capture previous [model_providers.eggpool] table for ownership-aware remove"
                    .to_owned(),
            );
        }
        Ok(MutationPlan {
            target: ClientTarget::Codex,
            schema_variant: ClientSchemaVariant::CodexToml,
            proposed_document: proposed,
            diff_summary,
            previous_raw: None,
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

    fn remove_owned(
        &self,
        existing: &str,
        previous: &RemovalPrevious,
    ) -> Result<String, ClientConfigError> {
        match previous {
            RemovalPrevious::Codex(previous) => Ok(crate::codex::remove_codex_owned_text(
                existing, previous, false,
            )),
            RemovalPrevious::Opencode { .. } => Err(ClientConfigError::InvalidField {
                field: "previous".to_owned(),
                detail: "Codex adapter cannot consume OpenCode removal state".to_owned(),
            }),
        }
    }

    fn verification_plan(&self) -> VerificationPlan {
        VerificationPlan {
            checks: vec![
                "codex debug models".to_owned(),
                "codex doctor --json".to_owned(),
            ],
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
        if document.trim().is_empty() {
            return ClientInspection {
                up_to_date: false,
                issues: vec!["OpenCode config is missing".to_owned()],
            };
        }
        let variant = match crate::opencode::select_opencode_variant(document, None) {
            Ok(variant) => variant,
            Err(error) => {
                return ClientInspection {
                    up_to_date: false,
                    issues: vec![error.to_string()],
                };
            }
        };
        let expected = match crate::opencode::expected_opencode_provider_for(
            variant,
            &profile.base_url,
            &profile.models,
        ) {
            Ok(value) => value,
            Err(error) => {
                return ClientInspection {
                    up_to_date: false,
                    issues: vec![error.to_string()],
                };
            }
        };
        match crate::jsonc::parse_value(document) {
            Ok(value) => {
                let current = crate::opencode::current_owned_entry(&value, variant);
                if current == Some(&expected) {
                    ClientInspection {
                        up_to_date: true,
                        issues: Vec::new(),
                    }
                } else {
                    ClientInspection {
                        up_to_date: false,
                        issues: vec![format!(
                            "OpenCode {} provider differs",
                            crate::opencode::owned_path_for(variant)
                        )],
                    }
                }
            }
            Err(error) => ClientInspection {
                up_to_date: false,
                issues: vec![error.detail()],
            },
        }
    }

    fn plan_mutation(
        &self,
        existing: &str,
        profile: &AgentIntegrationProfileV1,
        options: &PlanOptions<'_>,
    ) -> Result<MutationPlan, ClientConfigError> {
        let variant = crate::opencode::select_opencode_variant(existing, options.client_version)?;
        let expected = crate::opencode::expected_opencode_provider_for(
            variant,
            &profile.base_url,
            &profile.models,
        )?;
        let mutation = crate::opencode::apply_opencode_document(existing, variant, &expected)?;
        let summary = if existing.trim().is_empty() {
            format!(
                "create OpenCode {} provider",
                crate::opencode::owned_path_for(variant)
            )
        } else if mutation.previous_raw.is_some() {
            format!(
                "converge OpenCode {} provider (previous captured)",
                crate::opencode::owned_path_for(variant)
            )
        } else {
            format!(
                "converge OpenCode {} provider",
                crate::opencode::owned_path_for(variant)
            )
        };
        Ok(MutationPlan {
            target: ClientTarget::Opencode,
            schema_variant: variant,
            proposed_document: mutation.text,
            diff_summary: vec![summary],
            previous_raw: mutation.previous_raw,
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

    fn remove_owned(
        &self,
        existing: &str,
        previous: &RemovalPrevious,
    ) -> Result<String, ClientConfigError> {
        match previous {
            RemovalPrevious::Opencode { variant, raw } => {
                crate::opencode::remove_opencode_document(existing, *variant, raw.as_deref())
            }
            RemovalPrevious::Codex(_) => Err(ClientConfigError::InvalidField {
                field: "previous".to_owned(),
                detail: "OpenCode adapter cannot consume Codex removal state".to_owned(),
            }),
        }
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
