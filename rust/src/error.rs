use std::io;

use thiserror::Error;

/// Errors that can terminate the native EggPool process.
#[derive(Debug, Error)]
pub enum AppError {
    /// Clap owns user-facing parser/help/version rendering.
    #[error("{0}")]
    Cli(#[from] clap::Error),

    /// Bootstrap failures are rendered without debug details or backtraces.
    #[error("{0}")]
    Bootstrap(#[from] BootstrapError),
}

impl AppError {
    /// Return the process status appropriate for this top-level error.
    #[must_use]
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Cli(error) => error.exit_code() as u8,
            Self::Bootstrap(error) => error.exit_code(),
        }
    }
}

/// Local errors from the minimal process bootstrap.
#[derive(Debug, Error)]
pub enum BootstrapError {
    /// The scaffold could not write its deterministic help output.
    #[error("Rust bootstrap output failed: {source}")]
    Output { source: io::Error },

    /// A recognized command is unavailable in this build. This is deliberately
    /// distinct from a successful no-op.
    #[error("{command}: not implemented")]
    NotImplemented { command: String },

    /// Configuration could not be loaded or validated.
    #[error("{0}")]
    Config(#[from] crate::config::ConfigError),

    /// The development HTTP server could not start or shut down cleanly.
    #[error("Rust server failed: {detail}")]
    Server { detail: String },

    /// A parsed serve option is unavailable in the current runtime.
    #[error("{detail}")]
    ServeUnsupported { detail: &'static str },

    /// A command-specific operational result with a stable, Python-compatible
    /// exit category.
    #[error("{detail}")]
    Command { code: u8, detail: String },

    /// Explicit user interruption (for example Ctrl-C inside an interactive
    /// selector). This is not a validation failure: it exits with the
    /// conventional `128 + SIGINT` status.
    #[error("Interrupted.")]
    Interrupted,
}

impl BootstrapError {
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Command { code, .. } => *code,
            Self::Interrupted => 130,
            _ => 1,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::BootstrapError;

    #[test]
    fn interruption_exits_130_with_concise_message() {
        let error = BootstrapError::Interrupted;
        assert_eq!(error.exit_code(), 130);
        assert_eq!(error.to_string(), "Interrupted.");
        assert!(!error.to_string().contains("configuration file"));
    }
}
