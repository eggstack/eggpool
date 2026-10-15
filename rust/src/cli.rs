use std::{ffi::OsString, path::PathBuf};

use clap::{ArgAction, CommandFactory, Parser, Subcommand, error::ErrorKind};

use crate::version::PACKAGE_VERSION;

/// The complete EggPool command parser.
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
    #[command(name = "configremote")]
    Configremote(ConfigremoteArgs),
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
    #[command(subcommand)]
    Users(UsersCommand),
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
    #[command(name = "install-provenance", hide = true)]
    InstallProvenance {
        /// Emit bounded tab-separated fields for installer automation.
        #[arg(long)]
        shell: bool,
    },
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
    Status {
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
    /// Print the new key in plaintext. By default only a redacted form is
    /// shown because stdout is captured by logs/systemd/journal.
    #[arg(long = "show-secrets")]
    pub show_secrets: bool,
}
#[derive(Debug, clap::Args)]
pub struct OnboardArgs {
    #[arg(long, value_name = "PATH")]
    pub providers: Option<PathBuf>,
    #[arg(long, default_value = "admin")]
    pub admin_username: String,
    #[arg(long)]
    pub password_stdin: bool,
    #[arg(long)]
    pub show_secret: bool,
}

#[derive(Debug, Subcommand)]
pub enum UsersCommand {
    /// Create the first administrator and personal inference credential.
    Bootstrap(UserPasswordArgs),
    /// Create a member with a temporary password and optional initial profile.
    Add(UserAddArgs),
    /// Disable a user, revoking sessions and personal credentials.
    Disable(UserLifecycleArgs),
    /// Reactivate a disabled user; issue a new personal token separately.
    Enable(UserLifecycleArgs),
    /// Show one local user's non-secret status.
    Show(UserNameArgs),
    /// Show local account names and status without secrets.
    List,
    /// Reset a user's password and revoke their API credentials.
    ResetPassword(UserPasswordArgs),
    /// Issue a new personal API credential. Plaintext is shown once.
    Token(UserTokenArgs),
    /// Revoke every API credential owned by one user.
    Revoke(UserNameArgs),
    /// Retire legacy access after public dashboard access is disabled.
    EnableManaged(UserActorArgs),
    /// Create and assign model-policy profiles during the CLI-only M004 phase.
    #[command(subcommand)]
    Policy(UsersPolicyCommand),
    /// Set or remove durable per-user budget rules.
    #[command(subcommand)]
    Budget(UsersBudgetCommand),
    /// Set or remove instance-wide budget ceilings intersected with every
    /// per-user rule. Requires an active administrator.
    #[command(subcommand)]
    GlobalBudget(UsersGlobalBudgetCommand),
}

#[derive(Debug, Subcommand)]
pub enum UsersBudgetCommand {
    Set(UserBudgetSetArgs),
    Remove(UserBudgetRemoveArgs),
}

#[derive(Debug, Subcommand)]
pub enum UsersGlobalBudgetCommand {
    Set(UserGlobalBudgetSetArgs),
    Remove(UserGlobalBudgetRemoveArgs),
}

