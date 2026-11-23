//! Safe, local database backup and recovery for O006.
//!
//! The archive format is intentionally small and boring: a stored ZIP with a
//! TOML `META` member, the reviewed config and optional env file, and a
//! consistent SQLite snapshot.  All archive input is validated before any
//! target path is touched.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use crate::{
    Config, ConfigError,
    db::{Database, DatabaseError, MigrationRunner},
};

pub const BACKUP_FORMAT_VERSION: u32 = 1;
pub const CONFIG_BASENAME: &str = "config.toml";
pub const ENV_BASENAME: &str = ".env";
pub const DB_BASENAME: &str = "usage.sqlite3";
pub const META_BASENAME: &str = "META";
const OAUTH_DIR_BASENAME: &str = ".eggpool-oauth";
const RESTORE_LOCK_BASENAME: &str = ".eggpool-oauth-restore.lock";
const OAUTH_MEMBER_PREFIX: &str = ".eggpool-oauth/";
const MAX_OAUTH_CREDENTIAL_FILES: usize = 4096;
const MAX_OAUTH_CREDENTIAL_BYTES: u64 = 64 * 1024 * 1024;
const MAX_OAUTH_CREDENTIAL_FILE_BYTES: u64 = 1024 * 1024;
const MAX_ARCHIVE_MEMBERS: usize = MAX_OAUTH_CREDENTIAL_FILES + 4;
const MAX_CONFIG_BYTES: u64 = 8 * 1024 * 1024;
const MAX_ENV_BYTES: u64 = 8 * 1024 * 1024;
const MAX_DATABASE_BYTES: u64 = 512 * 1024 * 1024;
const MAX_ARCHIVE_BYTES: u64 =
    MAX_DATABASE_BYTES + MAX_CONFIG_BYTES + MAX_ENV_BYTES + MAX_OAUTH_CREDENTIAL_BYTES + 1_048_576;
