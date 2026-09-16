//! Portable client-configuration errors.
//!
//! All variants are secret-free. Callers must never interpolate resolved
//! credentials, prompts, raw bodies, or cache keys into these errors.

use thiserror::Error;

/// Secret-free portable error for client-config policy.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ClientConfigError {
    /// Connection-profile or integration-profile schema is unsupported.
    #[error("unsupported profile schema: {detail}")]
    UnsupportedSchema { detail: String },
    /// A required field is missing or invalid.
    #[error("invalid profile field {field}: {detail}")]
    InvalidField { field: String, detail: String },
    /// Base URL is not an absolute HTTP(S) URL.
    #[error("base URL must be an absolute HTTP(S) URL")]
    InvalidBaseUrl,
    /// Token envelope is malformed or violates bounds.
    #[error("invalid connection token: {detail}")]
    InvalidToken { detail: String },
    /// Token or payload exceeds bounded size.
    #[error("connection token exceeds bounded size ({detail})")]
    TooLarge { detail: String },
    /// Rendered catalog or profile exceeds bounded size.
    #[error("model catalog exceeds bounded size ({count} models)")]
    CatalogTooLarge { count: usize },
    /// Generated document is too large.
    #[error("generated document is too large")]
    DocumentTooLarge,
    /// JSON serialization failed (secret-free).
    #[error("profile JSON serialization failed: {detail}")]
    Json { detail: String },
    /// Client document drift or validation failure.
    #[error("client config drift detected: {detail}")]
    Drift { detail: String },
    /// Refusing an unsafe rewrite.
    #[error("refusing to rewrite client config: {detail}")]
    UnsafeRewrite { detail: String },
}

impl From<serde_json::Error> for ClientConfigError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json {
            detail: error.to_string(),
        }
    }
}
