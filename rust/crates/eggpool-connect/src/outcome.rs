//! Stable error and output contract for `eggpool-connect`.
//!
//! Human output distinguishes nothing-changed, installed-and-verified,
//! installed-without-native-verification, refused-before-mutation,
//! write-failed-with-rollback, write-failed-with-rollback-failure, and
//! restore results. Machine-readable JSON redacts secrets and never includes
//! captured arbitrary client output unless sanitized and bounded.

use thiserror::Error;

/// Stable process exit codes.
///
/// These are deliberately distinct so scripts can separate pre-mutation
/// refusals from post-write recovery states.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitCode {
    /// Success, including verified install and no-op.
    Success = 0,
    /// Generic failure (unexpected IO, etc.).
    General = 1,
    /// Invalid connection profile or token.
    InvalidProfile = 2,
    /// Authentication or network failure talking to EggPool.
    AuthNetwork = 3,
    /// Unsupported client, version, or schema variant.
    UnsupportedClient = 4,
    /// Unsafe existing config (symlink/special file, unparseable JSONC,
    /// ambiguous schema, drift without `--force`).
    UnsafeConfig = 5,
    /// Byte-exact backup could not be committed.
    BackupFailure = 6,
    /// Filesystem mutation failed (before validation).
    MutationFailure = 7,
    /// Post-write validation failed (rollback result reported separately).
    ValidationFailure = 8,
    /// Rollback itself failed; recovery evidence is retained and the backup
    /// ID/path is reported (never contents or secrets).
    RollbackFailure = 9,
}

impl ExitCode {
    #[must_use]
    pub const fn code(self) -> i32 {
        self as i32
    }
}

/// Secret-free helper errors.
///
/// Callers must never interpolate the resolved EggPool key, backup contents,
/// or unrelated client config into these errors.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ConnectError {
    /// Connection token or profile is malformed or violates bounds.
    #[error("invalid connection profile: {detail}")]
    InvalidProfile { detail: String },
    /// Credential is missing or unusable.
    #[error("credential unavailable: {detail}")]
    Credential { detail: String },
    /// Authenticated EggPool fetch failed.
    #[error("eggpool request failed: {detail}")]
    AuthNetwork { detail: String },
    /// Client or schema is unsupported; fail closed with guidance.
    #[error("unsupported client: {detail}")]
    UnsupportedClient { detail: String },
    /// Existing config is unsafe to mutate.
    #[error("refusing to mutate client config: {detail}")]
    UnsafeConfig { detail: String },
    /// Owned-field drift requires review or `--force`.
    #[error("client config drift detected: {detail}")]
    Drift { detail: String },
    /// Backup could not be committed before the first write.
    #[error("backup failed: {detail}")]
    Backup { detail: String },
    /// Atomic replacement failed.
    #[error("mutation failed: {detail}")]
    Mutation { detail: String },
    /// Post-write validation failed.
    #[error("validation failed: {detail}")]
    Validation { detail: String },
    /// Rollback after failure also failed. The backup ID/path pin recovery
    /// evidence; contents and secrets are never included.
    #[error("rollback failed (backup {backup_id} at {backup_path}): {detail}")]
    Rollback {
        backup_id: String,
        backup_path: String,
        detail: String,
    },
    /// Backup ID is unknown.
    #[error("unknown backup: {detail}")]
    UnknownBackup { detail: String },
    /// Filesystem or process IO failed with a bounded generic message.
    #[error("operation failed: {detail}")]
    Io { detail: String },
}

impl ConnectError {
    #[must_use]
    pub const fn exit_code(&self) -> ExitCode {
        match self {
            Self::InvalidProfile { .. } => ExitCode::InvalidProfile,
            Self::Credential { .. } | Self::AuthNetwork { .. } => ExitCode::AuthNetwork,
            Self::UnsupportedClient { .. } => ExitCode::UnsupportedClient,
            Self::UnsafeConfig { .. } | Self::Drift { .. } => ExitCode::UnsafeConfig,
            Self::Backup { .. } => ExitCode::BackupFailure,
            Self::Mutation { .. } | Self::Io { .. } => ExitCode::MutationFailure,
            Self::Validation { .. } => ExitCode::ValidationFailure,
            Self::Rollback { .. } => ExitCode::RollbackFailure,
            Self::UnknownBackup { .. } => ExitCode::General,
        }
    }
}

/// Replace every occurrence of the resolved key with `[redacted]`.
///
/// Used before printing captured subprocess output, JSON results, or error
/// details that may have echoed the key back from the environment.
#[must_use]
pub fn redact(text: &str, api_key: &str) -> String {
    if api_key.is_empty() {
        return text.to_owned();
    }
    text.replace(api_key, "[redacted]")
}

/// Truncate captured output to a bounded prefix for diagnostics.
#[must_use]
pub fn bound_output(text: &str, max_chars: usize) -> String {
    if text.len() <= max_chars {
        return text.to_owned();
    }
    let mut truncated = text
        .char_indices()
        .take_while(|(index, _)| *index < max_chars)
        .map(|(_, character)| character)
        .collect::<String>();
    truncated.push_str("…[truncated]");
    truncated
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_codes_are_stable_and_distinct() {
        assert_eq!(ExitCode::Success.code(), 0);
        assert_eq!(
            ConnectError::InvalidProfile {
                detail: String::new()
            }
            .exit_code(),
            ExitCode::InvalidProfile
        );
        assert_eq!(
            ConnectError::AuthNetwork {
                detail: String::new()
            }
            .exit_code(),
            ExitCode::AuthNetwork
        );
        assert_eq!(
            ConnectError::UnsupportedClient {
                detail: String::new()
            }
            .exit_code(),
            ExitCode::UnsupportedClient
        );
        assert_eq!(
            ConnectError::Drift {
                detail: String::new()
            }
            .exit_code(),
            ExitCode::UnsafeConfig
        );
        assert_eq!(
            ConnectError::Backup {
                detail: String::new()
            }
            .exit_code(),
            ExitCode::BackupFailure
        );
        assert_eq!(
            ConnectError::Rollback {
                backup_id: "b".to_owned(),
                backup_path: "/tmp".to_owned(),
                detail: String::new(),
            }
            .exit_code(),
            ExitCode::RollbackFailure
        );
    }

    #[test]
    fn redact_never_leaks_key_and_handles_empty() {
        assert_eq!(redact("hello", ""), "hello");
        assert_eq!(
            redact("key ep_test_123 here ep_test_123", "ep_test_123"),
            "key [redacted] here [redacted]"
        );
    }

    #[test]
    fn bound_output_truncates_with_marker() {
        assert_eq!(bound_output("abc", 10), "abc");
        let truncated = bound_output("abcdefghij", 4);
        assert!(truncated.starts_with("abcd"));
        assert!(truncated.contains("truncated"));
    }
}
