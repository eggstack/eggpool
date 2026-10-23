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
    /// A lifecycle deadline could not be represented on this platform.  This
    /// is the single typed failure for the checked-deadline helper; every
    /// consumer converts it into its own existing failure category.
    #[error("process lifecycle deadline is not representable")]
    Deadline,
}

/// Compute a lifecycle deadline from `duration` without panicking.
///
/// `Instant::now() + duration` panics when the result is not representable.
/// A configured interval large enough to overflow the platform's instant
/// range must fail closed with a typed error instead of crashing the
/// process.  This helper is owned by the local process lifecycle; the server
/// and runtime lifecycle modules consume it as a stable contract.
pub fn checked_deadline(duration: Duration) -> Result<Instant, ProcessError> {
    Instant::now()
        .checked_add(duration)
        .ok_or(ProcessError::Deadline)
}

/// Resolve, read-only, the process files for the current environment.
pub fn runtime_paths() -> RuntimePaths {
    RuntimePaths::resolve()
}

/// Read a positive decimal PID.  Missing or malformed files are observations,
/// not fatal process errors, because stale local state must be recoverable.
/// An existing but unsafe file (symlink, special, foreign owner) fails closed
/// instead of reading as absent: treating it as `None` would let a second
/// server start alongside the owner.
pub fn read_pid(path: &Path) -> Result<Option<i32>, ProcessError> {
    if !safe_pid_file(path) {
        if std::fs::symlink_metadata(path).is_ok() {
            return Err(ProcessError::UnsafePidPath);
        }
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
    // Unpredictable temp (pid + counter) with `create_new` so a reused PID
    // cannot collide on a predictable name.
    static PID_TMP_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let temporary = parent.join(format!(
        ".{}.tmp-{}-{}",
        file_name.to_string_lossy(),
        pid,
        PID_TMP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
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
///
/// An unrepresentable timeout yields no deadline, so the wait ends at the
/// existing "not observed gone" outcome instead of panicking.
pub async fn wait_for_exit(pid: i32, timeout_duration: Duration) -> bool {
    let deadline = checked_deadline(timeout_duration).ok();
    while deadline.is_some_and(|deadline| Instant::now() < deadline) {
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
///
/// An unrepresentable timeout yields no deadline, so the wait ends at the
/// existing "not observed gone" outcome instead of panicking.
pub async fn wait_for_exit_or_pid_clear(
    pid: i32,
    pid_file: &Path,
    timeout_duration: Duration,
) -> bool {
    let deadline = checked_deadline(timeout_duration).ok();
    while deadline.is_some_and(|deadline| Instant::now() < deadline) {
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
        if contents.trim() != self.pid.to_string() {
            return;
        }
        // Non-atomic read+remove: verify the path still refers to our own
        // file (the same file as the held create-new handle) so a replacement
        // created between read and remove — including under a reused PID —
        // is never deleted.
        if !path_names_published_file(&self.path, &self._file) {
            return;
        }
        let _ = fs::remove_file(&self.path);
    }
}

/// The ownership state of an existing lock file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LockOwner {
    /// The lock file no longer exists.
    Absent,
    /// The lock file exists but its owner could not be determined: it could
    /// not be read, or its contents did not parse as a process id.  An
    /// undeterminable owner is never treated as a stale owner.
    Undetermined,
    /// The lock file was read successfully and is empty.  An empty lock
    /// names no owner: a publisher creates the file before it writes its
    /// process id into it, so an empty file is a publication that never
    /// completed.  The publisher confirms that the path still names its own
    /// file after it writes, so reclaiming an ownerless lock can never hand
    /// the lock to two processes at once.
    Ownerless { identity: FileIdentity },
    /// The lock file was read and parsed into a positive process id.  The
    /// identity is the file identity observed at read time.
    Parsed { pid: i32, identity: FileIdentity },
}

/// The identity of a lock file.  It proves that the file was not replaced
/// between the ownership check and the removal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FileIdentity {
    /// The first identity component: the device on unix, the file length
    /// elsewhere.
    first: u64,
    /// The second identity component: the inode on unix, the file's modified
    /// time in nanoseconds elsewhere.
    second: u64,
}

/// Acquire the bounded watchdog guard, recovering only an owner PID that is
/// demonstrably gone.  The file is private and create-new, so two cron
/// invocations cannot both enter the spawn section.
pub fn acquire_start_guard(paths: &RuntimePaths) -> Result<StartGuard, ProcessError> {
    acquire_start_guard_with_observer(paths, |_| {})
}

/// Acquire the bounded watchdog guard with an explicit observation seam.
///
/// `observer` is invoked after a lock file has been classified as
/// reclaimable and before it is moved aside.  The seam exists so the
/// removal race can be driven deterministically instead of by timing.
pub fn acquire_start_guard_with_observer(
    paths: &RuntimePaths,
    observer: impl Fn(&Path),
) -> Result<StartGuard, ProcessError> {
    acquire_start_guard_with_seams(paths, observer, |_| {})
}

/// Acquire the bounded watchdog guard with both ownership seams.
///
/// `before_remove` is invoked after a lock file has been classified as
/// reclaimable and before it is moved aside.  `after_publish` is invoked
/// after this process has written its owner into the lock file and before
/// the path is confirmed to still name that file.  The seams exist so the
/// two ownership races can be driven deterministically instead of by timing.
pub fn acquire_start_guard_with_seams(
    paths: &RuntimePaths,
    before_remove: impl Fn(&Path),
    after_publish: impl Fn(&Path),
) -> Result<StartGuard, ProcessError> {
    paths.ensure_state_dir()?;
    let path = paths.state_dir.join("eggpool.ensure-running.lock");
    for _ in 0..2 {
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                let pid = std::process::id() as i32;
                if let Err(error) = publish_lock_owner(&mut file, pid) {
                    // The publication never completed.  Remove the file this
                    // process created so it cannot wedge the next acquisition.
                    discard_published_lock(&path, &file);
                    return Err(error);
                }
                after_publish(&path);
                // A reclaim that raced this publication may have moved the
                // lock aside.  Confirm the path still names the published
                // file before treating the acquisition as held, otherwise two
                // processes could enter the spawn section together.
                if !path_names_published_file(&path, &file) {
                    return Err(ProcessError::LockHeld);
                }
                return Ok(StartGuard {
                    path,
                    pid,
                    _file: file,
                });
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                match observe_lock_owner(&path) {
                    LockOwner::Absent => continue,
                    LockOwner::Undetermined => return Err(ProcessError::LockHeld),
                    LockOwner::Ownerless { identity } => {
                        before_remove(&path);
                        match remove_ownerless_lock(&path, identity)? {
                            LockRemoval::Removed | LockRemoval::Vanished => continue,
                            LockRemoval::Replaced => return Err(ProcessError::LockHeld),
                        }
                    }
                    LockOwner::Parsed { pid, identity } => {
                        if process_exists(pid) {
                            return Err(ProcessError::LockHeld);
                        }
                        before_remove(&path);
                        match remove_observed_lock(&path, identity)? {
                            LockRemoval::Removed | LockRemoval::Vanished => continue,
                            LockRemoval::Replaced => return Err(ProcessError::LockHeld),
                        }
                    }
                }
            }
            Err(error) => return Err(ProcessError::Lock(error)),
        }
    }
    Err(ProcessError::LockHeld)
}

/// Write this process's owner into a freshly created lock file.
///
/// The owner is written before the file is treated as published, and the
/// permissions are set on the open handle so the path is never modified
/// through its name.
fn publish_lock_owner(file: &mut File, pid: i32) -> Result<(), ProcessError> {
    use std::io::Write;
    file.write_all(pid.to_string().as_bytes())
        .map_err(ProcessError::Lock)?;
    file.sync_all().map_err(ProcessError::Lock)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(ProcessError::Lock)?;
    }
    Ok(())
}

/// Whether the lock path still names the file this process published.
fn path_names_published_file(path: &Path, file: &File) -> bool {
    let Some(published) = file.metadata().ok().map(|metadata| identity_of(&metadata)) else {
        return false;
    };
    let Some(current) = fs::metadata(path)
        .ok()
        .map(|metadata| identity_of(&metadata))
    else {
        return false;
    };
    published == current
}

/// Remove a lock file this process created but never finished publishing.
fn discard_published_lock(path: &Path, file: &File) {
    if !path_names_published_file(path, file) {
        return;
    }
    let _ = fs::remove_file(path);
}

/// Read the owner of an existing lock file.
///
/// The owner is reported only when the file was read successfully and parsed
/// into a positive process id.  A file that cannot be read, or that does not
/// parse, yields `Undetermined`: the owner cannot be confirmed absent, so the
/// lock is held and the file is left in place.  An empty file yields
/// `Ownerless`: a publisher creates the file before it writes its process id
/// into it, so an empty file names no owner and is a publication that never
/// completed.
fn observe_lock_owner(path: &Path) -> LockOwner {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return LockOwner::Absent,
        Err(_) => return LockOwner::Undetermined,
    };
    if !metadata.file_type().is_file() {
        return LockOwner::Undetermined;
    }
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return LockOwner::Absent,
        Err(_) => return LockOwner::Undetermined,
    };
    let Ok(metadata) = file.metadata() else {
        return LockOwner::Undetermined;
    };
    let identity = identity_of(&metadata);
    let mut contents = String::new();
    if std::io::Read::read_to_string(&mut file, &mut contents).is_err() {
        return LockOwner::Undetermined;
    }
    let trimmed = contents.trim();
    if trimmed.is_empty() {
        return LockOwner::Ownerless { identity };
    }
    let Ok(pid) = trimmed.parse::<i32>() else {
        return LockOwner::Undetermined;
    };
    if pid <= 0 {
        return LockOwner::Undetermined;
    }
    LockOwner::Parsed { pid, identity }
}