#[derive(Debug, clap::Args)]
pub struct UserGlobalBudgetSetArgs {
    #[arg(long)]
    pub model_id: Option<String>,
    #[arg(long, value_parser = ["day", "month"])]
    pub window: String,
    #[arg(long)]
    pub max_requests: Option<i64>,
    #[arg(long)]
    pub max_tokens: Option<i64>,
    #[arg(long)]
    pub max_microdollars: Option<i64>,
    #[arg(long)]
    pub max_concurrent: Option<i64>,
    /// `estimated` keeps estimator projections (default, explicit); `hard`
    /// requires a provable worst-case bound and fails closed when the request
    /// or price cannot be bounded.
    #[arg(long, default_value = "estimated", value_parser = ["hard", "estimated"])]
    pub enforcement: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserGlobalBudgetRemoveArgs {
    #[arg(long)]
    pub model_id: Option<String>,
    #[arg(long, value_parser = ["day", "month"])]
    pub window: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserBudgetSetArgs {
    pub username: String,
    #[arg(long)]
    pub model_id: Option<String>,
    #[arg(long, value_parser = ["day", "month"])]
    pub window: String,
    #[arg(long)]
    pub max_requests: Option<i64>,
    #[arg(long)]
    pub max_tokens: Option<i64>,
    #[arg(long)]
    pub max_microdollars: Option<i64>,
    #[arg(long)]
    pub max_concurrent: Option<i64>,
    /// `estimated` keeps estimator projections (default, explicit); `hard`
    /// requires a provable worst-case bound and fails closed when the request
    /// or price cannot be bounded. Existing rules stay estimated until an
    /// admin explicitly promotes them.
    #[arg(long, default_value = "estimated", value_parser = ["hard", "estimated"])]
    pub enforcement: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserBudgetRemoveArgs {
    pub username: String,
    #[arg(long)]
    pub model_id: Option<String>,
    #[arg(long, value_parser = ["day", "month"])]
    pub window: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, Subcommand)]
pub enum UsersPolicyCommand {
    /// Create a named profile with an allow-all or default-deny base.
    Create(PolicyProfileCreateArgs),
    /// Add or replace one exact canonical or model/provider rule.
    Rule(PolicyProfileRuleArgs),
    /// Assign a profile to one local user.
    Assign(PolicyProfileAssignArgs),
    /// Set a permitted client recommendation, or clear it when omitted.
    Prefer(PolicyProfilePreferArgs),
}

#[derive(Debug, clap::Args)]
pub struct PolicyProfileCreateArgs {
    pub id: String,
    pub name: String,
    #[arg(value_parser = ["allow", "deny"])]
    pub default_effect: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct PolicyProfileRuleArgs {
    pub profile_id: String,
    pub model_id: String,
    #[arg(value_parser = ["allow", "deny"])]
    pub effect: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct PolicyProfileAssignArgs {
    pub username: String,
    pub profile_id: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct PolicyProfilePreferArgs {
    pub profile_id: String,
    pub model_id: Option<String>,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserPasswordArgs {
    #[arg(default_value = "admin")]
    pub username: String,
    /// Read one password line from stdin instead of prompting on a TTY.
    #[arg(long)]
    pub password_stdin: bool,
    /// Allow one-time token output when stdout is not a TTY.
    #[arg(long)]
    pub show_secret: bool,
}

#[derive(Debug, clap::Args)]
pub struct UserAddArgs {
    pub username: String,
    /// Read the temporary password from stdin instead of prompting on a TTY.
    #[arg(long)]
    pub password_stdin: bool,
    /// Assign an existing model-policy profile in the same transaction.
    #[arg(long)]
    pub profile_id: Option<String>,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserLifecycleArgs {
    pub username: String,
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserActorArgs {
    #[arg(long, default_value = "admin")]
    pub actor: String,
}

#[derive(Debug, clap::Args)]
pub struct UserTokenArgs {
    pub username: String,
    #[arg(long, default_value = "personal client")]
    pub label: String,
    /// Allow one-time token output when stdout is not a TTY.
    #[arg(long)]
    pub show_secret: bool,
}

#[derive(Debug, clap::Args)]
pub struct UserNameArgs {
    pub username: String,
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
    #[arg(long, conflicts_with = "off")]
    pub on: bool,
    #[arg(long, conflicts_with = "on")]
    pub off: bool,
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
    #[arg(long, action = ArgAction::SetTrue, conflicts_with = "apply")]
    pub dry_run: bool,
    #[arg(long, action = ArgAction::SetTrue, conflicts_with = "dry_run")]
    pub apply: bool,
    #[arg(long)]
    pub limit: Option<u32>,
}
#[derive(Debug, clap::Args)]
pub struct StatsRepairCostsArgs {
    #[arg(long)]
    pub provider: Option<String>,
    #[arg(long)]
    pub since: Option<String>,
    #[arg(long, action = ArgAction::SetTrue, conflicts_with = "apply")]
    pub dry_run: bool,
    #[arg(long, action = ArgAction::SetTrue, conflicts_with = "dry_run")]
    pub apply: bool,
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

#[derive(Debug, clap::Args)]
pub struct ConfigremoteArgs {
    /// Remote client target (`codex` or `opencode`).
    pub target: String,
    /// Override the advertised URL for this profile only.
    #[arg(long = "base-url")]
    pub base_url: Option<String>,
    /// Output shape: `command` (human), `token`, or `json`.
    #[arg(long, default_value = "command")]
    pub format: String,
    /// Bootstrap shell rendering: `auto`, `posix`, `powershell`, or `all`.
    /// `auto`/`all` print both the POSIX and PowerShell invocations; the
    /// commands are pinned to this EggPool release and verify SHA-256
    /// against the release SHA256SUMS before executing anything.
    #[arg(long, default_value = "auto")]
    pub shell: String,
    /// Print token/profile only without bootstrap guidance.
    #[arg(long = "no-bootstrap")]
    pub no_bootstrap: bool,
}

#[derive(Debug, Subcommand)]
pub enum ConfigsetupCommand {
    Opencode(ConfigsetupLifecycleArgs),
    ClaudeCode,
    Aider(ConfigsetupArgs),
    Codex(ConfigsetupLifecycleArgs),
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

#[derive(Debug, clap::Args)]
pub struct ConfigsetupLifecycleArgs {
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
    #[arg(long, conflicts_with_all = ["sync", "check", "remove"])]
    pub apply: bool,
    #[arg(long, conflicts_with_all = ["apply", "check", "remove"])]
    pub sync: bool,
    #[arg(long, conflicts_with_all = ["apply", "sync", "remove"])]
    pub check: bool,
    #[arg(long, conflicts_with_all = ["apply", "sync", "check"])]
    pub remove: bool,
    #[arg(long = "dry-run")]
    pub dry_run: bool,
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
            Self::Configremote(_) => "configremote",
            Self::Deploy(_) => "deploy",
            Self::Accounts(_) => "accounts",
            Self::Dashboard(_) => "dashboard",
            Self::Db(_) => "db",
            Self::Models(_) => "models",
            Self::Modelinfo(_) => "modelinfo",
            Self::Stats(_) => "stats",
            Self::Users(_) => "users",
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
            Self::InstallProvenance { .. } => "install-provenance",
            Self::Set { .. } => "set",
            Self::Rehash { .. } => "rehash",
            Self::RuntimeStatus { .. } => "runtime-status",
            Self::Status { .. } => "status",
            Self::Backup { .. } => "backup",
        }
    }
}
