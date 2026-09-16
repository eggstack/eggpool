//! Byte-exact backups with secret-free manifests.
//!
//! Every mutation commits a durable byte-exact snapshot before the first
//! write. Layout (portably resolved):
//!
//! ```text
//! <user-state>/eggpool-connect/
//!   backups/
//!     <backup-id>/
//!       manifest.json
//!       config.bin            (absent when the target file was absent)
//!       generated-artifacts/… (pre-write helper-owned artifacts, e.g. Codex catalog)
//! ```
//!
//! Backup and state roots are user-private (owner-only on POSIX). Retention
//! keeps the newest N per target and never silently deletes the only usable
//! recovery point.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use eggpool_client_config::{ClientTarget, sha256_hex};
use serde::{Deserialize, Serialize};

use crate::atomic::read_existing;
use crate::outcome::ConnectError;
use crate::paths::backups_root_for;

/// Current backup-manifest schema version.
pub const BACKUP_SCHEMA_VERSION: u32 = 1;
/// Newest backups retained per target (conservative, documented).
pub const BACKUP_RETENTION_PER_TARGET: usize = 10;

/// Helper version recorded in manifests (tracks the helper crate).
pub const HELPER_VERSION: &str = env!("CARGO_PKG_VERSION");

/// One generated artifact snapshot owned by this install.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactRecord {
    /// Helper-relative artifact name (for example `codex-catalog.json`).
    pub name: String,
    /// Absolute receiving-machine path the artifact was read from.
    pub path: PathBuf,
    /// SHA-256 of the pre-write bytes (`None` when the artifact was absent).
    pub sha256: Option<String>,
    pub existed: bool,
}

/// Secret-free backup manifest. Never carries credentials, backup contents,
/// or unrelated config values — only hashes, paths, and recovery metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackupManifest {
    pub schema_version: u32,
    pub backup_id: String,
    pub created_unix: u64,
    pub target: String,
    pub config_path: PathBuf,
    pub config_existed: bool,
    pub pre_sha256: String,
    pub file_mode: Option<u32>,
    pub client_version: Option<String>,
    pub schema_variant: String,
    /// SHA-256 of the canonical connection profile JSON (fingerprint, never
    /// a credential or token).
    pub profile_fingerprint: String,
    pub generated_artifacts: Vec<ArtifactRecord>,
    pub parent_backup: Option<String>,
    pub helper_version: String,
    /// Previous EggPool-owned values captured before mutation (for
    /// ownership-aware `remove`; secret-free).
    #[serde(default)]
    pub previous_values: BTreeMap<String, String>,
}

impl BackupManifest {
    pub fn validate(&self) -> Result<(), ConnectError> {
        if self.schema_version != BACKUP_SCHEMA_VERSION {
            return Err(ConnectError::Backup {
                detail: format!(
                    "unsupported backup schema {}; expected {BACKUP_SCHEMA_VERSION}",
                    self.schema_version
                ),
            });
        }
        if self.backup_id.is_empty() || self.backup_id.len() > 128 {
            return Err(ConnectError::Backup {
                detail: "backup ID has invalid length".to_owned(),
            });
        }
        if self.target != "codex" && self.target != "opencode" {
            return Err(ConnectError::Backup {
                detail: "backup target must be codex or opencode".to_owned(),
            });
        }
        if self.pre_sha256.len() != 64 || !self.pre_sha256.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(ConnectError::Backup {
                detail: "backup hash must be hex SHA-256".to_owned(),
            });
        }
        Ok(())
    }
}

/// Committed backup record.
#[derive(Debug, Clone)]
pub struct BackupRecord {
    pub id: String,
    pub dir: PathBuf,
    pub manifest: BackupManifest,
}

/// Inputs for [`create_backup`]. Filesystem reads stay here; pure policy
/// lives in the portable crate.
pub struct BackupInput<'a> {
    pub state_root: &'a Path,
    pub target: ClientTarget,
    pub config_path: &'a Path,
    pub profile_fingerprint: &'a str,
    pub client_version: Option<&'a str>,
    pub schema_variant: &'a str,
    /// Pre-write helper-owned artifacts to snapshot: `(name, path)`.
    pub artifacts: &'a [(String, PathBuf)],
    pub previous_values: BTreeMap<String, String>,
}

