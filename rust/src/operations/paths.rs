//! Canonical, read-only runtime path resolution for local operations.
//!
//! Resolution is intentionally side-effect free.  Callers that are about to
//! bind a control socket or write lifecycle state must use the explicit
//! `ensure_*` helpers, which create and validate private directories.

use std::{
    env,
    fs::{self, OpenOptions},
    io,
    path::{Path, PathBuf},
};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

const CONTROL_SOCKET_NAME: &str = "eggpool.sock";
const PID_FILE_NAME: &str = "eggpool.pid";
const LOG_FILE_NAME: &str = "eggpool.log";

/// A snapshot of the environment values used by path resolution.
///
/// Keeping this as a value makes precedence tests deterministic without
/// mutating the process environment (which is unsafe while tests run in
/// parallel).
#[derive(Debug, Clone, Default)]
pub struct PathEnvironment {
    pub home: Option<PathBuf>,
    pub cwd: Option<PathBuf>,
    pub eggpool_config: Option<String>,
    pub eggpool_env: Option<String>,
    pub eggpool_runtime_dir: Option<PathBuf>,
    pub eggpool_pid_file: Option<PathBuf>,
    pub eggpool_log_file: Option<PathBuf>,
    pub xdg_config_home: Option<PathBuf>,
    pub xdg_data_home: Option<PathBuf>,
    pub xdg_state_home: Option<PathBuf>,
    pub xdg_runtime_dir: Option<PathBuf>,
    pub uid: u32,
}

impl PathEnvironment {
    pub fn current() -> Self {
        Self {
            home: env::var_os("HOME").map(PathBuf::from),
            cwd: env::current_dir().ok(),
            eggpool_config: env::var("EGGPOOL_CONFIG").ok(),
            eggpool_env: env::var("EGGPOOL_ENV").ok(),
            eggpool_runtime_dir: env::var_os("EGGPOOL_RUNTIME_DIR").map(PathBuf::from),
            eggpool_pid_file: env::var_os("EGGPOOL_PID_FILE").map(PathBuf::from),
            eggpool_log_file: env::var_os("EGGPOOL_LOG_FILE").map(PathBuf::from),
            xdg_config_home: env::var_os("XDG_CONFIG_HOME").map(PathBuf::from),
            xdg_data_home: env::var_os("XDG_DATA_HOME").map(PathBuf::from),
            xdg_state_home: env::var_os("XDG_STATE_HOME").map(PathBuf::from),
            xdg_runtime_dir: env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from),
            uid: current_uid(),
        }
    }

    fn home(&self) -> PathBuf {
        self.home.clone().unwrap_or_else(|| PathBuf::from("."))
    }

    fn cwd(&self) -> PathBuf {
        self.cwd.clone().unwrap_or_else(|| PathBuf::from("."))
    }
}

/// All local paths used by O002 and later lifecycle commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimePaths {
    pub config_path: PathBuf,
    pub config_dir: PathBuf,
    pub data_dir: PathBuf,
    pub state_dir: PathBuf,
    pub env_path: Option<PathBuf>,
    pub runtime_dir: PathBuf,
    pub pid_file: PathBuf,
    pub log_file: PathBuf,
    pub control_socket: PathBuf,
}

impl RuntimePaths {
    /// Resolve paths using the current process environment without creating
    /// files or directories.
    pub fn resolve() -> Self {
        Self::resolve_with(&PathEnvironment::current())
    }

    pub fn resolve_with(environment: &PathEnvironment) -> Self {
        let home = environment.home();
        let cwd = environment.cwd();
        let config_dir = environment
            .xdg_config_home
            .clone()
            .unwrap_or_else(|| home.join(".config"))
            .join("eggpool");
        let config_path = environment
            .eggpool_config
            .as_deref()
            .filter(|value| !value.trim().is_empty())
            .map(|value| expand(value, &cwd, &home))
            .or_else(|| {
                let candidate = config_dir.join("config.toml");
                candidate.exists().then_some(absolute(candidate, &cwd))
            })
            .unwrap_or_else(|| absolute(cwd.join("config.toml"), &cwd));
        let data_dir = environment
            .xdg_data_home
            .clone()
            .unwrap_or_else(|| home.join(".local/share"))
            .join("eggpool");
        let state_dir = environment
            .xdg_state_home
            .clone()
            .unwrap_or_else(|| home.join(".local/state"))
            .join("eggpool");
        let runtime_dir = resolve_runtime_dir(environment, &state_dir, &home);
        let pid_file = environment.eggpool_pid_file.clone().unwrap_or_else(|| {
            environment
                .xdg_runtime_dir
                .clone()
                .map(|path| path.join(PID_FILE_NAME))
                .or_else(|| state_dir.exists().then(|| state_dir.join(PID_FILE_NAME)))
                .unwrap_or_else(|| uid_tmp(environment.uid, "pid"))
        });
        let log_file = environment.eggpool_log_file.clone().unwrap_or_else(|| {
            if state_dir.exists() {
                state_dir.join(LOG_FILE_NAME)
            } else {
                uid_tmp(environment.uid, "log")
            }
        });
        let env_path = environment
            .eggpool_env
            .as_deref()
            .filter(|value| !value.trim().is_empty())
            .map(|value| expand(value, &cwd, &home))
            .or_else(|| {
                let alongside = config_path.parent()?.join(".env");
                alongside.exists().then_some(absolute(alongside, &cwd))
            });
        let control_socket = runtime_dir.join(CONTROL_SOCKET_NAME);
        Self {
            config_path,
            config_dir,
            data_dir,
            state_dir,
            env_path,
            runtime_dir,
            pid_file,
            log_file,
            control_socket,
        }
    }

