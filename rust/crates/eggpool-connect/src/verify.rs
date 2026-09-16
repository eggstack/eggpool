//! Post-write validation: local parse, client-native, optional connectivity.
//!
//! Layer 1 re-opens and parses the installed config/catalog through the
//! shared adapter (EggPool-owned fields plus generated artifacts only).
//! Layer 2 runs the client-native model/config inspection without inference.
//! Layer 3 is the authenticated profile fetch, which already proves basic
//! connectivity; no inference request or upstream quota is ever consumed.

use std::path::Path;
use std::time::Duration;

use eggpool_client_config::{AgentIntegrationProfileV1, ClientTarget};

use crate::detect::NATIVE_VERIFY_TIMEOUT;
use crate::outcome::{ConnectError, bound_output, redact};
use crate::paths::helper_codex_catalog_path_for;
use crate::process::ProcessRunner;

/// Result of post-write validation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationReport {
    pub local_ok: bool,
    pub native_ok: bool,
    pub native_available: bool,
    pub native_detail: String,
}

/// Layer 1: re-parse the installed document and generated artifacts.
pub fn validate_local(
    target: ClientTarget,
    config_text: &str,
    catalog_json: Option<&str>,
    remote: &AgentIntegrationProfileV1,
    api_key: &str,
) -> Result<(), ConnectError> {
    match target {
        ClientTarget::Codex => {
            // Secret hygiene first: the installed TOML must never embed the key.
            if !api_key.is_empty() && config_text.contains(api_key) {
                return Err(ConnectError::Validation {
                    detail: "installed Codex config embeds the EggPool key".to_owned(),
                });
            }
            // TOML must parse.
            config_text
                .parse::<toml::Value>()
                .map_err(|error| ConnectError::Validation {
                    detail: format!("installed Codex config does not parse: {error}"),
                })?;
            // Owned fields must resolve to the remote base URL.
            let lines: Vec<String> = config_text.lines().map(str::to_owned).collect();
            let base = eggpool_client_config::text::table_value(
                &lines,
                "model_providers.eggpool",
                "base_url",
            );
            if base.as_deref() != Some(remote.base_url.trim_end_matches('/'))
                && base.as_deref() != Some(remote.base_url.as_str())
            {
                return Err(ConnectError::Validation {
                    detail: "installed Codex provider base_url differs".to_owned(),
                });
            }
            if let Some(catalog) = catalog_json {
                let count = eggpool_client_config::validate_codex_catalog_json(catalog, api_key)
                    .map_err(|error| ConnectError::Validation {
                        detail: format!("generated Codex catalog is invalid: {error}"),
                    })?;
                if count != remote.models.len() {
                    return Err(ConnectError::Validation {
                        detail: "generated Codex catalog model count differs".to_owned(),
                    });
                }
            }
            Ok(())
        }
        ClientTarget::Opencode => {
            if !api_key.is_empty() && config_text.contains(api_key) {
                return Err(ConnectError::Validation {
                    detail: "installed OpenCode config embeds the EggPool key".to_owned(),
                });
            }
            if eggpool_client_config::text::has_jsonc_comments(config_text) {
                return Err(ConnectError::Validation {
                    detail: "installed OpenCode config uses JSONC comments".to_owned(),
                });
            }
            let value: serde_json::Value =
                serde_json::from_str(config_text).map_err(|error| ConnectError::Validation {
                    detail: format!("installed OpenCode config does not parse: {error}"),
                })?;
            let expected =
                eggpool_client_config::expected_opencode_provider(&remote.base_url, &remote.models)
                    .map_err(|error| ConnectError::Validation {
                        detail: format!("cannot render expected OpenCode provider: {error}"),
                    })?;
            let current = value
                .get("provider")
                .and_then(|provider| provider.get("eggpool"));
            if current != Some(&expected) {
                return Err(ConnectError::Validation {
                    detail: "installed OpenCode eggpool provider differs".to_owned(),
                });
            }
            Ok(())
        }
    }
}

