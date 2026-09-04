use std::io;

use thiserror::Error;

/// Errors that can terminate the Rust migration candidate.
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
            Self::Bootstrap(_) => 1,
        }
    }
}

/// Local errors from the minimal process bootstrap.
#[derive(Debug, Error)]
pub enum BootstrapError {
    /// The scaffold could not write its deterministic help output.
    #[error("Rust bootstrap output failed: {source}")]
    Output { source: io::Error },

    /// A command is represented by the migration parser but belongs to a later
    /// Rust milestone. This is deliberately distinct from a successful no-op.
    #[error("{command}: not implemented in Rust candidate")]
    NotImplemented { command: String },

    /// Configuration could not be loaded or validated.
    #[error("{0}")]
    Config(#[from] crate::config::ConfigError),

    /// The development HTTP server could not start or shut down cleanly.
    #[error("Rust server failed: {detail}")]
    Server { detail: String },

    /// A parsed serve option belongs to a migration-stage runtime that is not
    /// implemented by the Rust candidate yet.
    #[error("{detail}")]
    ServeUnsupported { detail: &'static str },
}
