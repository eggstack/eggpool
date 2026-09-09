use std::{
    env,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    process::{Command as OsCommand, Stdio},
    time::Duration,
};

use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::{Instant, sleep, timeout},
};

use crate::{
    BootstrapError, Cli, Command,
    cli::ServeArgs,
    config,
    operations::{
        backup::{self, BackupService},
        config_mutation::{self, ApplyMode, ApplyOutcome},
        paths::RuntimePaths,
        process,
        update::{ReleaseTarget, UpdateError, UpdateService},
    },
    version::PACKAGE_VERSION,
};

const EXIT_VALIDATION: u8 = 1;
const EXIT_RESTART_REQUIRED: u8 = 2;
const EXIT_CONTROL_UNAVAILABLE: u8 = 3;
const EXIT_RELOAD_BUSY: u8 = 4;
const EXIT_PREPARATION_FAILED: u8 = 5;
const EXIT_DIGEST_MISMATCH: u8 = 6;
const MAX_STATUS_BODY_BYTES: usize = 1_048_576;
const STATUS_TIMEOUT: Duration = Duration::from_secs(5);
const WATCHDOG_START_TIMEOUT: Duration = Duration::from_secs(2);

/// Initialize process-local diagnostics and dispatch the operational CLI.
pub async fn run(cli: Cli) -> Result<(), BootstrapError> {
    let _ = tracing_subscriber::fmt()
        .with_target(false)
        .with_ansi(false)
        .try_init();
    tracing::debug!(
        version = PACKAGE_VERSION,
        "Rust migration candidate initialized"
    );

    let config_path = cli.resolved_config_path();
    match cli.command {
        Some(Command::Version) => println!("{PACKAGE_VERSION}"),
        Some(Command::CheckConfig) => check_config(&config_path)?,
        Some(Command::Serve(args)) => serve(&config_path, &args).await?,
        Some(Command::Stop(args)) => stop(&config_path, args.timeout).await?,
        Some(Command::Restart(args)) => restart(&config_path, args.timeout).await?,
        Some(Command::Rehash { json }) => rehash(&config_path, json).await?,
        Some(Command::RuntimeStatus { json }) => runtime_status(&config_path, json).await?,
        Some(Command::Croncheck) => croncheck(),
        Some(Command::EnsureRunning) => ensure_running(&config_path).await?,
        Some(Command::Update(args)) => update(&config_path, args).await?,
        Some(Command::Connect(args)) => connect(&config_path, args).await?,
        Some(Command::Logout { target }) => logout(&config_path, target.as_deref()).await?,
        Some(Command::Edit) => edit(&config_path)?,
        Some(Command::Getkey) => getkey(&config_path)?,
        Some(Command::Newkey(args)) => newkey(&config_path, args.show_old).await?,
        Some(Command::InitConfig { target, force }) => {
            init_config(target.as_deref(), &config_path, force)?
        }
        Some(Command::Set { key, value }) => set_config(&config_path, &key, &value).await?,
        Some(Command::Dashboard(crate::cli::DashboardCommand::Public(args))) => {
            dashboard_public(
                &config_path,
                args.on.then_some(true).or(args.off.then_some(false)),
            )
            .await?
        }
        Some(Command::Onboard(args)) => onboard(&config_path, args).await?,
        Some(Command::Configsetup(command)) => configsetup(&config_path, command).await?,
        Some(Command::Migrate) => migrate(&config_path).await?,
        Some(Command::Db(crate::cli::DbCommand::Vacuum)) => vacuum(&config_path).await?,
        Some(Command::Backup { output_dir }) => backup(&config_path, output_dir).await?,
        Some(Command::Recover { source }) => recover(&config_path, source).await?,
        Some(Command::Accounts(command)) => accounts(&config_path, command).await?,
        Some(Command::Models(crate::cli::ModelsCommand::Refresh)) => {
            models_refresh(&config_path).await?
        }
        Some(Command::Modelinfo(command)) => modelinfo(&config_path, command).await?,
        Some(Command::Stats(command)) => stats(&config_path, command).await?,
        Some(Command::Deploy(args)) => deploy(&config_path, args).await?,
        Some(Command::Uninstall(args)) => uninstall(&config_path, args).await?,
        None => {
            println!("{}", crate::cli::help_text());
            println!("\nConfig file: {}", config_path.display());
        }
        Some(Command::Help) => println!("{}", crate::cli::help_text()),
    }
    Ok(())
}

async fn deploy(path: &Path, args: crate::cli::DeployArgs) -> Result<(), BootstrapError> {
    let Some(command) = args.command else {
        println!("Use `eggpool deploy systemd|cron|backup-cron|logrotate|all`.");
        return Ok(());
    };
    match command {
        crate::cli::DeployCommand::Systemd(args) => deploy_systemd(path, args).await,
        crate::cli::DeployCommand::Cron(args) => deploy_cron(path, args),
        crate::cli::DeployCommand::BackupCron(args) => deploy_backup_cron(path, args),
        crate::cli::DeployCommand::Logrotate(args) => deploy_logrotate(args),
        crate::cli::DeployCommand::All(args) => {
            deploy_systemd(
                path,
                crate::cli::DeploySystemdArgs {
                    install: args.install,
                    production: false,
                    as_root: false,
                },
            )
            .await?;
            deploy_logrotate(crate::cli::DeployLogrotateArgs {
                install: args.install,
            })?;
            deploy_cron(
                path,
                crate::cli::DeployCronArgs {
                    install: args.install,
                    uninstall: false,
                    interval: None,
                    user: None,
                },
            )?;
            println!(
                "Note: nightly backups are configured separately via `eggpool deploy backup-cron --install`."
            );
            Ok(())
        }
    }
}

async fn deploy_systemd(
    path: &Path,
    args: crate::cli::DeploySystemdArgs,
) -> Result<(), BootstrapError> {
    use crate::operations::deploy::{self as deployment, CommandRunner};

    let binary = deployment::resolve_rust_binary().map_err(deployment_error)?;
    let mut runner = deployment::SystemCommandRunner;
    if args.production {
        #[cfg(unix)]
        if args.install && !nix::unistd::geteuid().is_root() {
            return Err(command_error(
                EXIT_VALIDATION,
                "production deployment requires root privileges",
            ));
        }
        let config = PathBuf::from(deployment::PRODUCTION_CONFIG_DIR).join("config.toml");
        let unit = deployment::render_production_systemd(&deployment::ProductionSystemdSpec {
            binary: binary.clone(),
        });
        println!("EggPool production systemd unit:\n\n{unit}");
        if !args.install {
            return Ok(());
        }
        if !prompt_confirmation("Provision the production EggPool layout and start the service?")? {
            println!("Aborted.");
            return Ok(());
        }
        if Path::new(deployment::SYSTEMD_UNIT_PATH).exists() {
            let stop = vec!["stop".to_owned(), deployment::SERVICE_NAME.to_owned()];
            let _ = runner.run("systemctl", &stop, None);
        }
        if RuntimePaths::resolve().pid_file.exists() {
            stop(path, 10.0).await?;
        }
        let id_args = vec!["-u".to_owned(), "eggpool".to_owned()];
        let user_exists = runner
            .run("id", &id_args, None)
            .map_err(deployment_error)?
            .status
            == 0;
        if !user_exists {
            let useradd = vec![
                "-r".to_owned(),
                "-s".to_owned(),
                "/usr/sbin/nologin".to_owned(),
                "-d".to_owned(),
                deployment::PRODUCTION_DATA_DIR.to_owned(),
                "eggpool".to_owned(),
            ];
            deployment::run_required(&mut runner, "useradd", &useradd, None)
                .map_err(deployment_error)?;
        }
        for (directory, mode, owner) in [
            (deployment::PRODUCTION_DATA_DIR, 0o750, "eggpool:eggpool"),
            (deployment::PRODUCTION_LOG_DIR, 0o750, "eggpool:eggpool"),
            (deployment::PRODUCTION_BACKUP_DIR, 0o750, "eggpool:eggpool"),
            (deployment::PRODUCTION_CONFIG_DIR, 0o755, "root:eggpool"),
        ] {
            fs::create_dir_all(directory)
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
            fs::set_permissions(
                directory,
                std::os::unix::fs::PermissionsExt::from_mode(mode),
            )
            .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
            let chown = vec![owner.to_owned(), directory.to_owned()];
            deployment::run_required(&mut runner, "chown", &chown, None)
                .map_err(deployment_error)?;
        }
        if !config.exists() {
            let source = fs::read(path).map_err(|error| {
                command_error(
                    EXIT_VALIDATION,
                    format!("production config is missing and could not be seeded: {error}"),
                )
            })?;
            deployment::write_atomic(&config, &source, 0o640).map_err(deployment_error)?;
            let chown = vec!["root:eggpool".to_owned(), config.display().to_string()];
            deployment::run_required(&mut runner, "chown", &chown, None)
                .map_err(deployment_error)?;
            println!("Seeded {} from {}.", config.display(), path.display());
        }
        let env_path = PathBuf::from(deployment::PRODUCTION_CONFIG_DIR).join("env");
        if !env_path.exists() {
            deployment::write_atomic(
                &env_path,
                b"# Environment for the Rust EggPool candidate.\n",
                0o640,
            )
            .map_err(deployment_error)?;
            let chown = vec!["root:eggpool".to_owned(), env_path.display().to_string()];
            deployment::run_required(&mut runner, "chown", &chown, None)
                .map_err(deployment_error)?;
            println!("Seeded {}.", env_path.display());
        }
        deployment::validate_config(&config).map_err(deployment_error)?;
        deployment::write_atomic(
            Path::new(deployment::SYSTEMD_UNIT_PATH),
            unit.as_bytes(),
            0o644,
        )
        .map_err(deployment_error)?;
        for args in [
            vec!["daemon-reload".to_owned()],
            vec!["enable".to_owned(), deployment::SERVICE_NAME.to_owned()],
            vec!["start".to_owned(), deployment::SERVICE_NAME.to_owned()],
        ] {
            deployment::run_required(&mut runner, "systemctl", &args, None)
                .map_err(deployment_error)?;
        }
        println!("Production systemd service installed and started.");
        return Ok(());
    }

    let user = deployment::DeployUser::current();
    if args.install && user.direct_root && !args.as_root {
        return Err(command_error(
            EXIT_VALIDATION,
            "refusing to install a personal systemd unit as direct root; pass --as-root or --production",
        ));
    }
    let paths = RuntimePaths::resolve();
    let config = fs::canonicalize(path)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let env_file = crate::config::resolve_env_path(Some(&config));
    let unit = deployment::render_personal_systemd(&deployment::PersonalSystemdSpec {
        binary: binary.clone(),
        config: config.clone(),
        data_dir: paths.data_dir.clone(),
        env_file,
        home: user.home.clone(),
        user: user.name.clone(),
        group: user.group.clone(),
    });
    println!("EggPool personal systemd unit:\n\n{unit}");
    if !args.install {
        return Ok(());
    }
    if !prompt_confirmation("Install the personal EggPool systemd unit and start the service?")? {
        println!("Aborted.");
        return Ok(());
    }
    if paths.pid_file.exists() {
        stop(path, 10.0).await?;
    }
    let owner = format!("{}:{}", user.name, user.group);
    let directories = vec![
        (paths.config_dir, 0o700, owner.clone()),
        (paths.data_dir, 0o750, owner.clone()),
        (paths.state_dir, 0o700, owner.clone()),
    ];
    deployment::install_systemd(
        &mut runner,
        Path::new(deployment::SYSTEMD_UNIT_PATH),
        &unit,
        &config,
        &directories,
        true,
    )
    .map_err(deployment_error)?;
    println!("Personal systemd service installed and started.");
    Ok(())
}

