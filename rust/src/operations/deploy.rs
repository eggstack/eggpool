//! Local deployment and removal services for O009.
//!
//! This module deliberately stays boring: snippets are pure renderers, OS
//! commands are argv based, and all destructive filesystem operations are
//! constrained to explicitly resolved EggPool targets.  The CLI owns prompts
//! and presentation; the services here are also usable with fake runners in
//! deterministic tests.

use std::{
    env, fs,
    io::{self, Write},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{SystemTime, UNIX_EPOCH},
};

use crate::{Config, config::ConfigError};

pub const SERVICE_NAME: &str = "eggpool";
pub const SYSTEMD_UNIT_PATH: &str = "/etc/systemd/system/eggpool.service";
pub const LOGROTATE_PATH: &str = "/etc/logrotate.d/eggpool";
pub const PRODUCTION_CONFIG_DIR: &str = "/etc/eggpool";
pub const PRODUCTION_DATA_DIR: &str = "/var/lib/eggpool";
pub const PRODUCTION_LOG_DIR: &str = "/var/log/eggpool";
pub const PRODUCTION_BACKUP_DIR: &str = "/var/backups/eggpool";
pub const PRODUCTION_CRON_PATH: &str = "/etc/cron.d/eggpool-backup";
pub const BACKUP_SCRIPT_PATH: &str = "/usr/local/bin/eggpool-backup";

const WATCHDOG_BEGIN: &str = "# BEGIN EggPool watchdog (managed by eggpool deploy cron)";
const WATCHDOG_END: &str = "# END EggPool watchdog";
const BACKUP_BEGIN: &str = "# BEGIN EggPool backup (managed by eggpool deploy backup-cron)";
const BACKUP_END: &str = "# END EggPool backup";
const TEMP_PREFIX: &str = ".eggpool-deploy";

