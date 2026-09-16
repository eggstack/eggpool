//! CLI contract for `eggpool-connect`.
//!
//! ```text
//! eggpool-connect install --profile <epc-token>
//! eggpool-connect plan --profile <epc-token>
//! eggpool-connect verify --client codex|opencode
//! eggpool-connect backups [--client ...]
//! eggpool-connect restore <backup-id>
//! eggpool-connect remove --client codex|opencode
//! eggpool-connect help
//! ```
//!
//! The API key is never accepted as a normal argv option. Credential sources
//! are `EGGPOOL_API_KEY`, a secure TTY prompt, or `--api-key-stdin`.

use std::path::PathBuf;

use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(
    name = "eggpool-connect",
    version,
    about = "Transactional EggPool client configurator (Codex/OpenCode)",
    long_about = "Receive an EggPool connection profile and safely configure a supported local client.\n\nThe helper is not an agent harness and is not a second EggPool proxy. It owns only receiving a connection profile and configuring a local client transactionally with byte-exact backups and automatic rollback."
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Debug, Subcommand)]
pub enum Commands {
    /// Show the proposed mutation without changing the filesystem.
    Plan {
        /// Shareable `epc1.…` connection token.
        #[arg(long)]
        profile: String,
        /// Limit to one client (`codex` or `opencode`). Defaults to the
        /// profile targets.
        #[arg(long)]
        client: Option<String>,
        /// Explicit client config path override (local user input only).
        #[arg(long)]
        config: Option<PathBuf>,
        /// Skip the optional pre-write remote reachability check. The
        /// required authenticated profile fetch still runs for `install`;
        /// `plan` stays read-only either way.
        #[arg(long = "no-verify-network")]
        no_verify_network: bool,
        /// Machine-readable result (secret-free).
        #[arg(long)]
        json: bool,
        /// Override the helper state root (tests/advanced operators).
        #[arg(long = "state-dir")]
        state_dir: Option<PathBuf>,
    },
    /// Fetch the remote profile, back up, mutate atomically, and verify.
    Install {
        /// Shareable `epc1.…` connection token.
        #[arg(long)]
        profile: String,
        /// Limit to one client (`codex` or `opencode`). Defaults to the
        /// profile targets.
        #[arg(long)]
        client: Option<String>,
        /// Explicit client config path override (local user input only).
        #[arg(long)]
        config: Option<PathBuf>,
        /// Non-interactive approval after all safety checks.
        #[arg(long)]
        yes: bool,
        /// Converge despite owned-field drift (never bypasses malformed
        /// profiles, unsafe paths, failed backups, unsupported schemas, or
        /// failed validation).
        #[arg(long)]
        force: bool,
        /// Read the credential from stdin (automation).
        #[arg(long = "api-key-stdin")]
        api_key_stdin: bool,
        /// Skip the optional pre-write remote reachability check.
        #[arg(long = "no-verify-network")]
        no_verify_network: bool,
        /// Machine-readable result (secret-free).
        #[arg(long)]
        json: bool,
        /// Override the helper state root (tests/advanced operators).
        #[arg(long = "state-dir")]
        state_dir: Option<PathBuf>,
    },
    /// Re-validate the current install (local parse + client-native probe).
    Verify {
        /// Client to verify (`codex` or `opencode`).
        #[arg(long)]
        client: String,
        /// Explicit client config path override.
        #[arg(long)]
        config: Option<PathBuf>,
        /// Machine-readable result (secret-free).
        #[arg(long)]
        json: bool,
        /// Override the helper state root.
        #[arg(long = "state-dir")]
        state_dir: Option<PathBuf>,
    },
    /// List backup IDs, timestamps, targets, paths, and hashes (never
    /// contents).
    Backups {
        /// Filter by client (`codex` or `opencode`).
        #[arg(long)]
        client: Option<String>,
        /// Machine-readable result (secret-free).
        #[arg(long)]
        json: bool,
        /// Override the helper state root.
        #[arg(long = "state-dir")]
        state_dir: Option<PathBuf>,
    },
    /// Restore a backup reversibly (takes a pre-restore backup first).
    Restore {
        /// Backup ID from `backups`.
        backup_id: String,
        /// Machine-readable result (secret-free).
        #[arg(long)]
        json: bool,
        /// Override the helper state root.
        #[arg(long = "state-dir")]
        state_dir: Option<PathBuf>,
        /// Read the credential from stdin (reserved; restore needs no
        /// credential today and the flag is accepted for script uniformity).
        #[arg(long = "api-key-stdin")]
        api_key_stdin: bool,
    },
    /// Remove only EggPool-owned fields/artifacts (never unrelated config).
    Remove {
        /// Client to clean (`codex` or `opencode`).
        #[arg(long)]
        client: String,
        /// Explicit client config path override.
        #[arg(long)]
        config: Option<PathBuf>,
        /// Non-interactive approval after safety checks.
        #[arg(long)]
        yes: bool,
        /// Remove despite owned-field drift.
        #[arg(long)]
        force: bool,
        /// Machine-readable result (secret-free).
        #[arg(long)]
        json: bool,
        /// Override the helper state root.
        #[arg(long = "state-dir")]
        state_dir: Option<PathBuf>,
    },
}

/// Parse a closed client target (`codex` or `opencode` only).
pub fn parse_client_target(
    value: Option<&str>,
) -> Result<Option<eggpool_client_config::ClientTarget>, crate::outcome::ConnectError> {
    match value {
        None => Ok(None),
        Some(text) => text
            .parse::<eggpool_client_config::ClientTarget>()
            .map(Some)
            .map_err(|_| crate::outcome::ConnectError::UnsupportedClient {
                detail: format!("unsupported client {text:?}; expected codex or opencode"),
            }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_contract_parses_all_commands() {
        let cli = Cli::try_parse_from(["eggpool-connect", "plan", "--profile", "epc1.abc"])
            .expect("plan parses");
        assert!(matches!(cli.command, Commands::Plan { .. }));

        let cli = Cli::try_parse_from([
            "eggpool-connect",
            "install",
            "--profile",
            "epc1.abc",
            "--yes",
            "--api-key-stdin",
            "--no-verify-network",
            "--json",
        ])
        .expect("install parses");
        assert!(matches!(cli.command, Commands::Install { .. }));

        let cli = Cli::try_parse_from(["eggpool-connect", "verify", "--client", "codex"])
            .expect("verify parses");
        assert!(matches!(cli.command, Commands::Verify { .. }));

        let cli = Cli::try_parse_from(["eggpool-connect", "backups"]).expect("backups parses");
        assert!(matches!(cli.command, Commands::Backups { .. }));

        let cli =
            Cli::try_parse_from(["eggpool-connect", "restore", "b123"]).expect("restore parses");
        assert!(matches!(cli.command, Commands::Restore { .. }));

        let cli = Cli::try_parse_from(["eggpool-connect", "remove", "--client", "opencode"])
            .expect("remove parses");
        assert!(matches!(cli.command, Commands::Remove { .. }));
    }

    #[test]
    fn install_requires_profile_and_verify_requires_client() {
        assert!(Cli::try_parse_from(["eggpool-connect", "install"]).is_err());
        assert!(Cli::try_parse_from(["eggpool-connect", "verify"]).is_err());
        assert!(Cli::try_parse_from(["eggpool-connect", "plan"]).is_err());
    }

    #[test]
    fn client_targets_are_closed() {
        assert!(parse_client_target(Some("vscode")).is_err());
        assert!(parse_client_target(Some("codex")).expect("codex").is_some());
        assert!(parse_client_target(None).expect("none").is_none());
    }
}
