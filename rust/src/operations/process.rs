//! Conservative process and health observations for M9 lifecycle commands.
//!
//! A PID is advisory state.  A TERM is permitted only when the PID file still
//! names the target and a separate health/control observation proves that the
//! process is EggPool.  This prevents a reused PID from receiving a signal.

use std::{
    fs::{self, File, OpenOptions},
    io,
    net::{IpAddr, SocketAddr},
    path::{Path, PathBuf},
    time::Duration,
};

use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::{Instant, timeout},
};

#[cfg(unix)]
use std::os::unix::fs::MetadataExt;

use super::paths::{PathError, RuntimePaths, create_private_file, ensure_private_directory};

const MAX_HEALTH_RESPONSE_BYTES: usize = 8192;
const HEALTH_PROBE_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Debug, thiserror::Error)]
pub enum ProcessError {
    #[error("process signaling is unsupported on this platform")]
    UnsupportedPlatform,
    #[error("PID file contains an invalid process id")]
    InvalidPid,
    #[error("PID file path is not safe")]
    UnsafePidPath,
    #[error("PID file operation failed")]
    Io(#[source] io::Error),
    #[error("private runtime path operation failed")]
    Path(#[from] PathError),
    #[error("process lock is already held")]
    LockHeld,
    #[error("process lock could not be acquired")]
    Lock(io::Error),
}

/// Resolve, read-only, the process files for the current environment.
pub fn runtime_paths() -> RuntimePaths {
    RuntimePaths::resolve()
}

/// Read a positive decimal PID.  Missing or malformed files are observations,
/// not fatal process errors, because stale local state must be recoverable.
pub fn read_pid(path: &Path) -> Result<Option<i32>, ProcessError> {
    if !safe_pid_file(path) {
        return Ok(None);
    }
    let contents = match std::fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(ProcessError::Io(error)),
    };
    let Ok(value) = contents.trim().parse::<i32>() else {
        return Ok(None);
    };
    if value <= 0 {
        return Ok(None);
    }
    Ok(Some(value))
}

/// Atomically publish a decimal PID with private file permissions.
pub fn write_pid_atomic(path: &Path, pid: i32) -> Result<(), ProcessError> {
    if pid <= 0 {
        return Err(ProcessError::InvalidPid);
    }
    let parent = path.parent().ok_or(ProcessError::UnsafePidPath)?;
    ensure_private_parent(parent)?;
    let file_name = path.file_name().ok_or(ProcessError::UnsafePidPath)?;
    let temporary = parent.join(format!(".{}.tmp-{}", file_name.to_string_lossy(), pid));
    let mut file = match create_private_file(&temporary) {
        Ok(file) => file,
        Err(PathError::Io(error)) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(ProcessError::Io(error));
        }
        Err(error) => return Err(ProcessError::Path(error)),
    };
    use std::io::Write;
    let result = (|| {
        file.write_all(pid.to_string().as_bytes())?;
        file.sync_all()?;
        if path.exists() && !safe_pid_file(path) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unsafe PID file target",
            ));
        }
        std::fs::rename(&temporary, path)?;
        Ok::<(), io::Error>(())
    })();
    if let Err(error) = result {
        let _ = std::fs::remove_file(&temporary);
        return Err(ProcessError::Io(error));
    }
    Ok(())
}

/// Remove a PID file only when it still belongs to the caller's process.
/// This prevents a retiring process from deleting a replacement's PID file.
pub fn clear_pid_if_matches(path: &Path, pid: i32) -> Result<bool, ProcessError> {
    if pid <= 0 || !safe_pid_file(path) || read_pid(path)? != Some(pid) {
        return Ok(false);
    }
    match fs::remove_file(path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(ProcessError::Io(error)),
    }
}

/// Remove a PID file only if it still contains the expected PID and that PID
/// is no longer alive.
pub fn clear_stale_pid(path: &Path, expected_pid: Option<i32>) -> Result<bool, ProcessError> {
    if !safe_pid_file(path) {
        return Ok(false);
    }
    let Some(pid) = read_pid(path)? else {
        if path.exists() {
            std::fs::remove_file(path).map_err(ProcessError::Io)?;
            return Ok(true);
        }
        return Ok(false);
    };
    if expected_pid.is_some_and(|expected| expected != pid) || process_exists(pid) {
        return Ok(false);
    }
    match std::fs::remove_file(path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(ProcessError::Io(error)),
    }
}