#[derive(Debug, thiserror::Error)]
pub enum DeployError {
    #[error("deployment command failed: {program} {args}: {detail}")]
    Command {
        program: String,
        args: String,
        detail: String,
    },
    #[error("deployment filesystem operation failed")]
    Io(#[source] io::Error),
    #[error("deployment path is unsafe: {0}")]
    UnsafePath(String),
    #[error("deployment validation failed: {0}")]
    Config(#[from] ConfigError),
    #[error("deployment was cancelled")]
    Cancelled,
    #[error("deployment requires {0}")]
    Requires(&'static str),
    #[error("unsupported deployment operation: {0}")]
    Unsupported(String),
}

impl From<io::Error> for DeployError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandResult {
    pub status: i32,
    pub stdout: String,
    pub stderr: String,
}

pub trait CommandRunner {
    fn run(
        &mut self,
        program: &str,
        args: &[String],
        stdin: Option<&[u8]>,
    ) -> Result<CommandResult, DeployError>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct SystemCommandRunner;

impl CommandRunner for SystemCommandRunner {
    fn run(
        &mut self,
        program: &str,
        args: &[String],
        stdin: Option<&[u8]>,
    ) -> Result<CommandResult, DeployError> {
        let mut command = Command::new(program);
        command
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if stdin.is_some() {
            command.stdin(Stdio::piped());
        }
        let mut child = command.spawn().map_err(|error| DeployError::Command {
            program: program.to_owned(),
            args: args.join(" "),
            detail: error.to_string(),
        })?;
        if let Some(input) = stdin {
            child
                .stdin
                .take()
                .ok_or_else(|| DeployError::Command {
                    program: program.to_owned(),
                    args: args.join(" "),
                    detail: "command stdin was unavailable".to_owned(),
                })?
                .write_all(input)?;
        }
        let output = child.wait_with_output().map_err(DeployError::Io)?;
        Ok(CommandResult {
            status: output.status.code().unwrap_or(1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

/// Small runner useful to callers and tests that need to inspect argv without
/// touching the host.  A queued result is returned for each invocation.
#[derive(Debug, Default)]
pub struct RecordingCommandRunner {
    pub calls: Vec<RecordedCommand>,
    pub results: Vec<CommandResult>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedCommand {
    pub program: String,
    pub args: Vec<String>,
    pub stdin: Option<Vec<u8>>,
}

impl CommandRunner for RecordingCommandRunner {
    fn run(
        &mut self,
        program: &str,
        args: &[String],
        stdin: Option<&[u8]>,
    ) -> Result<CommandResult, DeployError> {
        self.calls.push(RecordedCommand {
            program: program.to_owned(),
            args: args.to_vec(),
            stdin: stdin.map(<[u8]>::to_vec),
        });
        Ok(self.results.pop().unwrap_or(CommandResult {
            status: 0,
            stdout: String::new(),
            stderr: String::new(),
        }))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeployUser {
    pub name: String,
    pub group: String,
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    pub home: PathBuf,
    pub direct_root: bool,
    pub sudo: bool,
}

impl DeployUser {
    pub fn resolve(environment: &[(String, String)]) -> Self {
        let value = |key: &str| {
            environment
                .iter()
                .find(|(candidate, _)| candidate == key)
                .map(|(_, value)| value.trim().to_owned())
                .filter(|value| !value.is_empty())
        };
        if let Some(name) = value("SUDO_USER") {
            let uid = value("SUDO_UID").and_then(|raw| raw.parse().ok());
            let gid = value("SUDO_GID").and_then(|raw| raw.parse().ok());
            return Self {
                group: value("SUDO_GID").unwrap_or_else(|| name.clone()),
                home: value("HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("/var/empty")),
                name,
                uid,
                gid,
                direct_root: false,
                sudo: true,
            };
        }
        let uid = value("EUID").and_then(|raw| raw.parse().ok());
        let direct_root = uid == Some(0);
        let name =
            value("USER").unwrap_or_else(|| if direct_root { "root" } else { "eggpool" }.into());
        let group = value("GROUP").unwrap_or_else(|| name.clone());
        Self {
            home: value("HOME").map(PathBuf::from).unwrap_or_else(|| {
                if direct_root {
                    PathBuf::from("/root")
                } else {
                    PathBuf::from(".")
                }
            }),
            name,
            group,
            uid,
            gid: value("GID").and_then(|raw| raw.parse().ok()),
            direct_root,
            sudo: false,
        }
    }

    pub fn current() -> Self {
        let environment = env::vars().collect::<Vec<_>>();
        let mut user = Self::resolve(&environment);
        #[cfg(unix)]
        {
            user.direct_root = nix::unistd::geteuid().is_root() && !user.sudo;
            if user.uid.is_none() {
                user.uid = Some(nix::unistd::getuid().as_raw());
            }
            if user.gid.is_none() {
                user.gid = Some(nix::unistd::getgid().as_raw());
            }
        }
        user
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PersonalSystemdSpec {
    pub binary: PathBuf,
    pub config: PathBuf,
    pub data_dir: PathBuf,
    pub env_file: Option<PathBuf>,
    pub user: String,
    pub group: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProductionSystemdSpec {
    pub binary: PathBuf,
}

pub fn render_personal_systemd(spec: &PersonalSystemdSpec) -> String {
    let environment_file = spec
        .env_file
        .as_ref()
        .map(|path| format!("\nEnvironmentFile={}", systemd_quote(path)))
        .unwrap_or_default();
    format!(
        "[Unit]\nDescription=EggPool\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser={}\nGroup={}\nExecStart={} --config {} serve\nWorkingDirectory={}\nEnvironment=EGGPOOL_CONFIG={}\n# Configuration changes require: systemctl restart eggpool\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nKillSignal=SIGTERM{}\n\n[Install]\nWantedBy=multi-user.target\n",
        systemd_word(&spec.user),
        systemd_word(&spec.group),
        systemd_quote(&spec.binary),
        systemd_quote(&spec.config),
        systemd_quote(&spec.data_dir),
        systemd_quote(&spec.config),
        environment_file,
    )
}

pub fn render_production_systemd(spec: &ProductionSystemdSpec) -> String {
    format!(
        "[Unit]\nDescription=EggPool\nDocumentation=https://github.com/eggstack/eggpool\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=eggpool\nGroup=eggpool\nWorkingDirectory=/var/lib/eggpool\nExecStart={} --config /etc/eggpool/config.toml serve\nRestart=on-failure\nRestartSec=5\nStartLimitIntervalSec=300\nStartLimitBurst=5\n\n# Graceful shutdown\nTimeoutStopSec=30\nKillSignal=SIGTERM\n\n# Security hardening\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nReadWritePaths=/var/lib/eggpool /var/lib/eggpool/backups /var/log/eggpool\nPrivateTmp=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\nRestrictSUIDSGID=yes\nRestrictNamespaces=yes\nRestrictRealtime=yes\nLockPersonality=yes\n\n# Network\nRestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n\n# System call filtering\nSystemCallFilter=@system-service\nSystemCallArchitectures=native\n\nEnvironmentFile=/etc/eggpool/env\nEnvironment=EGGPOOL_LOG_FILE=/var/log/eggpool/eggpool.log\n\n[Install]\nWantedBy=multi-user.target\n",
        systemd_quote(&spec.binary),
    )
}

pub fn render_logrotate(log_dir: &Path) -> String {
    format!(
        "{}/*.log {{\n    daily\n    rotate 14\n    compress\n    delaycompress\n    missingok\n    notifempty\n    copytruncate\n    dateext\n    dateformat -%Y%m%d\n    maxsize 100M\n}}\n",
        log_dir.display()
    )
}

pub fn render_watchdog_cron(
    binary: &Path,
    config: &Path,
    log: &Path,
    interval_minutes: u64,
) -> Result<String, DeployError> {
    if !(1..=59).contains(&interval_minutes) {
        return Err(DeployError::Unsupported(format!(
            "watchdog interval must be 1-59 minutes, got {interval_minutes}"
        )));
    }
    let command = format!(
        "{} --config {} ensure-running",
        shell_quote(binary),
        shell_quote(config)
    );
    Ok(format!(
        "{WATCHDOG_BEGIN}\n@reboot {command} >> {} 2>&1\n*/{interval_minutes} * * * * {command} >> {} 2>&1\n{WATCHDOG_END}\n",
        shell_quote(log),
        shell_quote(log)
    ))
}

pub fn render_backup_cron(binary: &Path, config: &Path, production: bool) -> String {
    if production {
        format!(
            "# EggPool daily backup\n0 2 * * * root {}\n",
            shell_quote(Path::new(BACKUP_SCRIPT_PATH))
        )
    } else {
        format!(
            "{BACKUP_BEGIN}\n0 2 * * * {} --config {} backup\n{BACKUP_END}\n",
            shell_quote(binary),
            shell_quote(config)
        )
    }
}

pub fn render_backup_script(binary: &Path, config: &Path) -> String {
    format!(
        "#!/bin/sh\n# Managed by eggpool deploy backup-cron.\nset -eu\nexec {} --config {} backup\n",
        shell_quote(binary),
        shell_quote(config)
    )
}

pub fn strip_managed_cron_blocks(text: &str) -> String {
    let mut output = Vec::new();
    let mut inside = false;
    let mut saw_open = false;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("# BEGIN EggPool") {
            inside = true;
            saw_open = true;
            continue;
        }
        if inside && trimmed.starts_with("# END EggPool") {
            inside = false;
            continue;
        }
        if !inside {
            output.push(line);
        }
    }
    if saw_open && inside {
        return text.to_owned();
    }
    let mut result = output.join("\n");
    if text.ends_with('\n') {
        result.push('\n');
    }
    result
}

pub fn merge_cron_block(existing: &str, block: &str) -> String {
    let base = strip_managed_cron_blocks(existing).trim_end().to_owned();
    if base.is_empty() {
        block.to_owned()
    } else {
        format!("{base}\n\n{block}")
    }
}

pub fn write_atomic(path: &Path, content: &[u8], mode: u32) -> Result<(), DeployError> {
    reject_symlink_ancestors(path)?;
    let parent = path
        .parent()
        .ok_or_else(|| DeployError::UnsafePath(path.display().to_string()))?;
    fs::create_dir_all(parent)?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos());
    let temporary = parent.join(format!("{TEMP_PREFIX}-{nonce}"));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.set_permissions(fs::Permissions::from_mode(mode))?;
        file.write_all(content)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        Ok::<(), io::Error>(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map_err(DeployError::Io)
}

pub fn install_cron_block<R: CommandRunner>(
    runner: &mut R,
    user: &str,
    block: &str,
) -> Result<(), DeployError> {
    let user_args = vec!["-u".to_owned(), user.to_owned(), "-l".to_owned()];
    let listed = runner.run("crontab", &user_args, None)?;
    let existing = if listed.status == 0 {
        listed.stdout
    } else {
        String::new()
    };
    let content = merge_cron_block(&existing, block);
    let write_args = vec!["-u".to_owned(), user.to_owned(), "-".to_owned()];
    run_required(runner, "crontab", &write_args, Some(content.as_bytes()))
}

pub fn uninstall_cron_blocks<R: CommandRunner>(
    runner: &mut R,
    user: &str,
) -> Result<(), DeployError> {
    let list_args = vec!["-u".to_owned(), user.to_owned(), "-l".to_owned()];
    let listed = runner.run("crontab", &list_args, None)?;
    if listed.status != 0 {
        return Ok(());
    }
    let content = strip_managed_cron_blocks(&listed.stdout);
    if content == listed.stdout {
        return Ok(());
    }
    let write_args = vec!["-u".to_owned(), user.to_owned(), "-".to_owned()];
    run_required(runner, "crontab", &write_args, Some(content.as_bytes()))
}

pub fn install_systemd<R: CommandRunner>(
    runner: &mut R,
    unit_path: &Path,
    unit: &str,
    config_path: &Path,
    directories: &[(PathBuf, u32, String)],
    yes: bool,
) -> Result<(), DeployError> {
    if !yes {
        return Err(DeployError::Cancelled);
    }
    validate_config(config_path)?;
    for (directory, mode, owner) in directories {
        fs::create_dir_all(directory)?;
        fs::set_permissions(directory, fs::Permissions::from_mode(*mode))?;
        let chown_args = vec![owner.clone(), directory.display().to_string()];
        run_required(runner, "chown", &chown_args, None)?;
    }
    write_atomic(unit_path, unit.as_bytes(), 0o644)?;
    run_required(runner, "systemctl", &["daemon-reload".to_owned()], None)?;
    run_required(
        runner,
        "systemctl",
        &["enable".to_owned(), SERVICE_NAME.to_owned()],
        None,
    )?;
    run_required(
        runner,
        "systemctl",
        &["start".to_owned(), SERVICE_NAME.to_owned()],
        None,
    )?;
    Ok(())
}

pub fn install_logrotate<R: CommandRunner>(
    runner: &mut R,
    target: &Path,
    content: &str,
    yes: bool,
) -> Result<(), DeployError> {
    if !yes {
        return Err(DeployError::Cancelled);
    }
    write_atomic(target, content.as_bytes(), 0o644)?;
    let args = vec!["-d".to_owned(), target.display().to_string()];
    let result = runner.run("logrotate", &args, None)?;
    if result.status != 0 {
        return Err(DeployError::Command {
            program: "logrotate".to_owned(),
            args: args.join(" "),
            detail: result.stderr,
        });
    }
    Ok(())
}

pub fn validate_config(path: &Path) -> Result<Config, DeployError> {
    let config = Config::from_toml(path)?;
    config.validate_account_credentials()?;
    Ok(config)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UninstallTargets {
    pub binary: PathBuf,
    pub config: PathBuf,
    pub env: Option<PathBuf>,
    pub data_dir: PathBuf,
    pub state_dir: PathBuf,
    pub systemd_unit: PathBuf,
    pub logrotate: PathBuf,
    pub production_cron: PathBuf,
    pub backup_script: PathBuf,
    pub shell_rc_files: Vec<PathBuf>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct KeepFlags {
    pub data: bool,
    pub config: bool,
    pub path: bool,
    pub deploy_artifacts: bool,
}

pub fn resolve_rust_binary() -> Result<PathBuf, DeployError> {
    let path = env::current_exe().map_err(DeployError::Io)?;
    if !path.is_file() {
        return Err(DeployError::Requires("a resolvable Rust executable"));
    }
    Ok(path)
}

pub fn uninstall<R: CommandRunner>(
    runner: &mut R,
    targets: &UninstallTargets,
    keep: KeepFlags,
    yes: bool,
) -> Result<Vec<PathBuf>, DeployError> {
    if !yes {
        return Err(DeployError::Cancelled);
    }
    stop_owned_service(runner)?;
    if !keep.deploy_artifacts {
        remove_known_file(&targets.systemd_unit)?;
        remove_known_file(&targets.logrotate)?;
        remove_known_file(&targets.production_cron)?;
        remove_known_file(&targets.backup_script)?;
        uninstall_cron_blocks(runner, &resolved_cron_user())?;
        let daemon_reload = vec!["daemon-reload".to_owned()];
        run_required(runner, "systemctl", &daemon_reload, None)?;
    }
    if !keep.config {
        remove_known_file(&targets.config)?;
        if let Some(env_path) = &targets.env {
            remove_known_file(env_path)?;
        }
    }
    if !keep.data {
        remove_owned_tree(&targets.data_dir)?;
        remove_owned_tree(&targets.state_dir)?;
    }
    if !keep.path {
        for path in &targets.shell_rc_files {
            scrub_path_file(path)?;
        }
    }
    remove_known_file(&targets.binary)?;
    let leftovers = [
        &targets.binary,
        &targets.config,
        &targets.data_dir,
        &targets.state_dir,
        &targets.systemd_unit,
        &targets.logrotate,
        &targets.production_cron,
        &targets.backup_script,
    ]
    .into_iter()
    .filter(|path| path.exists())
    .cloned()
    .collect();
    Ok(leftovers)
}

fn stop_owned_service<R: CommandRunner>(runner: &mut R) -> Result<(), DeployError> {
    let stop = vec![
        "disable".to_owned(),
        "--now".to_owned(),
        SERVICE_NAME.to_owned(),
    ];
    let result = runner.run("systemctl", &stop, None)?;
    if result.status != 0 && !result.stderr.contains("not loaded") {
        return Err(DeployError::Command {
            program: "systemctl".to_owned(),
            args: stop.join(" "),
            detail: result.stderr,
        });
    }
    Ok(())
}

pub fn run_required<R: CommandRunner>(
    runner: &mut R,
    program: &str,
    args: &[String],
    stdin: Option<&[u8]>,
) -> Result<(), DeployError> {
    let result = runner.run(program, args, stdin)?;
    if result.status == 0 {
        return Ok(());
    }
    Err(DeployError::Command {
        program: program.to_owned(),
        args: args.join(" "),
        detail: if result.stderr.is_empty() {
            result.stdout
        } else {
            result.stderr
        },
    })
}

fn reject_symlink(path: &Path) -> Result<(), DeployError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            Err(DeployError::UnsafePath(path.display().to_string()))
        }
        Ok(_) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(DeployError::Io(error)),
    }
}

fn reject_symlink_ancestors(path: &Path) -> Result<(), DeployError> {
    reject_symlink(path)?;
    let mut ancestors = path.parent();
    while let Some(parent) = ancestors {
        match fs::symlink_metadata(parent) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                // macOS exposes /var as a compatibility symlink to
                // /private/var.  It is an OS-owned prefix, not an
                // EggPool-controlled escape; descendants are still checked.
                if parent != Path::new("/var") {
                    return Err(DeployError::UnsafePath(parent.display().to_string()));
                }
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(DeployError::Io(error)),
        }
        ancestors = parent.parent();
    }
    Ok(())
}

fn remove_known_file(path: &Path) -> Result<(), DeployError> {
    reject_symlink_ancestors(path)?;
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() => Err(DeployError::UnsafePath(format!(
            "refusing to remove directory as a file: {}",
            path.display()
        ))),
        Ok(_) => fs::remove_file(path).map_err(DeployError::Io),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(DeployError::Io(error)),
    }
}

pub fn remove_artifact(path: &Path) -> Result<(), DeployError> {
    remove_known_file(path)
}

fn remove_owned_tree(path: &Path) -> Result<(), DeployError> {
    let resolved = path.canonicalize().unwrap_or_else(|_| path.to_owned());
    if resolved == Path::new("/") || resolved.parent().is_none() {
        return Err(DeployError::UnsafePath(path.display().to_string()));
    }
    if resolved == home_dir() || resolved.parent() == Some(Path::new("/")) {
        return Err(DeployError::UnsafePath(path.display().to_string()));
    }
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(DeployError::Io(error)),
    };
    if metadata.file_type().is_symlink() {
        return Err(DeployError::UnsafePath(path.display().to_string()));
    }
    if !metadata.is_dir() {
        return Err(DeployError::UnsafePath(path.display().to_string()));
    }
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let child = entry.path();
        let child_metadata = fs::symlink_metadata(&child)?;
        if child_metadata.file_type().is_symlink() {
            fs::remove_file(child)?;
        } else if child_metadata.is_dir() {
            remove_owned_tree(&child)?;
        } else {
            fs::remove_file(child)?;
        }
    }
    fs::remove_dir(path).map_err(DeployError::Io)
}

fn scrub_path_file(path: &Path) -> Result<(), DeployError> {
    reject_symlink_ancestors(path)?;
    let contents = match fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(DeployError::Io(error)),
    };
    let kept = contents
        .lines()
        .filter(|line| {
            let lower = line.to_ascii_lowercase();
            !(lower.contains("eggpool") && (lower.contains("path") || lower.contains("export")))
        })
        .collect::<Vec<_>>();
    let mut result = kept.join("\n");
    if contents.ends_with('\n') {
        result.push('\n');
    }
    if result != contents {
        write_atomic(path, result.as_bytes(), 0o644)?;
    }
    Ok(())
}

fn resolved_cron_user() -> String {
    env::var("SUDO_USER")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| env::var("USER").ok())
        .unwrap_or_else(|| "root".to_owned())
}

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/root"))
}

fn validate_systemd_word(value: &str) -> String {
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || "_-.".contains(character))
    {
        value.to_owned()
    } else {
        systemd_escape(value)
    }
}

fn systemd_word(value: &str) -> String {
    validate_systemd_word(value)
}

fn systemd_quote(path: &Path) -> String {
    systemd_escape(&path.display().to_string())
}

fn systemd_escape(value: &str) -> String {
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || "/._-:@+".contains(character))
    {
        return value.to_owned();
    }
    format!(
        "\"{}\"",
        value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('%', "%%")
    )
}

fn shell_quote(path: &Path) -> String {
    let value = path.display().to_string();
    format!("'{}'", value.replace('\'', "'\\''"))
}