/// Create a durable byte-exact backup before the first write.
pub fn create_backup(input: BackupInput<'_>) -> Result<BackupRecord, ConnectError> {
    let state_root = input.state_root;
    let backups_root = backups_root_for(state_root);
    ensure_private_dir(&backups_root)?;

    let existing = read_existing(input.config_path).map_err(|error| ConnectError::Backup {
        detail: error.to_string(),
    })?;
    let (kind, mode) = crate::atomic::inspect_target(input.config_path)
        .map(|(kind, mode)| (format!("{kind:?}"), mode))
        .unwrap_or_else(|_| ("Absent".to_owned(), None));
    let _ = kind;
    let config_existed = existing.is_some();
    let pre_bytes = existing.as_deref().unwrap_or(b"");
    let pre_sha256 = sha256_hex(pre_bytes);

    let id = new_backup_id();
    let dir = backups_root.join(&id);
    ensure_private_dir(&dir)?;

    // Snapshot generated artifacts (pre-write bytes, byte-exact).
    let mut records = Vec::with_capacity(input.artifacts.len());
    let artifacts_dir = dir.join("generated-artifacts");
    if !input.artifacts.is_empty() {
        ensure_private_dir(&artifacts_dir)?;
    }
    for (name, path) in input.artifacts {
        let bytes = read_existing(path).map_err(|error| ConnectError::Backup {
            detail: format!("cannot snapshot artifact {}: {error}", path.display()),
        })?;
        let record = ArtifactRecord {
            name: name.clone(),
            path: path.clone(),
            sha256: bytes.as_ref().map(|content| sha256_hex(content)),
            existed: bytes.is_some(),
        };
        if let Some(content) = bytes {
            let safe_name = safe_artifact_name(name)?;
            write_private_file(&artifacts_dir.join(safe_name), &content)?;
        }
        records.push(record);
    }

    if let Some(content) = &existing {
        write_private_file(&dir.join("config.bin"), content)?;
    }

    let parent = latest_backup_id(state_root, input.target).ok().flatten();
    let manifest = BackupManifest {
        schema_version: BACKUP_SCHEMA_VERSION,
        backup_id: id.clone(),
        created_unix: now_unix(),
        target: input.target.name().to_owned(),
        config_path: normalize_path(input.config_path),
        config_existed,
        pre_sha256,
        file_mode: mode,
        client_version: input.client_version.map(str::to_owned),
        schema_variant: input.schema_variant.to_owned(),
        profile_fingerprint: input.profile_fingerprint.to_owned(),
        generated_artifacts: records,
        parent_backup: parent.filter(|previous| previous != &id),
        helper_version: HELPER_VERSION.to_owned(),
        previous_values: input.previous_values,
    };
    manifest.validate()?;
    let rendered = serde_json::to_vec_pretty(&manifest).map_err(|_| ConnectError::Backup {
        detail: "cannot serialize backup manifest".to_owned(),
    })?;
    write_private_file(&dir.join("manifest.json"), &rendered)?;

    // Conservative retention: keep newest N per target, never the only point.
    let _ = enforce_retention(state_root, input.target);

    Ok(BackupRecord { id, dir, manifest })
}