fn deploy_cron(path: &Path, args: crate::cli::DeployCronArgs) -> Result<(), BootstrapError> {
    use crate::operations::deploy as deployment;

    let binary = deployment::resolve_rust_binary().map_err(deployment_error)?;
    let config = fs::canonicalize(path)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let paths = RuntimePaths::resolve();
    let interval = args.interval.unwrap_or(5);
    let block = deployment::render_watchdog_cron(&binary, &config, &paths.log_file, interval)
        .map_err(deployment_error)?;
    let user = args.user.unwrap_or_else(|| {
        env::var("SUDO_USER")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| env::var("USER").ok())
            .unwrap_or_else(|| "root".to_owned())
    });
    if args.install == args.uninstall {
        println!("EggPool watchdog cron:\n\n{block}");
        return Ok(());
    }
    if args.install {
        if !prompt_confirmation(&format!("Install watchdog cron for user {user}?"))? {
            println!("Aborted.");
            return Ok(());
        }
        let mut runner = deployment::SystemCommandRunner;
        deployment::install_cron_block(&mut runner, &user, &block).map_err(deployment_error)?;
        println!("Watchdog cron installed for {user}.");
    } else {
        let mut runner = deployment::SystemCommandRunner;
        deployment::uninstall_cron_blocks(&mut runner, &user).map_err(deployment_error)?;
        println!("EggPool cron blocks removed from {user}'s crontab.");
    }
    Ok(())
}

fn deploy_backup_cron(
    path: &Path,
    args: crate::cli::DeployBackupCronArgs,
) -> Result<(), BootstrapError> {
    use crate::operations::deploy as deployment;

    let binary = deployment::resolve_rust_binary().map_err(deployment_error)?;
    let config = fs::canonicalize(path)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let block = deployment::render_backup_cron(&binary, &config, args.production);
    let script = deployment::render_backup_script(&binary, &config);
    if args.install == args.uninstall {
        println!("EggPool backup cron:\n\n{block}");
        return Ok(());
    }
    let mut runner = deployment::SystemCommandRunner;
    let user = args.user.unwrap_or_else(|| {
        env::var("SUDO_USER")
            .or_else(|_| env::var("USER"))
            .unwrap_or_else(|_| "root".to_owned())
    });
    if args.production {
        #[cfg(unix)]
        if !nix::unistd::geteuid().is_root() {
            return Err(command_error(
                EXIT_VALIDATION,
                "production backup cron requires root privileges",
            ));
        }
        if args.install {
            deployment::write_atomic(
                Path::new(deployment::BACKUP_SCRIPT_PATH),
                script.as_bytes(),
                0o755,
            )
            .map_err(deployment_error)?;
            deployment::write_atomic(
                Path::new(deployment::PRODUCTION_CRON_PATH),
                block.as_bytes(),
                0o644,
            )
            .map_err(deployment_error)?;
            println!("Production backup cron installed.");
        } else {
            deployment::remove_artifact(Path::new(deployment::PRODUCTION_CRON_PATH))
                .map_err(deployment_error)?;
            deployment::remove_artifact(Path::new(deployment::BACKUP_SCRIPT_PATH))
                .map_err(deployment_error)?;
            println!("Production backup cron removed.");
        }
    } else if args.install {
        if !prompt_confirmation(&format!("Install backup cron for user {user}?"))? {
            println!("Aborted.");
            return Ok(());
        }
        deployment::install_cron_block(&mut runner, &user, &block).map_err(deployment_error)?;
        println!("Backup cron installed for {user}.");
    } else {
        deployment::uninstall_cron_blocks(&mut runner, &user).map_err(deployment_error)?;
        println!("EggPool backup cron blocks removed from {user}'s crontab.");
    }
    Ok(())
}

fn deploy_logrotate(args: crate::cli::DeployLogrotateArgs) -> Result<(), BootstrapError> {
    use crate::operations::deploy as deployment;

    let content = deployment::render_logrotate(Path::new(deployment::PRODUCTION_LOG_DIR));
    println!("EggPool logrotate configuration:\n\n{content}");
    if !args.install {
        return Ok(());
    }
    if !prompt_confirmation("Install the EggPool logrotate configuration?")? {
        println!("Aborted.");
        return Ok(());
    }
    let mut runner = deployment::SystemCommandRunner;
    if let Err(error) = deployment::install_logrotate(
        &mut runner,
        Path::new(deployment::LOGROTATE_PATH),
        &content,
        true,
    ) {
        if matches!(error, deployment::DeployError::Command { ref program, .. } if program == "logrotate")
            && !command_exists("logrotate")
        {
            eprintln!(
                "Warning: logrotate is not installed; configuration was written but not validated."
            );
            return Ok(());
        }
        return Err(deployment_error(error));
    }
    println!("Logrotate configuration installed.");
    Ok(())
}

async fn uninstall(path: &Path, args: crate::cli::UninstallArgs) -> Result<(), BootstrapError> {
    use crate::operations::deploy as deployment;

    let binary = deployment::resolve_rust_binary().map_err(deployment_error)?;
    if fs::symlink_metadata(path)
        .map(|metadata| metadata.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(command_error(
            EXIT_VALIDATION,
            "refusing to uninstall through a symlinked configuration path",
        ));
    }
    let config = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
    let production_config =
        PathBuf::from(crate::operations::deploy::PRODUCTION_CONFIG_DIR).join("config.toml");
    let production = config == production_config;
    let runtime_paths = RuntimePaths::resolve();
    let targets = deployment::UninstallTargets {
        binary,
        config: config.clone(),
        config_dir: production.then(|| PathBuf::from(deployment::PRODUCTION_CONFIG_DIR)),
        env: if production {
            Some(PathBuf::from(deployment::PRODUCTION_CONFIG_DIR).join("env"))
        } else {
            crate::config::resolve_env_path(Some(&config))
        },
        data_dir: if production {
            PathBuf::from(deployment::PRODUCTION_DATA_DIR)
        } else {
            runtime_paths.data_dir
        },
        state_dir: if production {
            PathBuf::from(deployment::PRODUCTION_LOG_DIR)
        } else {
            runtime_paths.state_dir
        },
        backup_dir: production.then(|| PathBuf::from(deployment::PRODUCTION_BACKUP_DIR)),
        systemd_unit: PathBuf::from(deployment::SYSTEMD_UNIT_PATH),
        logrotate: PathBuf::from(deployment::LOGROTATE_PATH),
        production_cron: PathBuf::from(deployment::PRODUCTION_CRON_PATH),
        backup_script: PathBuf::from(deployment::BACKUP_SCRIPT_PATH),
        shell_rc_files: home_rc_files(),
    };
    println!("EggPool uninstall targets:");
    println!("  binary: {}", targets.binary.display());
    println!("  config: {}", targets.config.display());
    println!("  data:   {}", targets.data_dir.display());
    if !args.yes && !prompt_confirmation("Remove the Rust EggPool installation and selected data?")?
    {
        println!("Aborted.");
        return Ok(());
    }
    let keep = deployment::KeepFlags {
        data: args.keep_data,
        config: args.keep_config,
        path: args.keep_path,
        deploy_artifacts: !args.deploy_artifacts,
    };
    let mut runner = deployment::SystemCommandRunner;
    let leftovers =
        deployment::uninstall(&mut runner, &targets, keep, true).map_err(deployment_error)?;
    if leftovers.is_empty() {
        println!("Rust EggPool uninstall completed; known targets are absent.");
    } else {
        eprintln!("Uninstall completed with leftovers; remove these after reviewing permissions:");
        for path in leftovers {
            eprintln!("  {}", path.display());
        }
    }
    Ok(())
}

fn deployment_error(error: crate::operations::deploy::DeployError) -> BootstrapError {
    command_error(EXIT_VALIDATION, error.to_string())
}

fn prompt_confirmation(message: &str) -> Result<bool, BootstrapError> {
    print!("{message} [y/N] ");
    io::stdout()
        .flush()
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let mut answer = String::new();
    io::stdin()
        .read_line(&mut answer)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    Ok(matches!(
        answer.trim().to_ascii_lowercase().as_str(),
        "y" | "yes"
    ))
}

fn command_exists(name: &str) -> bool {
    env::var_os("PATH")
        .map(|path| env::split_paths(&path).any(|directory| directory.join(name).is_file()))
        .unwrap_or(false)
}

