//! Safe, local database backup and recovery for O006.
//!
//! The archive format is intentionally small and boring: a stored ZIP with a
//! TOML `META` member, the reviewed config and optional env file, and a
//! consistent SQLite snapshot.  All archive input is validated before any
//! target path is touched.

use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
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
const MAX_ARCHIVE_MEMBERS: usize = 6;
const MAX_CONFIG_BYTES: u64 = 8 * 1024 * 1024;
const MAX_ENV_BYTES: u64 = 8 * 1024 * 1024;
const MAX_DATABASE_BYTES: u64 = 512 * 1024 * 1024;
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
        if self.include_env {
            if let Some(env) = &self.paths.env {
                if env.exists() {
                    validate_source_file(env, MAX_ENV_BYTES, ENV_BASENAME)?;
                }
            }
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
            let mut members = vec![CONFIG_BASENAME.to_owned(), DB_BASENAME.to_owned()];
            if env_bytes.is_some() {
                members.push(ENV_BASENAME.to_owned());
            }
            let created = SystemTime::now();
            let metadata = metadata_text(&self.paths, &members, &self.install_method, created);
            let archive = write_archive(
                &self.paths.output_dir,
                &metadata,
                &config_bytes,
                env_bytes.as_deref(),
                &staged_db,
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
        let mut entries = list_backups(&self.paths.output_dir)?;
        if entries.len() <= retain_count as usize {
            return Ok(Vec::new());
        }
        entries.sort_by(|a, b| b.cmp(a));
        let mut removed = Vec::new();
        for path in entries.into_iter().skip(retain_count as usize) {
            if fs::remove_file(&path).is_ok() {
                removed.push(path);
            }
        }
        Ok(removed)
    }

    pub fn recover(&self, archive: &Path) -> Result<RestoreResult, BackupError> {
        let prepared = Self::validated_archive(archive)?;
        atomic_restore(prepared)
    }

    /// Validate every archive member, manifest, target, config, and database
    /// before a caller decides whether it is safe to stop a running server.
    pub fn validate_archive(archive: &Path) -> Result<(), BackupError> {
        let _ = Self::validated_archive(archive)?;
        Ok(())
    }

    fn validated_archive(archive: &Path) -> Result<PreparedRestore, BackupError> {
        let prepared = prepare_restore(archive)?;
        validate_restore_targets(&prepared.targets)?;
        validate_staged_config(&prepared.config_bytes, &prepared.targets.config)?;
        validate_staged_database(&prepared.database_bytes, &prepared.targets.database)?;
        Ok(prepared)
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
    paths.sort_by(|a, b| b.file_name().cmp(&a.file_name()));
    Ok(paths)
}

fn write_archive(
    directory: &Path,
    metadata: &str,
    config: &[u8],
    env: Option<&[u8]>,
    database: &Path,
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

fn prepare_restore(archive_path: &Path) -> Result<PreparedRestore, BackupError> {
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
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(BackupError::Archive)?;
        if entry.is_dir() || entry.encrypted() {
            return Err(BackupError::InvalidArchive(
                "directories and encrypted members are not allowed".to_owned(),
            ));
        }
        let name = entry.name().to_owned();
        if !matches!(
            name.as_str(),
            META_BASENAME | CONFIG_BASENAME | ENV_BASENAME | DB_BASENAME
        ) || !names.insert(name.clone())
        {
            return Err(BackupError::InvalidArchive(
                "unexpected or duplicate member".to_owned(),
            ));
        }
        if entry
            .unix_mode()
            .is_some_and(|mode| mode & 0o170000 != 0o100000)
        {
            return Err(BackupError::InvalidArchive(
                "special-file member is not allowed".to_owned(),
            ));
        }
        let cap = match name.as_str() {
            META_BASENAME => 64 * 1024,
            CONFIG_BASENAME => MAX_CONFIG_BYTES,
            ENV_BASENAME => MAX_ENV_BYTES,
            DB_BASENAME => MAX_DATABASE_BYTES,
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
            _ => unreachable!(),
        }
    }
    let metadata =
        metadata.ok_or_else(|| BackupError::InvalidArchive("META is required".to_owned()))?;
    let value: toml::Value = std::str::from_utf8(&metadata)
        .map_err(|_| BackupError::InvalidArchive("META is not UTF-8".to_owned()))?
        .parse()
        .map_err(|_| BackupError::InvalidArchive("META is not valid TOML".to_owned()))?;
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
    })
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
    let conn = tokio_rusqlite::rusqlite::Connection::open(path)
        .map_err(|_| BackupError::InvalidArchive("database could not be opened".to_owned()))?;
    let status: String = conn
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(|_| BackupError::InvalidArchive("database integrity check failed".to_owned()))?;
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
        .map_err(|_| BackupError::InvalidArchive("migration ledger is missing".to_owned()))?;
    if !has_ledger {
        return Err(BackupError::InvalidArchive(
            "migration ledger is missing".to_owned(),
        ));
    }
    let mut statement = conn
        .prepare("SELECT version, name FROM _migrations ORDER BY version")
        .map_err(|_| BackupError::InvalidArchive("migration ledger is unreadable".to_owned()))?;
    let applied = statement
        .query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|_| BackupError::InvalidArchive("migration ledger is unreadable".to_owned()))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| BackupError::InvalidArchive("migration ledger is unreadable".to_owned()))?;
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

