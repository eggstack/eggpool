//! Side-by-side Rust implementation boundary for EggPool.
//!
//! The migration candidate grows in bounded slices. Python remains the
//! production implementation until the migration is explicitly cut over.

#![forbid(unsafe_code)]

pub mod accounts;
pub mod catalog;
mod cli;
pub mod config;
pub mod coordinator;
pub mod db;
mod error;
pub mod health;
pub mod model_router;
pub mod providers;
pub mod quota;
pub mod request;
pub mod routing;
mod runtime;
pub mod server;
pub mod version;
pub mod wire;

pub use cli::{Cli, Command};
pub use config::{AppConfig, Config, ConfigError};
pub use error::{AppError, BootstrapError};

/// Run the migration candidate with an explicit argument iterator.
pub async fn run<I, T>(args: I) -> Result<(), AppError>
where
    I: IntoIterator<Item = T>,
    T: Into<std::ffi::OsString> + Clone,
{
    let cli = match cli::parse(args) {
        Ok(cli) => cli,
        Err(error) if cli::is_display_probe(&error) => {
            error
                .print()
                .map_err(|source| AppError::from(BootstrapError::Output { source }))?;
            return Ok(());
        }
        Err(error) => return Err(error.into()),
    };
    runtime::run(cli).await.map_err(AppError::from)
}

#[cfg(test)]
mod tests {
    use clap::{Parser, error::ErrorKind};

    use super::{AppError, BootstrapError, Cli, version};

    #[test]
    fn parser_supports_help_and_version_probes() {
        let help = Cli::try_parse_from(["eggpool", "--help"])
            .expect_err("--help should be handled by clap");
        assert_eq!(help.kind(), ErrorKind::DisplayHelp);

        let version = Cli::try_parse_from(["eggpool", "--version"])
            .expect_err("--version should be handled by clap");
        assert_eq!(version.kind(), ErrorKind::DisplayVersion);
    }

    #[test]
    fn version_is_exposed_from_cargo_metadata() {
        assert_eq!(version::PACKAGE_VERSION, env!("CARGO_PKG_VERSION"));
    }

    #[test]
    fn top_level_error_display_is_operator_safe() {
        let error = AppError::from(BootstrapError::Output {
            source: std::io::Error::other("permission denied"),
        });

        assert_eq!(
            error.to_string(),
            "Rust bootstrap output failed: permission denied"
        );
        assert!(!error.to_string().contains("BootstrapError"));
    }
}