fn home_rc_files() -> Vec<PathBuf> {
    let Some(home) = env::var_os("HOME").map(PathBuf::from) else {
        return Vec::new();
    };
    [".zshrc", ".bashrc", ".bash_profile", ".profile"]
        .into_iter()
        .map(|name| home.join(name))
        .filter(|path| path.is_file())
        .collect()
}

async fn open_maintenance_database(
    config: &config::Config,
    require_existing: bool,
) -> Result<crate::db::Database, BootstrapError> {
    let path = config::Config::runtime_path(&config.database.path);
    if require_existing && !path.is_file() {
        return Err(command_error(
            EXIT_VALIDATION,
            format!("database not found: {}", path.display()),
        ));
    }
    let mut database_config = crate::db::DatabaseConfig::from(&config.database);
    database_config.path = path.display().to_string();
    crate::db::Database::open(database_config)
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))
}

async fn open_operational_database(
    config: &config::Config,
    require_existing: bool,
) -> Result<crate::db::Database, BootstrapError> {
    let database = open_maintenance_database(config, require_existing).await?;
    if let Err(error) = crate::db::MigrationRunner::new(&database).run().await {
        let _ = database.close().await;
        return Err(command_error(EXIT_VALIDATION, error.to_string()));
    }
    Ok(database)
}

async fn accounts(path: &Path, command: crate::cli::AccountsCommand) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    match command {
        crate::cli::AccountsCommand::List => {
            print_accounts_human(&crate::operations::operator::account_list(&config), false);
        }
        crate::cli::AccountsCommand::Status => {
            print_accounts_human(&crate::operations::operator::account_status(&config), true);
        }
        crate::cli::AccountsCommand::Explain(args) => {
            let model = args
                .model
                .as_deref()
                .ok_or_else(|| command_error(EXIT_VALIDATION, "the --model option is required"))?;
            let database = open_operational_database(&config, false).await?;
            let result = crate::operations::operator::explain_accounts(
                &config,
                &database,
                model,
                args.provider.as_deref(),
                args.protocol.as_deref(),
                args.scores,
                args.gates,
            )
            .await
            .map_err(|error| command_error(EXIT_VALIDATION, error));
            let close = database.close().await;
            let value = result;
            close.map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
            let value = value?;
            print_account_explain_human(&value);
        }
    }
    Ok(())
}

fn print_accounts_human(rows: &[Value], status: bool) {
    if rows.is_empty() {
        println!(
            "{}",
            if status {
                "No provider accounts configured."
            } else {
                "No configured accounts. Run `eggpool connect` to add one."
            }
        );
        return;
    }
    if !status {
        println!("Configured accounts:");
        for row in rows {
            println!(
                "  {}/{}",
                row["provider"].as_str().unwrap_or_default(),
                row["name"].as_str().unwrap_or_default()
            );
        }
        println!("\nTotal: {} accounts", rows.len());
        return;
    }
    for row in rows {
        println!(
            "  {}: provider={}, priority={}, enabled={}, weight={}, api_key_env={} (set={})",
            row["name"].as_str().unwrap_or_default(),
            row["provider"].as_str().unwrap_or_default(),
            row["routing_priority"].as_i64().unwrap_or_default(),
            row["enabled"].as_bool().unwrap_or(false),
            row["weight"].as_f64().unwrap_or_default(),
            row["api_key_env"].as_str().unwrap_or_default(),
            if row["credential_configured"].as_bool().unwrap_or(false) {
                "yes"
            } else {
                "no"
            }
        );
    }
    println!("\nTotal accounts: {}", rows.len());
}

fn print_account_explain_human(value: &Value) {
    println!(
        "Account eligibility for model {:?}:",
        value["model_id"].as_str().unwrap_or_default()
    );
    let Some(accounts) = value["accounts"].as_array() else {
        return;
    };
    println!("  Account                 Eligible  Reason");
    println!("  ----------------------  --------  ------------------------------");
    for account in accounts {
        println!(
            "  {:<22}  {:<8}  {}",
            account["name"].as_str().unwrap_or_default(),
            if account["eligible"].as_bool().unwrap_or(false) {
                "yes"
            } else {
                "no"
            },
            account["reason_code"].as_str().unwrap_or_default()
        );
        if let Some(gates) = account["gates"].as_object() {
            let rendered = gates
                .iter()
                .map(|(key, value)| format!("{key}={}", value))
                .collect::<Vec<_>>()
                .join(", ");
            println!("    gates: {rendered}");
        }
    }
    if let Some(candidates) = value["candidates"].as_array() {
        for candidate in candidates {
            if let Some(score) = candidate.get("score") {
                println!(
                    "    score {}: {}",
                    candidate["account_name"].as_str().unwrap_or_default(),
                    score
                );
            }
        }
    }
}

async fn models_refresh(path: &Path) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let database = open_operational_database(&config, false).await?;
    let result = crate::operations::operator::refresh_catalog(&config, path, &database)
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error));
    let close = database.close().await;
    let summary = result;
    close.map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let summary = summary?;
    println!(
        "Refreshed catalog: {} models found ({} new, {} withdrawn; accounts: {} succeeded, {} failed, {} skipped)",
        summary.model_count,
        summary.new_model_count,
        summary.withdrawn_model_count,
        summary.successful_accounts,
        summary.failed_accounts,
        summary.skipped_accounts
    );
    Ok(())
}

async fn modelinfo(
    path: &Path,
    command: crate::cli::ModelInfoCommand,
) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    if let crate::cli::ModelInfoCommand::List {
        status: Some(status),
    } = &command
    {
        crate::operations::operator::validate_model_info_status(status)
            .map_err(|error| command_error(EXIT_VALIDATION, error))?;
    }
    let database = open_operational_database(&config, false).await?;
    let result: Result<(&str, Value), String> = async {
        match command {
        crate::cli::ModelInfoCommand::Aliases { model_id, source } => {
            crate::operations::operator::list_aliases(&database, &model_id, source.as_deref())
                .await
                .map(|rows| ("aliases", Value::Array(rows)))
                .map_err(|error| error.to_string())
        }
        crate::cli::ModelInfoCommand::List { status } => {
            crate::operations::operator::list_model_info(&database, status.as_deref())
                .await
                .map(|rows| ("model_info", Value::Array(rows)))
                .map_err(|error| error.to_string())
        }
        crate::cli::ModelInfoCommand::Show { model_id } => {
            crate::operations::operator::show_model_info(&database, &model_id)
                .await
                .map(|row| ("model_info", row.unwrap_or(Value::Null)))
                .map_err(|error| error.to_string())
        }
        crate::cli::ModelInfoCommand::Refresh { .. } => {
            let catalog = crate::operations::operator::refresh_catalog(&config, path, &database)
                .await
                ?;
            let (created, updated) =
                crate::operations::operator::refresh_model_info_from_catalog(&config, &database)
                    .await
                    .map_err(|error| error.to_string())?;
            let aliases = crate::operations::operator::seed_configured_aliases(&config, &database)
                .await
                .map_err(|error| error.to_string())?;
            Ok((
                "refresh",
                json!({"catalog": catalog, "canonical_created": created, "canonical_updated": updated, "aliases": aliases}),
            ))
        }
        crate::cli::ModelInfoCommand::Repair { limit } => {
            let (scanned, upgraded, skipped, errors) =
                crate::operations::operator::repair_model_info(&database, limit.unwrap_or(200))
                    .await
                    .map_err(|error| error.to_string())?;
            Ok((
                "repair",
                json!({"scanned": scanned, "upgraded": upgraded, "skipped": skipped, "errors": errors}),
            ))
        }
        }
    }
    .await;
    let close = database.close().await;
    let result = result;
    close.map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let (kind, value) = result.map_err(|error| command_error(EXIT_VALIDATION, error))?;
    if kind == "aliases" && value.as_array().is_some_and(Vec::is_empty) {
        return Err(command_error(EXIT_VALIDATION, "no aliases found"));
    }
    if kind == "model_info" && value.as_array().is_some_and(Vec::is_empty) {
        println!("No model-info rows found.");
    } else if kind == "model_info" && value.is_null() {
        return Err(command_error(EXIT_VALIDATION, "model-info row not found"));
    } else {
        print_modelinfo_human(kind, &value);
    }
    Ok(())
}

fn print_modelinfo_human(kind: &str, value: &Value) {
    match kind {
        "aliases" => {
            println!("Aliases:");
            if let Some(rows) = value.as_array() {
                for row in rows {
                    println!(
                        "  {}  {}  provider={} active={} confidence={}",
                        row["source"].as_str().unwrap_or_default(),
                        row["alias"].as_str().unwrap_or_default(),
                        row["provider_id"].as_str().unwrap_or("—"),
                        row["active"].as_bool().unwrap_or(false),
                        row["confidence"]
                    );
                }
            }
        }
        "model_info" if value.is_array() => {
            println!(
                "Model ID                                             Status           Sparse  Summary"
            );
            println!("{}", "-".repeat(110));
            if let Some(rows) = value.as_array() {
                for row in rows {
                    println!(
                        "{:<50} {:<16} {:<7} {}",
                        row["model_id"].as_str().unwrap_or_default(),
                        row["status"].as_str().unwrap_or_default(),
                        if row["sparse"].as_bool().unwrap_or(false) {
                            "yes"
                        } else {
                            "no"
                        },
                        row["summary"].as_str().unwrap_or_default()
                    );
                }
                println!("\nTotal: {}", rows.len());
            }
        }
        "model_info" if value.is_object() => {
            println!("Model: {}", value["model_id"].as_str().unwrap_or_default());
            println!("Status: {}", value["status"].as_str().unwrap_or_default());
            println!("Sparse: {}", value["sparse"].as_bool().unwrap_or(false));
            if let Some(summary) = value["summary"].as_str() {
                println!("Summary: {summary}");
            }
            println!("Detail: {}", value["detail"]);
            println!("Provenance: {}", value["provenance"]);
            if value["conflicts"]
                .as_object()
                .is_some_and(|object| !object.is_empty())
            {
                println!("Conflicts: {}", value["conflicts"]);
            }
            println!("First seen: {}", value["first_seen_at"]);
            println!("Last seen: {}", value["last_seen_at"]);
        }
        "refresh" => {
            println!("Refreshing provider catalog observations...");
            println!("  Catalog: {}", value["catalog"]);
            println!("  Canonical created: {}", value["canonical_created"]);
            println!("  Canonical updated: {}", value["canonical_updated"]);
            println!("  Aliases created: {}", value["aliases"]);
            println!("Done.");
        }
        "repair" => {
            println!("Running legacy detail backfill...");
            println!("  Scanned: {}", value["scanned"]);
            println!("  Upgraded: {}", value["upgraded"]);
            println!("  Skipped: {}", value["skipped"]);
            println!("  Errors: {}", value["errors"]);
            println!("Done.");
        }
        _ => println!("{value}"),
    }
}