/// Probe kernel process existence.  Permission failures are false: existence
/// without the ability to prove ownership must not enable lifecycle actions.
pub fn process_exists(pid: i32) -> bool {
    #[cfg(unix)]
    {
        nix::sys::signal::kill(nix::unistd::Pid::from_raw(pid), None).is_ok()
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        false
    }
}

/// Evidence required before signaling a process.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProcessIdentityProof {
    pub pid_file_matches: bool,
    pub health: HealthProbe,
    pub control_socket_reachable: bool,
}

impl ProcessIdentityProof {
    pub fn proves_eggpool(self) -> bool {
        self.pid_file_matches
            && (self.health == HealthProbe::Healthy || self.control_socket_reachable)
    }
}

/// Send TERM only after PID plus independent EggPool evidence has been
/// collected by the caller.
pub fn signal_term(pid: i32, proof: ProcessIdentityProof) -> Result<(), ProcessError> {
    if pid <= 0 || !proof.proves_eggpool() {
        return Err(ProcessError::UnsafePidPath);
    }
    #[cfg(unix)]
    {
        nix::sys::signal::kill(
            nix::unistd::Pid::from_raw(pid),
            nix::sys::signal::Signal::SIGTERM,
        )
        .map_err(|error| ProcessError::Io(io::Error::other(error.to_string())))
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        Err(ProcessError::UnsupportedPlatform)
    }
}

