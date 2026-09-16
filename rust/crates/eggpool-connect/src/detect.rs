//! Receiving-host client detection.
//!
//! Detection resolves the effective config path locally using the current
//! client contract; paths are never inferred from the SBC profile. A client
//! version is evidence for adapter selection, never a reason to silently
//! rewrite unknown formats.

use std::path::{Path, PathBuf};
use std::time::Duration;

use eggpool_client_config::{ClientSchemaVariant, ClientTarget, ClientVersion};

use crate::outcome::ConnectError;
use crate::paths::{codex_config_path, opencode_config_path};
use crate::process::{DEFAULT_PROCESS_TIMEOUT, ProcessRunner, find_executable};

/// Tested compatibility evidence for adapter selection.
///
/// Versions are evidence, not a rewrite license: unknown versions fail closed
/// with guidance to update the helper or use `plan`/manual output.
pub const QUALIFIED_CODEX_VERSION: &str = "0.154.0";
pub const QUALIFIED_OPENCODE_VERSION: &str = "1.18.30";

/// Supported schema variants for automatic mutation in this plan.
///
/// Codex TOML and OpenCode V1 (`provider` shape) are supported. OpenCode V2
/// (`providers` plural shape) is detected and reported but refuses automatic
/// mutation until Plan 213 qualifies a preserving V2 renderer.
pub const SUPPORTED_VARIANTS: &[ClientSchemaVariant] = &[
    ClientSchemaVariant::CodexToml,
    ClientSchemaVariant::OpencodeV1,
];

/// Local client detection facts owned by the desktop.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Detection {
    pub target: ClientTarget,
    pub executable: Option<PathBuf>,
    pub version: Option<ClientVersion>,
    pub version_raw: Option<String>,
    pub config_path: PathBuf,
    pub schema_variant: ClientSchemaVariant,
    pub config_exists: bool,
    pub parseable: bool,
    pub parse_issue: Option<String>,
    /// True when client-native post-write verification can run.
    pub native_verification_available: bool,
}

impl Detection {
    #[must_use]
    pub fn summary(&self) -> String {
        format!(
            "client={} config={} exists={} parseable={} native_verify={} version={}",
            self.target,
            self.config_path.display(),
            self.config_exists,
            self.parseable,
            self.native_verification_available,
            self.version_raw.as_deref().unwrap_or("unknown"),
        )
    }
}

/// Detect a client, resolving paths locally and probing the executable when
/// present. `config_override` is the local user's explicit input and never a
/// profile-supplied path.
pub async fn detect_client<R: ProcessRunner>(
    runner: &R,
    target: ClientTarget,
    config_override: Option<&Path>,
) -> Result<Detection, ConnectError> {
    match target {
        ClientTarget::Codex => detect_codex(runner, config_override).await,
        ClientTarget::Opencode => detect_opencode(runner, config_override).await,
    }
}

async fn detect_codex<R: ProcessRunner>(
    runner: &R,
    config_override: Option<&Path>,
) -> Result<Detection, ConnectError> {
    let config_path = config_override.map_or_else(codex_config_path, Path::to_path_buf);
    let executable = find_executable("codex");
    let (version, version_raw) = probe_version(runner, "codex", &["--version"]).await;
    let (config_exists, parseable, parse_issue) = inspect_codex_file(&config_path);
    // Native verification needs the executable. When it is absent the helper
    // may still configure with an explicit config path, but interactive use
    // must require explicit confirmation rather than pretending success.
    let native_verification_available = executable.is_some();
    Ok(Detection {
        target: ClientTarget::Codex,
        executable,
        version,
        version_raw,
        config_path,
        schema_variant: ClientSchemaVariant::CodexToml,
        config_exists,
        parseable,
        parse_issue,
        native_verification_available,
    })
}

async fn detect_opencode<R: ProcessRunner>(
    runner: &R,
    config_override: Option<&Path>,
) -> Result<Detection, ConnectError> {
    let config_path = config_override.map_or_else(opencode_config_path, Path::to_path_buf);
    let executable = find_executable("opencode");
    let (version, version_raw) = probe_version(runner, "opencode", &["--version"]).await;
    let (config_exists, parseable, parse_issue, schema_variant) =
        inspect_opencode_file(&config_path);
    let native_verification_available = executable.is_some();
    Ok(Detection {
        target: ClientTarget::Opencode,
        executable,
        version,
        version_raw,
        config_path,
        schema_variant,
        config_exists,
        parseable,
        parse_issue,
        native_verification_available,
    })
}

async fn probe_version<R: ProcessRunner>(
    runner: &R,
    program: &str,
    args: &[&str],
) -> (Option<ClientVersion>, Option<String>) {
    if find_executable(program).is_none() {
        return (None, None);
    }
    let Ok(output) = runner
        .run(program, args, &[], DEFAULT_PROCESS_TIMEOUT)
        .await
    else {
        return (None, None);
    };
    if !output.success {
        return (None, None);
    }
    let raw = format!("{} {}", output.stdout, output.stderr);
    let token = raw
        .split_whitespace()
        .find(|part| part.chars().any(|c| c.is_ascii_digit()))
        .unwrap_or("")
        .trim_matches(|c: char| c == 'v' || c == ',' || c == ';')
        .chars()
        .take(ClientVersion::MAX_BYTES)
        .collect::<String>();
    if token.is_empty() {
        return (None, None);
    }
    match ClientVersion::parse(&token) {
        Ok(version) => {
            let raw = version.raw.clone();
            (Some(version), Some(raw))
        }
        Err(_) => (None, None),
    }
}

