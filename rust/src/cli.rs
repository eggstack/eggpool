use std::ffi::OsString;

use clap::{CommandFactory, Parser, Subcommand, error::ErrorKind};

use crate::version::PACKAGE_VERSION;

/// The deliberately small F001 command surface.
#[derive(Debug, Parser)]
#[command(
    name = "eggpool",
    version = PACKAGE_VERSION,
    about = "EggPool Rust migration candidate",
    long_about = "Side-by-side Rust migration scaffold; Python remains canonical during migration."
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Option<Command>,
}

/// Commands needed to probe the scaffold before F003 ports the full CLI.
#[derive(Debug, Subcommand)]
pub enum Command {
    /// Print the package version and exit.
    Version,
}

pub fn parse<I, T>(args: I) -> Result<Cli, clap::Error>
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    Cli::try_parse_from(args)
}

pub(crate) fn is_display_probe(error: &clap::Error) -> bool {
    matches!(
        error.kind(),
        ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
    )
}

pub fn help_text() -> String {
    let mut command = Cli::command();
    command.render_help().to_string()
}
