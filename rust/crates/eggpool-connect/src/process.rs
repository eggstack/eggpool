//! Injectable process runner for client detection and native validation.
//!
//! The portable crate never spawns subprocesses. This module owns the narrow
//! desktop-side runner with bounded output and time bounds so tests can inject
//! fakes without sleeping or touching real user executables.

use std::collections::HashMap;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

use crate::outcome::{ConnectError, bound_output, redact};

/// Bounded stdout/stderr capture per child process.
pub const MAX_CAPTURE_CHARS: usize = 16 * 1024;
/// Default time bound for client probes and native validators.
pub const DEFAULT_PROCESS_TIMEOUT: Duration = Duration::from_secs(15);

/// Bounded child-process result (secret-free by construction at the call
/// site; callers redact before display).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessOutput {
    pub success: bool,
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

/// Narrow process-runner seam. Implementations must be time-bounded and must
/// not echo secrets into logs.
pub trait ProcessRunner: Send + Sync {
    /// Run `program` with `args` and extra environment, bounded by `timeout`.
    fn run(
        &self,
        program: &str,
        args: &[&str],
        env_extra: &[(&str, &str)],
        timeout_duration: Duration,
    ) -> impl std::future::Future<Output = Result<ProcessOutput, ConnectError>> + Send;
}

/// Real runner backed by `tokio::process`.
#[derive(Debug, Clone, Copy, Default)]
pub struct RealProcessRunner;

impl ProcessRunner for RealProcessRunner {
    async fn run(
        &self,
        program: &str,
        args: &[&str],
        env_extra: &[(&str, &str)],
        timeout_duration: Duration,
    ) -> Result<ProcessOutput, ConnectError> {
        let mut command = Command::new(program);
        command.args(args);
        for (key, value) in env_extra {
            command.env(key, value);
        }
        command.stdin(Stdio::null());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());
        // Child processes inherit a minimal environment plus the caller's
        // explicit extras; the EggPool key is only forwarded when the caller
        // explicitly passes it for `EGGPOOL_API_KEY` availability checks.
        let child = command.spawn().map_err(|error| ConnectError::Io {
            detail: format!("cannot run {program}: {error}"),
        })?;
        let output = tokio::time::timeout(timeout_duration, child.wait_with_output())
            .await
            .map_err(|_| ConnectError::Validation {
                detail: format!("{program} timed out"),
            })?
            .map_err(|error| ConnectError::Io {
                detail: format!("{program} failed: {error}"),
            })?;
        Ok(ProcessOutput {
            success: output.status.success(),
            exit_code: output.status.code(),
            stdout: bound_output(&String::from_utf8_lossy(&output.stdout), MAX_CAPTURE_CHARS),
            stderr: bound_output(&String::from_utf8_lossy(&output.stderr), MAX_CAPTURE_CHARS),
        })
    }
}

/// Search `PATH` for an executable (with `.exe` on Windows).
#[must_use]
pub fn find_executable(name: &str) -> Option<std::path::PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    let mut candidates = vec![name.to_owned()];
    if cfg!(windows) && !name.ends_with(".exe") {
        candidates.push(format!("{name}.exe"));
    }
    for dir in std::env::split_paths(&path_var) {
        for candidate in &candidates {
            let full = dir.join(candidate);
            if is_executable_file(&full) {
                return Some(full);
            }
        }
    }
    None
}

#[cfg(unix)]
fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.is_file()
        && std::fs::metadata(path)
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable_file(path: &Path) -> bool {
    path.is_file()
}

/// Redact a key from captured output before diagnostics.
#[must_use]
pub fn redacted_output(output: &ProcessOutput, api_key: &str) -> ProcessOutput {
    ProcessOutput {
        success: output.success,
        exit_code: output.exit_code,
        stdout: redact(&output.stdout, api_key),
        stderr: redact(&output.stderr, api_key),
    }
}

/// Fake runner for deterministic tests.
#[derive(Debug, Default)]
pub struct FakeProcessRunner {
    outputs: HashMap<String, ProcessOutput>,
    /// Ordered call log of `program args...`.
    pub calls: std::sync::Mutex<Vec<String>>,
}

impl FakeProcessRunner {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, program: &str, args: &[&str], output: ProcessOutput) {
        self.outputs.insert(Self::key(program, args), output);
    }

    fn key(program: &str, args: &[&str]) -> String {
        format!("{program} {}", args.join(" "))
    }

    #[must_use]
    pub fn call_log(&self) -> Vec<String> {
        self.calls.lock().expect("call log").clone()
    }
}

impl ProcessRunner for FakeProcessRunner {
    async fn run(
        &self,
        program: &str,
        args: &[&str],
        _env_extra: &[(&str, &str)],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ConnectError> {
        self.calls
            .lock()
            .expect("call log")
            .push(Self::key(program, args));
        self.outputs
            .get(&Self::key(program, args))
            .cloned()
            .ok_or_else(|| ConnectError::Validation {
                detail: format!("no fake output for {program} {}", args.join(" ")),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn fake_runner_records_calls_and_returns_bounded_output() {
        let mut fake = FakeProcessRunner::new();
        fake.insert(
            "codex",
            &["--version"],
            ProcessOutput {
                success: true,
                exit_code: Some(0),
                stdout: "codex-cli 0.154.0\n".to_owned(),
                stderr: String::new(),
            },
        );
        let output = fake
            .run("codex", &["--version"], &[], Duration::from_secs(1))
            .await
            .expect("fake output");
        assert!(output.success);
        assert!(output.stdout.contains("0.154.0"));
        assert_eq!(fake.call_log(), vec!["codex --version".to_owned()]);
    }

    #[test]
    fn redaction_removes_key_from_captured_output() {
        let output = ProcessOutput {
            success: false,
            exit_code: Some(1),
            stdout: "using ep_secret_123".to_owned(),
            stderr: String::new(),
        };
        let redacted = redacted_output(&output, "ep_secret_123");
        assert!(!redacted.stdout.contains("ep_secret_123"));
    }
}
