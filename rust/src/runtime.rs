use crate::{BootstrapError, Cli, Command, config, version::PACKAGE_VERSION};

/// Initialize process-local diagnostics and dispatch the migration candidate.
pub async fn run(cli: Cli) -> Result<(), BootstrapError> {
    let _ = tracing_subscriber::fmt()
        .with_target(false)
        .with_ansi(false)
        .try_init();
    tracing::debug!(
        version = PACKAGE_VERSION,
        "Rust migration scaffold initialized"
    );

    let config_path = cli.resolved_config_path();
    match cli.command {
        Some(Command::Version) => println!("{PACKAGE_VERSION}"),
        Some(Command::CheckConfig) => {
            let config = config::Config::from_toml(&config_path)?;
            config.validate_account_credentials()?;
            let account_count = config.all_accounts().len();
            let digest = config::content_digest(&config_path)?;
            println!(
                "Configuration loaded successfully from {}",
                config_path.display()
            );
            println!("  Server: {}:{}", config.server.host, config.server.port);
            println!("  Accounts: {account_count}");
            println!("  Database: {}", config.database.path);
            println!("  Content digest: {digest}");
        }
        None => {
            println!("{}", crate::cli::help_text());
            println!("\nConfig file: {}", config_path.display());
        }
        Some(Command::Help) => println!("{}", crate::cli::help_text()),
        Some(command) => {
            return Err(BootstrapError::NotImplemented {
                command: command.unavailable_name().to_string(),
            });
        }
    }

    Ok(())
}