const BACKUP_TIMEOUT: Duration = Duration::from_secs(60);
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, thiserror::Error)]
pub enum BackupError {
    #[error("backup source is unavailable: {0}")]
    Source(String),
    #[error("backup filesystem operation failed")]
    Io(#[source] io::Error),
    #[error("database operation failed: {0}")]
    Database(#[from] DatabaseError),
    #[error("configuration validation failed: {0}")]
    Config(#[from] ConfigError),
    #[error("backup archive operation failed")]
    Archive(#[source] zip::result::ZipError),
    #[error("backup archive is invalid: {0}")]
    InvalidArchive(String),
    #[error("backup snapshot exceeded its time budget")]
    Timeout,
    #[error("restore failed and the original state was retained")]
    RestoreRollback,
}

impl From<io::Error> for BackupError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<zip::result::ZipError> for BackupError {
    fn from(error: zip::result::ZipError) -> Self {
        Self::Archive(error)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackupPaths {
    pub config: PathBuf,
    pub database: PathBuf,
    pub env: Option<PathBuf>,
    pub output_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackupResult {
    pub archive: PathBuf,
    pub members: Vec<String>,
    pub size_bytes: u64,
    pub duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestoreResult {
    pub config: PathBuf,
    pub database: PathBuf,
    pub env: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct BackupService {
    pub paths: BackupPaths,
    pub include_env: bool,
    pub install_method: String,
}

impl BackupService {
    pub fn from_config(config_path: &Path, config: &Config) -> Self {
        let config_file = absolute(config_path);
        let database = Config::runtime_path(&config.database.path);
        let env = config_file.parent().map(|parent| parent.join(ENV_BASENAME));
        let output_dir = config
            .backup
            .directory
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(default_backup_dir);
        Self {
            paths: BackupPaths {
                config: config_file,
                database,
                env,
                output_dir,
            },
            include_env: config.backup.include_env,
            install_method: "rust".to_owned(),
        }
    }

    pub fn with_output_dir(mut self, output_dir: PathBuf) -> Self {
        self.paths.output_dir = output_dir;
        self
    }

    /// Capture the database through SQLite's online backup API, then publish
    /// the complete archive with a create-new collision-safe finalization.
    pub async fn create(&self, database: &Database) -> Result<BackupResult, BackupError> {
        let started = std::time::Instant::now();
        validate_source_file(&self.paths.config, MAX_CONFIG_BYTES, CONFIG_BASENAME)?;
        validate_source_file(&self.paths.database, MAX_DATABASE_BYTES, DB_BASENAME)?;
        if self.include_env
            && let Some(env) = &self.paths.env
            && env.exists()
        {
            validate_source_file(env, MAX_ENV_BYTES, ENV_BASENAME)?;
        }
        ensure_private_directory(&self.paths.output_dir)?;
        let staging = private_staging_dir(&self.paths.output_dir, "backup")?;
        let staged_db = staging.join(DB_BASENAME);
        let result = async {
            tokio::time::timeout(BACKUP_TIMEOUT, database.backup_to(staged_db.clone()))
                .await
                .map_err(|_| BackupError::Timeout)??;
            validate_sqlite_snapshot(&staged_db).await?;
            let config_bytes = stable_read(&self.paths.config, MAX_CONFIG_BYTES, CONFIG_BASENAME)?;
            let env_bytes = if self.include_env {
                self.paths
                    .env
                    .as_ref()
                    .and_then(|path| {
                        path.exists()
                            .then(|| stable_read(path, MAX_ENV_BYTES, ENV_BASENAME))
                    })
                    .transpose()?
            } else {
                None
            };
            let oauth_files = read_oauth_credentials(&self.paths.config)?;
            let mut members = vec![CONFIG_BASENAME.to_owned(), DB_BASENAME.to_owned()];
            if env_bytes.is_some() {
                members.push(ENV_BASENAME.to_owned());
            }
            members.extend(
                oauth_files
                    .keys()
                    .map(|name| format!("{OAUTH_MEMBER_PREFIX}{name}")),
            );
            let created = SystemTime::now();
            let metadata = metadata_text(&self.paths, &members, &self.install_method, created);
            let archive = write_archive(
                &self.paths.output_dir,
                &metadata,
                &config_bytes,
                env_bytes.as_deref(),
                &staged_db,
                &oauth_files,
                created,
            )?;
            Ok::<_, BackupError>((archive, members))
        }
        .await;
        let _ = fs::remove_dir_all(&staging);
        let (archive, members) = result?;
        let size_bytes = fs::metadata(&archive)?.len();
        Ok(BackupResult {
            archive,
            members,
            size_bytes,
            duration_ms: started.elapsed().as_millis().min(u64::MAX as u128) as u64,
        })
    }

    /// Retention is deliberately best-effort, and is called only after a
    /// successful publication.
    pub fn prune(&self, retain_count: u64) -> Result<Vec<PathBuf>, BackupError> {
        let entries = list_backups(&self.paths.output_dir)?;
        if entries.len() <= retain_count as usize {
            return Ok(Vec::new());
        }
        // `list_backups` already orders newest-first by mtime; keep that
        // order instead of re-sorting by name (which mis-orders `-10`/`-2`).
        let mut removed = Vec::new();
        for path in entries.into_iter().skip(retain_count as usize) {
            if fs::remove_file(&path).is_ok() {
                removed.push(path);
            }
        }
        Ok(removed)
    }

    pub fn recover(&self, archive: &Path) -> Result<RestoreResult, BackupError> {
        let prepared = Self::validated_archive(archive, Some(&self.paths))?;
        atomic_restore(prepared)
    }

    /// Validate every archive member, manifest, target, config, and database
    /// before a caller decides whether it is safe to stop a running server.
    pub fn validate_archive(archive: &Path) -> Result<(), BackupError> {
        let _ = Self::validated_archive(archive, None)?;
        Ok(())
    }

    fn validated_archive(
        archive: &Path,
        live: Option<&BackupPaths>,
    ) -> Result<PreparedRestore, BackupError> {
        let prepared = prepare_restore(archive)?;
        validate_restore_targets(&prepared.targets)?;
        // Allowlist: META-nominated absolute targets must match the live
        // resolved paths. An attacker-controlled archive must not redirect
        // restores to /etc, ~/.ssh, or other absolute locations.
        if let Some(live) = live {
            Self::validate_restore_targets_against_live(&prepared.targets, live)?;
        }
        validate_staged_config(&prepared.config_bytes, &prepared.targets.config)?;
        validate_staged_database(&prepared.database_bytes, &prepared.targets.database)?;
        Ok(prepared)
    }

    /// Require archive targets to equal the live service paths. Fail closed
    /// when live paths are unknown (empty fallback during corrupt-config
    /// recovery) instead of skipping the allowlist.
    fn validate_restore_targets_against_live(
        targets: &RestoreTargets,
        live: &BackupPaths,
    ) -> Result<(), BackupError> {
        // Empty live paths mean the current config could not be parsed;
        // fail closed rather than allowing arbitrary absolute writes.
        if live.config.as_os_str().is_empty() || live.database.as_os_str().is_empty() {
            return Err(BackupError::InvalidArchive(
                "live configuration paths are unknown; restore refused".to_owned(),
            ));
        }
        if targets.config != live.config || targets.database != live.database {
            return Err(BackupError::InvalidArchive(
                "restore targets do not match the live configuration paths".to_owned(),
            ));
        }
        match (&targets.env, &live.env) {
            (None, _) => Ok(()),
            (Some(target), Some(live_env)) if target == live_env => Ok(()),
            _ => Err(BackupError::InvalidArchive(
                "restore env target does not match the live configuration path".to_owned(),
            )),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RestoreTargets {
    config: PathBuf,
    database: PathBuf,
    env: Option<PathBuf>,
}

struct PreparedRestore {
    targets: RestoreTargets,
    config_bytes: Vec<u8>,
    database_bytes: Vec<u8>,
    env_bytes: Option<Vec<u8>>,
    oauth_files: BTreeMap<String, Vec<u8>>,
}

pub fn default_backup_dir() -> PathBuf {
    if let Some(root) = std::env::var_os("XDG_BACKUP_HOME") {
        return PathBuf::from(root).join("eggpool");
    }
    let production = Path::new("/var/lib/eggpool");
    if production.is_dir() {
        return production.join("backups");
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("backups/eggpool")
}

pub fn backup_filename_at(time: SystemTime) -> String {
    let seconds = time
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let days = seconds / 86_400;
    let day_seconds = seconds % 86_400;
    let (year, month, day) = civil_from_days(days as i64);
    format!(
        "eggpool-backup-{year:04}{month:02}{day:02}-{:02}{:02}{:02}.zip",
        day_seconds / 3600,
        (day_seconds / 60) % 60,
        day_seconds % 60
    )
}

pub fn list_backups(directory: &Path) -> Result<Vec<PathBuf>, BackupError> {
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut paths = Vec::new();
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        if entry.file_type()?.is_file() && is_backup_name(&path) {
            paths.push(path);
        }
    }
    // Newest first by modification time (fallback to name): lexicographic
    // name order mis-sorts collision suffixes (`-10` < `-2`) and clock skew.
    paths.sort_by(|a, b| {
        let mtime = |path: &Path| {
            std::fs::metadata(path)
                .and_then(|metadata| metadata.modified())
                .ok()
        };
        mtime(b)
            .cmp(&mtime(a))
            .then_with(|| b.file_name().cmp(&a.file_name()))
    });
    Ok(paths)
}

fn write_archive(
    directory: &Path,
    metadata: &str,
    config: &[u8],
    env: Option<&[u8]>,
    database: &Path,
    oauth_files: &BTreeMap<String, Vec<u8>>,
    created: SystemTime,
) -> Result<PathBuf, BackupError> {
    let stamp = backup_filename_at(created);
    for suffix in 0..10_000_u32 {
        let name = if suffix == 0 {
            stamp.clone()
        } else {
            stamp.replace(".zip", &format!("-{suffix}.zip"))
        };
        let final_path = directory.join(name);
        let temp = directory.join(format!(
            ".eggpool-backup-{}-{}.tmp",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let result: Result<PathBuf, BackupError> = (|| {
            let file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp)?;
            set_private_file(&temp)?;
            let mut writer = zip::ZipWriter::new(file);
            let options = zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Stored)
                .unix_permissions(0o600);
            writer.start_file(META_BASENAME, options)?;
            writer.write_all(metadata.as_bytes())?;
            writer.start_file(CONFIG_BASENAME, options)?;
            writer.write_all(config)?;
            if let Some(env) = env {
                writer.start_file(ENV_BASENAME, options)?;
                writer.write_all(env)?;
            }
            writer.start_file(DB_BASENAME, options)?;
            let mut db = File::open(database)?;
            io::copy(&mut db, &mut writer)?;
            for (name, bytes) in oauth_files {
                writer.start_file(format!("{OAUTH_MEMBER_PREFIX}{name}"), options)?;
                writer.write_all(bytes)?;
            }
            let file = writer.finish()?;
            file.sync_all()?;
            match fs::hard_link(&temp, &final_path) {
                Ok(()) => {
                    fs::remove_file(&temp)?;
                    Ok(final_path)
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                    let _ = fs::remove_file(&temp);
                    Err(BackupError::Io(error))
                }
                Err(error) => Err(BackupError::Io(error)),
            }
        })();
        let _ = fs::remove_file(&temp);
        match result {
            Ok(path) => return Ok(path),
            Err(BackupError::Io(error)) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(BackupError::Source(
        "too many backup name collisions".to_owned(),
    ))
}

fn read_oauth_credentials(config_path: &Path) -> Result<BTreeMap<String, Vec<u8>>, BackupError> {
    let parent = config_path
        .parent()
        .ok_or_else(|| BackupError::InvalidArchive("config target has no parent".to_owned()))?;
    let _restore_lock = acquire_oauth_restore_lock(parent)?;
    read_oauth_credentials_locked(config_path)
}

fn read_oauth_credentials_locked(
    config_path: &Path,
) -> Result<BTreeMap<String, Vec<u8>>, BackupError> {
    let root = crate::oauth::credential_root_for_config(Some(config_path));
    reject_symlink_ancestors(&root)?;
    crate::oauth::recover_interrupted_restore_directory_locked(&root)
        .map_err(|_| BackupError::Source("OAuth credential restore recovery failed".to_owned()))?;
    let directory = match fs::symlink_metadata(&root) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(error) => return Err(error.into()),
    };
    verify_private_oauth_metadata(&directory, true)?;
    let mut files = BTreeMap::new();
    let mut total_bytes = 0_u64;
    for entry in fs::read_dir(&root)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            return Err(BackupError::Source(
                "OAuth credential directory contains an invalid entry".to_owned(),
            ));
        };
        let metadata = fs::symlink_metadata(entry.path())?;
        verify_private_oauth_metadata(&metadata, false)?;
        if name.ends_with(".lock")
            || (name.starts_with(".oauth-")
                && (name.ends_with(".tmp") || name.ends_with(".rollback")))
        {
            continue;
        }
        if !is_oauth_credential_filename(name) {
            return Err(BackupError::Source(
                "OAuth credential directory contains an unexpected entry".to_owned(),
            ));
        }
        if metadata.len() > MAX_OAUTH_CREDENTIAL_FILE_BYTES {
            return Err(BackupError::Source(
                "OAuth credential file exceeds the backup limit".to_owned(),
            ));
        }
        let bytes = stable_read(
            &entry.path(),
            MAX_OAUTH_CREDENTIAL_FILE_BYTES,
            "OAuth credential",
        )?;
        validate_oauth_credential_archive_member(name, &bytes)
            .map_err(|_| BackupError::Source("OAuth credential file is invalid".to_owned()))?;
        total_bytes = total_bytes.checked_add(bytes.len() as u64).ok_or_else(|| {
            BackupError::Source("OAuth credential backup size overflow".to_owned())
        })?;
        if files.len() >= MAX_OAUTH_CREDENTIAL_FILES || total_bytes > MAX_OAUTH_CREDENTIAL_BYTES {
            return Err(BackupError::Source(
                "OAuth credential backup exceeds its size limit".to_owned(),
            ));
        }
        files.insert(name.to_owned(), bytes);
    }
    Ok(files)
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ArchivedOAuthCredential {
    format_version: u32,
    user_id: String,
    provider_id: String,
    account_name: String,
    credentials: eggpass_auth::Credentials,
}

fn validate_oauth_credential_archive_member(name: &str, bytes: &[u8]) -> Result<(), BackupError> {
    if !is_oauth_credential_filename(name) || bytes.len() as u64 > MAX_OAUTH_CREDENTIAL_FILE_BYTES {
        return Err(BackupError::InvalidArchive(
            "invalid OAuth credential member".to_owned(),
        ));
    }
    let record: ArchivedOAuthCredential = serde_json::from_slice(bytes)
        .map_err(|_| BackupError::InvalidArchive("invalid OAuth credential record".to_owned()))?;
    let identity = crate::oauth::credential_filename(
        &record.user_id,
        &record.provider_id,
        &record.account_name,
    )
    .ok_or_else(|| BackupError::InvalidArchive("invalid OAuth credential owner".to_owned()))?;
    if record.format_version != 1 || identity != name || record.credentials.issuer().is_empty() {
        return Err(BackupError::InvalidArchive(
            "OAuth credential owner does not match its file".to_owned(),
        ));
    }
    Ok(())
}

fn is_oauth_credential_filename(name: &str) -> bool {
    name.strip_suffix(".json").is_some_and(|stem| {
        stem.len() == 64
            && stem
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn oauth_member_filename(name: &str) -> Option<&str> {
    let file_name = name.strip_prefix(OAUTH_MEMBER_PREFIX)?;
    is_oauth_credential_filename(file_name).then_some(file_name)
}

#[cfg(unix)]
fn verify_private_oauth_metadata(
    metadata: &fs::Metadata,
    directory: bool,
) -> Result<(), BackupError> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    if (directory && !metadata.is_dir())
        || (!directory && !metadata.is_file())
        || metadata.uid() != nix::unistd::geteuid().as_raw()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(BackupError::Source(
            "OAuth credential storage is not owner-private".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn verify_private_oauth_metadata(
    _metadata: &fs::Metadata,
    _directory: bool,
) -> Result<(), BackupError> {
    Err(BackupError::Source(
        "OAuth credential backup requires Unix permissions".to_owned(),
    ))
}

fn prepare_restore(archive_path: &Path) -> Result<PreparedRestore, BackupError> {
    reject_duplicate_archive_members(archive_path)?;
    let file = File::open(archive_path)?;
    let mut archive = zip::ZipArchive::new(file).map_err(BackupError::Archive)?;
    if archive.is_empty() || archive.len() > MAX_ARCHIVE_MEMBERS {
        return Err(BackupError::InvalidArchive(
            "invalid member count".to_owned(),
        ));
    }
    let mut names = BTreeSet::new();
    let mut metadata = None;
    let mut config = None;
    let mut database = None;
    let mut env = None;
    let mut oauth_files = BTreeMap::new();
    let mut oauth_total_bytes = 0_u64;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(BackupError::Archive)?;
        if entry.is_dir() || entry.encrypted() {
            return Err(BackupError::InvalidArchive(
                "directories and encrypted members are not allowed".to_owned(),
            ));
        }
        let name = entry.name().map_err(BackupError::Archive)?.into_owned();
        if (!matches!(
            name.as_str(),
            META_BASENAME | CONFIG_BASENAME | ENV_BASENAME | DB_BASENAME
        ) && oauth_member_filename(&name).is_none())
            || !names.insert(name.clone())
        {
            return Err(BackupError::InvalidArchive(
                "unexpected or duplicate member".to_owned(),
            ));
        }
        if entry.unix_mode().is_some_and(|mode| {
            let file_type = mode & 0o170000;
            // Python's stdlib zipfile leaves the Unix file-type bits unset
            // for ordinary members.  Treat that as a regular file for the
            // cross-implementation archive contract, while still rejecting
            // an explicitly encoded directory/device/FIFO/socket type.
            file_type != 0 && file_type != 0o100000
        }) {
            return Err(BackupError::InvalidArchive(
                "special-file member is not allowed".to_owned(),
            ));
        }
        let cap = match name.as_str() {
            META_BASENAME => 64 * 1024,
            CONFIG_BASENAME => MAX_CONFIG_BYTES,
            ENV_BASENAME => MAX_ENV_BYTES,
            DB_BASENAME => MAX_DATABASE_BYTES,
            _ if oauth_member_filename(&name).is_some() => MAX_OAUTH_CREDENTIAL_FILE_BYTES,
            _ => 0,
        };
        if entry.size() > cap {
            return Err(BackupError::InvalidArchive(
                "member exceeds size limit".to_owned(),
            ));
        }
        let mut bytes = Vec::with_capacity(entry.size().min(1024 * 1024) as usize);
        entry.read_to_end(&mut bytes).map_err(BackupError::Io)?;
        match name.as_str() {
            META_BASENAME => metadata = Some(bytes),
            CONFIG_BASENAME => config = Some(bytes),
            ENV_BASENAME => env = Some(bytes),
            DB_BASENAME => database = Some(bytes),
            _ if let Some(file_name) = oauth_member_filename(&name) => {
                validate_oauth_credential_archive_member(file_name, &bytes)?;
                oauth_total_bytes = oauth_total_bytes
                    .checked_add(bytes.len() as u64)
                    .ok_or_else(|| {
                        BackupError::InvalidArchive("OAuth credential size overflow".to_owned())
                    })?;
                if oauth_total_bytes > MAX_OAUTH_CREDENTIAL_BYTES {
                    return Err(BackupError::InvalidArchive(
                        "OAuth credentials exceed the archive limit".to_owned(),
                    ));
                }
                oauth_files.insert(file_name.to_owned(), bytes);
            }
            _ => {
                return Err(BackupError::InvalidArchive("unknown member".to_owned()));
            }
        }
    }
    let metadata =
        metadata.ok_or_else(|| BackupError::InvalidArchive("META is required".to_owned()))?;
    let value: toml::Value = std::str::from_utf8(&metadata)
        .map_err(|error| BackupError::InvalidArchive(format!("META is not UTF-8: {error}")))?
        .parse()
        .map_err(|error| BackupError::InvalidArchive(format!("META is not valid TOML: {error}")))?;
    if value
        .get("format_version")
        .and_then(toml::Value::as_integer)
        != Some(BACKUP_FORMAT_VERSION as i64)
    {
        return Err(BackupError::InvalidArchive(
            "unsupported format version".to_owned(),
        ));
    }
    let members = value
        .get("members")
        .and_then(toml::Value::as_array)
        .ok_or_else(|| BackupError::InvalidArchive("META members are missing".to_owned()))?;
    let declared = members
        .iter()
        .map(|v| v.as_str().unwrap_or(""))
        .collect::<BTreeSet<_>>();
    let actual = names
        .iter()
        .map(String::as_str)
        .filter(|name| *name != META_BASENAME)
        .collect::<BTreeSet<_>>();
    if declared != actual {
        return Err(BackupError::InvalidArchive(
            "META member list does not match archive".to_owned(),
        ));
    }
    let config_bytes =
        config.ok_or_else(|| BackupError::InvalidArchive("config.toml is required".to_owned()))?;
    let database_bytes = database
        .ok_or_else(|| BackupError::InvalidArchive("usage.sqlite3 is required".to_owned()))?;
    let target = |key: &str| -> Result<PathBuf, BackupError> {
        let raw = value
            .get(key)
            .and_then(toml::Value::as_str)
            .ok_or_else(|| BackupError::InvalidArchive(format!("META {key} is missing")))?;
        safe_absolute_target(raw)
    };
    let config_target = target("config_path")?;
    let db_target = target("db_path")?;
    let env_target = if env.is_some() {
        Some(target("env_path")?)
    } else {
        None
    };
    Ok(PreparedRestore {
        targets: RestoreTargets {
            config: config_target,
            database: db_target,
            env: env_target,
        },
        config_bytes,
        database_bytes,
        env_bytes: env,
        oauth_files,
    })
}

/// Reject duplicate central-directory names before `zip` builds its
/// name-indexed archive view.  `zip 2.4` intentionally deduplicates names in
/// that view, which would otherwise make the application-level duplicate
/// member contract unobservable.  Only the bounded central directory is
/// scanned; payload validation remains owned by `ZipArchive` below.
///
/// The scan is memory-bounded: the EOCD record is located within the trailing
/// window (22-byte record plus up to 64 KiB of ZIP comment per the format),
/// and only the central-directory window is read. Payload bytes are never
/// loaded.
const EOCD_SEARCH_WINDOW: u64 = 65_536 + 22;

fn reject_duplicate_archive_members(archive_path: &Path) -> Result<(), BackupError> {
    let size = fs::metadata(archive_path)?.len();
    if size > MAX_ARCHIVE_BYTES {
        return Err(BackupError::InvalidArchive(
            "archive exceeds the total size limit".to_owned(),
        ));
    }
    let mut file = File::open(archive_path)?;
    let window = size.min(EOCD_SEARCH_WINDOW);
    file.seek(SeekFrom::End(-(window as i64)))?;
    let mut tail = vec![0u8; window as usize];
    file.read_exact(&mut tail)?;
    let Some(relative) = tail.windows(4).rposition(|window| window == b"PK\x05\x06") else {
        return Ok(());
    };
    if relative + 22 > tail.len() {
        return Ok(());
    }
    let eocd_absolute = size - window + relative as u64;
    let Ok(central_size_bytes): Result<[u8; 4], _> = tail[relative + 12..relative + 16].try_into()
    else {
        return Ok(());
    };
    let Ok(central_offset_bytes): Result<[u8; 4], _> =
        tail[relative + 16..relative + 20].try_into()
    else {
        return Ok(());
    };
    let central_size = u32::from_le_bytes(central_size_bytes) as u64;
    let central_offset = u32::from_le_bytes(central_offset_bytes) as u64;
    if central_size == u64::from(u32::MAX) || central_offset == u64::from(u32::MAX) {
        return Ok(());
    }
    let central_end = central_offset
        .checked_add(central_size)
        .ok_or_else(|| BackupError::InvalidArchive("invalid central directory".to_owned()))?;
    if central_offset > size || central_end > size || central_end > eocd_absolute {
        return Ok(());
    }
    let central_offset = usize::try_from(central_offset).map_err(|error| {
        BackupError::InvalidArchive(format!("invalid central directory: {error}"))
    })?;
    let central_len = usize::try_from(central_size).map_err(|error| {
        BackupError::InvalidArchive(format!("invalid central directory: {error}"))
    })?;
    central_offset
        .checked_add(central_len)
        .ok_or_else(|| BackupError::InvalidArchive("invalid central directory".to_owned()))?;
    file.seek(SeekFrom::Start(central_offset as u64))?;
    let mut central = vec![0u8; central_len];
    file.read_exact(&mut central)?;
    let mut names = BTreeSet::<Vec<u8>>::new();
    let mut cursor = 0usize;
    while cursor < central_len {
        if cursor + 46 > central_len || &central[cursor..cursor + 4] != b"PK\x01\x02" {
            return Ok(());
        }
        let Ok(name_len_bytes): Result<[u8; 2], _> = central[cursor + 28..cursor + 30].try_into()
        else {
            return Ok(());
        };
        let Ok(extra_len_bytes): Result<[u8; 2], _> = central[cursor + 30..cursor + 32].try_into()
        else {
            return Ok(());
        };
        let Ok(comment_len_bytes): Result<[u8; 2], _> =
            central[cursor + 32..cursor + 34].try_into()
        else {
            return Ok(());
        };
        let name_len = u16::from_le_bytes(name_len_bytes) as usize;
        let extra_len = u16::from_le_bytes(extra_len_bytes) as usize;
        let comment_len = u16::from_le_bytes(comment_len_bytes) as usize;
        let name_start = cursor + 46;
        let record_len = 46usize
            .checked_add(name_len)
            .and_then(|length| length.checked_add(extra_len))
            .and_then(|length| length.checked_add(comment_len))
            .ok_or_else(|| BackupError::InvalidArchive("invalid central directory".to_owned()))?;
        if cursor + record_len > central_len {
            return Ok(());
        }
        if !names.insert(central[name_start..name_start + name_len].to_vec()) {
            return Err(BackupError::InvalidArchive(
                "unexpected or duplicate member".to_owned(),
            ));
        }
        cursor += record_len;
    }
    Ok(())
}

fn validate_staged_config(bytes: &[u8], path: &Path) -> Result<(), BackupError> {
    Config::from_toml_bytes(path, bytes)?;
    Ok(())
}

fn validate_staged_database(bytes: &[u8], path: &Path) -> Result<(), BackupError> {
    let directory = tempfile_path(path, "validate")?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&directory)?;
    set_private_file(&directory)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    let result = futures_lite_block_on_validate(&directory);
    let _ = fs::remove_file(&directory);
    result
}

fn futures_lite_block_on_validate(path: &Path) -> Result<(), BackupError> {
    // Recovery runs on the Tokio CLI runtime. This helper is replaced by the
    // synchronous SQLite integrity check below so validation never touches a
    // final target or opens the live database.
    let conn = tokio_rusqlite::rusqlite::Connection::open(path).map_err(|error| {
        BackupError::InvalidArchive(format!("database could not be opened: {error}"))
    })?;
    let status: String = conn
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(|error| {
            BackupError::InvalidArchive(format!("database integrity check failed: {error}"))
        })?;
    if !status.eq_ignore_ascii_case("ok") {
        return Err(BackupError::InvalidArchive(
            "database integrity check failed".to_owned(),
        ));
    }
    let has_ledger: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_migrations')",
            [],
            |row| row.get(0),
        )
        .map_err(|error| {
            BackupError::InvalidArchive(format!("migration ledger is missing: {error}"))
        })?;
    if !has_ledger {
        return Err(BackupError::InvalidArchive(
            "migration ledger is missing".to_owned(),
        ));
    }
    let mut statement = conn
        .prepare("SELECT version, name FROM _migrations ORDER BY version")
        .map_err(|error| {
            BackupError::InvalidArchive(format!("migration ledger is unreadable: {error}"))
        })?;
    let applied = statement
        .query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|error| {
            BackupError::InvalidArchive(format!("migration ledger is unreadable: {error}"))
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| {
            BackupError::InvalidArchive(format!("migration ledger is unreadable: {error}"))
        })?;
    for (version, name) in applied {
        let Some(migration) = MigrationRunner::migrations()
            .iter()
            .find(|migration| migration.version as i64 == version)
        else {
            return Err(BackupError::InvalidArchive(
                "database contains an unknown migration".to_owned(),
            ));
        };
        if name != migration.name && name != migration.name.trim_end_matches(".sql") {
            return Err(BackupError::InvalidArchive(
                "database migration ledger is incompatible".to_owned(),
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
fn restore_lock_path(config_target: &Path) -> Result<PathBuf, BackupError> {
    let parent = config_target
        .parent()
        .ok_or_else(|| BackupError::InvalidArchive("config target has no parent".to_owned()))?;
    Ok(parent.join(RESTORE_LOCK_BASENAME))
}

#[cfg(unix)]
fn validate_restore_lock_parent(parent: &Path) -> Result<(), BackupError> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = fs::symlink_metadata(parent)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(BackupError::Source(
            "OAuth restore lock directory is unsafe".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn validate_restore_lock_parent(_parent: &Path) -> Result<(), BackupError> {
    Err(BackupError::Source(
        "safe OAuth restore locking is unavailable on this platform".to_owned(),
    ))
}

#[cfg(all(unix, target_os = "linux"))]
fn open_restore_lock_file(path: &Path) -> Result<File, BackupError> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
    const O_NOFOLLOW: i32 = 0o400000;

    let mut options = OpenOptions::new();
    options
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(O_NOFOLLOW);
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != nix::unistd::geteuid().as_raw() {
        return Err(BackupError::Source(
            "OAuth restore lock file is unsafe".to_owned(),
        ));
    }
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    Ok(file)
}

#[cfg(all(unix, target_os = "macos"))]
fn open_restore_lock_file(path: &Path) -> Result<File, BackupError> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
    const O_NOFOLLOW: i32 = 0x0100;

    let mut options = OpenOptions::new();
    options
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(O_NOFOLLOW);
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != nix::unistd::geteuid().as_raw() {
        return Err(BackupError::Source(
            "OAuth restore lock file is unsafe".to_owned(),
        ));
    }
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    Ok(file)
}

#[cfg(not(all(unix, any(target_os = "linux", target_os = "macos"))))]
fn open_restore_lock_file(_path: &Path) -> Result<File, BackupError> {
    Err(BackupError::Source(
        "safe OAuth restore locking is unavailable on this platform".to_owned(),
    ))
}

#[cfg(unix)]
fn verify_restore_lock_file(path: &Path, file: &File) -> Result<(), BackupError> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let opened = file.metadata()?;
    let named = fs::symlink_metadata(path)?;
    if !opened.is_file()
        || named.file_type().is_symlink()
        || !named.is_file()
        || opened.dev() != named.dev()
        || opened.ino() != named.ino()
        || opened.uid() != nix::unistd::geteuid().as_raw()
        || opened.permissions().mode() & 0o077 != 0
    {
        return Err(BackupError::Source(
            "OAuth restore lock file is unsafe".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn verify_restore_lock_file(_path: &Path, _file: &File) -> Result<(), BackupError> {
    Err(BackupError::Source(
        "safe OAuth restore locking is unavailable on this platform".to_owned(),
    ))
}

pub(crate) fn acquire_oauth_restore_lock(parent: &Path) -> Result<File, BackupError> {
    reject_symlink_ancestors(parent)?;
    fs::create_dir_all(parent)?;
    reject_symlink_ancestors(parent)?;
    validate_restore_lock_parent(parent)?;

    let lock_path = parent.join(RESTORE_LOCK_BASENAME);
    reject_symlink_ancestors(&lock_path)?;
    let file = open_restore_lock_file(&lock_path)?;
    verify_restore_lock_file(&lock_path, &file)?;
    file.lock()?;
    verify_restore_lock_file(&lock_path, &file)?;
    Ok(file)
}

fn acquire_restore_lock(config_target: &Path) -> Result<File, BackupError> {
    let parent = config_target
        .parent()
        .ok_or_else(|| BackupError::InvalidArchive("config target has no parent".to_owned()))?;
    acquire_oauth_restore_lock(parent)
}

fn atomic_restore(prepared: PreparedRestore) -> Result<RestoreResult, BackupError> {
    atomic_restore_with_hooks(prepared, || {}, || {})
}

fn atomic_restore_with_hooks(
    prepared: PreparedRestore,
    before_lock: impl FnOnce(),
    after_lock: impl FnOnce(),
) -> Result<RestoreResult, BackupError> {
    before_lock();
    let _restore_lock = acquire_restore_lock(&prepared.targets.config)?;
    after_lock();
    atomic_restore_locked(prepared)
}

fn atomic_restore_locked(prepared: PreparedRestore) -> Result<RestoreResult, BackupError> {
    let targets = &prepared.targets;
    // Validate staged bytes BEFORE touching any live target. `validated_archive`
    // already validated, but `atomic_restore` must stay safe on its own.
    validate_staged_config(&prepared.config_bytes, &targets.config)?;
    validate_staged_database(&prepared.database_bytes, &targets.database)?;
    let root = targets
        .database
        .parent()
        .ok_or_else(|| BackupError::InvalidArchive("database target has no parent".to_owned()))?;
    fs::create_dir_all(root)?;
    set_private_dir(root)?;
    let safety = private_staging_dir(root, "restore")?;
    let oauth_dir = targets
        .config
        .parent()
        .ok_or_else(|| BackupError::InvalidArchive("config target has no parent".to_owned()))?
        .join(OAUTH_DIR_BASENAME);
    let files = [
        Some(&targets.config),
        targets.env.as_ref(),
        Some(&targets.database),
    ];
    let mut originals = Vec::new();
    for (index, target) in files.into_iter().flatten().enumerate() {
        if target.exists() {
            let saved = safety.join(index.to_string());
            fs::copy(target, &saved)?;
            originals.push((target.clone(), Some(saved)));
        } else {
            originals.push((target.clone(), None));
        }
    }
    let oauth_rollback = replace_oauth_directory_locked(&oauth_dir, &prepared.oauth_files)?;
    let result = (|| {
        write_atomic(&targets.config, &prepared.config_bytes, 0o600)?;
        if let (Some(target), Some(bytes)) = (&targets.env, &prepared.env_bytes) {
            write_atomic(target, bytes, 0o600)?;
        }
        write_atomic(&targets.database, &prepared.database_bytes, 0o600)?;
        Ok(())
    })();
    if result.is_err() {
        // Rollback by atomic rename (temp in target parent + rename), not
        // non-atomic `fs::copy`, to avoid torn state on failure.
        let rollback = (|| {
            restore_oauth_directory_locked(&oauth_dir, oauth_rollback.as_deref())?;
            originals.iter().try_for_each(|(target, saved)| {
                match saved {
                    Some(saved) => {
                        let bytes = fs::read(saved)?;
                        write_atomic(target, &bytes, 0o600)?;
                        set_private_file(target)?;
                    }
                    None => {
                        let _ = fs::remove_file(target);
                    }
                }
                Ok::<_, BackupError>(())
            })
        })();
        if rollback.is_err() {
            return Err(BackupError::RestoreRollback);
        }
        let _ = fs::remove_dir_all(&safety);
        let Err(error) = result else {
            // Checked `is_err` above; an `Ok` here would mean the guard was
            // removed. The restore already succeeded, so there is no error to
            // return — surface the rollback-path bug without panicking.
            debug_assert!(false, "restore result was checked as an error");
            return Err(BackupError::RestoreRollback);
        };
        return Err(error);
    }
    if let Some(previous) = oauth_rollback {
        if fs::remove_dir_all(previous).is_err() {
            tracing::warn!("could not remove prior OAuth credential directory after restore");
        } else if fsync_parent_dir(oauth_dir.parent().unwrap_or(root)).is_err() {
            // The replacement was already directory-synced before this cleanup.
            // Cleanup failure does not undo the committed restore.
            tracing::warn!("could not sync OAuth credential cleanup after restore");
        }
    }
    fs::remove_dir_all(&safety)?;
    Ok(RestoreResult {
        config: targets.config.clone(),
        database: targets.database.clone(),
        env: targets.env.clone(),
    })
}

fn reject_unresolved_oauth_restore_rollbacks(target: &Path) -> Result<(), BackupError> {
    let parent = target.parent().ok_or_else(|| {
        BackupError::InvalidArchive("OAuth restore target has no parent".to_owned())
    })?;
    let target_name = target
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            BackupError::InvalidArchive("OAuth restore target name is invalid".to_owned())
        })?;
    let prefix = format!("{target_name}-restore-old-");
    for entry in fs::read_dir(parent)? {
        let entry = entry?;
        if entry
            .file_name()
            .to_str()
            .is_some_and(|name| name.starts_with(&prefix))
        {
            return Err(BackupError::Source(
                "OAuth restore has unresolved prior rollback state".to_owned(),
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
fn replace_oauth_directory(
    target: &Path,
    files: &BTreeMap<String, Vec<u8>>,
) -> Result<Option<PathBuf>, BackupError> {
    let parent = target.parent().ok_or_else(|| {
        BackupError::InvalidArchive("OAuth restore target has no parent".to_owned())
    })?;
    let _restore_lock = acquire_oauth_restore_lock(parent)?;
    replace_oauth_directory_locked(target, files)
}

fn replace_oauth_directory_locked(
    target: &Path,
    files: &BTreeMap<String, Vec<u8>>,
) -> Result<Option<PathBuf>, BackupError> {
    replace_oauth_directory_locked_with_hook(target, files, || Ok(()))
}

fn replace_oauth_directory_locked_with_hook(
    target: &Path,
    files: &BTreeMap<String, Vec<u8>>,
    before_publish: impl FnOnce() -> io::Result<()>,
) -> Result<Option<PathBuf>, BackupError> {
    let parent = target.parent().ok_or_else(|| {
        BackupError::InvalidArchive("OAuth restore target has no parent".to_owned())
    })?;
    fs::create_dir_all(parent)?;
    reject_symlink_ancestors(target)?;
    crate::oauth::recover_interrupted_restore_directory_locked(target)
        .map_err(|_| BackupError::RestoreRollback)?;
    reject_unresolved_oauth_restore_rollbacks(target)?;
    let staging = if files.is_empty() {
        None
    } else {
        let staging = private_staging_dir(parent, "oauth-restore")?;
        for (name, bytes) in files {
            validate_oauth_credential_archive_member(name, bytes)?;
            write_atomic(&staging.join(name), bytes, 0o600)?;
        }
        Some(staging)
    };
    let previous = match fs::symlink_metadata(target) {
        Ok(metadata) => {
            if let Err(error) = verify_private_oauth_metadata(&metadata, true) {
                if let Some(staging) = &staging {
                    let _ = fs::remove_dir_all(staging);
                }
                return Err(error);
            }
            let mut candidate = None;
            for _ in 0..32 {
                let path = parent.join(format!(
                    ".eggpool-oauth-restore-old-{}-{}",
                    std::process::id(),
                    TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
                ));
                if fs::symlink_metadata(&path)
                    .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
                {
                    candidate = Some(path);
                    break;
                }
            }
            let Some(backup) = candidate else {
                if let Some(staging) = &staging {
                    let _ = fs::remove_dir_all(staging);
                }
                return Err(BackupError::Source(
                    "could not allocate OAuth restore rollback path".to_owned(),
                ));
            };
            // Keep this prefix stable: OAuth startup and backup reads recover
            // this directory if a crash occurs before staging is published.
            if let Err(error) = fs::rename(target, &backup) {
                if let Some(staging) = &staging {
                    let _ = fs::remove_dir_all(staging);
                }
                return Err(error.into());
            }
            Some(backup)
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    let publish = (|| {
        before_publish()?;
        if let Some(staging) = &staging {
            fs::rename(staging, target)?;
        }
        fsync_parent_dir(parent)
    })();
    if let Err(error) = publish {
        let rollback = (|| {
            remove_oauth_directory_if_present(target)?;
            if let Some(previous) = &previous {
                fs::rename(previous, target)?;
            }
            if let Some(staging) = &staging {
                let _ = fs::remove_dir_all(staging);
            }
            fsync_parent_dir(parent)
        })();
        if rollback.is_err() {
            return Err(BackupError::RestoreRollback);
        }
        return Err(error);
    }
    Ok(previous)
}

#[cfg(test)]
fn restore_oauth_directory(target: &Path, previous: Option<&Path>) -> Result<(), BackupError> {
    let parent = target.parent().ok_or_else(|| {
        BackupError::InvalidArchive("OAuth restore target has no parent".to_owned())
    })?;
    let _restore_lock = acquire_oauth_restore_lock(parent)?;
    restore_oauth_directory_locked(target, previous)
}

fn restore_oauth_directory_locked(
    target: &Path,
    previous: Option<&Path>,
) -> Result<(), BackupError> {
    remove_oauth_directory_if_present(target)?;
    if let Some(previous) = previous {
        fs::rename(previous, target)?;
    }
    if let Some(parent) = target.parent() {
        fsync_parent_dir(parent)?;
    }
    Ok(())
}

fn remove_oauth_directory_if_present(target: &Path) -> Result<(), BackupError> {
    match fs::symlink_metadata(target) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(BackupError::Source(
                    "OAuth restore target is not a directory".to_owned(),
                ));
            }
            fs::remove_dir_all(target)?;
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

async fn validate_sqlite_snapshot(path: &Path) -> Result<(), BackupError> {
    let connection = Database::open(crate::db::DatabaseConfig {
        path: path.display().to_string(),
        read_only: true,
        ..Default::default()
    })
    .await?;
    let result = connection.quick_check().await.map_err(BackupError::from);
    let close = connection.close().await;
    result.and(close.map_err(BackupError::from))
}

fn validate_restore_targets(targets: &RestoreTargets) -> Result<(), BackupError> {
    let mut unique = BTreeSet::new();
    for path in [&targets.config, &targets.database]
        .into_iter()
        .chain(targets.env.as_ref())
    {
        if !unique.insert(path) {
            return Err(BackupError::InvalidArchive(
                "restore targets must be distinct".to_owned(),
            ));
        }
        if path.file_name().is_none() || path.parent().is_none() {
            return Err(BackupError::InvalidArchive(
                "invalid restore target".to_owned(),
            ));
        }
        if path.parent().is_some_and(|parent| {
            parent
                .components()
                .any(|c| matches!(c, Component::ParentDir))
        }) {
            return Err(BackupError::InvalidArchive(
                "restore target contains parent traversal".to_owned(),
            ));
        }
        reject_symlink_ancestors(path)?;
    }
    Ok(())
}

fn safe_absolute_target(raw: &str) -> Result<PathBuf, BackupError> {
    let path = PathBuf::from(raw);
    if !path.is_absolute()
        || raw.contains('\0')
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
    {
        return Err(BackupError::InvalidArchive(
            "META contains an unsafe target path".to_owned(),
        ));
    }
    Ok(path)
}

fn stable_read(path: &Path, max: u64, member: &str) -> Result<Vec<u8>, BackupError> {
    reject_symlink_ancestors(path)?;
    let before = fs::symlink_metadata(path)?;
    if !before.is_file() || before.len() > max {
        return Err(BackupError::Source(format!(
            "{member} is unavailable or too large"
        )));
    }
    // O_NOFOLLOW open so a symlink swap between the metadata check and open
    // cannot redirect the read. Falls back to a symlink re-check on platforms
    // without `custom_flags`.
    let file = open_nofollow(path)?;
    // Verify the opened file is the same inode as the pre-check (Unix) and
    // that the path did not change under us (before consuming `file` for read).
    verify_opened_same_file(path, &file, &before)?;
    let mut bytes = Vec::with_capacity(before.len().min(max) as usize);
    file.take(max + 1).read_to_end(&mut bytes)?;
    let after = fs::symlink_metadata(path)?;
    if before.len() != after.len() || before.modified().ok() != after.modified().ok() {
        return Err(BackupError::Source(format!(
            "{member} changed during backup"
        )));
    }
    if (bytes.len() as u64) > max {
        return Err(BackupError::Source(format!(
            "{member} is unavailable or too large"
        )));
    }
    Ok(bytes)
}

#[cfg(unix)]
fn open_nofollow(path: &Path) -> Result<File, BackupError> {
    use std::os::unix::fs::OpenOptionsExt;
    // `O_NOFOLLOW` is 0o400000 on Linux/macOS; use libc value via custom_flags
    // without a new dependency (0x20000 on macOS is also covered by attempting
    // open and re-validating symlink below on failure).
    const O_NOFOLLOW: i32 = 0o400000;
    let result = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(path);
    match result {
        Ok(file) => Ok(file),
        Err(error) if error.kind() == io::ErrorKind::InvalidInput => {
            // Platform did not honor the flag: fall back to open + symlink check.
            let file = File::open(path)?;
            if fs::symlink_metadata(path)?.file_type().is_symlink() {
                return Err(BackupError::Source(
                    "symlink path is not allowed".to_owned(),
                ));
            }
            Ok(file)
        }
        Err(error) => Err(error.into()),
    }
}

#[cfg(not(unix))]
fn open_nofollow(path: &Path) -> Result<File, BackupError> {
    let file = File::open(path)?;
    if fs::symlink_metadata(path)?.file_type().is_symlink() {
        return Err(BackupError::Source(
            "symlink path is not allowed".to_owned(),
        ));
    }
    Ok(file)
}

#[cfg(unix)]
fn verify_opened_same_file(
    path: &Path,
    file: &File,
    before: &fs::Metadata,
) -> Result<(), BackupError> {
    use std::os::unix::fs::MetadataExt;
    let opened = file.metadata().map_err(BackupError::from)?;
    // Same device+inode as the pre-check; otherwise a swap raced us.
    if opened.dev() != before.dev()
        || opened.ino() != before.ino()
        || opened.file_type().is_symlink()
    {
        return Err(BackupError::Source(
            "source changed during backup".to_owned(),
        ));
    }
    // Path itself must still not be a symlink.
    if fs::symlink_metadata(path)?.file_type().is_symlink() {
        return Err(BackupError::Source(
            "symlink path is not allowed".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn verify_opened_same_file(
    _path: &Path,
    _file: &File,
    _before: &fs::Metadata,
) -> Result<(), BackupError> {
    Ok(())
}

fn validate_source_file(path: &Path, max: u64, member: &str) -> Result<(), BackupError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| BackupError::Source(format!("{member} is unavailable: {error}")))?;
    if !metadata.is_file() || metadata.len() > max {
        return Err(BackupError::Source(format!(
            "{member} is unavailable or too large"
        )));
    }
    reject_symlink_ancestors(path)
}

fn reject_symlink_ancestors(path: &Path) -> Result<(), BackupError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        if fs::symlink_metadata(&current).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            // macOS exposes /tmp and /var through /private aliases. They are
            // OS path aliases, not archive-controlled links; reviewed
            // source/target files themselves are still rejected when they
            // are symlinks.
            if current == Path::new("/tmp") || current == Path::new("/var") {
                continue;
            }
            return Err(BackupError::Source(
                "symlink path is not allowed".to_owned(),
            ));
        }
    }
    Ok(())
}

fn ensure_private_directory(path: &Path) -> Result<(), BackupError> {
    if let Ok(metadata) = fs::symlink_metadata(path) {
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(BackupError::Source(
                "backup directory is not a private directory".to_owned(),
            ));
        }
    } else {
        fs::create_dir_all(path)?;
    }
    reject_symlink_ancestors(path)?;
    set_private_dir(path)
}

fn metadata_text(
    paths: &BackupPaths,
    members: &[String],
    install_method: &str,
    created: SystemTime,
) -> String {
    let env_line = if members.iter().any(|member| member == ENV_BASENAME) {
        paths
            .env
            .as_ref()
            .map(|path| format!("env_path = {:?}\n", path.display().to_string()))
            .unwrap_or_default()
    } else {
        String::new()
    };
    format!(
        "format_version = {BACKUP_FORMAT_VERSION}\ncreated_at = {:?}\ninstall_method = {:?}\nconfig_path = {:?}\ndb_path = {:?}\n{}members = [{}]\n",
        iso_timestamp(created),
        install_method,
        paths.config.display().to_string(),
        paths.database.display().to_string(),
        env_line,
        members
            .iter()
            .map(|m| format!("{m:?}"))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn private_staging_dir(parent: &Path, kind: &str) -> Result<PathBuf, BackupError> {
    for _ in 0..32 {
        let path = parent.join(format!(
            ".eggpool-{kind}-staging-{}-{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        match fs::create_dir(&path) {
            Ok(()) => {
                set_private_dir(&path)?;
                return Ok(path);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(BackupError::Source(
        "could not allocate private staging directory".to_owned(),
    ))
}

fn write_atomic(path: &Path, bytes: &[u8], mode: u32) -> Result<(), BackupError> {
    let parent = path
        .parent()
        .ok_or_else(|| BackupError::Source("target has no parent".to_owned()))?;
    fs::create_dir_all(parent)?;
    let temp = parent.join(format!(
        ".eggpool-restore-{}-{}",
        std::process::id(),
        TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)?;
        set_private_file(&temp)?;
        // The mode is part of the published content, so it is applied to the
        // temporary file: a failure here must not leave the new bytes already
        // committed at `path`.
        set_mode(&temp, mode)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        // Fsync the parent directory so the rename itself survives a crash.
        fsync_parent_dir(parent)
    })();
    let _ = fs::remove_file(&temp);
    result
}

fn fsync_parent_dir(parent: &Path) -> Result<(), BackupError> {
    let directory = File::open(parent)?;
    directory.sync_all()?;
    Ok(())
}

fn tempfile_path(path: &Path, kind: &str) -> Result<PathBuf, BackupError> {
    let parent = path
        .parent()
        .ok_or_else(|| BackupError::Source("temporary file has no parent".to_owned()))?;
    fs::create_dir_all(parent)?;
    Ok(parent.join(format!(
        ".eggpool-{kind}-{}-{}",
        std::process::id(),
        TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    )))
}

fn absolute(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}
fn is_backup_name(path: &Path) -> bool {
    path.file_name().and_then(|n| n.to_str()).is_some_and(|n| {
        let Some(rest) = n.strip_prefix("eggpool-backup-") else {
            return false;
        };
        rest.ends_with(".zip")
            && rest.len() >= 19
            && rest.get(..15).is_some_and(|prefix| {
                prefix
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || byte == b'-')
            })
    })
}
fn set_private_dir(path: &Path) -> Result<(), BackupError> {
    set_mode(path, 0o700)
}
fn set_private_file(path: &Path) -> Result<(), BackupError> {
    set_mode(path, 0o600)
}
fn set_mode(path: &Path, mode: u32) -> Result<(), BackupError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    }
    Ok(())
}
fn iso_timestamp(time: SystemTime) -> String {
    let seconds = time
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let days = seconds / 86_400;
    let rem = seconds % 86_400;
    let (y, m, d) = civil_from_days(days as i64);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+00:00",
        rem / 3600,
        (rem / 60) % 60,
        rem % 60
    )
}
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    (y + if m <= 2 { 1 } else { 0 }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "eggpool-backup-oauth-{}-{}",
                std::process::id(),
                TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn restore_config_bytes(host: &str) -> Vec<u8> {
        let mut config = Config::default();
        config.server.host = host.to_owned();
        toml::to_string(&config).unwrap().into_bytes()
    }

    fn restore_database_bytes(directory: &Path, label: &str) -> Vec<u8> {
        let path = directory.join(format!("restore-source-{label}.sqlite3"));
        let connection = tokio_rusqlite::rusqlite::Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE _migrations (version INTEGER NOT NULL, name TEXT NOT NULL);                 CREATE TABLE restore_marker (value TEXT NOT NULL);",
            )
            .unwrap();
        connection
            .execute("INSERT INTO restore_marker(value) VALUES (?1)", [label])
            .unwrap();
        drop(connection);
        let bytes = fs::read(&path).unwrap();
        fs::remove_file(path).unwrap();
        bytes
    }

    fn credential_record(user_id: &str, account_name: &str) -> (String, Vec<u8>) {
        let name = crate::oauth::credential_filename(user_id, "xai", account_name).unwrap();
        let bytes = serde_json::to_vec(&serde_json::json!({
            "format_version": 1,
            "user_id": user_id,
            "provider_id": "xai",
            "account_name": account_name,
            "credentials": {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "id_token": "id-secret",
                "token_type": "Bearer",
                "scope": "openid offline_access",
                "client_id": "test-client",
                "issuer": "https://auth.x.ai",
                "expires_at": 4102444800_i64,
                "obtained_at": 1_i64
            }
        }))
        .unwrap();
        (name, bytes)
    }

    #[test]
    fn oauth_credentials_are_backed_up_and_owner_binding_is_validated() {
        let directory = TestDirectory::new();
        let config = directory.path().join(CONFIG_BASENAME);
        let database = directory.path().join(DB_BASENAME);
        fs::write(&config, b"config").unwrap();
        fs::write(&database, b"database").unwrap();
        let oauth_dir = directory.path().join(OAUTH_DIR_BASENAME);
        fs::create_dir(&oauth_dir).unwrap();
        set_private_dir(&oauth_dir).unwrap();
        let (name, bytes) = credential_record("user-a", "account-a");
        let credential_path = oauth_dir.join(&name);
        fs::write(&credential_path, &bytes).unwrap();
        set_private_file(&credential_path).unwrap();

        let collected = read_oauth_credentials(&config).unwrap();
        assert_eq!(collected.get(&name), Some(&bytes));
        assert!(validate_oauth_credential_archive_member(&name, &bytes).is_ok());
        let (other_name, _) = credential_record("user-b", "account-a");
        assert!(validate_oauth_credential_archive_member(&other_name, &bytes).is_err());

        let paths = BackupPaths {
            config: config.clone(),
            database: database.clone(),
            env: None,
            output_dir: directory.path().to_owned(),
        };
        let members = vec![
            CONFIG_BASENAME.to_owned(),
            DB_BASENAME.to_owned(),
            format!("{OAUTH_MEMBER_PREFIX}{name}"),
        ];
        let metadata = metadata_text(&paths, &members, "test", UNIX_EPOCH);
        let archive = write_archive(
            directory.path(),
            &metadata,
            b"config",
            None,
            &database,
            &collected,
            UNIX_EPOCH,
        )
        .unwrap();
        let prepared = prepare_restore(&archive).unwrap();
        assert_eq!(prepared.oauth_files, collected);
    }

    #[test]
    fn restoring_legacy_or_replaced_oauth_files_never_keeps_stale_credentials() {
        let directory = TestDirectory::new();
        let target = directory.path().join(OAUTH_DIR_BASENAME);
        fs::create_dir(&target).unwrap();
        set_private_dir(&target).unwrap();
        let (stale_name, stale_bytes) = credential_record("old-user", "account-a");
        let stale_path = target.join(&stale_name);
        fs::write(&stale_path, &stale_bytes).unwrap();
        set_private_file(&stale_path).unwrap();

        let (new_name, new_bytes) = credential_record("new-user", "account-a");
        let replacement = BTreeMap::from([(new_name.clone(), new_bytes.clone())]);
        let previous = replace_oauth_directory(&target, &replacement).unwrap();
        assert!(!target.join(&stale_name).exists());
        assert_eq!(fs::read(target.join(&new_name)).unwrap(), new_bytes);
        assert!(previous.as_ref().is_some_and(|path| path.exists()));

        restore_oauth_directory(&target, previous.as_deref()).unwrap();
        assert!(target.join(&stale_name).exists());
        assert!(!target.join(&new_name).exists());

        let previous = replace_oauth_directory(&target, &BTreeMap::new()).unwrap();
        assert!(
            !target.exists(),
            "a legacy backup clears newer OAuth credentials"
        );
        restore_oauth_directory(&target, previous.as_deref()).unwrap();
        assert!(target.join(&stale_name).exists());
    }

    #[test]
    fn restore_refuses_to_create_a_second_rollback_beside_an_unresolved_one() {
        let directory = TestDirectory::new();
        let target = directory.path().join(OAUTH_DIR_BASENAME);
        fs::create_dir(&target).unwrap();
        set_private_dir(&target).unwrap();
        let (current_name, current_bytes) = credential_record("current-user", "account-a");
        let current = target.join(&current_name);
        fs::write(&current, &current_bytes).unwrap();
        set_private_file(&current).unwrap();

        let unresolved = directory.path().join(".eggpool-oauth-restore-old-123-1");
        fs::create_dir(&unresolved).unwrap();
        set_private_dir(&unresolved).unwrap();
        let (old_name, old_bytes) = credential_record("old-user", "account-a");
        let old = unresolved.join(&old_name);
        fs::write(&old, &old_bytes).unwrap();
        set_private_file(&old).unwrap();

        let (new_name, new_bytes) = credential_record("new-user", "account-a");
        let result =
            replace_oauth_directory(&target, &BTreeMap::from([(new_name.clone(), new_bytes)]));

        assert!(matches!(result, Err(BackupError::Source(_))));
        assert_eq!(fs::read(target.join(current_name)).unwrap(), current_bytes);
        assert!(unresolved.join(old_name).exists());
        assert!(!target.join(new_name).exists());
    }

    #[test]
    fn competing_atomic_restores_serialize_the_complete_transaction() {
        use std::sync::mpsc;
        use std::thread;

        let directory = TestDirectory::new();
        let config = directory.path().join(CONFIG_BASENAME);
        let database = directory.path().join(DB_BASENAME);
        let env = directory.path().join(ENV_BASENAME);
        let oauth_dir = directory.path().join(OAUTH_DIR_BASENAME);
        fs::write(&config, b"original config").unwrap();
        fs::write(&database, b"original database").unwrap();
        fs::write(&env, b"ORIGINAL=1\n").unwrap();
        fs::create_dir(&oauth_dir).unwrap();
        set_private_dir(&oauth_dir).unwrap();
        let (old_name, old_bytes) = credential_record("original-user", "account-a");
        let old_path = oauth_dir.join(old_name);
        fs::write(&old_path, old_bytes).unwrap();
        set_private_file(&old_path).unwrap();

        let targets = RestoreTargets {
            config: config.clone(),
            database: database.clone(),
            env: Some(env.clone()),
        };
        let config_a = restore_config_bytes("127.0.0.2");
        let database_a = restore_database_bytes(directory.path(), "a");
        let env_a = b"RESTORE=A\n".to_vec();
        let (oauth_a_name, oauth_a) = credential_record("restore-a", "account-a");
        let prepared_a = PreparedRestore {
            targets: targets.clone(),
            config_bytes: config_a.clone(),
            database_bytes: database_a.clone(),
            env_bytes: Some(env_a.clone()),
            oauth_files: BTreeMap::from([(oauth_a_name.clone(), oauth_a.clone())]),
        };

        let config_b = restore_config_bytes("127.0.0.3");
        let database_b = restore_database_bytes(directory.path(), "b");
        let env_b = b"RESTORE=B\n".to_vec();
        let (oauth_b_name, oauth_b) = credential_record("restore-b", "account-a");
        let prepared_b = PreparedRestore {
            targets,
            config_bytes: config_b.clone(),
            database_bytes: database_b.clone(),
            env_bytes: Some(env_b.clone()),
            oauth_files: BTreeMap::from([(oauth_b_name.clone(), oauth_b.clone())]),
        };

        let lock_path = restore_lock_path(&config).unwrap();
        assert_eq!(lock_path, directory.path().join(RESTORE_LOCK_BASENAME));
        assert_ne!(lock_path, oauth_dir);

        let (first_locked_tx, first_locked_rx) = mpsc::sync_channel(0);
        let (release_first_tx, release_first_rx) = mpsc::sync_channel(0);
        let first = thread::spawn(move || {
            atomic_restore_with_hooks(
                prepared_a,
                || {},
                || {
                    first_locked_tx.send(()).unwrap();
                    release_first_rx
                        .recv_timeout(Duration::from_secs(10))
                        .expect("release first restore");
                },
            )
        });
        first_locked_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("first restore holds the lock");

        let (contender_busy_tx, contender_busy_rx) = mpsc::sync_channel(0);
        let (first_state_tx, first_state_rx) = mpsc::sync_channel(0);
        let contender_lock_path = lock_path.clone();
        let contender_config = config.clone();
        let contender_database = database.clone();
        let contender_env = env.clone();
        let contender_oauth_dir = oauth_dir.clone();
        let observed_oauth_a_name = oauth_a_name.clone();
        let contender = thread::spawn(move || {
            atomic_restore_with_hooks(
                prepared_b,
                move || {
                    let lock_file = OpenOptions::new()
                        .read(true)
                        .write(true)
                        .open(contender_lock_path)
                        .unwrap();
                    let busy = match lock_file.try_lock() {
                        Err(std::fs::TryLockError::WouldBlock) => true,
                        Err(std::fs::TryLockError::Error(_)) | Ok(()) => false,
                    };
                    contender_busy_tx.send(busy).unwrap();
                },
                move || {
                    let state = (
                        fs::read(contender_config).unwrap(),
                        fs::read(contender_database).unwrap(),
                        fs::read(contender_env).unwrap(),
                        fs::read(contender_oauth_dir.join(observed_oauth_a_name)).unwrap(),
                    );
                    first_state_tx.send(state).unwrap();
                },
            )
        });
        let contender_was_blocked = contender_busy_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("competing restore probes the held OS lock");
        release_first_tx.send(()).unwrap();
        let observed_first_state = first_state_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("second restore observes the first committed state");

        assert!(first.join().unwrap().is_ok());
        assert!(contender.join().unwrap().is_ok());
        assert!(contender_was_blocked, "the OS lock must be exclusive");
        assert_eq!(observed_first_state, (config_a, database_a, env_a, oauth_a));
        assert_eq!(fs::read(&config).unwrap(), config_b);
        assert_eq!(fs::read(&database).unwrap(), database_b);
        assert_eq!(fs::read(&env).unwrap(), env_b);
        assert_eq!(fs::read(oauth_dir.join(&oauth_b_name)).unwrap(), oauth_b);
        assert!(!oauth_dir.join(oauth_a_name).exists());
        assert!(lock_path.is_file(), "the stable lock file is retained");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(lock_path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn backup_recovers_an_interrupted_oauth_restore_before_collecting_files() {
        let directory = TestDirectory::new();
        let config = directory.path().join("config.toml");
        let root = crate::oauth::credential_root_for_config(Some(&config));
        let rollback = directory.path().join(".eggpool-oauth-restore-old-123-1");
        fs::create_dir(&rollback).unwrap();
        set_private_dir(&rollback).unwrap();
        let (name, bytes) = credential_record("recovered-user", "account-a");
        let previous = rollback.join(&name);
        fs::write(&previous, &bytes).unwrap();
        set_private_file(&previous).unwrap();

        let recovered = read_oauth_credentials(&config).unwrap();

        assert_eq!(recovered, BTreeMap::from([(name.clone(), bytes.clone())]));
        assert_eq!(fs::read(root.join(name)).unwrap(), bytes);
        assert!(!rollback.exists());
    }

    #[test]
    fn oauth_backup_read_waits_until_restore_rollback_finishes() {
        use std::{sync::mpsc, thread};

        let directory = TestDirectory::new();
        let config = directory.path().join(CONFIG_BASENAME);
        let root = crate::oauth::credential_root_for_config(Some(&config));
        fs::create_dir(&root).unwrap();
        set_private_dir(&root).unwrap();
        let (old_name, old_bytes) = credential_record("original-user", "account-a");
        let old_path = root.join(&old_name);
        fs::write(&old_path, &old_bytes).unwrap();
        set_private_file(&old_path).unwrap();
        let (new_name, new_bytes) = credential_record("replacement-user", "account-a");
        let replacement = BTreeMap::from([(new_name.clone(), new_bytes)]);

        let restore_parent = directory.path().to_owned();
        let restore_root = root.clone();
        let (rollback_window_tx, rollback_window_rx) = mpsc::sync_channel(0);
        let (continue_rollback_tx, continue_rollback_rx) = mpsc::sync_channel(0);
        let restore = thread::spawn(move || {
            let _restore_lock = acquire_oauth_restore_lock(&restore_parent).unwrap();
            replace_oauth_directory_locked_with_hook(&restore_root, &replacement, || {
                rollback_window_tx.send(()).unwrap();
                continue_rollback_rx
                    .recv_timeout(Duration::from_secs(10))
                    .expect("continue restore rollback");
                Err(io::Error::other("injected OAuth publish failure"))
            })
        });
        rollback_window_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("restore paused after moving the prior OAuth root");
        assert!(!root.exists(), "the restore rollback window is open");

        let contender_config = config.clone();
        let lock_path = directory.path().join(RESTORE_LOCK_BASENAME);
        let (reader_started_tx, reader_started_rx) = mpsc::sync_channel(0);
        let (read_result_tx, read_result_rx) = mpsc::sync_channel(0);
        let reader = thread::spawn(move || {
            let probe = OpenOptions::new()
                .read(true)
                .write(true)
                .open(lock_path)
                .unwrap();
            let restore_lock_is_held =
                matches!(probe.try_lock(), Err(std::fs::TryLockError::WouldBlock));
            reader_started_tx.send(restore_lock_is_held).unwrap();
            let result = read_oauth_credentials(&contender_config);
            read_result_tx.send(result).unwrap();
        });
        assert!(
            reader_started_rx
                .recv_timeout(Duration::from_secs(10))
                .expect("reader probes the restore lock during the rename gap"),
            "the restore transaction must hold the stable sibling lock"
        );
        assert!(
            matches!(
                read_result_rx.recv_timeout(Duration::from_secs(1)),
                Err(mpsc::RecvTimeoutError::Timeout)
            ),
            "credential recovery/read must wait for restore rollback"
        );

        continue_rollback_tx.send(()).unwrap();
        assert!(matches!(restore.join().unwrap(), Err(BackupError::Io(_))));
        let snapshot = read_result_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("OAuth read continues after rollback")
            .unwrap();
        reader.join().unwrap();

        assert_eq!(
            snapshot,
            BTreeMap::from([(old_name.clone(), old_bytes.clone())])
        );
        assert_eq!(fs::read(root.join(old_name)).unwrap(), old_bytes);
        assert!(!root.join(new_name).exists());
        assert!(
            fs::read_dir(directory.path()).unwrap().all(|entry| !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".eggpool-oauth-restore-old-")),
            "rollback restores the original directory rather than losing it"
        );
    }

    #[test]
    fn backup_names_match_generated_stamps_and_reject_non_boundary_names() {
        let genuine = backup_filename_at(UNIX_EPOCH + Duration::from_secs(1_786_771_200));
        assert!(genuine.starts_with("eggpool-backup-"));
        assert!(genuine.ends_with(".zip"));
        assert!(is_backup_name(Path::new(&genuine)));
        // A multi-byte name straddling the 15-byte prefix boundary must be
        // rejected, not panic on str slicing.
        assert!(!is_backup_name(Path::new(
            "eggpool-backup-12345678901234é.zip"
        )));
        assert!(!is_backup_name(Path::new("eggpool-backup-short.zip")));
        assert!(!is_backup_name(Path::new("other-20260812-000000.zip")));
    }
}