/// List backups newest-first, optionally filtered by target. Never prints
/// file contents.
pub fn list_backups(
    state_root: &Path,
    filter: Option<ClientTarget>,
) -> Result<Vec<BackupManifest>, ConnectError> {
    let root = backups_root_for(state_root);
    let mut manifests = Vec::new();
    let entries = match fs::read_dir(&root) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(ConnectError::Io {
                detail: format!("cannot list backups: {error}"),
            });
        }
        Ok(entries) => entries,
    };
    for entry in entries.flatten() {
        let manifest_path = entry.path().join("manifest.json");
        let bytes = match fs::read(&manifest_path) {
            Ok(bytes) => bytes,
            Err(_) => continue,
        };
        let manifest: BackupManifest = match serde_json::from_slice(&bytes) {
            Ok(manifest) => manifest,
            Err(_) => continue,
        };
        if manifest.validate().is_err() {
            continue;
        }
        if let Some(target) = filter
            && manifest.target != target.name()
        {
            continue;
        }
        manifests.push(manifest);
    }
    manifests.sort_by(|left, right| {
        right
            .created_unix
            .cmp(&left.created_unix)
            .then_with(|| right.backup_id.cmp(&left.backup_id))
    });
    Ok(manifests)
}

/// Load one backup by ID (validates the manifest).
pub fn load_backup(
    state_root: &Path,
    backup_id: &str,
) -> Result<(BackupManifest, PathBuf), ConnectError> {
    if backup_id.is_empty()
        || backup_id.len() > 128
        || backup_id.contains('/')
        || backup_id.contains('\\')
        || backup_id.contains('\0')
        || backup_id.contains("..")
    {
        return Err(ConnectError::UnknownBackup {
            detail: "backup ID has an invalid shape".to_owned(),
        });
    }
    let dir = backups_root_for(state_root).join(backup_id);
    let bytes = fs::read(dir.join("manifest.json")).map_err(|_| ConnectError::UnknownBackup {
        detail: format!("unknown backup {backup_id:?}"),
    })?;
    let manifest: BackupManifest =
        serde_json::from_slice(&bytes).map_err(|_| ConnectError::UnknownBackup {
            detail: format!("backup {backup_id:?} has an invalid manifest"),
        })?;
    manifest
        .validate()
        .map_err(|error| ConnectError::UnknownBackup {
            detail: error.to_string(),
        })?;
    Ok((manifest, dir))
}

/// Read the byte-exact pre-write config snapshot (`None` = originally absent).
pub fn read_snapshot(
    backup_dir: &Path,
    manifest: &BackupManifest,
) -> Result<Option<Vec<u8>>, ConnectError> {
    if !manifest.config_existed {
        return Ok(None);
    }
    let bytes = fs::read(backup_dir.join("config.bin")).map_err(|error| ConnectError::Backup {
        detail: format!("backup snapshot is missing: {error}"),
    })?;
    // Verify hash so a corrupt snapshot never silently restores.
    if sha256_hex(&bytes) != manifest.pre_sha256 {
        return Err(ConnectError::Backup {
            detail: "backup snapshot hash mismatch; refusing to restore".to_owned(),
        });
    }
    Ok(Some(bytes))
}

/// Read one snapshotted artifact by record name.
pub fn read_artifact_snapshot(
    backup_dir: &Path,
    name: &str,
) -> Result<Option<Vec<u8>>, ConnectError> {
    let safe = safe_artifact_name(name)?;
    match fs::read(backup_dir.join("generated-artifacts").join(safe)) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(ConnectError::Backup {
            detail: format!("cannot read artifact snapshot: {error}"),
        }),
    }
}

fn safe_artifact_name(name: &str) -> Result<String, ConnectError> {
    if name.is_empty() || name.len() > 128 {
        return Err(ConnectError::Backup {
            detail: "artifact name has invalid length".to_owned(),
        });
    }
    if name.contains('/') || name.contains('\\') || name.contains('\0') || name.contains("..") {
        return Err(ConnectError::Backup {
            detail: "artifact name must be a plain file name".to_owned(),
        });
    }
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
    {
        return Err(ConnectError::Backup {
            detail: "artifact name contains unsafe characters".to_owned(),
        });
    }
    Ok(name.to_owned())
}

fn latest_backup_id(
    state_root: &Path,
    target: ClientTarget,
) -> Result<Option<String>, ConnectError> {
    Ok(list_backups(state_root, Some(target))?
        .first()
        .map(|manifest| manifest.backup_id.clone()))
}

