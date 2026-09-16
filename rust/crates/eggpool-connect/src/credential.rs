//! Credential handling: environment, secure TTY prompt, stdin.
//!
//! Order is explicit and safe: existing `EGGPOOL_API_KEY` first, secure TTY
//! prompt second, `--api-key-stdin` for automation. The key is never printed,
//! never included in backup metadata, fingerprints, logs, or generated
//! client config.

use std::io::{self, BufRead, Read, Write};

use crate::outcome::ConnectError;

/// Maximum accepted credential bytes (the server shape is 8–512; stdin is
/// bounded slightly wider before validation).
pub const MAX_CREDENTIAL_BYTES: usize = 8192;

/// Where the credential came from (secret-free label only).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialSource {
    Env,
    Tty,
    Stdin,
}

impl CredentialSource {
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Env => "environment",
            Self::Tty => "tty-prompt",
            Self::Stdin => "stdin",
        }
    }
}

/// Resolve the EggPool credential without ever accepting it as a normal
/// argv option (process listings and shell history can expose argv).
pub fn resolve_api_key(api_key_stdin: bool) -> Result<(String, CredentialSource), ConnectError> {
    if let Ok(value) = std::env::var("EGGPOOL_API_KEY") {
        let trimmed = value.trim().to_owned();
        if !trimmed.is_empty() {
            validate_credential_shape(&trimmed)?;
            return Ok((trimmed, CredentialSource::Env));
        }
    }
    if api_key_stdin {
        let key = read_stdin_key()?;
        return Ok((key, CredentialSource::Stdin));
    }
    let key = prompt_tty_key()?;
    Ok((key, CredentialSource::Tty))
}

/// Validate the credential shape without logging its value.
fn validate_credential_shape(key: &str) -> Result<(), ConnectError> {
    if key.len() > MAX_CREDENTIAL_BYTES {
        return Err(ConnectError::Credential {
            detail: "credential exceeds bounded size".to_owned(),
        });
    }
    if key.is_empty() || key.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(ConnectError::Credential {
            detail: "credential is empty or contains whitespace/control characters".to_owned(),
        });
    }
    Ok(())
}

/// Read a credential from stdin for automation (`--api-key-stdin`).
///
/// The value is trimmed of a single trailing newline; surrounding whitespace
/// is otherwise rejected by shape validation.
pub fn read_stdin_key() -> Result<String, ConnectError> {
    let stdin = io::stdin();
    let mut locked = stdin.lock();
    let mut raw = String::new();
    // Bound allocation before reading an unbounded pipe.
    let mut limited = locked.by_ref().take((MAX_CREDENTIAL_BYTES + 1) as u64);
    limited
        .read_to_string(&mut raw)
        .map_err(|_| ConnectError::Credential {
            detail: "cannot read credential from stdin".to_owned(),
        })?;
    if raw.len() > MAX_CREDENTIAL_BYTES + 1 {
        return Err(ConnectError::Credential {
            detail: "credential exceeds bounded size".to_owned(),
        });
    }
    let trimmed = raw.trim().to_owned();
    if trimmed.is_empty() {
        return Err(ConnectError::Credential {
            detail: "no credential on stdin; pipe the key or set EGGPOOL_API_KEY".to_owned(),
        });
    }
    validate_credential_shape(&trimmed)?;
    Ok(trimmed)
}

/// Prompt securely from the TTY without echoing the value where the platform
/// permits it.
///
/// On POSIX the helper best-effort disables echo via `stty -echo` around the
/// `/dev/tty` (or stdin fallback) read. When echo cannot be disabled the
/// prompt still reads without logging, and automation should prefer
/// `EGGPOOL_API_KEY` or `--api-key-stdin`.
pub fn prompt_tty_key() -> Result<String, ConnectError> {
    let stderr = io::stderr();
    let mut err = stderr.lock();
    let _ = writeln!(
        err,
        "EggPool API key (input hidden where supported; prefer EGGPOOL_API_KEY or --api-key-stdin for automation):"
    );
    let _ = err.flush();

    #[cfg(unix)]
    let echo_guard = DisableEcho::best_effort();

    let key = read_tty_line().map(|line| line.trim().to_owned())?;

    #[cfg(unix)]
    drop(echo_guard);
    let _ = writeln!(io::stderr());

    if key.is_empty() {
        return Err(ConnectError::Credential {
            detail: "no credential entered; set EGGPOOL_API_KEY or use --api-key-stdin".to_owned(),
        });
    }
    validate_credential_shape(&key)?;
    Ok(key)
}

fn read_tty_line() -> Result<String, ConnectError> {
    #[cfg(unix)]
    {
        if let Ok(file) = std::fs::File::open("/dev/tty") {
            let mut reader = io::BufReader::new(file);
            let mut line = String::new();
            reader
                .read_line(&mut line)
                .map_err(|_| ConnectError::Credential {
                    detail: "cannot read credential from /dev/tty".to_owned(),
                })?;
            if !line.is_empty() {
                return Ok(line);
            }
        }
    }
    // Fallback: stdin line (used on Windows and when /dev/tty is absent).
    // Callers running non-interactively should set EGGPOOL_API_KEY or use
    // --api-key-stdin instead.
    let stdin = io::stdin();
    let mut line = String::new();
    stdin
        .lock()
        .read_line(&mut line)
        .map_err(|_| ConnectError::Credential {
            detail: "cannot read credential; set EGGPOOL_API_KEY or use --api-key-stdin".to_owned(),
        })?;
    if line.is_empty() {
        return Err(ConnectError::Credential {
            detail: "no TTY available; set EGGPOOL_API_KEY or use --api-key-stdin".to_owned(),
        });
    }
    Ok(line)
}

#[cfg(unix)]
struct DisableEcho {
    restored: bool,
}

#[cfg(unix)]
impl DisableEcho {
    fn best_effort() -> Self {
        let restored = std::process::Command::new("stty")
            .arg("-echo")
            .stdin(std::process::Stdio::inherit())
            .status()
            .is_ok();
        Self { restored }
    }
}

#[cfg(unix)]
impl Drop for DisableEcho {
    fn drop(&mut self) {
        if self.restored {
            let _ = std::process::Command::new("stty")
                .arg("echo")
                .stdin(std::process::Stdio::inherit())
                .status();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_shape_rejects_whitespace_and_oversize() {
        assert!(validate_credential_shape("ep_valid-key_123").is_ok());
        assert!(validate_credential_shape("").is_err());
        assert!(validate_credential_shape("has space").is_err());
        assert!(validate_credential_shape("has\nnewline").is_err());
        assert!(validate_credential_shape(&"x".repeat(MAX_CREDENTIAL_BYTES + 1)).is_err());
    }

    #[test]
    fn env_source_name_is_stable() {
        assert_eq!(CredentialSource::Env.name(), "environment");
        assert_eq!(CredentialSource::Tty.name(), "tty-prompt");
        assert_eq!(CredentialSource::Stdin.name(), "stdin");
    }
}
