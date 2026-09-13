//! Safe local server lifecycle workflows for CLI and configuration operations.
//!
//! `process` remains the primitive owner for PID files, probes, identity
//! evidence, and signaling. This module composes those primitives into the
//! start/stop/restart/watchdog workflows while leaving CLI presentation to
//! `runtime`.

use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

use tokio::time::{Instant, sleep};

use crate::{Config, operations::paths::RuntimePaths};

use super::process::{self, ControlProbe, HealthProbe, ProcessError, ProcessIdentityProof};

const WATCHDOG_START_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, thiserror::Error)]
pub enum LifecycleError {
    #[error(transparent)]
    Process(#[from] ProcessError),
    #[error(transparent)]
    Path(#[from] super::paths::PathError),
    #[error("{0}")]
    Message(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopOutcome {
    NoPid,
    StalePid,
    Stopped { pid: i32 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestartOutcome {
    AlreadyStopped,
    Started { pid: u32 },
    Restarted { pid: u32 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnsureRunningOutcome {
    AlreadyRunning,
    AlreadyHealthy,
    Started { pid: u32 },
}

pub fn ensure_start_safe() -> Result<(), LifecycleError> {
    let paths = RuntimePaths::prepare()?;
    let pid = process::read_pid(&paths.pid_file)?;
    if let Some(pid) = pid {
        if process::process_exists(pid) {
            return Err(LifecycleError::Message(format!(
                "server is already running (PID {pid})"
            )));
        }
        process::clear_stale_pid(&paths.pid_file, Some(pid))?;
    }
    Ok(())
}

pub async fn ensure_start_safe_with_listener(config: &Config) -> Result<(), LifecycleError> {
    ensure_start_safe()?;
    if process::probe_health(&config.server.host, config.server.port).await == HealthProbe::Healthy
    {
        return Err(LifecycleError::Message(format!(
            "another process is already serving {}:{}",
            config.server.host, config.server.port
        )));
    }
    Ok(())
}

pub fn spawn_detached(
    config_path: &Path,
    log_path: Option<&Path>,
    quiet: bool,
) -> Result<Child, LifecycleError> {
    let executable = std::env::current_exe().map_err(|error| {
        LifecycleError::Message(format!("cannot resolve eggpool executable: {error}"))
    })?;
    let resolved_config = fs::canonicalize(config_path)
        .map_err(|error| LifecycleError::Message(format!("cannot resolve config path: {error}")))?;
    let paths = RuntimePaths::prepare()?;
    let explicit_log = log_path.map(PathBuf::from);
    let log_target = if quiet && explicit_log.is_none() {
        None
    } else {
        if explicit_log.is_none() {
            paths
                .ensure_state_dir()
                .map_err(|error| LifecycleError::Message(error.to_string()))?;
        }
        Some(explicit_log.unwrap_or(paths.log_file))
    };
    let (stdout, stderr) = if let Some(log_path) = log_target.as_deref() {
        let file = open_log(log_path)?;
        let duplicate = file.try_clone().map_err(|error| {
            LifecycleError::Message(format!("cannot duplicate log handle: {error}"))
        })?;
        (Stdio::from(file), Stdio::from(duplicate))
    } else {
        (Stdio::null(), Stdio::null())
    };
    let mut command = Command::new(executable);
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
        .map_err(|error| LifecycleError::Message(format!("failed to spawn server: {error}")))
}

pub async fn stop(
    config_path: &Path,
    timeout: Duration,
    before_signal: impl FnOnce(i32),
) -> Result<StopOutcome, LifecycleError> {
    let paths = RuntimePaths::resolve();
    let Some(pid) = process::read_pid(&paths.pid_file)? else {
        return Ok(StopOutcome::NoPid);
    };
    if !process::process_exists(pid) {
        process::clear_stale_pid(&paths.pid_file, Some(pid))?;
        return Ok(StopOutcome::StalePid);
    }
    let proof = identity_proof(config_path, &paths, pid).await;
    if !proof.proves_eggpool() {
        return Err(LifecycleError::Message(
            "refusing to signal PID without independent EggPool identity evidence".to_owned(),
        ));
    }
    before_signal(pid);
    process::signal_term(pid, proof)?;
    if !process::wait_for_exit_or_pid_clear(pid, &paths.pid_file, timeout).await {
        return Err(LifecycleError::Message(format!(
            "server did not stop within {}s",
            timeout.as_secs_f64()
        )));
    }
    process::clear_pid_if_matches(&paths.pid_file, pid)?;
    Ok(StopOutcome::Stopped { pid })
}

pub async fn restart(
    config_path: &Path,
    config: &Config,
    timeout: Duration,
    start_if_missing: bool,
    before_signal: impl FnOnce(i32),
) -> Result<RestartOutcome, LifecycleError> {
    let paths = RuntimePaths::resolve();
    let Some(pid) = process::read_pid(&paths.pid_file)? else {
        if !start_if_missing {
            return Ok(RestartOutcome::AlreadyStopped);
        }
        ensure_listener_is_free(config).await?;
        let child = spawn_detached(config_path, None, false)?;
        return Ok(RestartOutcome::Started { pid: child.id() });
    };

    if process::process_exists(pid) {
        let proof = identity_proof(config_path, &paths, pid).await;
        if !proof.proves_eggpool() {
            return Err(LifecycleError::Message(
                "refusing to restart an unproven process associated with the PID file".to_owned(),
            ));
        }
        before_signal(pid);
        process::signal_term(pid, proof)?;
        if !process::wait_for_exit_or_pid_clear(pid, &paths.pid_file, timeout).await {
            return Err(LifecycleError::Message(format!(
                "server did not stop within {}s; replacement was not started",
                timeout.as_secs_f64()
            )));
        }
        process::clear_pid_if_matches(&paths.pid_file, pid)?;
    } else {
        process::clear_stale_pid(&paths.pid_file, Some(pid))?;
        if !start_if_missing {
            return Ok(RestartOutcome::AlreadyStopped);
        }
    }

    ensure_listener_is_free(config).await?;
    let child = spawn_detached(config_path, None, false)?;
    Ok(RestartOutcome::Restarted { pid: child.id() })
}

pub async fn ensure_running(
    config_path: &Path,
    config: &Config,
) -> Result<EnsureRunningOutcome, LifecycleError> {
    let paths = RuntimePaths::prepare()?;
    if process::read_pid(&paths.pid_file)?.is_some_and(process::process_exists) {
        return Ok(EnsureRunningOutcome::AlreadyRunning);
    }
    let _ = process::clear_stale_pid(&paths.pid_file, None)?;
    let guard = process::acquire_start_guard(&paths)?;
    if process::read_pid(&paths.pid_file)?.is_some_and(process::process_exists) {
        drop(guard);
        return Ok(EnsureRunningOutcome::AlreadyRunning);
    }
    if process::probe_health(&config.server.host, config.server.port).await == HealthProbe::Healthy
    {
        drop(guard);
        return Ok(EnsureRunningOutcome::AlreadyHealthy);
    }
    let child = spawn_detached(config_path, None, false)?;
    let deadline = Instant::now() + WATCHDOG_START_TIMEOUT;
    loop {
        if process::read_pid(&paths.pid_file)?.is_some_and(process::process_exists)
            || process::probe_health(&config.server.host, config.server.port).await
                == HealthProbe::Healthy
        {
            drop(guard);
            return Ok(EnsureRunningOutcome::Started { pid: child.id() });
        }
        if Instant::now() >= deadline {
            return Err(LifecycleError::Message(format!(
                "server did not confirm startup within 2s (child PID {})",
                child.id()
            )));
        }
        sleep(Duration::from_millis(50)).await;
    }
}

pub async fn server_is_running(config_path: &Path, paths: &RuntimePaths) -> bool {
    let Some(pid) = process::read_pid(&paths.pid_file).ok().flatten() else {
        return false;
    };
    process::process_exists(pid)
        && identity_proof(config_path, paths, pid)
            .await
            .proves_eggpool()
}

async fn ensure_listener_is_free(config: &Config) -> Result<(), LifecycleError> {
    if process::probe_health(&config.server.host, config.server.port).await == HealthProbe::Healthy
    {
        return Err(LifecycleError::Message(
            "replacement was not started because the configured listener is still healthy"
                .to_owned(),
        ));
    }
    Ok(())
}

async fn identity_proof(
    config_path: &Path,
    paths: &RuntimePaths,
    pid: i32,
) -> ProcessIdentityProof {
    let health = match Config::from_toml(config_path) {
        Ok(config) => process::probe_health(&config.server.host, config.server.port).await,
        Err(_) => HealthProbe::Unreachable,
    };
    let control = process::probe_control(&paths.control_socket).await;
    ProcessIdentityProof {
        pid_file_matches: process::read_pid(&paths.pid_file).ok().flatten() == Some(pid),
        health,
        control_socket_reachable: control == ControlProbe::Reachable,
    }
}

fn open_log(path: &Path) -> Result<File, LifecycleError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            LifecycleError::Message(format!("cannot prepare log directory: {error}"))
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
        .map_err(|error| LifecycleError::Message(format!("cannot open log file: {error}")))
}