async fn stats(path: &Path, command: crate::cli::StatsCommand) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let database = open_operational_database(&config, false).await?;
    let json_output = match &command {
        crate::cli::StatsCommand::Transcoding(args) => args.json,
        crate::cli::StatsCommand::ExplainDashboard(args) => args.json,
        crate::cli::StatsCommand::RecomputeCosts(_) | crate::cli::StatsCommand::RepairCosts(_) => {
            false
        }
    };
    let apply = match &command {
        crate::cli::StatsCommand::RecomputeCosts(args) => args.apply,
        crate::cli::StatsCommand::RepairCosts(args) => args.apply,
        crate::cli::StatsCommand::Transcoding(_)
        | crate::cli::StatsCommand::ExplainDashboard(_) => false,
    };
    let result: Result<(&str, Value), String> = match command {
        crate::cli::StatsCommand::Transcoding(args) => {
            let period = args.period.unwrap_or_else(|| "24h".into());
            crate::operations::operator::transcoding_stats(
                &database,
                &period,
            )
            .await
            .map(|value| {
                let mut object = value.as_object().cloned().unwrap_or_default();
                object.insert("period".into(), Value::String(period));
                ("transcoding", Value::Object(object))
            })
            .map_err(|error| error.to_string())
        }
        crate::cli::StatsCommand::ExplainDashboard(args) => {
            let period = args.period.as_deref().unwrap_or("24h").to_owned();
            let bucket = args.bucket.as_deref().unwrap_or("hour").to_owned();
            let group_by = args
                .group_by
                .as_deref()
                .unwrap_or("provider_model")
                .to_owned();
            crate::operations::operator::explain_dashboard(
                &database,
                &period,
                &bucket,
                &group_by,
            )
            .await
            .map(|rows| ("explain-dashboard", json!({"period": period, "bucket": bucket, "group_by": group_by, "queries": rows})))
            .map_err(|error| error.to_string())
        }
        crate::cli::StatsCommand::RecomputeCosts(args) => crate::operations::operator::recompute_costs(
            &database,
            args.limit,
            args.apply,
        )
        .await
        .map(|summary| ("recompute-costs", json!({"scanned": summary.scanned, "updated": summary.updated, "skipped": summary.skipped, "skipped_no_snapshot": summary.skipped_no_snapshot, "skipped_missing_tokens": summary.skipped_missing_tokens, "old_total": summary.old_total, "new_total": summary.new_total, "changes": summary.changes})))
        .map_err(|error| error.to_string()),
        crate::cli::StatsCommand::RepairCosts(args) => crate::operations::operator::repair_costs(
            &database,
            args.provider.as_deref(),
            args.since.as_deref(),
            args.limit,
            args.apply,
        )
        .await
        .map(|summary| ("repair-costs", json!({"scanned": summary.scanned, "suspicious": summary.suspicious, "repaired": summary.repaired, "skipped_provider_reported": summary.skipped_provider_reported, "unchanged": summary.unchanged, "old_total": summary.old_total, "proposed_total": summary.proposed_total, "changes": summary.changes, "breakdown": summary.breakdown})))
        .map_err(|error| error.to_string()),
    };
    let close = database.close().await;
    let result = result;
    close.map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let (kind, value) = result.map_err(|error| command_error(EXIT_VALIDATION, error))?;
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&value)
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?
        );
    } else {
        print_stats_human(kind, &value, apply);
    }
    Ok(())
}

fn print_stats_human(kind: &str, value: &Value, apply: bool) {
    match kind {
        "transcoding" => {
            println!("Period: {}", value["period"].as_str().unwrap_or("24h"));
            println!("Total requests: {}", value["total"].as_i64().unwrap_or(0));
            println!(
                "Native (no transcoding): {}",
                value["native_count"].as_i64().unwrap_or(0)
            );
            println!(
                "Transcoded: {}",
                value["transcoded_count"].as_i64().unwrap_or(0)
            );
            if let Some(directions) = value["per_direction"].as_object()
                && !directions.is_empty()
            {
                println!("\nDirection                         Count");
                println!("-------------------------------- ------");
                for (direction, count) in directions {
                    println!("{direction:<32} {:>6}", count.as_i64().unwrap_or_default());
                }
            }
        }
        "explain-dashboard" => {
            println!(
                "Period: {}  Bucket: {}  Group-by: {}",
                value["period"].as_str().unwrap_or("24h"),
                value["bucket"].as_str().unwrap_or("hour"),
                value["group_by"].as_str().unwrap_or("provider_model")
            );
            println!("{}", "─".repeat(70));
            if let Some(queries) = value["queries"].as_array() {
                for query in queries {
                    println!("{}:", query["name"].as_str().unwrap_or("query"));
                    if let Some(plan) = query["plan"].as_array() {
                        for line in plan.iter().filter_map(Value::as_str) {
                            println!("  {line}");
                        }
                    }
                    println!();
                }
            }
        }
        "recompute-costs" => {
            println!(
                "{}: scanned {} rows, updated {}, skipped {}",
                if apply { "APPLY" } else { "DRY-RUN" },
                value["scanned"].as_u64().unwrap_or_default(),
                value["updated"].as_u64().unwrap_or_default(),
                value["skipped"].as_u64().unwrap_or_default()
            );
            print_cost_changes(value);
        }
        "repair-costs" => {
            println!(
                "{}: scanned {} rows, flagged {} suspicious, repaired {}, skipped {} provider-reported, unchanged {}",
                if apply { "APPLY" } else { "DRY-RUN" },
                value["scanned"].as_u64().unwrap_or_default(),
                value["suspicious"].as_u64().unwrap_or_default(),
                value["repaired"].as_u64().unwrap_or_default(),
                value["skipped_provider_reported"]
                    .as_u64()
                    .unwrap_or_default(),
                value["unchanged"].as_u64().unwrap_or_default()
            );
            println!(
                "old total {} μ$  proposed total {} μ$",
                value["old_total"].as_i64().unwrap_or_default(),
                value["proposed_total"].as_i64().unwrap_or_default()
            );
            print_cost_changes(value);
        }
        _ => println!("{}", value),
    }
}

fn print_cost_changes(value: &Value) {
    if let Some(changes) = value["changes"].as_array() {
        for change in changes {
            println!(
                "  {} / {}: {} → {}",
                change["model_id"].as_str().unwrap_or_default(),
                change["provider_id"].as_str().unwrap_or_default(),
                change["old_cost"].as_i64().unwrap_or_default(),
                change["new_cost"].as_i64().unwrap_or_default()
            );
        }
    }
}

async fn migrate(path: &Path) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let database = open_maintenance_database(&config, false).await?;
    let result = crate::db::MigrationRunner::new(&database).run().await;
    let close = database.close().await;
    match (result, close) {
        (Ok(state), Ok(())) => {
            println!(
                "Migrations completed successfully\n  Applied migrations: {}\n  Current schema: {}",
                state.applied_this_run.len(),
                state.applied_versions.last().copied().unwrap_or_default()
            );
            Ok(())
        }
        (Err(error), _) | (Ok(_), Err(error)) => {
            Err(command_error(EXIT_VALIDATION, error.to_string()))
        }
    }
}

async fn vacuum(path: &Path) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let database = open_maintenance_database(&config, true).await?;
    let result = database.vacuum().await;
    let close = database.close().await;
    match (result, close) {
        (Ok(()), Ok(())) => {
            println!("Database vacuum completed successfully");
            Ok(())
        }
        (Err(error), _) | (Ok(()), Err(error)) => {
            Err(command_error(EXIT_VALIDATION, error.to_string()))
        }
    }
}

async fn backup(path: &Path, output_dir: Option<PathBuf>) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let mut service = BackupService::from_config(path, &config);
    if let Some(output_dir) = output_dir {
        service = service.with_output_dir(output_dir);
    }
    let database = open_maintenance_database(&config, true).await?;
    let result = service.create(&database).await;
    let close = database.close().await;
    let result = match result {
        Ok(result) => {
            if let Err(error) = close {
                return Err(command_error(EXIT_VALIDATION, error.to_string()));
            }
            result
        }
        Err(error) => return Err(command_error(EXIT_VALIDATION, error.to_string())),
    };
    println!("Wrote backup: {}", result.archive.display());
    for member in result.members {
        println!("  included: {member}");
    }
    Ok(())
}