/// Wait without blocking the Tokio executor.  `true` means the process was
/// observed gone before the deadline.
pub async fn wait_for_exit(pid: i32, timeout_duration: Duration) -> bool {
    let deadline = Instant::now() + timeout_duration;
    while Instant::now() < deadline {
        if !process_exists(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    !process_exists(pid)
}

/// Wait for a process to exit or for its owner to retire the matching PID
/// file.  Some Unix hosts continue to report a just-reaped child as existing
/// to unrelated observers, so the private PID file is also part of the
/// lifecycle evidence.
pub async fn wait_for_exit_or_pid_clear(
    pid: i32,
    pid_file: &Path,
    timeout_duration: Duration,
) -> bool {
    let deadline = Instant::now() + timeout_duration;
    while Instant::now() < deadline {
        if read_pid(pid_file).ok().flatten() != Some(pid) || !process_exists(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    read_pid(pid_file).ok().flatten() != Some(pid) || !process_exists(pid)
}

/// A create-new local guard for the watchdog start race.
pub struct StartGuard {
    path: PathBuf,
    pid: i32,
    _file: File,
}

impl Drop for StartGuard {
    fn drop(&mut self) {
        let Ok(contents) = fs::read_to_string(&self.path) else {
            return;
        };
        if contents.trim() == self.pid.to_string() {
            let _ = fs::remove_file(&self.path);
        }
    }
}

/// Acquire the bounded watchdog guard, recovering only an owner PID that is
/// demonstrably gone.  The file is private and create-new, so two cron
/// invocations cannot both enter the spawn section.
pub fn acquire_start_guard(paths: &RuntimePaths) -> Result<StartGuard, ProcessError> {
    paths.ensure_state_dir()?;
    let path = paths.state_dir.join("eggpool.ensure-running.lock");
    for _ in 0..2 {
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                use std::io::Write;
                let pid = std::process::id() as i32;
                file.write_all(pid.to_string().as_bytes())
                    .map_err(ProcessError::Lock)?;
                file.sync_all().map_err(ProcessError::Lock)?;
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                        .map_err(ProcessError::Lock)?;
                }
                return Ok(StartGuard {
                    path,
                    pid,
                    _file: file,
                });
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let owner = fs::read_to_string(&path)
                    .ok()
                    .and_then(|value| value.trim().parse::<i32>().ok());
                if owner.is_some_and(process_exists) {
                    return Err(ProcessError::LockHeld);
                }
                if owner.is_none_or(|value| !process_exists(value)) {
                    let _ = fs::remove_file(&path);
                    continue;
                }
                return Err(ProcessError::LockHeld);
            }
            Err(error) => return Err(ProcessError::Lock(error)),
        }
    }
    Err(ProcessError::LockHeld)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HealthProbe {
    Healthy,
    Unhealthy,
    Unreachable,
    Unsupported,
}

/// Probe the data-plane health endpoint separately from PID state.
pub async fn probe_health(host: &str, port: u16) -> HealthProbe {
    #[cfg(not(unix))]
    {
        let _ = (host, port);
        return HealthProbe::Unsupported;
    }
    #[cfg(unix)]
    {
        let host = match host {
            "0.0.0.0" | "::" => "127.0.0.1",
            value => value,
        };
        let addresses = match host.parse::<IpAddr>() {
            Ok(ip) => vec![SocketAddr::new(ip, port)],
            Err(_) => match tokio::net::lookup_host((host, port)).await {
                Ok(addresses) => addresses.collect(),
                Err(_) => return HealthProbe::Unreachable,
            },
        };
        let result = timeout(HEALTH_PROBE_TIMEOUT, health_request(addresses)).await;
        match result {
            Ok(Ok(healthy)) => {
                if healthy {
                    HealthProbe::Healthy
                } else {
                    HealthProbe::Unhealthy
                }
            }
            _ => HealthProbe::Unreachable,
        }
    }
}

async fn health_request(addresses: Vec<SocketAddr>) -> io::Result<bool> {
    let mut stream = TcpStream::connect(addresses.as_slice()).await?;
    stream
        .write_all(b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await?;
    let mut response = Vec::with_capacity(256);
    let mut chunk = [0_u8; 1024];
    while response.len() < MAX_HEALTH_RESPONSE_BYTES {
        let count = stream.read(&mut chunk).await?;
        if count == 0 {
            break;
        }
        let remaining = MAX_HEALTH_RESPONSE_BYTES - response.len();
        response.extend_from_slice(&chunk[..count.min(remaining)]);
        if response.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let first_line = response
        .split(|byte| *byte == b'\n')
        .next()
        .unwrap_or_default();
    Ok(first_line.starts_with(b"HTTP/1.1 200 ") || first_line.starts_with(b"HTTP/1.0 200 "))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlProbe {
    Reachable,
    Unreachable,
    Unsupported,
}

#[cfg(unix)]
pub async fn probe_control(path: &Path) -> ControlProbe {
    match timeout(HEALTH_PROBE_TIMEOUT, tokio::net::UnixStream::connect(path)).await {
        Ok(Ok(_)) => ControlProbe::Reachable,
        _ => ControlProbe::Unreachable,
    }
}

#[cfg(not(unix))]
pub async fn probe_control(_path: &Path) -> ControlProbe {
    ControlProbe::Unsupported
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcessState {
    Running,
    Stopped,
    StalePid,
    PortOccupiedHealthyUnknownOwner,
    Error,
}

/// Combine independent observations without treating a PID alone as proof.
pub fn classify(
    pid: Option<i32>,
    pid_alive: bool,
    health: HealthProbe,
    control_socket_reachable: bool,
) -> ProcessState {
    if pid.is_some_and(|_| pid_alive)
        && (health == HealthProbe::Healthy || control_socket_reachable)
    {
        ProcessState::Running
    } else if pid.is_some() && !pid_alive {
        ProcessState::StalePid
    } else if pid.is_some() {
        ProcessState::Error
    } else if health == HealthProbe::Healthy || control_socket_reachable {
        ProcessState::PortOccupiedHealthyUnknownOwner
    } else {
        ProcessState::Stopped
    }
}

fn ensure_private_parent(path: &Path) -> Result<(), ProcessError> {
    ensure_private_directory(path).map_err(ProcessError::Path)
}

fn safe_pid_file(path: &Path) -> bool {
    #[cfg(unix)]
    {
        let Ok(metadata) = std::fs::symlink_metadata(path) else {
            return false;
        };
        metadata.file_type().is_file() && metadata.uid() == nix::unistd::geteuid().as_raw()
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        false
    }
}