    /// Create and validate the private state directory used by lifecycle
    /// files.  This is the only path helper that intentionally mutates disk.
    pub fn ensure_state_dir(&self) -> Result<(), PathError> {
        ensure_private_dir(&self.state_dir)
    }

    /// Create and validate the private runtime directory used by the socket.
    pub fn ensure_runtime_dir(&self) -> Result<(), PathError> {
        ensure_private_dir(&self.runtime_dir)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum PathError {
    #[error("private runtime path is unsupported on this platform")]
    UnsupportedPlatform,
    #[error("private runtime path is not a directory")]
    NotDirectory,
    #[error("private runtime path has unsafe ownership or permissions")]
    UnsafeDirectory,
    #[error("private runtime path could not be prepared")]
    Io(#[source] io::Error),
}

fn resolve_runtime_dir(environment: &PathEnvironment, state_dir: &Path, home: &Path) -> PathBuf {
    if let Some(path) = &environment.eggpool_runtime_dir {
        return path.clone();
    }
    if let Some(path) = &environment.xdg_runtime_dir {
        if is_private_directory(path) {
            return path.join("eggpool");
        }
    }
    if is_private_directory(state_dir) {
        return state_dir.join("runtime");
    }
    let _ = home; // Keeps the resolution inputs explicit for future deploy-user rules.
    uid_tmp(environment.uid, "runtime")
}

fn uid_tmp(uid: u32, suffix: &str) -> PathBuf {
    PathBuf::from(format!("/tmp/eggpool-{uid}.{suffix}"))
}

fn expand(value: &str, cwd: &Path, home: &Path) -> PathBuf {
    if let Some(tail) = value.strip_prefix("~/") {
        return home.join(tail);
    }
    let path = PathBuf::from(value);
    if path.is_absolute() {
        path
    } else {
        cwd.join(path)
    }
}

fn absolute(path: PathBuf, cwd: &Path) -> PathBuf {
    if path.is_absolute() {
        path
    } else {
        cwd.join(path)
    }
}

#[cfg(unix)]
fn current_uid() -> u32 {
    nix::unistd::geteuid().as_raw()
}

#[cfg(not(unix))]
fn current_uid() -> u32 {
    0
}

#[cfg(unix)]
fn is_private_directory(path: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    metadata.is_dir() && metadata.uid() == current_uid() && metadata.mode() & 0o077 == 0
}

#[cfg(not(unix))]
fn is_private_directory(_path: &Path) -> bool {
    false
}

#[cfg(unix)]
fn ensure_private_dir(path: &Path) -> Result<(), PathError> {
    if path.exists() {
        if !is_private_directory(path) {
            return Err(PathError::UnsafeDirectory);
        }
        return Ok(());
    }
    fs::create_dir_all(path).map_err(PathError::Io)?;
    let mut permissions = fs::metadata(path).map_err(PathError::Io)?.permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(path, permissions).map_err(PathError::Io)?;
    if is_private_directory(path) {
        Ok(())
    } else {
        Err(PathError::UnsafeDirectory)
    }
}

#[cfg(not(unix))]
fn ensure_private_dir(_path: &Path) -> Result<(), PathError> {
    Err(PathError::UnsupportedPlatform)
}

/// Create a private 0600 file without replacing an existing path.
pub(crate) fn create_private_file(path: &Path) -> Result<fs::File, PathError> {
    #[cfg(unix)]
    {
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(PathError::Io)
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Err(PathError::UnsupportedPlatform)
    }
}

pub(crate) fn ensure_private_directory(path: &Path) -> Result<(), PathError> {
    ensure_private_dir(path)
}