async fn recover(path: &Path, source: Option<PathBuf>) -> Result<(), BootstrapError> {
    let archive = match source {
        Some(source) => {
            let source =
                if let Some(tail) = source.to_str().and_then(|value| value.strip_prefix("~/")) {
                    std::env::var_os("HOME")
                        .map(PathBuf::from)
                        .unwrap_or_default()
                        .join(tail)
                } else {
                    source
                };
            if source.is_absolute() {
                source
            } else {
                backup::default_backup_dir().join(source)
            }
        }
        None => {
            let entries = backup::list_backups(&backup::default_backup_dir())
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
            if entries.is_empty() {
                println!("No backups found in the default backup directory.");
                return Ok(());
            }
            println!("Available backups:");
            for (index, entry) in entries.iter().enumerate() {
                let size = fs::metadata(entry)
                    .map(|meta| meta.len())
                    .unwrap_or_default();
                println!("  {}. {} ({} bytes)", index + 1, entry.display(), size);
            }
            print!("Select a backup (blank to cancel): ");
            io::stdout()
                .flush()
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
            let mut answer = String::new();
            io::stdin()
                .read_line(&mut answer)
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
            let Ok(index) = answer.trim().parse::<usize>() else {
                println!("Aborted.");
                return Ok(());
            };
            entries
                .get(index.saturating_sub(1))
                .cloned()
                .ok_or_else(|| command_error(EXIT_VALIDATION, "invalid backup selection"))?
        }
    };
    if !archive.is_file() {
        return Err(command_error(
            EXIT_VALIDATION,
            format!("backup not found: {}", archive.display()),
        ));
    }
    backup::BackupService::validate_archive(&archive)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    print!("Overwrite current configuration and database? [y/N] ");
    io::stdout()
        .flush()
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    let mut answer = String::new();
    io::stdin()
        .read_line(&mut answer)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    if !matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes") {
        println!("Aborted.");
        return Ok(());
    }
    // Stop before replacement. The recovery service itself never touches a
    // running database and does not restart the process after success.
    let paths = RuntimePaths::resolve();
    if paths.pid_file.is_file() {
        stop(path, 10.0).await?;
    }
    let config = config::Config::from_toml(path).ok();
    let service = config
        .as_ref()
        .map(|config| BackupService::from_config(path, config))
        .unwrap_or_else(|| BackupService {
            paths: backup::BackupPaths {
                config: path.to_owned(),
                database: PathBuf::new(),
                env: None,
                output_dir: backup::default_backup_dir(),
            },
            include_env: true,
            install_method: "rust".to_owned(),
        });
    let restored = service
        .recover(&archive)
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    println!("Restore complete. Restart the server to load the new config.");
    println!(
        "  config: {}\n  db: {}",
        restored.config.display(),
        restored.database.display()
    );
    Ok(())
}

async fn configsetup(
    path: &Path,
    command: crate::cli::ConfigsetupCommand,
) -> Result<(), BootstrapError> {
    use crate::operations::integrations::{self, SnippetOptions, Target};

    let (target, args) = match command {
        crate::cli::ConfigsetupCommand::Opencode => (Target::Opencode, None),
        crate::cli::ConfigsetupCommand::ClaudeCode => (Target::ClaudeCode, None),
        crate::cli::ConfigsetupCommand::Aider(args) => (Target::Aider, Some(args)),
        crate::cli::ConfigsetupCommand::Codex(args) => (Target::Codex, Some(args)),
        crate::cli::ConfigsetupCommand::QwenCode(args) => (Target::QwenCode, Some(args)),
        crate::cli::ConfigsetupCommand::Kilo(args) => (Target::Kilo, Some(args)),
        crate::cli::ConfigsetupCommand::Continue(args) => (Target::Continue, Some(args)),
        crate::cli::ConfigsetupCommand::Cline(args) => (Target::Cline, Some(args)),
        crate::cli::ConfigsetupCommand::RooCode(args) => (Target::RooCode, Some(args)),
        crate::cli::ConfigsetupCommand::Goose(args) => (Target::Goose, Some(args)),
        crate::cli::ConfigsetupCommand::Openhands(args) => (Target::Openhands, Some(args)),
    };

    let mut context = if target == Target::ClaudeCode {
        integrations::build_endpoint_context(path)
            .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?
    } else {
        integrations::build_integration_context(path)
            .await
            .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?
    };
    if context.config_mutated {
        eprintln!("Generated new server API key.");
    }

    if target == Target::Opencode {
        let snippet = integrations::render_target(target, &context, None)
            .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        let delivery = integrations::deliver(
            &snippet,
            target,
            &SnippetOptions {
                print_secret: true,
                no_clipboard: false,
                force: false,
                output: None,
                write: false,
            },
            None,
            None,
        )
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        if let Some(stdout) = delivery.stdout {
            println!("{stdout}");
        }
        if !delivery
            .messages
            .iter()
            .any(|message| message == "Copied config to clipboard.")
        {
            eprintln!("Could not copy to clipboard. Use the printed config above.");
        }
        for message in delivery.messages {
            eprintln!("{message}");
        }
        if !context.models.is_empty() {
            eprintln!("Generated config with {} models.", context.models.len());
        } else {
            eprintln!(
                "Generated provider connection block (no model limits). Run 'eggpool models refresh' to populate model metadata."
            );
        }
        if context.config_mutated || context.transcoder_mutated {
            restart_after_integration_mutation(path).await?;
        }
        eprintln!("Paste into ~/.config/opencode/opencode.json.");
        return Ok(());
    }

    if target == Target::ClaudeCode {
        let snippet = integrations::render_target(target, &context, None)
            .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        let delivery = integrations::deliver(
            &snippet,
            target,
            &SnippetOptions {
                print_secret: false,
                no_clipboard: false,
                force: false,
                output: None,
                write: false,
            },
            None,
            Some("Paste into ~/.claude/settings.json or pass via --api-key and --base-url to the Claude Code CLI."),
        )
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        let copied = delivery
            .messages
            .iter()
            .any(|message| message == "Copied config to clipboard.");
        if !copied {
            eprintln!(
                "Could not copy to clipboard. Use `eggpool getkey` and pass --api-key to the Claude Code CLI."
            );
        }
        for message in delivery.messages {
            if !message.starts_with("Secret not printed") {
                eprintln!("{message}");
            }
        }
        return Ok(());
    }

    if let Some(args) = args {
        context =
            integrations::apply_overrides(context, args.host.as_deref(), args.base_url.as_deref())
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        let model = integrations::resolve_model(
            target,
            args.model.as_deref(),
            &context,
            args.write || args.output.is_some(),
        )
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        let snippet = integrations::render_target(target, &context, model.as_deref())
            .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        let delivery = integrations::deliver(
            &snippet,
            target,
            &SnippetOptions {
                print_secret: args.print_secret,
                no_clipboard: args.no_clipboard,
                force: args.force,
                output: args.output,
                write: args.write,
            },
            integrations::default_path(target).as_deref(),
            integrations::paste_hint(target),
        )
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        if let Some(stdout) = delivery.stdout {
            println!("{stdout}");
        }
        for message in delivery.messages {
            eprintln!("{message}");
        }
        if context.config_mutated || context.transcoder_mutated {
            restart_after_integration_mutation(path).await?;
        }
    }
    Ok(())
}

async fn restart_after_integration_mutation(path: &Path) -> Result<(), BootstrapError> {
    match config_mutation::apply_after_mutation(path, ApplyMode::RestartIfRunning)
        .await
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?
    {
        ApplyOutcome::Restarted => eprintln!("Server restarted to apply generated config."),
        ApplyOutcome::ServerNotRunning => {
            eprintln!("Start or restart the server to apply generated config.")
        }
        outcome => render_apply(outcome),
    }
    Ok(())
}

fn command_error(code: u8, detail: impl Into<String>) -> BootstrapError {
    BootstrapError::Command {
        code,
        detail: detail.into(),
    }
}

fn check_config(path: &Path) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    let digest = config::content_digest(path)?;
    println!(
        "Configuration loaded successfully from {}\n  Server: {}:{}\n  Accounts: {}\n  Database: {}\n  Content digest: {}",
        path.display(),
        config.server.host,
        config.server.port,
        config.all_accounts().len(),
        config.database.path,
        digest
    );
    Ok(())
}

fn mutation_error(error: config_mutation::MutationError, code: u8) -> BootstrapError {
    command_error(code, error.to_string())
}

fn render_apply(outcome: ApplyOutcome) {
    match outcome {
        ApplyOutcome::ServerNotRunning => println!("Server is not running."),
        ApplyOutcome::RehashApplied => println!("Configuration applied live."),
        ApplyOutcome::RehashNoop => println!("Configuration unchanged."),
        ApplyOutcome::RestartRequired(paths) => {
            println!("Restart required for: {}", paths.join(", "));
        }
        ApplyOutcome::ControlUnavailable => println!(
            "Server is healthy but the control socket is unavailable.\n  Check socket permissions, or restart manually with `eggpool restart`."
        ),
        ApplyOutcome::RehashFailed(message) => {
            println!("Configuration was written; live apply failed: {message}")
        }
        ApplyOutcome::Restarted => println!("Server restarted."),
    }
}

async fn connect(path: &Path, args: crate::cli::ConnectArgs) -> Result<(), BootstrapError> {
    if let Some(crate::cli::ConnectCommand::List) = args.command {
        let templates = config_mutation::load_provider_templates(args.providers.as_deref())
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
        let config = config::Config::from_toml(path).ok();
        println!("Available providers:");
        for (id, template) in templates {
            let marker = if template.recommended { "*" } else { " " };
            let priority = config
                .as_ref()
                .and_then(|value| value.providers.get(&id))
                .map_or_else(
                    || {
                        if matches!(template.status.as_str(), "verified" | "experimental") {
                            " (priority 0)".to_owned()
                        } else {
                            String::new()
                        }
                    },
                    |provider| format!(" (priority {})", provider.routing_priority),
                );
            let notes = if template.notes.is_empty() {
                String::new()
            } else {
                format!(" — {}", template.notes)
            };
            println!(
                "  {marker} {id}: {} ({}) [{}]{priority}{notes}",
                template.display, template.url, template.status
            );
        }
        return Ok(());
    }
    let provider = config_mutation::connect(path, args.providers.as_deref())
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    if provider.is_some() {
        let outcome = config_mutation::apply_after_mutation(path, ApplyMode::LiveOrReport)
            .await
            .map_err(|error| mutation_error(error, EXIT_CONTROL_UNAVAILABLE))?;
        render_apply(outcome);
    }
    Ok(())
}