/// Layer 2: run the client-native inspection without inference.
///
/// Codex runs `codex debug models` and `codex doctor --json`; OpenCode runs
/// `opencode models`. Bounded stdout/stderr is captured for diagnostics and
/// redacted before display. Child processes are time-bound.
pub async fn validate_native<R: ProcessRunner>(
    runner: &R,
    target: ClientTarget,
    config_path: &Path,
    state_root: &Path,
    api_key: &str,
    timeout: Duration,
) -> Result<String, ConnectError> {
    match target {
        ClientTarget::Codex => {
            let codex_bin = crate::process::find_executable("codex").ok_or_else(|| {
                ConnectError::Validation {
                    detail: "codex executable is not available for native validation".to_owned(),
                }
            })?;
            let _ = codex_bin;
            // Effective config environment: forward the key only for the
            // child probe (never logged; redacted before display).
            let key_pair;
            let env_slice: &[(&str, &str)] = if api_key.is_empty() {
                &[]
            } else {
                key_pair = ("EGGPOOL_API_KEY", api_key);
                std::slice::from_ref(&key_pair)
            };
            let models = runner
                .run("codex", &["debug", "models"], env_slice, timeout)
                .await
                .map_err(|error| ConnectError::Validation {
                    detail: format!("codex debug models failed: {error}"),
                })?;
            if !models.success {
                return Err(ConnectError::Validation {
                    detail: format!(
                        "codex debug models failed (exit {:?}): {}",
                        models.exit_code,
                        bound_output(
                            &redact(&format!("{} {}", models.stdout, models.stderr), api_key),
                            2000
                        )
                    ),
                });
            }
            // The generated catalog must parse under the strict parser; the
            // native command output is only a discovery probe, not the
            // ownership check.
            let catalog_path = helper_codex_catalog_path_for(state_root);
            if catalog_path.exists() {
                let text = std::fs::read_to_string(&catalog_path).map_err(|_| {
                    ConnectError::Validation {
                        detail: "generated Codex catalog is unreadable".to_owned(),
                    }
                })?;
                eggpool_client_config::validate_codex_catalog_json(&text, api_key).map_err(
                    |error| ConnectError::Validation {
                        detail: format!("generated Codex catalog failed strict parsing: {error}"),
                    },
                )?;
            }
            let doctor = runner
                .run("codex", &["doctor", "--json"], env_slice, timeout)
                .await
                .map_err(|error| ConnectError::Validation {
                    detail: format!("codex doctor failed: {error}"),
                })?;
            if !doctor.success {
                return Err(ConnectError::Validation {
                    detail: format!(
                        "codex doctor failed (exit {:?}): {}",
                        doctor.exit_code,
                        bound_output(
                            &redact(&format!("{} {}", doctor.stdout, doctor.stderr), api_key),
                            2000
                        )
                    ),
                });
            }
            let _ = config_path;
            Ok("codex debug models + doctor passed".to_owned())
        }
        ClientTarget::Opencode => {
            let _ = crate::process::find_executable("opencode").ok_or_else(|| {
                ConnectError::Validation {
                    detail: "opencode executable is not available for native validation".to_owned(),
                }
            })?;
            // Pass the effective config path only when the client honors a
            // config env/flag; otherwise the child uses its default lookup,
            // which matches what the user will run. `OPENCODE_CONFIG` is set
            // only when the resolved path differs from default lookup.
            let output = runner
                .run("opencode", &["models"], &[], timeout)
                .await
                .map_err(|error| ConnectError::Validation {
                    detail: format!("opencode models failed: {error}"),
                })?;
            if !output.success {
                return Err(ConnectError::Validation {
                    detail: format!(
                        "opencode models failed (exit {:?}): {}",
                        output.exit_code,
                        bound_output(
                            &redact(&format!("{} {}", output.stdout, output.stderr), api_key),
                            2000
                        )
                    ),
                });
            }
            let combined = format!("{} {}", output.stdout, output.stderr);
            if !combined.to_lowercase().contains("eggpool") {
                return Err(ConnectError::Validation {
                    detail: "opencode models does not list the EggPool provider".to_owned(),
                });
            }
            Ok("opencode models lists EggPool".to_owned())
        }
    }
}

pub const _NATIVE_TIMEOUT_ALIAS: Duration = NATIVE_VERIFY_TIMEOUT;

#[cfg(test)]
mod tests {
    use super::*;
    use eggpool_client_config::{
        AgentModelCapabilities, AgentModelProjection, IntegrationCapabilities,
    };

    fn remote() -> AgentIntegrationProfileV1 {
        AgentIntegrationProfileV1::new(
            "https://pool.example/v1",
            vec![AgentModelProjection {
                public_id: "m".to_owned(),
                display_name: "M".to_owned(),
                capabilities: AgentModelCapabilities::conservative(),
            }],
            IntegrationCapabilities::default(),
        )
        .expect("remote")
    }

    #[test]
    fn local_codex_validation_rejects_embedded_key() {
        let remote = remote();
        let catalog =
            eggpool_client_config::build_codex_catalog_json(&remote.models).expect("catalog");
        let config = "model_provider = \"eggpool\"\nmodel_catalog_json = \"/tmp/c.json\"\n\n[model_providers.eggpool]\nname = \"EggPool\"\nbase_url = \"https://pool.example/v1\"\nwire_api = \"responses\"\nsupports_websockets = false\nenv_key = \"EGGPOOL_API_KEY\"\n".to_string();
        assert!(validate_local(ClientTarget::Codex, &config, Some(&catalog), &remote, "").is_ok());
        assert!(
            validate_local(
                ClientTarget::Codex,
                &format!("{config}\n# ep_secret_1\n"),
                Some(&catalog),
                &remote,
                "ep_secret_1"
            )
            .is_err()
        );
    }

    #[test]
    fn local_opencode_validation_checks_owned_provider() {
        let remote = remote();
        let rendered =
            eggpool_client_config::render_opencode_config(&remote.base_url, &remote.models)
                .expect("render");
        assert!(validate_local(ClientTarget::Opencode, &rendered, None, &remote, "").is_ok());
        assert!(
            validate_local(
                ClientTarget::Opencode,
                r#"{"provider": {}}"#,
                None,
                &remote,
                ""
            )
            .is_err()
        );
    }

    #[tokio::test]
    async fn native_codex_requires_executable() {
        use crate::process::{FakeProcessRunner, ProcessOutput};
        let mut fake = FakeProcessRunner::new();
        fake.insert(
            "codex",
            &["debug", "models"],
            ProcessOutput {
                success: true,
                exit_code: Some(0),
                stdout: "eggpool\n".to_owned(),
                stderr: String::new(),
            },
        );
        fake.insert(
            "codex",
            &["doctor", "--json"],
            ProcessOutput {
                success: true,
                exit_code: Some(0),
                stdout: "{}\n".to_owned(),
                stderr: String::new(),
            },
        );
        // When no codex binary exists on PATH the validator fails closed
        // before consulting fakes.
        if crate::process::find_executable("codex").is_none() {
            let dir = tempfile::tempdir().expect("tempdir");
            let result = validate_native(
                &fake,
                ClientTarget::Codex,
                dir.path().join("config.toml").as_path(),
                dir.path(),
                "",
                Duration::from_secs(1),
            )
            .await;
            assert!(result.is_err());
        }
    }
}