/// The conclusion of a lock-file removal attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LockRemoval {
    /// The lock file was removed; the lock is free.
    Removed,
    /// The lock path no longer exists; the lock is free.
    Vanished,
    /// The lock path names a live lock that was not removed.
    Replaced,
}

/// The conclusion of moving a lock file aside.
enum AsideOutcome {
    /// The lock path no longer exists.
    Vanished,
    /// The moved file is not the file that was observed; it was put back.
    Foreign,
    /// The moved file is the observed file.
    Ours { quarantine: PathBuf },
}

/// Move a lock file aside and determine whether the moved file is still the
/// exact file that was observed.
///
/// The rename is atomic, so the file that lands at the quarantine path is
/// either the observed file or a replacement published after the observation.
/// A replacement is put back and never removed.
fn move_lock_aside(path: &Path, observed: FileIdentity) -> Result<AsideOutcome, ProcessError> {
    let quarantine = quarantine_path(path);
    match fs::rename(path, &quarantine) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(AsideOutcome::Vanished);
        }
        Err(error) => return Err(ProcessError::Io(error)),
    }
    let moved = fs::metadata(&quarantine)
        .ok()
        .map(|metadata| identity_of(&metadata));
    match moved {
        Some(moved) if moved == observed => Ok(AsideOutcome::Ours { quarantine }),
        _ => {
            // Another process replaced the lock after the ownership check.
            // Put the moved file back instead of deleting a live lock.
            restore_lock(&quarantine, path);
            Ok(AsideOutcome::Foreign)
        }
    }
}