async fn logout(path: &Path, target: Option<&str>) -> Result<(), BootstrapError> {
    let account = config_mutation::logout(path, target)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let Some(account) = account else {
        if let Some(target) = target {
            println!("No configured provider or API key found for {target:?}.");
        } else {
            println!("No configured accounts found.");
        }
        return Ok(());
    };
    println!(
        "Removed {}/{} from {}.",
        account.provider_id,
        account.name,
        path.display()
    );
    let outcome = config_mutation::apply_after_mutation(path, ApplyMode::LiveOrReport)
        .await
        .map_err(|error| mutation_error(error, EXIT_CONTROL_UNAVAILABLE))?;
    render_apply(outcome);
    Ok(())
}

fn edit(path: &Path) -> Result<(), BootstrapError> {
    if !path.exists() {
        config_mutation::init_config(path, false)
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    }
    let editor = std::env::var_os("EDITOR")
        .or_else(|| std::env::var_os("VISUAL"))
        .or_else(|| {
            ["hx", "vim", "vi", "nano"]
                .iter()
                .find_map(|name| which(name).map(Into::into))
        })
        .ok_or_else(|| {
            command_error(
                EXIT_VALIDATION,
                "No editor found. Set $EDITOR or install vim/helix.",
            )
        })?;
    let status = OsCommand::new(&editor)
        .arg(path)
        .status()
        .map_err(|error| {
            command_error(
                EXIT_VALIDATION,
                format!("editor could not be started: {error}"),
            )
        })?;
    if !status.success() {
        return Err(command_error(EXIT_VALIDATION, "editor process failed"));
    }
    Ok(())
}

fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|directory| directory.join(name))
        .find(|candidate| candidate.is_file())
}

fn getkey(path: &Path) -> Result<(), BootstrapError> {
    let key = config_mutation::read_server_key(path)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let Some(key) = key else {
        return Err(command_error(
            EXIT_VALIDATION,
            "No API key configured. Run `eggpool newkey` to generate one.",
        ));
    };
    print!("{key}");
    std::io::stdout()
        .flush()
        .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
    Ok(())
}

async fn newkey(path: &Path, show_old: bool) -> Result<(), BootstrapError> {
    let old = config_mutation::read_server_key(path)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let key =
        config_mutation::generate_key().map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let written = config_mutation::write_server_key(path, &key)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    if let Some(old) = old {
        if show_old {
            println!("Old key (expired): {old}");
        } else {
            println!(
                "Old key (expired, redacted): {}",
                config_mutation::redact_key(&old)
            );
        }
    }
    println!("New key (use this): {key}");
    if !written {
        eprintln!(
            "Warning: [server] api_key_env owns the server key; rotate that environment variable instead."
        );
    }
    let outcome = config_mutation::apply_after_mutation(path, ApplyMode::RestartIfRunning)
        .await
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    render_apply(outcome);
    Ok(())
}

fn init_config(target: Option<&Path>, resolved: &Path, force: bool) -> Result<(), BootstrapError> {
    let target = target.unwrap_or_else(|| Path::new("config.toml"));
    config_mutation::init_config(target, force)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let _ = resolved;
    println!("Config written to {}", target.display());
    Ok(())
}

async fn set_config(path: &Path, key: &str, value: &str) -> Result<(), BootstrapError> {
    config_mutation::set_server_value(path, key, value)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    println!("Set {key} = {value} in {}.", path.display());
    let outcome = config_mutation::apply_after_mutation(path, ApplyMode::RestartIfRunning)
        .await
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    render_apply(outcome);
    Ok(())
}

async fn dashboard_public(path: &Path, setting: Option<bool>) -> Result<(), BootstrapError> {
    let current = config_mutation::read_dashboard_public(path)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let new_value = setting.unwrap_or(!current);
    config_mutation::set_dashboard_public(path, Some(new_value))
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    if new_value {
        println!("Dashboard is now public (no API key required).");
    } else {
        println!("Dashboard now requires API key authentication.");
    }
    let outcome = config_mutation::apply_after_mutation(path, ApplyMode::RestartIfRunning)
        .await
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    render_apply(outcome);
    Ok(())
}

async fn onboard(path: &Path, args: crate::cli::OnboardArgs) -> Result<(), BootstrapError> {
    println!("\n=== EggPool Onboarding ===\n");
    if !path.exists() {
        config_mutation::init_config(path, false)
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
        println!("  Created configuration at {}", path.display());
    }
    if config_mutation::read_server_key(path)
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?
        .is_none()
    {
        let key = config_mutation::generate_key()
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
        if config_mutation::write_server_key(path, &key)
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?
        {
            println!("  Generated server API key");
        }
    }
    config_mutation::set_server_value(path, "host", "127.0.0.1")
        .map_err(|error| mutation_error(error, EXIT_VALIDATION))?;
    let mut connected = 0_u32;
    loop {
        if config_mutation::connect(path, args.providers.as_deref())
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?
            .is_some()
        {
            connected += 1;
            let outcome = config_mutation::apply_after_mutation(path, ApplyMode::LiveOrReport)
                .await
                .map_err(|error| mutation_error(error, EXIT_CONTROL_UNAVAILABLE))?;
            render_apply(outcome);
        }
        let Some(answer) = config_mutation::read_line_prompt("Add another provider? (y/n): ")
            .map_err(|error| mutation_error(error, EXIT_VALIDATION))?
        else {
            break;
        };
        if !matches!(answer.to_ascii_lowercase().as_str(), "y" | "yes") {
            break;
        }
    }
    config::Config::from_toml(path)
        .map_err(BootstrapError::from)?
        .validate_account_credentials()
        .map_err(BootstrapError::from)?;
    println!("\nConnected {connected} provider(s).\n");
    let paths = RuntimePaths::resolve();
    if paths.pid_file.exists()
        && process::read_pid(&paths.pid_file)
            .ok()
            .flatten()
            .is_some_and(process::process_exists)
    {
        println!(
            "Server is already running. Use `eggpool restart` to apply configuration changes."
        );
        return Ok(());
    }
    serve(
        path,
        &ServeArgs {
            verbose: false,
            log_file: None,
            quiet: false,
            as_root: false,
        },
    )
    .await
}

async fn serve(path: &Path, args: &ServeArgs) -> Result<(), BootstrapError> {
    validate_root(args.as_root)?;
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    warn_without_accounts(&config);
    if args.verbose {
        let digest = config::content_digest(path)?;
        crate::server::run_with_digest(config, digest, Some(path.to_path_buf()))
            .await
            .map_err(server_error)
    } else {
        ensure_start_safe(path, &config).await?;
        let child = spawn_detached(path, args.log_file.as_deref(), args.quiet)?;
        let paths = RuntimePaths::resolve();
        let log = if args.quiet && args.log_file.is_none() {
            "/dev/null".to_owned()
        } else {
            args.log_file.as_deref().map_or_else(
                || paths.log_file.display().to_string(),
                |path| path.display().to_string(),
            )
        };
        println!(
            "Server spawned (PID {}).\n  Log: {}\n  PID file: {}",
            child.id(),
            log,
            paths.pid_file.display()
        );
        Ok(())
    }
}

fn server_error(error: crate::server::ServerError) -> BootstrapError {
    BootstrapError::Server {
        detail: error.to_string(),
    }
}

fn validate_root(as_root: bool) -> Result<(), BootstrapError> {
    #[cfg(unix)]
    if nix::unistd::geteuid().is_root() && !as_root {
        return Err(command_error(
            EXIT_VALIDATION,
            "refusing to run as root for personal deployment; pass --as-root for an intentional system install",
        ));
    }
    Ok(())
}

fn warn_without_accounts(config: &config::Config) {
    if config.all_accounts().is_empty() {
        eprintln!(
            "Warning: No provider accounts configured.\n  Use `eggpool connect` to add a provider, then restart."
        );
    }
}

async fn ensure_start_safe(_path: &Path, config: &config::Config) -> Result<(), BootstrapError> {
    let paths = RuntimePaths::prepare().map_err(path_error)?;
    let pid = process::read_pid(&paths.pid_file).map_err(process_error)?;
    if let Some(pid) = pid {
        if process::process_exists(pid) {
            return Err(command_error(
                EXIT_VALIDATION,
                format!("server is already running (PID {pid})"),
            ));
        }
        process::clear_stale_pid(&paths.pid_file, Some(pid)).map_err(process_error)?;
    }
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        return Err(command_error(
            EXIT_VALIDATION,
            format!(
                "another process is already serving {}:{}",
                config.server.host, config.server.port
            ),
        ));
    }
    Ok(())
}

fn process_error(error: process::ProcessError) -> BootstrapError {
    command_error(EXIT_VALIDATION, error.to_string())
}

fn path_error(error: crate::operations::paths::PathError) -> BootstrapError {
    process_error(process::ProcessError::Path(error))
}

fn open_log(path: &Path) -> Result<File, BootstrapError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            command_error(
                EXIT_VALIDATION,
                format!("cannot prepare log directory: {error}"),
            )
        })?;
    }
    let mut options = OpenOptions::new();
    options.create(true).append(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .map_err(|error| command_error(EXIT_VALIDATION, format!("cannot open log file: {error}")))
}

fn spawn_detached(
    config_path: &Path,
    log_path: Option<&Path>,
    quiet: bool,
) -> Result<std::process::Child, BootstrapError> {
    let executable = std::env::current_exe().map_err(|error| {
        command_error(
            EXIT_VALIDATION,
            format!("cannot resolve eggpool executable: {error}"),
        )
    })?;
    let resolved_config = fs::canonicalize(config_path).map_err(|error| {
        command_error(
            EXIT_VALIDATION,
            format!("cannot resolve config path: {error}"),
        )
    })?;
    let paths = RuntimePaths::prepare().map_err(path_error)?;
    let explicit_log = log_path.map(PathBuf::from);
    let log_target = if quiet && explicit_log.is_none() {
        None
    } else {
        if explicit_log.is_none() {
            paths
                .ensure_state_dir()
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        }
        Some(explicit_log.unwrap_or(paths.log_file))
    };
    let (stdout, stderr) = if let Some(log_path) = log_target.as_deref() {
        let file = open_log(log_path)?;
        let duplicate = file.try_clone().map_err(|error| {
            command_error(
                EXIT_VALIDATION,
                format!("cannot duplicate log handle: {error}"),
            )
        })?;
        (Stdio::from(file), Stdio::from(duplicate))
    } else {
        (Stdio::null(), Stdio::null())
    };
    let mut command = OsCommand::new(executable);
    command
        .arg("--config")
        .arg(resolved_config)
        .arg("serve")
        .arg("--verbose")
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
        .spawn()
        .map_err(|error| command_error(EXIT_VALIDATION, format!("failed to spawn server: {error}")))
}