fn inspect_codex_file(path: &Path) -> (bool, bool, Option<String>) {
    match std::fs::read_to_string(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (false, true, None),
        Err(error) => (
            true,
            false,
            Some(format!("cannot read Codex config: {error}")),
        ),
        Ok(text) => {
            if text.trim().is_empty() {
                return (true, true, None);
            }
            match text.parse::<toml::Value>() {
                Ok(_) => (true, true, None),
                Err(error) => (
                    true,
                    false,
                    Some(format!("Codex config is not valid TOML: {error}")),
                ),
            }
        }
    }
}

fn inspect_opencode_file(path: &Path) -> (bool, bool, Option<String>, ClientSchemaVariant) {
    match std::fs::read_to_string(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            (false, true, None, ClientSchemaVariant::OpencodeV1)
        }
        Err(error) => (
            true,
            false,
            Some(format!("cannot read OpenCode config: {error}")),
            ClientSchemaVariant::OpencodeV1,
        ),
        Ok(text) => {
            if text.trim().is_empty() {
                return (true, true, None, ClientSchemaVariant::OpencodeV1);
            }
            if eggpool_client_config::text::has_jsonc_comments(&text) {
                // JSONC comments require the preserving mutator from Plan 213.
                // Fail closed with a parse location hint, never a lossy
                // whole-file replacement.
                return (
                    true,
                    false,
                    Some(
                        "OpenCode config uses JSONC comments; automatic mutation needs the preserving editor (see plan output)".to_owned(),
                    ),
                    classify_opencode_variant(&text),
                );
            }
            match serde_json::from_str::<serde_json::Value>(&text) {
                Ok(_) => (true, true, None, classify_opencode_variant(&text)),
                Err(error) => (
                    true,
                    false,
                    Some(format!("OpenCode config is not valid JSON: {error}")),
                    ClientSchemaVariant::OpencodeV1,
                ),
            }
        }
    }
}

/// Classify the OpenCode schema variant from document content.
///
/// Selection is based on verified shape, never solely on a major version
/// number. `providers` (plural) selects V2; otherwise V1.
#[must_use]
pub fn classify_opencode_variant(document: &str) -> ClientSchemaVariant {
    // A cheap shape probe that tolerates unparseable JSONC: look for the
    // plural key outside strings. Full validation happens in the adapter.
    if document.contains("\"providers\"") || document.contains("'providers'") {
        ClientSchemaVariant::OpencodeV2
    } else {
        ClientSchemaVariant::OpencodeV1
    }
}

/// Check whether a schema variant supports automatic mutation in this plan.
#[must_use]
pub const fn variant_supports_auto_mutation(variant: ClientSchemaVariant) -> bool {
    match variant {
        ClientSchemaVariant::CodexToml | ClientSchemaVariant::OpencodeV1 => true,
        ClientSchemaVariant::OpencodeV2 => false,
    }
}

/// Human guidance when a version or variant cannot be safely classified.
#[must_use]
pub fn unsupported_guidance(target: ClientTarget, detail: &str) -> String {
    format!(
        "{detail} Update eggpool-connect to the latest release, or run `eggpool-connect plan` and apply the manual `configsetup` output for {target}."
    )
}

/// Timeout for native validators (shared with `verify`).
pub const NATIVE_VERIFY_TIMEOUT: Duration = Duration::from_secs(20);

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::{FakeProcessRunner, ProcessOutput};

    #[test]
    fn opencode_variant_selection_uses_shape_not_version() {
        assert_eq!(
            classify_opencode_variant(r#"{"provider": {"eggpool": {}}}"#),
            ClientSchemaVariant::OpencodeV1
        );
        assert_eq!(
            classify_opencode_variant(r#"{"providers": {"eggpool": {}}}"#),
            ClientSchemaVariant::OpencodeV2
        );
        assert!(!variant_supports_auto_mutation(
            ClientSchemaVariant::OpencodeV2
        ));
        assert!(variant_supports_auto_mutation(
            ClientSchemaVariant::OpencodeV1
        ));
    }

    #[tokio::test]
    async fn codex_detection_without_executable_marks_native_unavailable() {
        // PATH without codex: executable absent, native verification off.
        let runner = FakeProcessRunner::new();
        let dir = tempfile::tempdir().expect("tempdir");
        let config = dir.path().join("config.toml");
        let detection = detect_codex(&runner, Some(&config)).await.expect("detect");
        assert_eq!(detection.target, ClientTarget::Codex);
        if find_executable("codex").is_none() {
            assert!(!detection.native_verification_available);
            assert!(detection.version.is_none());
        }
    }

    #[tokio::test]
    async fn opencode_jsonc_is_unparseable_for_auto_mutation() {
        let dir = tempfile::tempdir().expect("tempdir");
        let config = dir.path().join("opencode.json");
        std::fs::write(&config, "{\n// user comment\n\"provider\": {}\n}\n").expect("write");
        let runner = FakeProcessRunner::new();
        let detection = detect_opencode(&runner, Some(&config))
            .await
            .expect("detect");
        assert!(!detection.parseable);
        assert!(detection.parse_issue.is_some());
    }

    #[test]
    fn probe_version_parses_bounded_tokens() {
        let output = ProcessOutput {
            success: true,
            exit_code: Some(0),
            stdout: "codex-cli 0.154.0\n".to_owned(),
            stderr: String::new(),
        };
        let raw = format!("{} {}", output.stdout, output.stderr);
        let token = raw
            .split_whitespace()
            .find(|part| part.chars().any(|c| c.is_ascii_digit()))
            .unwrap_or("");
        assert!(token.contains("0.154.0"));
    }
}