/// Remove a stale lock file, but only when it is still the exact file whose
/// owner was confirmed absent.
///
/// The file is moved aside with an atomic rename and its identity is
/// re-verified before the removal.  A lock created by another process after
/// the ownership check is therefore never deleted: it is moved back to its
/// original name and the acquisition fails with a held lock.
fn remove_observed_lock(path: &Path, observed: FileIdentity) -> Result<LockRemoval, ProcessError> {
    match move_lock_aside(path, observed)? {
        AsideOutcome::Vanished => Ok(LockRemoval::Vanished),
        AsideOutcome::Foreign => Ok(LockRemoval::Replaced),
        AsideOutcome::Ours { quarantine } => match fs::remove_file(&quarantine) {
            Ok(()) => Ok(LockRemoval::Removed),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(LockRemoval::Removed),
            Err(error) => Err(ProcessError::Io(error)),
        },
    }
}

/// Reclaim an ownerless lock file: a lock that was created but never named
/// an owner.
///
/// The file is moved aside and read again.  A publisher that raced the
/// reclaim wrote its process id into the moved file; that file names an
/// owner, so it is put back and the acquisition fails with a held lock.  A
/// moved file that is still empty named no owner and is removed.
fn remove_ownerless_lock(path: &Path, observed: FileIdentity) -> Result<LockRemoval, ProcessError> {
    match move_lock_aside(path, observed)? {
        AsideOutcome::Vanished => Ok(LockRemoval::Vanished),
        AsideOutcome::Foreign => Ok(LockRemoval::Replaced),
        AsideOutcome::Ours { quarantine } => {
            let contents = match fs::read_to_string(&quarantine) {
                Ok(contents) => contents,
                Err(_) => {
                    // The moved file cannot be read back, so its owner still
                    // cannot be determined.  Put it back and fail closed.
                    restore_lock(&quarantine, path);
                    return Ok(LockRemoval::Replaced);
                }
            };
            if !contents.trim().is_empty() {
                // A publisher named an owner while the file was being moved
                // aside.  The lock is live; put it back.
                restore_lock(&quarantine, path);
                return Ok(LockRemoval::Replaced);
            }
            match fs::remove_file(&quarantine) {
                Ok(()) => Ok(LockRemoval::Removed),
                Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(LockRemoval::Removed),
                Err(error) => Err(ProcessError::Io(error)),
            }
        }
    }
}

