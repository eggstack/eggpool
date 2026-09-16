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
            // Owned fields must resolve to the remote base URL with the
            // Responses contract: intended wire API, no WebSocket
            // advertisement, and the environment-key reference.
            let lines: Vec<String> = config_text.lines().map(str::to_owned).collect();
            let table = "model_providers.eggpool";
            let base = eggpool_client_config::text::table_value(&lines, table, "base_url");
            if base.as_deref() != Some(remote.base_url.trim_end_matches('/'))
                && base.as_deref() != Some(remote.base_url.as_str())
            {
                return Err(ConnectError::Validation {
                    detail: "installed Codex provider base_url differs".to_owned(),
                });
            }
            if eggpool_client_config::text::table_value(&lines, table, "wire_api").as_deref()
                != Some("responses")
            {
                return Err(ConnectError::Validation {
                    detail: "installed Codex provider wire_api is not responses".to_owned(),
                });
            }
            if eggpool_client_config::text::table_value(&lines, table, "supports_websockets")
                .as_deref()
                != Some("false")
            {
                return Err(ConnectError::Validation {
                    detail: "installed Codex provider must not advertise websockets".to_owned(),
                });
            }
            if eggpool_client_config::text::table_value(&lines, table, "env_key").as_deref()
                != Some("EGGPOOL_API_KEY")
            {
                return Err(ConnectError::Validation {
                    detail: "installed Codex provider env_key differs".to_owned(),
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
            // JSONC (comments, trailing commas) is valid OpenCode config and
            // is preserved by the editor; only truly invalid documents fail.
            let variant = eggpool_client_config::select_opencode_variant(config_text, None)
                .map_err(|error| ConnectError::Validation {
                    detail: format!("installed OpenCode config variant: {error}"),
                })?;
            let value = eggpool_client_config::jsonc::parse_value(config_text).map_err(|_| {
                ConnectError::Validation {
                    detail: "installed OpenCode config does not parse".to_owned(),
                }
            })?;
            let expected = eggpool_client_config::expected_opencode_provider_for(
                variant,
                &remote.base_url,
                &remote.models,
            )
            .map_err(|error| ConnectError::Validation {
                detail: format!("cannot render expected OpenCode provider: {error}"),
            })?;
            let current = eggpool_client_config::current_owned_entry(&value, variant);
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
/// `opencode models eggpool`. Bounded stdout/stderr is captured for
/// diagnostics and redacted before display. Child processes are time-bound.
///
/// `expected_models` carries remote public IDs for the OpenCode slug check.
/// `OPENCODE_CONFIG` is only a merge layer in current OpenCode, so the probe
/// sets it to the installed path explicitly and requires every expected
/// `eggpool/<public_id>` slug in the filtered listing; a global config that
/// merely names an `eggpool` provider cannot satisfy the slug check. An
/// empty list keeps the provider-presence check (used by `verify`, which has
/// no remote profile).
pub async fn validate_native<R: ProcessRunner>(
    runner: &R,
    target: ClientTarget,
    config_path: &Path,
    state_root: &Path,
    api_key: &str,
    timeout: Duration,
    expected_models: &[String],
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
            // child probe (never logged; redacted before display), and
            // forward ambient `CODEX_HOME` explicitly so the probe observes
            // the same home the helper detected rather than depending on
            // implicit inheritance.
            let key_pair;
            let home_value: Option<String> =
                std::env::var_os("CODEX_HOME").map(|value| value.to_string_lossy().into_owned());
            let home_pair;
            let mut env_vec: Vec<(&str, &str)> = Vec::new();
            if !api_key.is_empty() {
                key_pair = ("EGGPOOL_API_KEY", api_key);
                env_vec.push(key_pair);
            }
            if let Some(home) = home_value.as_deref() {
                home_pair = ("CODEX_HOME", home);
                env_vec.push(home_pair);
            }
            let env_slice: &[(&str, &str)] = &env_vec;
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
            // `OPENCODE_CONFIG` is a merge layer, not isolation: point it at
            // the installed file explicitly so the probe always observes the
            // installed provider entry rather than ambient global state.
            let config_value = config_path.to_string_lossy().into_owned();
            let config_pair;
            let env_slice: &[(&str, &str)] = {
                config_pair = ("OPENCODE_CONFIG", config_value.as_str());
                std::slice::from_ref(&config_pair)
            };
            let output = runner
                .run("opencode", &["models", "eggpool"], env_slice, timeout)
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
            // Precise slug check: every expected remote model must appear as
            // `eggpool/<public_id>`. A same-named provider from another layer
            // cannot satisfy this.
            let missing: Vec<&String> = expected_models
                .iter()
                .filter(|id| !combined.contains(&format!("eggpool/{id}")))
                .collect();
            if !missing.is_empty() {
                return Err(ConnectError::Validation {
                    detail: format!(
                        "opencode models does not list {} EggPool model(s)",
                        missing.len()
                    ),
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
    use crate::process::ProcessOutput;
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
                &[],
            )
            .await;
            assert!(result.is_err());
        }
    }

    /// Capturing runner: records env extras and serves canned output.
    struct EnvRunner {
        output: ProcessOutput,
        seen_env: std::sync::Mutex<Vec<(String, String)>>,
        seen_args: std::sync::Mutex<Vec<String>>,
    }

    impl crate::process::ProcessRunner for EnvRunner {
        async fn run(
            &self,
            _program: &str,
            args: &[&str],
            env_extra: &[(&str, &str)],
            _timeout: Duration,
        ) -> Result<ProcessOutput, crate::outcome::ConnectError> {
            *self.seen_args.lock().expect("args") =
                args.iter().map(|arg| (*arg).to_owned()).collect();
            *self.seen_env.lock().expect("env") = env_extra
                .iter()
                .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
                .collect();
            Ok(self.output.clone())
        }
    }

    #[tokio::test]
    async fn native_opencode_pins_config_path_and_model_slugs() {
        let runner = EnvRunner {
            output: ProcessOutput {
                success: true,
                exit_code: Some(0),
                stdout: "eggpool/qual-probe-model/qual-probe\neggpool/other\n".to_owned(),
                stderr: String::new(),
            },
            seen_env: std::sync::Mutex::new(Vec::new()),
            seen_args: std::sync::Mutex::new(Vec::new()),
        };
        let dir = tempfile::tempdir().expect("tempdir");
        let config = dir.path().join("opencode.json");
        let expected = vec!["qual-probe-model/qual-probe".to_owned()];
        // Without a real client binary the probe fails closed before
        // consulting runners (mirrors the Codex guard above).
        if crate::process::find_executable("opencode").is_none() {
            let error = validate_native(
                &runner,
                ClientTarget::Opencode,
                &config,
                dir.path(),
                "",
                Duration::from_secs(1),
                &expected,
            )
            .await
            .expect_err("must fail closed without an executable");
            assert!(error.to_string().contains("not available"));
            return;
        }
        let detail = validate_native(
            &runner,
            ClientTarget::Opencode,
            &config,
            dir.path(),
            "",
            Duration::from_secs(1),
            &expected,
        )
        .await
        .expect("precise probe passes");
        assert!(detail.contains("EggPool"));
        assert_eq!(
            runner.seen_args.lock().expect("args").as_slice(),
            &["models".to_owned(), "eggpool".to_owned()]
        );
        let env = runner.seen_env.lock().expect("env").clone();
        assert!(
            env.iter().any(|(key, value)| key == "OPENCODE_CONFIG"
                && std::path::Path::new(value) == config.as_path()),
            "probe must observe the installed file, not ambient global state"
        );
        // Same-named provider without our model slug fails closed.
        let thin = EnvRunner {
            output: ProcessOutput {
                success: true,
                exit_code: Some(0),
                stdout: "eggpool/unrelated-model\n".to_owned(),
                stderr: String::new(),
            },
            seen_env: std::sync::Mutex::new(Vec::new()),
            seen_args: std::sync::Mutex::new(Vec::new()),
        };
        let error = validate_native(
            &thin,
            ClientTarget::Opencode,
            &config,
            dir.path(),
            "",
            Duration::from_secs(1),
            &expected,
        )
        .await
        .expect_err("missing slug must fail");
        assert!(error.to_string().contains("does not list"));
    }
}