async fn stop(path: &Path, timeout_seconds: f64) -> Result<(), BootstrapError> {
    let timeout = lifecycle_timeout(timeout_seconds)?;
    let paths = RuntimePaths::resolve();
    let Some(pid) = process::read_pid(&paths.pid_file).map_err(process_error)? else {
        println!("Server is not running (no PID file found).");
        return Ok(());
    };
    if !process::process_exists(pid) {
        process::clear_stale_pid(&paths.pid_file, Some(pid)).map_err(process_error)?;
        println!("Server is not running (stale PID file).");
        return Ok(());
    }
    let proof = identity_proof(path, &paths, pid).await;
    if !proof.proves_eggpool() {
        return Err(command_error(
            EXIT_VALIDATION,
            "refusing to signal PID without independent EggPool identity evidence",
        ));
    }
    println!("Stopping server (PID {pid})...");
    process::signal_term(pid, proof).map_err(process_error)?;
    if !process::wait_for_exit_or_pid_clear(pid, &paths.pid_file, timeout).await {
        return Err(command_error(
            EXIT_VALIDATION,
            format!("server did not stop within {timeout_seconds}s"),
        ));
    }
    process::clear_pid_if_matches(&paths.pid_file, pid).map_err(process_error)?;
    println!("Server stopped.");
    Ok(())
}

async fn restart(path: &Path, timeout_seconds: f64) -> Result<(), BootstrapError> {
    let timeout = lifecycle_timeout(timeout_seconds)?;
    restart_server_inner(path, timeout, true, true)
        .await
        .map(|_| ())
}

/// Restart a running standalone server for an O004 mutation.  A missing or
/// stopped server is a successful observation (`Ok(false)`), matching the
/// Python mutation helpers which do not start a service as a side effect.
pub(crate) async fn restart_for_mutation(path: &Path) -> Result<bool, BootstrapError> {
    restart_server_inner(path, Duration::from_secs(10), false, false).await
}

async fn restart_server_inner(
    path: &Path,
    timeout: Duration,
    announce: bool,
    start_if_missing: bool,
) -> Result<bool, BootstrapError> {
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    let paths = RuntimePaths::resolve();
    let Some(pid) = process::read_pid(&paths.pid_file).map_err(process_error)? else {
        if !start_if_missing {
            return Ok(false);
        }
        if process::probe_health(&config.server.host, config.server.port).await
            == process::HealthProbe::Healthy
        {
            return Err(command_error(
                EXIT_VALIDATION,
                "replacement was not started because the configured listener is still healthy",
            ));
        }
        let child = spawn_detached(path, None, false)?;
        if announce {
            println!("Server started (PID {}).", child.id());
        }
        return Ok(true);
    };
    {
        if process::process_exists(pid) {
            let proof = identity_proof(path, &paths, pid).await;
            if !proof.proves_eggpool() {
                return Err(command_error(
                    EXIT_VALIDATION,
                    "refusing to restart an unproven process associated with the PID file",
                ));
            }
            if announce {
                println!("Stopping server (PID {pid})...");
            }
            process::signal_term(pid, proof).map_err(process_error)?;
            if !process::wait_for_exit_or_pid_clear(pid, &paths.pid_file, timeout).await {
                return Err(command_error(
                    EXIT_VALIDATION,
                    format!(
                        "server did not stop within {}s; replacement was not started",
                        timeout.as_secs_f64()
                    ),
                ));
            }
            process::clear_pid_if_matches(&paths.pid_file, pid).map_err(process_error)?;
        } else {
            process::clear_stale_pid(&paths.pid_file, Some(pid)).map_err(process_error)?;
            if !start_if_missing {
                return Ok(false);
            }
        }
    }
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        return Err(command_error(
            EXIT_VALIDATION,
            "replacement was not started because the configured listener is still healthy",
        ));
    }
    let child = spawn_detached(path, None, false)?;
    if announce {
        println!("Server started (PID {}).", child.id());
    }
    Ok(true)
}

async fn identity_proof(
    path: &Path,
    paths: &RuntimePaths,
    pid: i32,
) -> process::ProcessIdentityProof {
    let health = match config::Config::from_toml(path) {
        Ok(config) => process::probe_health(&config.server.host, config.server.port).await,
        Err(_) => process::HealthProbe::Unreachable,
    };
    let control = process::probe_control(&paths.control_socket).await;
    process::ProcessIdentityProof {
        pid_file_matches: process::read_pid(&paths.pid_file).ok().flatten() == Some(pid),
        health,
        control_socket_reachable: control == process::ControlProbe::Reachable,
    }
}

fn lifecycle_timeout(seconds: f64) -> Result<Duration, BootstrapError> {
    if !seconds.is_finite() || seconds <= 0.0 {
        return Err(command_error(
            EXIT_VALIDATION,
            "timeout must be finite and greater than zero",
        ));
    }
    Ok(Duration::from_secs_f64(seconds))
}

async fn rehash(path: &Path, json_output: bool) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    let digest = config::content_digest(path)?;
    let response = process_control_client()
        .reload(Some(digest))
        .await
        .map_err(|error| render_control_error(error, json_output))?;
    let code = reload_exit_code(&response);
    if json_output {
        let value = json!({"ok": response.ok, "stage": response.stage, "exit_code": code, "generation": response.generation, "changed_sections": response.changed_sections, "warnings": response.warnings, "restart_required": response.restart_required, "retirement_pending": response.retirement_pending, "message": response.message});
        println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("JSON rendering")
        );
    } else if response.ok {
        println!("\n{}", response.message);
        if !response.changed_sections.is_empty() {
            println!(
                "  Changed sections: {}",
                response.changed_sections.join(", ")
            );
        }
        if let Some(generation) = response.generation {
            println!("  Generation: {generation}");
        }
        if response.retirement_pending {
            println!(
                "  Old generation is draining; active requests will complete on their original configuration."
            );
        }
    } else {
        eprintln!("\n{}", response.message);
        if !response.restart_required.is_empty() {
            eprintln!("  Restart-required changes:");
            for field in &response.restart_required {
                eprintln!("    - {field}");
            }
        }
    }
    if code == 0 {
        Ok(())
    } else {
        Err(command_error(code, response.message))
    }
}

fn process_control_client() -> crate::operations::control::ControlClient {
    crate::operations::control::ControlClient::new(RuntimePaths::resolve().control_socket)
}

fn render_control_error(
    error: crate::operations::control::ControlClientError,
    json_output: bool,
) -> BootstrapError {
    let detail = error.to_string();
    if json_output {
        let stage = if matches!(
            error,
            crate::operations::control::ControlClientError::Timeout
        ) {
            "timeout"
        } else {
            "error"
        };
        println!("{}", serde_json::to_string_pretty(&json!({"ok": false, "stage": stage, "exit_code": EXIT_CONTROL_UNAVAILABLE, "generation": null, "changed_sections": [], "warnings": [], "restart_required": [], "retirement_pending": false, "message": detail})).expect("JSON rendering"));
    } else {
        eprintln!("\nControl socket unavailable: {detail}");
        eprintln!("Use `eggpool restart` for a disruptive configuration reload.");
    }
    command_error(EXIT_CONTROL_UNAVAILABLE, detail)
}

fn reload_exit_code(response: &crate::operations::control::ControlResponse) -> u8 {
    if response.ok {
        return 0;
    }
    if !response.restart_required.is_empty() {
        return EXIT_RESTART_REQUIRED;
    }
    if response
        .message
        .to_ascii_lowercase()
        .contains("digest mismatch")
    {
        return EXIT_DIGEST_MISMATCH;
    }
    match response.stage.as_str() {
        "reload_in_progress" => EXIT_RELOAD_BUSY,
        "preparation" | "reconciliation" | "commit" | "activation" => EXIT_PREPARATION_FAILED,
        _ => EXIT_VALIDATION,
    }
}

fn croncheck() {
    let paths = RuntimePaths::resolve();
    let healthy = process::read_pid(&paths.pid_file)
        .ok()
        .flatten()
        .is_some_and(process::process_exists);
    std::process::exit(if healthy { 0 } else { 1 });
}

