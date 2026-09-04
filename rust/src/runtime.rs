use crate::{BootstrapError, Cli, Command, version::PACKAGE_VERSION};

/// Initialize process-local diagnostics and dispatch the scaffold command.
pub async fn run(cli: Cli) -> Result<(), BootstrapError> {
    let _ = tracing_subscriber::fmt()
        .with_target(false)
        .with_ansi(false)
        .try_init();
    tracing::debug!(
        version = PACKAGE_VERSION,
        "Rust migration scaffold initialized"
    );

    match cli.command {
        Some(Command::Version) => println!("{PACKAGE_VERSION}"),
        None => println!("{}", crate::cli::help_text()),
    }

    Ok(())
}
