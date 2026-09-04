use std::{ffi::OsString, path::PathBuf};

use clap::{CommandFactory, Parser, Subcommand, error::ErrorKind};

use crate::version::PACKAGE_VERSION;

/// The complete migration-stage EggPool command parser.
#[derive(Debug, Parser)]
#[command(name = "eggpool", version = PACKAGE_VERSION, about = "EggPool - aggregate OpenCode Go subscriptions.", disable_help_subcommand = true)]
pub struct Cli {
    /// Path to the TOML configuration file.
    #[arg(
        long,
        global = true,
        value_name = "PATH",
        help = "Path to the TOML configuration file. Falls back to $EGGPOOL_CONFIG, then ~/.config/eggpool/config.toml, then ./config.toml."
    )]
    pub config: Option<PathBuf>,
    #[command(subcommand)]
    pub command: Option<Command>,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    Serve(ServeArgs),
    Connect(ConnectArgs),
    Logout {
        target: Option<String>,
    },
    #[command(name = "check-config")]
    CheckConfig,
    Edit,
    Getkey,
    Newkey(NewkeyArgs),
    #[command(subcommand)]
    Configsetup(ConfigsetupCommand),
    Deploy(DeployArgs),
    #[command(subcommand)]
    Accounts(AccountsCommand),
    #[command(subcommand)]
    Dashboard(DashboardCommand),
    #[command(subcommand)]
    Db(DbCommand),
    #[command(subcommand)]
    Models(ModelsCommand),
    #[command(subcommand)]
    Modelinfo(ModelInfoCommand),
    #[command(subcommand)]
    Stats(StatsCommand),
    Onboard(OnboardArgs),
    Croncheck,
    EnsureRunning,
    Migrate,
    Stop(TimeoutArgs),
    Restart(TimeoutArgs),
    #[command(name = "init-config")]
    InitConfig {
        target: Option<PathBuf>,
        #[arg(long)]
        force: bool,
    },
    Help,
    Recover {
        source: Option<PathBuf>,
    },
    Uninstall(UninstallArgs),
    Update(UpdateArgs),
    Set {
        key: String,
        value: String,
    },
    Rehash {
        #[arg(long)]
        json: bool,
    },
    RuntimeStatus {
        #[arg(long)]
        json: bool,
    },
    Backup {
        #[arg(long, value_name = "DIR")]
        output_dir: Option<PathBuf>,
    },
    Version,
}

#[derive(Debug, clap::Args)]
pub struct ServeArgs {
    #[arg(long)]
    pub verbose: bool,
    #[arg(long, value_name = "PATH")]
    pub log_file: Option<PathBuf>,
    #[arg(long)]
    pub quiet: bool,
    #[arg(long)]
    pub as_root: bool,
}
#[derive(Debug, clap::Args)]
pub struct ConnectArgs {
    #[arg(long, value_name = "PATH")]
    pub providers: Option<PathBuf>,
    #[command(subcommand)]
    pub command: Option<ConnectCommand>,
}
#[derive(Debug, Subcommand)]
pub enum ConnectCommand {
    List,
}
#[derive(Debug, clap::Args)]
pub struct NewkeyArgs {
    #[arg(long = "show-old")]
    pub show_old: bool,
}
#[derive(Debug, clap::Args)]
pub struct OnboardArgs {
    #[arg(long, value_name = "PATH")]
    pub providers: Option<PathBuf>,
}
#[derive(Debug, clap::Args)]
pub struct TimeoutArgs {
    #[arg(long, default_value_t = 10.0)]
    pub timeout: f64,
}
#[derive(Debug, clap::Args)]
pub struct UninstallArgs {
    #[arg(long)]
    pub yes: bool,
    #[arg(long = "keep-data")]
    pub keep_data: bool,
    #[arg(long = "keep-config")]
    pub keep_config: bool,
    #[arg(long = "keep-path")]
    pub keep_path: bool,
    #[arg(long = "deploy-artifacts")]
    pub deploy_artifacts: bool,
}
#[derive(Debug, clap::Args)]
pub struct UpdateArgs {
    pub requested_version: Option<String>,
    #[arg(long)]
    pub check: bool,
    #[arg(long = "from-source")]
    pub from_source: bool,
}

#[derive(Debug, Subcommand)]
pub enum AccountsCommand {
    List,
    Status,
    Explain(AccountsExplainArgs),
}
#[derive(Debug, clap::Args)]
pub struct AccountsExplainArgs {
    #[arg(long)]
    pub model: Option<String>,
    #[arg(long)]
    pub provider: Option<String>,
    #[arg(long)]
    pub protocol: Option<String>,
    #[arg(long)]
    pub scores: bool,
    #[arg(long)]
    pub gates: bool,
}
#[derive(Debug, Subcommand)]
pub enum DashboardCommand {
    Public(DashboardPublicArgs),
}
#[derive(Debug, clap::Args)]
pub struct DashboardPublicArgs {
    #[arg(long)]
    pub on: bool,
}
#[derive(Debug, Subcommand)]
pub enum DbCommand {
    Vacuum,
}
#[derive(Debug, Subcommand)]
pub enum ModelsCommand {
    Refresh,
}

#[derive(Debug, Subcommand)]
pub enum ModelInfoCommand {
    Aliases {
        model_id: String,
        #[arg(long)]
        source: Option<String>,
    },
    List {
        #[arg(long)]
        status: Option<String>,
    },
    Refresh {
        #[arg(long = "provider-catalog-only")]
        provider_catalog_only: bool,
    },
    Repair {
        #[arg(long)]
        limit: Option<u32>,
    },
    Show {
        model_id: String,
    },
}