async fn ensure_running(path: &Path) -> Result<(), BootstrapError> {
    let paths = RuntimePaths::prepare().map_err(path_error)?;
    if process::read_pid(&paths.pid_file)
        .map_err(process_error)?
        .is_some_and(process::process_exists)
    {
        return Ok(());
    }
    let _ = process::clear_stale_pid(&paths.pid_file, None).map_err(process_error)?;
    let guard = process::acquire_start_guard(&paths).map_err(process_error)?;
    if process::read_pid(&paths.pid_file)
        .map_err(process_error)?
        .is_some_and(process::process_exists)
    {
        drop(guard);
        return Ok(());
    }
    let config = config::Config::from_toml(path)?;
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        drop(guard);
        return Ok(());
    }
    let child = spawn_detached(path, None, false)?;
    let deadline = Instant::now() + WATCHDOG_START_TIMEOUT;
    loop {
        if process::read_pid(&paths.pid_file)
            .map_err(process_error)?
            .is_some_and(process::process_exists)
            || process::probe_health(&config.server.host, config.server.port).await
                == process::HealthProbe::Healthy
        {
            drop(guard);
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(command_error(
                EXIT_VALIDATION,
                format!(
                    "server did not confirm startup within 2s (child PID {})",
                    child.id()
                ),
            ));
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn update(path: &Path, args: crate::cli::UpdateArgs) -> Result<(), BootstrapError> {
    if args.from_source {
        return Err(command_error(
            EXIT_VALIDATION,
            "--from-source is not supported by the Rust updater; install a reviewed Rust release artifact",
        ));
    }
    let target = ReleaseTarget::parse(args.requested_version.as_deref()).map_err(update_error)?;
    let service = UpdateService::new().map_err(update_error)?;
    let current = service.current_version().map_err(update_error)?;
    let metadata = service.resolve(&target).await.map_err(update_error)?;

    if matches!(target, ReleaseTarget::Exact(_)) {
        println!("Current version: {}", current.as_str());
        println!("Requested version: {}", metadata.version.as_str());
    }
    if metadata.version.equivalent(&current) {
        if matches!(target, ReleaseTarget::Exact(_)) {
            println!("Requested version is already installed.");
        } else {
            println!("Already up to date.");
        }
        return Ok(());
    }
    if args.check {
        if matches!(target, ReleaseTarget::Exact(_)) {
            println!("Exact version is available.");
        } else if metadata.version.is_newer_than(&current) {
            println!("Current version: {}", current.as_str());
            println!("Latest version:  {}", metadata.version.as_str());
            println!("An update is available.");
        } else {
            println!("Already up to date.");
        }
        return Ok(());
    }

    let executable = std::env::current_exe().map_err(|error| {
        command_error(
            EXIT_VALIDATION,
            format!("cannot resolve eggpool executable: {error}"),
        )
    })?;
    let executable = fs::canonicalize(executable).map_err(|_| {
        command_error(
            EXIT_VALIDATION,
            "current executable path is unsupported or not writable",
        )
    })?;
    if executable
        .ancestors()
        .any(|candidate| candidate.join(".git").exists())
    {
        return Err(command_error(
            EXIT_VALIDATION,
            "current executable is from a source checkout; install a managed Rust release before updating",
        ));
    }

    let paths = RuntimePaths::resolve();
    let was_running = server_is_running(path, &paths).await;
    if was_running {
        stop(path, 10.0).await?;
    }
    println!(
        "Updating from {} to {}...",
        current.as_str(),
        metadata.version.as_str()
    );
    let restart = || async {
        restart_server_inner(path, Duration::from_secs(10), false, true)
            .await
            .map(|_| ())
            .map_err(|_| UpdateError::RestartFailed)
    };
    let result = service
        .apply_current_executable(&target, &executable, was_running, Some(restart))
        .await;
    match result {
        Ok(report) => {
            println!("Installed version: {}", report.target_version);
            if report.restarted {
                println!("Server restarted.");
            } else {
                println!("Server is not running.");
            }
            Ok(())
        }
        Err(error) => {
            if was_running {
                let _ = restart_server_inner(path, Duration::from_secs(10), false, true).await;
            }
            Err(update_error(error))
        }
    }
}

async fn server_is_running(path: &Path, paths: &RuntimePaths) -> bool {
    let Some(pid) = process::read_pid(&paths.pid_file).ok().flatten() else {
        return false;
    };
    process::process_exists(pid) && identity_proof(path, paths, pid).await.proves_eggpool()
}

fn update_error(error: UpdateError) -> BootstrapError {
    command_error(EXIT_VALIDATION, format!("update failed: {error}"))
}

async fn runtime_status(path: &Path, json_output: bool) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let value = fetch_runtime_status(&config)
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error))?;
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("JSON rendering")
        );
    } else {
        print_runtime_status(&value);
    }
    Ok(())
}

async fn fetch_runtime_status(config: &config::Config) -> Result<Value, String> {
    let host = match config.server.host.as_str() {
        "0.0.0.0" => "127.0.0.1",
        "::" => "::1",
        value => value,
    };
    let mut stream = timeout(
        STATUS_TIMEOUT,
        TcpStream::connect((host, config.server.port)),
    )
    .await
    .map_err(|_| "runtime-status connection timed out".to_owned())?
    .map_err(|_| "runtime-status server is not running".to_owned())?;
    let mut request =
        "GET /api/stats/runtime HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n".to_owned();
    if let Some(key) = config.resolved_server_api_key() {
        request.push_str(&format!("Authorization: Bearer {key}\r\n"));
    }
    request.push_str("\r\n");
    timeout(STATUS_TIMEOUT, stream.write_all(request.as_bytes()))
        .await
        .map_err(|_| "runtime-status request timed out".to_owned())?
        .map_err(|_| "runtime-status request failed".to_owned())?;
    let mut bytes = Vec::with_capacity(4096);
    let mut chunk = [0_u8; 8192];
    loop {
        let count = timeout(STATUS_TIMEOUT, stream.read(&mut chunk))
            .await
            .map_err(|_| "runtime-status response timed out".to_owned())?
            .map_err(|_| "runtime-status response failed".to_owned())?;
        if count == 0 {
            break;
        }
        if bytes.len().saturating_add(count) > MAX_STATUS_BODY_BYTES {
            return Err("runtime-status response body is too large".to_owned());
        }
        bytes.extend_from_slice(&chunk[..count]);
    }
    let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return Err("runtime-status response was malformed".to_owned());
    };
    let headers = &bytes[..header_end];
    let status = headers
        .split(|byte| *byte == b'\n')
        .next()
        .and_then(|line| line.split(|byte| *byte == b' ').nth(1))
        .and_then(|value| std::str::from_utf8(value).ok())
        .and_then(|value| value.trim().parse::<u16>().ok())
        .ok_or_else(|| "runtime-status response status was malformed".to_owned())?;
    if status != 200 {
        return Err(match status {
            401 | 403 => format!("runtime-status authentication failed (HTTP {status})"),
            404 => "runtime-status endpoint is unavailable".to_owned(),
            _ => format!("runtime-status server returned HTTP {status}"),
        });
    }
    serde_json::from_slice(&bytes[header_end + 4..])
        .map_err(|_| "runtime-status response body was malformed JSON".to_owned())
}

fn print_runtime_status(value: &Value) {
    let server = value.get("server").unwrap_or(&Value::Null);
    let memory = value.get("memory").unwrap_or(&Value::Null);
    let processes = value.get("processes").unwrap_or(&Value::Null);
    let db = value.get("db").unwrap_or(&Value::Null);
    let routing = value.get("routing_runtime").unwrap_or(&Value::Null);
    println!("=== EggPool Runtime Status ===\n");
    println!("  PID:            {}", display_value(server.get("pid")));
    println!(
        "  Uptime:         {}",
        format_duration(server.get("uptime_seconds"))
    );
    println!(
        "  Server Threads: {}",
        display_value(server.get("configured_server_threads"))
    );
    println!(
        "  Rust:           {}",
        display_value(server.get("rust_version"))
    );
    println!(
        "  RSS:            {}",
        format_bytes(memory.get("rss_bytes"))
    );
    println!(
        "  VMS:            {}",
        format_bytes(memory.get("vms_bytes"))
    );
    println!(
        "  Open FDs:       {}",
        display_value(memory.get("open_fd_count"))
    );
    println!(
        "  Threads:        {}",
        display_value(memory.get("thread_count"))
    );
    println!(
        "\n  EggPool Processes: {} (expected: {})",
        display_value(processes.get("eggpool_process_count")),
        display_value(processes.get("expected_worker_process_count"))
    );
    println!("\n  Background Tasks:");
    match value.get("background_tasks").and_then(Value::as_array) {
        Some(tasks) if !tasks.is_empty() => {
            for task in tasks {
                println!("    {}", display_value(task.get("name")));
            }
        }
        _ => println!("    (none)"),
    }
    println!("\n  DB Path:        {}", display_value(db.get("path")));
    println!(
        "  DB Size:        {}",
        format_bytes(db.get("file_size_bytes"))
    );
    println!(
        "  WAL Size:       {}",
        format_bytes(db.get("wal_size_bytes"))
    );
    println!(
        "\n  Pending Requests:   {}",
        display_value(routing.get("pending_count"))
    );
    println!(
        "  Active Reservations: {}",
        display_value(routing.get("active_reservations_count"))
    );
    println!(
        "  Reserved (μ$):      {}",
        display_value(routing.get("reserved_microdollars"))
    );
    let outbound = value.get("outbound_client").unwrap_or(&Value::Null);
    let provider_pool = value.get("provider_client_pool").unwrap_or(&Value::Null);
    println!("\n  Network:");
    println!(
        "    Outbound builds:   {}",
        display_value(outbound.get("build_count"))
    );
    println!(
        "    Outbound requests: {}",
        display_value(outbound.get("request_count"))
    );
    println!(
        "    Outbound errors:   {}",
        display_value(outbound.get("error_count"))
    );
    println!(
        "    Provider clients:  {}",
        display_value(provider_pool.get("build_count"))
    );
    if let Some(errors) = value.get("probe_errors").and_then(Value::as_array)
        && !errors.is_empty()
    {
        println!("\n  Probe Errors:");
        for error in errors {
            println!("    - {}", display_value(Some(error)));
        }
    }
}

fn display_value(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Number(value)) => value.to_string(),
        Some(Value::Bool(value)) => value.to_string(),
        Some(Value::Null) | None => "N/A".to_owned(),
        Some(value) => value.to_string(),
    }
}

fn format_duration(value: Option<&Value>) -> String {
    let Some(seconds) = value.and_then(Value::as_f64) else {
        return "N/A".to_owned();
    };
    let total = seconds.max(0.0) as u64;
    if total >= 3600 {
        format!("{}h {}m", total / 3600, (total % 3600) / 60)
    } else if total >= 60 {
        format!("{}m {}s", total / 60, total % 60)
    } else {
        format!("{total}s")
    }
}

fn format_bytes(value: Option<&Value>) -> String {
    let Some(bytes) = value.and_then(Value::as_u64) else {
        return "N/A".to_owned();
    };
    if bytes >= 1024 * 1024 * 1024 {
        format!("{:.2} GB", bytes as f64 / (1024.0 * 1024.0 * 1024.0))
    } else if bytes >= 1024 * 1024 {
        format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
    } else if bytes >= 1024 {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    } else {
        format!("{bytes} B")
    }
}