/// Keep the newest `BACKUP_RETENTION_PER_TARGET` per target. Never deletes
/// when that would remove the only recovery point.
fn enforce_retention(state_root: &Path, target: ClientTarget) -> Result<(), ConnectError> {
    let manifests = list_backups(state_root, Some(target))?;
    if manifests.len() <= BACKUP_RETENTION_PER_TARGET {
        return Ok(());
    }
    // Never delete the only known-good committed state: retention only trims
    // beyond the bound, oldest first, and stops at the bound.
    for stale in manifests.iter().skip(BACKUP_RETENTION_PER_TARGET) {
        let dir = backups_root_for(state_root).join(&stale.backup_id);
        let _ = fs::remove_dir_all(&dir);
    }
    Ok(())
}

fn new_backup_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let rand = std::process::id() as u128 ^ (nanos & 0xffff_ffff);
    format!("b{nanos:x}-{rand:x}")
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn normalize_path(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

fn ensure_private_dir(path: &Path) -> Result<(), ConnectError> {
    fs::create_dir_all(path).map_err(|error| ConnectError::Backup {
        detail: format!("cannot create {}: {error}", path.display()),
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o700));
    }
    Ok(())
}

fn write_private_file(path: &Path, bytes: &[u8]) -> Result<(), ConnectError> {
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|error| ConnectError::Backup {
        detail: format!("cannot write {}: {error}", path.display()),
    })?;
    use std::io::Write;
    file.write_all(bytes)
        .map_err(|error| ConnectError::Backup {
            detail: format!("cannot write {}: {error}", path.display()),
        })?;
    file.flush().map_err(|error| ConnectError::Backup {
        detail: format!("cannot flush {}: {error}", path.display()),
    })?;
    let _ = file.sync_all();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Fingerprint a connection profile (SHA-256 of canonical JSON, never a
/// credential or token).
#[must_use]
pub fn fingerprint_profile(canonical_json: &str) -> String {
    sha256_hex(canonical_json.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_FINGERPRINT: &str =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn input<'a>(state: &'a Path, config: &'a Path) -> BackupInput<'a> {
        BackupInput {
            state_root: state,
            target: ClientTarget::Codex,
            config_path: config,
            profile_fingerprint: TEST_FINGERPRINT,
            client_version: Some("0.154.0"),
            schema_variant: "codex-toml",
            artifacts: &[],
            previous_values: BTreeMap::new(),
        }
    }

    #[test]
    fn backup_is_byte_exact_including_absent() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("config.toml");
        // Originally absent.
        let record = create_backup(input(&state, &config)).expect("backup");
        assert!(!record.manifest.config_existed);
        assert!(!record.dir.join("config.bin").exists());
        assert!(record.dir.join("manifest.json").exists());
        assert_eq!(
            read_snapshot(&record.dir, &record.manifest).expect("snapshot"),
            None
        );

        std::fs::write(&config, b"hello\n").expect("write");
        let record = create_backup(input(&state, &config)).expect("backup");
        assert!(record.manifest.config_existed);
        assert_eq!(
            read_snapshot(&record.dir, &record.manifest).expect("snapshot"),
            Some(b"hello\n".to_vec())
        );
    }

    #[test]
    fn backup_manifest_is_secret_free_and_validated() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("config.toml");
        std::fs::write(&config, b"data\n").expect("write");
        let record = create_backup(input(&state, &config)).expect("backup");
        record.manifest.validate().expect("valid");
        let rendered = serde_json::to_string(&record.manifest).expect("json");
        assert!(!rendered.contains("ep_test"));
        let listed = list_backups(&state, Some(ClientTarget::Codex)).expect("list");
        assert!(!listed.is_empty());
    }

    #[test]
    #[cfg(unix)]
    fn backup_dirs_and_files_are_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("config.toml");
        std::fs::write(&config, b"data\n").expect("write");
        let record = create_backup(input(&state, &config)).expect("backup");
        let dir_mode = std::fs::metadata(&record.dir)
            .expect("meta")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(dir_mode, 0o700);
        let file_mode = std::fs::metadata(record.dir.join("manifest.json"))
            .expect("meta")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(file_mode, 0o600);
    }
}
