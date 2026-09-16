//! Atomic filesystem mutation.
//!
//! For each target: read without following unsafe path changes where the
//! platform permits, require a regular file or absent path, construct the
//! complete proposed bytes in memory, validate before write, stage in a
//! same-directory temporary file with restrictive permissions, fsync, and
//! atomically replace. No in-place seek/truncate writes.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::outcome::ConnectError;

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Observed target state before mutation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetKind {
    Absent,
    RegularFile,
}

/// Inspect a target without following unsafe path changes.
///
/// Rejects symlinks, directories, devices, FIFOs, and sockets. A
/// config-path override is the local user's explicit input; it never
/// authorizes writes to arbitrary profile-supplied paths (profiles carry no
/// paths by construction).
pub fn inspect_target(path: &Path) -> Result<(TargetKind, Option<u32>), ConnectError> {
    let metadata = match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok((TargetKind::Absent, None));
        }
        Err(error) => {
            return Err(ConnectError::Io {
                detail: format!("cannot inspect {}: {error}", path.display()),
            });
        }
        Ok(metadata) => metadata,
    };
    if metadata.file_type().is_symlink() {
        return Err(ConnectError::UnsafeConfig {
            detail: format!(
                "refusing to follow symlink at {} (use an explicit file path)",
                path.display()
            ),
        });
    }
    if metadata.is_dir() {
        return Err(ConnectError::UnsafeConfig {
            detail: format!("refusing to overwrite directory {}", path.display()),
        });
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileTypeExt;
        let file_type = metadata.file_type();
        if file_type.is_fifo()
            || file_type.is_socket()
            || file_type.is_block_device()
            || file_type.is_char_device()
        {
            return Err(ConnectError::UnsafeConfig {
                detail: format!(
                    "refusing to write special file {} (device/FIFO/socket)",
                    path.display()
                ),
            });
        }
    }
    if !metadata.is_file() {
        return Err(ConnectError::UnsafeConfig {
            detail: format!("refusing to write non-regular file {}", path.display()),
        });
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        Ok((
            TargetKind::RegularFile,
            Some(metadata.permissions().mode() & 0o777),
        ))
    }
    #[cfg(not(unix))]
    {
        Ok((TargetKind::RegularFile, None))
    }
}

/// Read existing bytes after a safety inspection.
pub fn read_existing(path: &Path) -> Result<Option<Vec<u8>>, ConnectError> {
    match inspect_target(path)? {
        (TargetKind::Absent, _) => Ok(None),
        (TargetKind::RegularFile, _) => {
            fs::read(path).map(Some).map_err(|error| ConnectError::Io {
                detail: format!("cannot read {}: {error}", path.display()),
            })
        }
    }
}

/// Atomically replace `path` with `bytes`.
///
/// Stages in a same-directory temporary file with owner-only permissions,
/// flushes/fsyncs, preserves the existing mode when the target existed, and
/// renames. Returns the previous mode (Unix) for backup metadata.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<Option<u32>, ConnectError> {
    let (kind, previous_mode) = inspect_target(path)?;
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent).map_err(|error| ConnectError::Mutation {
            detail: format!("cannot create parent {}: {error}", parent.display()),
        })?;
    }
    let temp = temp_path_for(path);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&temp)
        .map_err(|error| ConnectError::Mutation {
            detail: format!("cannot stage temporary file: {error}"),
        })?;
    file.write_all(bytes).map_err(|error| {
        let _ = fs::remove_file(&temp);
        ConnectError::Mutation {
            detail: format!("cannot write temporary file: {error}"),
        }
    })?;
    file.flush().map_err(|error| {
        let _ = fs::remove_file(&temp);
        ConnectError::Mutation {
            detail: format!("cannot flush temporary file: {error}"),
        }
    })?;
    // fsync where practical; a failed sync fails the write before rename.
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp);
        return Err(ConnectError::Mutation {
            detail: format!("cannot sync temporary file: {error}"),
        });
    }
    drop(file);
    // Preserve the intended mode: existing files keep their mode; new files
    // stay owner-only.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Some(mode) = previous_mode {
            let _ = fs::set_permissions(&temp, fs::Permissions::from_mode(mode));
        }
    }
    fs::rename(&temp, path).map_err(|error| {
        let _ = fs::remove_file(&temp);
        ConnectError::Mutation {
            detail: format!("cannot replace {}: {error}", path.display()),
        }
    })?;
    // Best-effort parent sync for durability; failures do not fail the write.
    if let Some(parent) = path.parent()
        && let Ok(dir) = fs::File::open(parent)
    {
        let _ = dir.sync_all();
    }
    let _ = kind;
    Ok(previous_mode)
}

fn temp_path_for(path: &Path) -> PathBuf {
    let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "config".to_owned());
    let temp_name = format!(".{file_name}.eggpool-connect-{pid}-{counter}.tmp");
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.join(temp_name),
        _ => PathBuf::from(temp_name),
    }
}

/// Restore bytes atomically (used by rollback and `restore`).
pub fn atomic_restore(path: &Path, bytes: Option<&[u8]>) -> Result<(), ConnectError> {
    match bytes {
        Some(content) => {
            atomic_write(path, content).map(|_| ())?;
            Ok(())
        }
        None => {
            // The file was originally absent: remove the mutated file so
            // restore is byte-exact (absent stays absent).
            match fs::remove_file(path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(ConnectError::Mutation {
                    detail: format!("cannot remove {}: {error}", path.display()),
                }),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_target_reads_as_none() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("missing.toml");
        assert_eq!(read_existing(&path).expect("read"), None);
    }

    #[test]
    fn atomic_write_round_trips_and_replaces() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("config.toml");
        atomic_write(&path, b"first\n").expect("write");
        assert_eq!(
            read_existing(&path).expect("read"),
            Some(b"first\n".to_vec())
        );
        atomic_write(&path, b"second\n").expect("rewrite");
        assert_eq!(
            read_existing(&path).expect("read"),
            Some(b"second\n".to_vec())
        );
        // No temp files leak in the target directory.
        let leftovers: Vec<_> = std::fs::read_dir(dir.path())
            .expect("readdir")
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .contains("eggpool-connect")
            })
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn atomic_restore_of_absent_removes_file() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("config.toml");
        atomic_write(&path, b"created\n").expect("write");
        atomic_restore(&path, None).expect("restore absent");
        assert!(!path.exists());
    }

    #[test]
    #[cfg(unix)]
    fn symlink_targets_are_refused() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().expect("tempdir");
        let real = dir.path().join("real.toml");
        std::fs::write(&real, b"real\n").expect("write");
        let link = dir.path().join("link.toml");
        symlink(&real, &link).expect("symlink");
        assert!(inspect_target(&link).is_err());
        assert!(atomic_write(&link, b"evil\n").is_err());
    }
}