#[derive(Debug, Subcommand)]
pub enum StatsCommand {
    ExplainDashboard(StatsExplainDashboardArgs),
    RecomputeCosts(StatsRecomputeCostsArgs),
    RepairCosts(StatsRepairCostsArgs),
    Transcoding(StatsTranscodingArgs),
}
#[derive(Debug, clap::Args)]
pub struct StatsExplainDashboardArgs {
    #[arg(long)]
    pub period: Option<String>,
    #[arg(long)]
    pub bucket: Option<String>,
    #[arg(long = "group-by")]
    pub group_by: Option<String>,
    #[arg(long)]
    pub json: bool,
}
#[derive(Debug, clap::Args)]
pub struct StatsRecomputeCostsArgs {
    #[arg(long)]
    pub dry_run: bool,
    #[arg(long)]
    pub limit: Option<u32>,
}
#[derive(Debug, clap::Args)]
pub struct StatsRepairCostsArgs {
    #[arg(long)]
    pub provider: Option<String>,
    #[arg(long)]
    pub since: Option<String>,
    #[arg(long)]
    pub dry_run: bool,
    #[arg(long)]
    pub limit: Option<u32>,
}
#[derive(Debug, clap::Args)]
pub struct StatsTranscodingArgs {
    #[arg(long)]
    pub period: Option<String>,
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, clap::Args)]
pub struct DeployArgs {
    #[command(subcommand)]
    pub command: Option<DeployCommand>,
}
#[derive(Debug, Subcommand)]
pub enum DeployCommand {
    All(DeployAllArgs),
    BackupCron(DeployBackupCronArgs),
    Cron(DeployCronArgs),
    Logrotate(DeployLogrotateArgs),
    Systemd(DeploySystemdArgs),
}
#[derive(Debug, clap::Args)]
pub struct DeployAllArgs {
    #[arg(long)]
    pub install: bool,
}
#[derive(Debug, clap::Args)]
pub struct DeployBackupCronArgs {
    #[arg(long)]
    pub install: bool,
    #[arg(long)]
    pub uninstall: bool,
    #[arg(long)]
    pub production: bool,
    #[arg(long)]
    pub user: Option<String>,
}
#[derive(Debug, clap::Args)]
pub struct DeployCronArgs {
    #[arg(long)]
    pub install: bool,
    #[arg(long)]
    pub uninstall: bool,
    #[arg(long)]
    pub interval: Option<u64>,
    #[arg(long)]
    pub user: Option<String>,
}
#[derive(Debug, clap::Args)]
pub struct DeployLogrotateArgs {
    #[arg(long)]
    pub install: bool,
}
#[derive(Debug, clap::Args)]
pub struct DeploySystemdArgs {
    #[arg(long)]
    pub install: bool,
    #[arg(long)]
    pub production: bool,
    #[arg(long)]
    pub as_root: bool,
}

#[derive(Debug, Subcommand)]
pub enum ConfigsetupCommand {
    Opencode,
    ClaudeCode,
    Aider(ConfigsetupArgs),
    Codex(ConfigsetupArgs),
    QwenCode(ConfigsetupArgs),
    Kilo(ConfigsetupArgs),
    Continue(ConfigsetupArgs),
    Cline(ConfigsetupArgs),
    RooCode(ConfigsetupArgs),
    Goose(ConfigsetupArgs),
    Openhands(ConfigsetupArgs),
}
#[derive(Debug, clap::Args)]
pub struct ConfigsetupArgs {
    #[arg(long = "print-secret")]
    pub print_secret: bool,
    #[arg(long = "no-clipboard")]
    pub no_clipboard: bool,
    #[arg(long)]
    pub force: bool,
    #[arg(long)]
    pub output: Option<PathBuf>,
    #[arg(long)]
    pub write: bool,
    #[arg(long)]
    pub model: Option<String>,
    #[arg(long = "base-url")]
    pub base_url: Option<String>,
    #[arg(long)]
    pub host: Option<String>,
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
    Cli::command().render_help().to_string()
}
impl Cli {
    pub fn resolved_config_path(&self) -> PathBuf {
        crate::config::resolve_config_path(self.config.as_deref())
    }
}
impl Command {
    pub fn unavailable_name(&self) -> &'static str {
        match self {
            Self::CheckConfig | Self::Version | Self::Help => "",
            Self::Serve(_) => "serve",
            Self::Connect(_) => "connect",
            Self::Logout { .. } => "logout",
            Self::Edit => "edit",
            Self::Getkey => "getkey",
            Self::Newkey(_) => "newkey",
            Self::Configsetup(_) => "configsetup",
            Self::Deploy(_) => "deploy",
            Self::Accounts(_) => "accounts",
            Self::Dashboard(_) => "dashboard",
            Self::Db(_) => "db",
            Self::Models(_) => "models",
            Self::Modelinfo(_) => "modelinfo",
            Self::Stats(_) => "stats",
            Self::Onboard(_) => "onboard",
            Self::Croncheck => "croncheck",
            Self::EnsureRunning => "ensure-running",
            Self::Migrate => "migrate",
            Self::Stop(_) => "stop",
            Self::Restart(_) => "restart",
            Self::InitConfig { .. } => "init-config",
            Self::Recover { .. } => "recover",
            Self::Uninstall(_) => "uninstall",
            Self::Update(_) => "update",
            Self::Set { .. } => "set",
            Self::Rehash { .. } => "rehash",
            Self::RuntimeStatus { .. } => "runtime-status",
            Self::Backup { .. } => "backup",
        }
    }
}