/// Put a moved lock file back at its original name.  A newer lock is never
/// clobbered: when the original name is occupied again the moved file is
/// left where it is.
fn restore_lock(quarantine: &Path, path: &Path) {
    if fs::hard_link(quarantine, path).is_ok() {
        let _ = fs::remove_file(quarantine);
    }
}

/// A unique sibling name used to move a stale lock file aside.
fn quarantine_path(path: &Path) -> PathBuf {
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "lock".to_owned());
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    parent.join(format!(
        "{name}.stale-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ))
}

fn identity_of(metadata: &fs::Metadata) -> FileIdentity {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        FileIdentity {
            first: metadata.dev(),
            second: metadata.ino(),
        }
    }
    #[cfg(not(unix))]
    {
        // A host without inode numbers has no stable file identity, so the
        // file's modified time and length stand in for it.  Two different
        // lock files that share both are indistinguishable, which can only
        // make the identity check more conservative, never less.
        let modified = metadata
            .modified()
            .ok()
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|elapsed| elapsed.as_nanos() as u64)
            .unwrap_or(0);
        FileIdentity {
            first: metadata.len(),
            second: modified,
        }
    }
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
    probe_endpoint(host, port, "/v1/healthz").await
}

/// Probe readiness independently from PID and health state.
pub async fn probe_readiness(host: &str, port: u16) -> HealthProbe {
    probe_endpoint(host, port, "/v1/readyz").await
}

async fn probe_endpoint(host: &str, port: u16, path: &str) -> HealthProbe {
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
        let result = timeout(HEALTH_PROBE_TIMEOUT, endpoint_request(addresses, path)).await;
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

async fn endpoint_request(addresses: Vec<SocketAddr>, path: &str) -> io::Result<bool> {
    let mut stream = TcpStream::connect(addresses.as_slice()).await?;
    let request = format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).await?;
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