fn atomic_restore(prepared: PreparedRestore) -> Result<RestoreResult, BackupError> {
    let targets = &prepared.targets;
    let root = targets
        .database
        .parent()
        .ok_or_else(|| BackupError::InvalidArchive("database target has no parent".to_owned()))?;
    fs::create_dir_all(root)?;
    set_private_dir(root)?;
    let safety = private_staging_dir(root, "restore")?;
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
    let result = (|| {
        write_atomic(&targets.config, &prepared.config_bytes, 0o600)?;
        if let (Some(target), Some(bytes)) = (&targets.env, &prepared.env_bytes) {
            write_atomic(target, bytes, 0o600)?;
        }
        write_atomic(&targets.database, &prepared.database_bytes, 0o600)?;
        validate_staged_config(&prepared.config_bytes, &targets.config)?;
        validate_staged_database(&prepared.database_bytes, &targets.database)
    })();
    if result.is_err() {
        let rollback = originals.iter().try_for_each(|(target, saved)| {
            match saved {
                Some(saved) => {
                    fs::copy(saved, target)?;
                    set_private_file(target)?;
                }
                None => {
                    let _ = fs::remove_file(target);
                }
            }
            Ok::<_, BackupError>(())
        });
        if rollback.is_err() {
            return Err(BackupError::RestoreRollback);
        }
        let _ = fs::remove_dir_all(&safety);
        let error = match result {
            Ok(()) => unreachable!("restore result was checked as an error"),
            Err(error) => error,
        };
        return Err(error);
    }
    fs::remove_dir_all(&safety)?;
    Ok(RestoreResult {
        config: targets.config.clone(),
        database: targets.database.clone(),
        env: targets.env.clone(),
    })
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
    let before = fs::symlink_metadata(path)?;
    if !before.is_file() || before.len() > max {
        return Err(BackupError::Source(format!(
            "{member} is unavailable or too large"
        )));
    }
    let file = File::open(path)?;
    let mut bytes = Vec::with_capacity(before.len() as usize);
    file.take(max + 1).read_to_end(&mut bytes)?;
    let after = fs::symlink_metadata(path)?;
    if before.len() != after.len() || before.modified().ok() != after.modified().ok() {
        return Err(BackupError::Source(format!(
            "{member} changed during backup"
        )));
    }
    Ok(bytes)
}

fn validate_source_file(path: &Path, max: u64, member: &str) -> Result<(), BackupError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| BackupError::Source(format!("{member} is unavailable")))?;
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
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        set_mode(path, mode)
    })();
    let _ = fs::remove_file(&temp);
    result
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
            && rest.len() >= 20
            && rest[..15].chars().all(|c| c.is_ascii_digit() || c == '-')
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
