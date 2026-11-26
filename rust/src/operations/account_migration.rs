//! Explicit migration out of the removed provider OAuth configuration.
//!
//! The preview is secret-free. Applying it journals progress before removing
//! legacy credential files and atomically replaces the validated TOML file.

use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

use serde::{Deserialize, Serialize};

const MAX_CONFIG_BYTES: usize = 8 * 1024 * 1024;
const MAX_CREDENTIAL_BYTES: u64 = 1024 * 1024;
const MAX_ACCOUNTS: usize = 4096;
const MAX_FIELD_CHARS: usize = 256;
const REPORT_NAME: &str = ".eggpool-oauth-migration.json";
const BACKUP_RECOMMENDATION: &str =
    "Back up the config and credential directory before proceeding.";
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, thiserror::Error)]
pub enum AccountMigrationError {
    #[error("migration input could not be read safely")]
    Input,
    #[error("legacy configuration is invalid or exceeds the migration limit")]
    Config,
    #[error("migration report exceeds its bounded account limit")]
    TooManyAccounts,
    #[error(
        "legacy credential directory contains an unexpected or unsafe entry; nothing was changed"
    )]
    UnsafeCredentialDirectory,
    #[error("owner evidence conflicts; migration was not applied")]
    OwnerConflict,
    #[error("explicit confirmation is required before migration")]
    ConfirmationRequired,
    #[error(
        "the server appears to be running; stop it or pass --allow-live-server only after coordination"
    )]
    LiveServer,
    #[error("configuration changed after preview; rerun the migration")]
    ConfigChanged,
    #[error("migration report could not be written")]
    Report,
    #[error("migration file operation failed")]
    Io,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OAuthAccountOwner {
    pub provider_id: Option<String>,
    pub account_name: String,
    pub owner_user_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MigrationSummary {
    pub format_version: u32,
    pub status: String,
    pub accounts: Vec<OAuthAccountOwner>,
    pub oauth_config_present: bool,
    pub credential_directory_present: bool,
    pub credential_files_found: usize,
    pub credential_files_removed: usize,
    pub config_updated: bool,
    pub credential_directory_removed: bool,
    pub report_file: String,
    pub backup_recommendation: String,
}

pub struct MigrationPlan {
    pub summary: MigrationSummary,
    config_path: PathBuf,
    original_config: Vec<u8>,
    rewritten_config: Vec<u8>,
    credential_directory: Option<PathBuf>,
    credential_files: Vec<PathBuf>,
    config_changed: bool,
}

impl std::fmt::Debug for MigrationPlan {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("MigrationPlan")
            .field("config_path", &self.config_path)
            .field("config_changed", &self.config_changed)
            .field(
                "credential_directory_present",
                &self.summary.credential_directory_present,
            )
            .field("credential_files_found", &self.credential_files.len())
            .field("oauth_accounts_found", &self.summary.accounts.len())
            .finish()
    }
}

impl MigrationPlan {
    pub fn has_changes(&self) -> bool {
        self.config_changed
            || !self.credential_files.is_empty()
            || self.summary.credential_directory_present
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct AccountKey {
    provider_id: Option<String>,
    account_name: String,
}

#[derive(Debug, Deserialize)]
struct LegacyCredentialHeader {
    format_version: u32,
    user_id: String,
    provider_id: String,
    account_name: String,
}

/// Read and report the legacy OAuth surfaces without changing any user data.
pub async fn inspect(config_path: &Path) -> Result<MigrationPlan, AccountMigrationError> {
    let original_config = read_config(config_path)?;
    let mut value: toml::Value = std::str::from_utf8(&original_config)
        .map_err(|_| AccountMigrationError::Config)?
        .parse()
        .map_err(|_| AccountMigrationError::Config)?;
    let oauth_config_present = value.get("oauth").is_some();
    let mut accounts = collect_oauth_accounts(&value)?;
    let database_config = database_config(&value);
    let bindings = load_owner_snapshot(&database_config).await;
    let (credential_directory, credential_files, credential_owners) =
        inspect_credential_directory(config_path)?;

    for (key, owner) in credential_owners {
        if let Some(owner) = owner {
            merge_owner(&mut accounts, key, owner)?;
        } else {
            accounts.entry(key).or_insert(None);
        }
    }
    let accounts = accounts
        .into_iter()
        .map(|(key, file_owner)| {
            let database_owner = key
                .provider_id
                .as_deref()
                .and_then(|provider| bindings.owner_user_id(provider, &key.account_name));
            if let (Some(file), Some(database)) = (file_owner.as_deref(), database_owner)
                && file != database
            {
                return Err(AccountMigrationError::OwnerConflict);
            }
            let owner = database_owner.map(str::to_owned).or(file_owner);
            Ok(OAuthAccountOwner {
                provider_id: key.provider_id,
                account_name: key.account_name,
                owner_user_id: owner,
            })
        })
        .collect::<Result<Vec<_>, AccountMigrationError>>()?;
    if accounts.len() > MAX_ACCOUNTS {
        return Err(AccountMigrationError::TooManyAccounts);
    }

    let removed_accounts = crate::config::strip_legacy_oauth_surfaces(&mut value);
    let config_changed = removed_accounts > 0 || oauth_config_present;
    let rewritten_config = if config_changed {
        let bytes = toml::to_string_pretty(&value)
            .map_err(|_| AccountMigrationError::Config)?
            .into_bytes();
        crate::config::Config::from_toml_bytes(config_path, &bytes)
            .map_err(|_| AccountMigrationError::Config)?;
        bytes
    } else {
        original_config.clone()
    };

    let summary = MigrationSummary {
        format_version: 1,
        status: "planned".to_owned(),
        accounts,
        oauth_config_present,
        credential_directory_present: credential_directory.is_some(),
        credential_files_found: credential_files.len(),
        credential_files_removed: 0,
        config_updated: false,
        credential_directory_removed: false,
        report_file: REPORT_NAME.to_owned(),
        backup_recommendation: BACKUP_RECOMMENDATION.to_owned(),
    };
    Ok(MigrationPlan {
        summary,
        config_path: config_path.to_owned(),
        original_config,
        rewritten_config,
        credential_directory,
        credential_files,
        config_changed,
    })
}

/// Apply a reviewed migration plan. Confirmation is checked here as well as in
/// the CLI so a caller cannot accidentally invoke a destructive plan by default.
pub async fn execute(
    plan: MigrationPlan,
    confirmed: bool,
    allow_live_server: bool,
) -> Result<MigrationSummary, AccountMigrationError> {
    let paths = crate::operations::paths::RuntimePaths::resolve();
    let pid = crate::operations::process::read_pid(&paths.pid_file)
        .map_err(|_| AccountMigrationError::Input)?;
    let pid_running = pid.is_some_and(crate::operations::process::process_exists);
    let control_running = crate::operations::process::probe_control(&paths.control_socket).await
        == crate::operations::process::ControlProbe::Reachable;
    execute_with_server_state(
        plan,
        confirmed,
        allow_live_server,
        pid_running || control_running,
    )
}

fn execute_with_server_state(
    mut plan: MigrationPlan,
    confirmed: bool,
    allow_live_server: bool,
    server_running: bool,
) -> Result<MigrationSummary, AccountMigrationError> {
    let report_path = report_path(&plan.config_path);
    if !plan.has_changes() {
        plan.summary.status = "nothing_to_do".to_owned();
        write_report(&report_path, &plan.summary)?;
        return Ok(plan.summary);
    }
    if !confirmed {
        return Err(AccountMigrationError::ConfirmationRequired);
    }
    if server_running && !allow_live_server {
        let mut blocked = plan.summary.clone();
        blocked.status = "blocked_live_server".to_owned();
        write_report(&report_path, &blocked)?;
        return Err(AccountMigrationError::LiveServer);
    }
    if read_config(&plan.config_path)? != plan.original_config {
        return Err(AccountMigrationError::ConfigChanged);
    }

    plan.summary.status = "in_progress".to_owned();
    write_report(&report_path, &plan.summary)?;
    for file in &plan.credential_files {
        match fs::remove_file(file) {
            Ok(()) => plan.summary.credential_files_removed += 1,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(AccountMigrationError::Io),
        }
        write_report(&report_path, &plan.summary)?;
    }
    if let Some(directory) = &plan.credential_directory {
        match fs::remove_dir(directory) {
            Ok(()) => plan.summary.credential_directory_removed = true,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                plan.summary.credential_directory_removed = true;
            }
            Err(_) => return Err(AccountMigrationError::UnsafeCredentialDirectory),
        }
        write_report(&report_path, &plan.summary)?;
    }
    if plan.config_changed {
        if read_config(&plan.config_path)? != plan.original_config {
            return Err(AccountMigrationError::ConfigChanged);
        }
        let mode = existing_mode(&plan.config_path)?;
        atomic_write(&plan.config_path, &plan.rewritten_config, mode)
            .map_err(|_| AccountMigrationError::Io)?;
        plan.summary.config_updated = true;
        write_report(&report_path, &plan.summary)?;
    }
    plan.summary.status = "completed".to_owned();
    write_report(&report_path, &plan.summary)?;
    Ok(plan.summary)
}

fn read_config(path: &Path) -> Result<Vec<u8>, AccountMigrationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| AccountMigrationError::Input)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(AccountMigrationError::Input);
    }
    let mut file = File::open(path).map_err(|_| AccountMigrationError::Input)?;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take((MAX_CONFIG_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| AccountMigrationError::Input)?;
    if bytes.len() > MAX_CONFIG_BYTES {
        return Err(AccountMigrationError::Config);
    }
    Ok(bytes)
}

fn collect_oauth_accounts(
    value: &toml::Value,
) -> Result<BTreeMap<AccountKey, Option<String>>, AccountMigrationError> {
    let mut accounts = BTreeMap::new();
    if let Some(entries) = value.get("accounts").and_then(toml::Value::as_array) {
        for account in entries
            .iter()
            .filter(|entry| crate::config::is_legacy_oauth_account(entry))
        {
            insert_account(&mut accounts, None, account)?;
        }
    }
    if let Some(providers) = value.get("providers").and_then(toml::Value::as_table) {
        for (provider_id, provider) in providers {
            if let Some(entries) = provider.get("accounts").and_then(toml::Value::as_array) {
                for account in entries
                    .iter()
                    .filter(|entry| crate::config::is_legacy_oauth_account(entry))
                {
                    insert_account(&mut accounts, Some(provider_id), account)?;
                }
            }
        }
    }
    Ok(accounts)
}

fn insert_account(
    accounts: &mut BTreeMap<AccountKey, Option<String>>,
    provider: Option<&str>,
    account: &toml::Value,
) -> Result<(), AccountMigrationError> {
    let name = account
        .get("name")
        .and_then(toml::Value::as_str)
        .ok_or(AccountMigrationError::Config)?;
    let provider_id = provider
        .or_else(|| account.get("provider_id").and_then(toml::Value::as_str))
        .or_else(|| account.get("provider").and_then(toml::Value::as_str))
        .map(bounded_field)
        .transpose()?;
    accounts
        .entry(AccountKey {
            provider_id,
            account_name: bounded_field(name)?,
        })
        .or_insert(None);
    if accounts.len() > MAX_ACCOUNTS {
        return Err(AccountMigrationError::TooManyAccounts);
    }
    Ok(())
}

/// The owner binding recorded for each account, where one is known.
type AccountOwners = BTreeMap<AccountKey, Option<String>>;

fn inspect_credential_directory(
    config_path: &Path,
) -> Result<(Option<PathBuf>, Vec<PathBuf>, AccountOwners), AccountMigrationError> {
    let parent = config_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let directory = parent.join(".eggpool-oauth");
    let metadata = match fs::symlink_metadata(&directory) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok((None, Vec::new(), BTreeMap::new()));
        }
        Err(_) => return Err(AccountMigrationError::Input),
    };
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(AccountMigrationError::UnsafeCredentialDirectory);
    }
    let mut files = Vec::new();
    let mut owners = BTreeMap::new();
    for entry in fs::read_dir(&directory).map_err(|_| AccountMigrationError::Input)? {
        let entry = entry.map_err(|_| AccountMigrationError::Input)?;
        let file_type = entry
            .file_type()
            .map_err(|_| AccountMigrationError::Input)?;
        let name = entry.file_name();
        let name = name
            .to_str()
            .ok_or(AccountMigrationError::UnsafeCredentialDirectory)?;
        if !file_type.is_file() || !is_managed_credential_file(name) {
            return Err(AccountMigrationError::UnsafeCredentialDirectory);
        }
        let path = entry.path();
        if is_credential_filename(name) {
            let metadata = entry.metadata().map_err(|_| AccountMigrationError::Input)?;
            if metadata.len() > MAX_CREDENTIAL_BYTES {
                return Err(AccountMigrationError::UnsafeCredentialDirectory);
            }
            let bytes = fs::read(&path).map_err(|_| AccountMigrationError::Input)?;
            let header = serde_json::from_slice::<LegacyCredentialHeader>(&bytes)
                .map_err(|_| AccountMigrationError::UnsafeCredentialDirectory)?;
            if header.format_version != 1 {
                return Err(AccountMigrationError::UnsafeCredentialDirectory);
            }
            let key = AccountKey {
                provider_id: Some(bounded_field(&header.provider_id)?),
                account_name: bounded_field(&header.account_name)?,
            };
            merge_owner(&mut owners, key, bounded_field(&header.user_id)?)?;
        }
        files.push(path);
    }
    Ok((Some(directory), files, owners))
}

fn is_managed_credential_file(name: &str) -> bool {
    is_credential_filename(name)
        || name.ends_with(".lock")
        || (name.starts_with(".oauth-") && (name.ends_with(".tmp") || name.ends_with(".rollback")))
}

fn is_credential_filename(name: &str) -> bool {
    name.strip_suffix(".json")
        .is_some_and(|stem| stem.len() == 64 && stem.bytes().all(|byte| byte.is_ascii_hexdigit()))
}

fn merge_owner(
    accounts: &mut BTreeMap<AccountKey, Option<String>>,
    key: AccountKey,
    owner: String,
) -> Result<(), AccountMigrationError> {
    let slot = accounts.entry(key).or_insert(None);
    if slot.as_ref().is_some_and(|current| current != &owner) {
        return Err(AccountMigrationError::OwnerConflict);
    }
    *slot = Some(owner);
    if accounts.len() > MAX_ACCOUNTS {
        return Err(AccountMigrationError::TooManyAccounts);
    }
    Ok(())
}

fn bounded_field(value: &str) -> Result<String, AccountMigrationError> {
    if value.chars().count() > MAX_FIELD_CHARS || value.chars().any(char::is_control) {
        return Err(AccountMigrationError::Config);
    }
    Ok(value.to_owned())
}

fn database_config(value: &toml::Value) -> crate::config::DatabaseConfig {
    value
        .get("database")
        .cloned()
        .and_then(|value| value.try_into().ok())
        .unwrap_or_default()
}

async fn load_owner_snapshot(
    config: &crate::config::DatabaseConfig,
) -> crate::personal_upstream::PersonalUpstreamSnapshot {
    let path = crate::config::Config::runtime_path(&config.path);
    if !path.is_file() {
        return crate::personal_upstream::PersonalUpstreamSnapshot::default();
    }
    let mut database_config = crate::db::DatabaseConfig::from(config);
    database_config.read_only = true;
    let Ok(database) = crate::db::Database::open(database_config).await else {
        return crate::personal_upstream::PersonalUpstreamSnapshot::default();
    };
    let snapshot = crate::personal_upstream::load_snapshot(&database)
        .await
        .unwrap_or_default();
    let _ = database.close().await;
    snapshot
}

fn report_path(config_path: &Path) -> PathBuf {
    config_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
        .join(REPORT_NAME)
}

fn write_report(path: &Path, summary: &MigrationSummary) -> Result<(), AccountMigrationError> {
    let mut bytes =
        serde_json::to_vec_pretty(summary).map_err(|_| AccountMigrationError::Report)?;
    bytes.push(b'\n');
    atomic_write(path, &bytes, private_mode()).map_err(|_| AccountMigrationError::Report)
}

fn existing_mode(path: &Path) -> Result<Option<fs::Permissions>, AccountMigrationError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            Err(AccountMigrationError::Input)
        }
        Ok(metadata) => Ok(Some(metadata.permissions())),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(AccountMigrationError::Input),
    }
}

fn private_mode() -> Option<fs::Permissions> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        Some(fs::Permissions::from_mode(0o600))
    }
    #[cfg(not(unix))]
    {
        None
    }
}

fn atomic_write(path: &Path, bytes: &[u8], mode: Option<fs::Permissions>) -> io::Result<()> {
    let parent = path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    if let Ok(metadata) = fs::symlink_metadata(path)
        && metadata.file_type().is_symlink()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "symlink target",
        ));
    }
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing filename"))?
        .to_string_lossy();
    let temporary = parent.join(format!(
        ".{name}.tmp-{}-{}",
        std::process::id(),
        TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary)?;
    let result = (|| {
        file.write_all(bytes)?;
        file.sync_all()?;
        if let Some(mode) = mode {
            fs::set_permissions(&temporary, mode)?;
        }
        fs::rename(&temporary, path)?;
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_oauth_accounts_are_removed_from_config_arrays() {
        let mut value: toml::Value = r#"
            [oauth]
            enabled = true
            [[providers.xai.accounts]]
            name = "personal"
            auth_mode = "oauth"
            [[providers.xai.accounts]]
            name = "static"
            auth_mode = "static_key"
            api_key_env = "XAI_KEY"
            [[providers.xai.accounts]]
            name = "agent"
            auth_mode = "agent"
        "#
        .parse()
        .unwrap();
        assert_eq!(collect_oauth_accounts(&value).unwrap().len(), 1);
        assert_eq!(crate::config::strip_legacy_oauth_surfaces(&mut value), 1);
        assert!(value.get("oauth").is_none());
        assert!(!value.to_string().contains("auth_mode = \"oauth\""));
        assert!(value.to_string().contains("static"));
        assert!(value.to_string().contains("agent"));
        assert_eq!(
            value["providers"]["xai"]["accounts"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }

    #[test]
    fn migration_plan_debug_redacts_configuration_bytes_and_secrets() {
        let secret = "plaintext-server-api-key-sentinel";
        let original_config = format!("[server]\napi_key = \"{secret}\"\n").into_bytes();
        let rewritten_config =
            format!("[server]\napi_key = \"{secret}\"\n# rewritten\n").into_bytes();
        let plan = MigrationPlan {
            summary: MigrationSummary {
                format_version: 1,
                status: "planned".to_owned(),
                accounts: vec![OAuthAccountOwner {
                    provider_id: Some("xai".to_owned()),
                    account_name: "personal".to_owned(),
                    owner_user_id: None,
                }],
                oauth_config_present: true,
                credential_directory_present: true,
                credential_files_found: 2,
                credential_files_removed: 0,
                config_updated: false,
                credential_directory_removed: false,
                report_file: REPORT_NAME.to_owned(),
                backup_recommendation: BACKUP_RECOMMENDATION.to_owned(),
            },
            config_path: PathBuf::from("/tmp/config.toml"),
            original_config: original_config.clone(),
            rewritten_config: rewritten_config.clone(),
            credential_directory: Some(PathBuf::from("/tmp/.eggpool-oauth")),
            credential_files: vec![PathBuf::from("/tmp/.eggpool-oauth/credential.json"); 2],
            config_changed: true,
        };
        let debug = format!("{plan:?}");
        assert!(!debug.contains(&format!("{original_config:?}")));
        assert!(!debug.contains(&format!("{rewritten_config:?}")));
        assert!(!debug.contains(secret));
        assert!(debug.contains("/tmp/config.toml"));
        assert!(debug.contains("config_changed: true"));
        assert!(debug.contains("credential_directory_present: true"));
        assert!(debug.contains("credential_files_found: 2"));
        assert!(debug.contains("oauth_accounts_found: 1"));
    }

    #[test]
    fn credential_directory_preflight_rejects_unexpected_files() {
        let directory = tempfile::tempdir().unwrap();
        let config = directory.path().join("config.toml");
        fs::write(&config, "[server]\\nport=8080\\n").unwrap();
        let credentials = directory.path().join(".eggpool-oauth");
        fs::create_dir(&credentials).unwrap();
        fs::write(credentials.join("unrelated.txt"), b"keep").unwrap();
        assert!(matches!(
            inspect_credential_directory(&config),
            Err(AccountMigrationError::UnsafeCredentialDirectory)
        ));
    }

    fn migration_fixture(directory: &Path) -> (PathBuf, PathBuf, Vec<u8>) {
        let config_path = directory.join("config.toml");
        let database_path = directory.join("missing.sqlite3");
        let config = format!(
            "[database]\npath = {:?}\n[oauth]\nenabled = true\n",
            database_path.display().to_string()
        )
        .into_bytes();
        fs::write(&config_path, &config).unwrap();
        let credential_directory = directory.join(".eggpool-oauth");
        fs::create_dir(&credential_directory).unwrap();
        let credential = credential_directory.join(format!("{}.json", "a".repeat(64)));
        fs::write(
            &credential,
            br#"{"format_version":1,"user_id":"owner-1","provider_id":"xai","account_name":"personal","access_token":"secret-sentinel"}"#,
        )
        .unwrap();
        (config_path, credential, config)
    }

    #[tokio::test]
    async fn migration_requires_confirmation_and_refuses_an_uncoordinated_live_server() {
        let directory = tempfile::tempdir().unwrap();
        let (config_path, credential, original) = migration_fixture(directory.path());
        let plan = inspect(&config_path).await.unwrap();
        assert_eq!(plan.summary.accounts.len(), 1);
        assert_eq!(
            plan.summary.accounts[0].owner_user_id.as_deref(),
            Some("owner-1")
        );
        assert!(matches!(
            execute_with_server_state(plan, false, false, false),
            Err(AccountMigrationError::ConfirmationRequired)
        ));
        assert_eq!(fs::read(&config_path).unwrap(), original);
        assert!(credential.exists());

        let plan = inspect(&config_path).await.unwrap();
        assert!(matches!(
            execute_with_server_state(plan, true, false, true),
            Err(AccountMigrationError::LiveServer)
        ));
        assert_eq!(fs::read(&config_path).unwrap(), original);
        assert!(credential.exists());
        let report: serde_json::Value =
            serde_json::from_slice(&fs::read(directory.path().join(REPORT_NAME)).unwrap()).unwrap();
        assert_eq!(report["status"], "blocked_live_server");
    }

    #[tokio::test]
    async fn migration_reports_removes_only_legacy_oauth_and_is_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let (config_path, credential, _) = migration_fixture(directory.path());
        let plan = inspect(&config_path).await.unwrap();
        let summary = execute_with_server_state(plan, true, false, false).unwrap();
        assert_eq!(summary.status, "completed");
        assert_eq!(summary.credential_files_removed, 1);
        assert!(summary.config_updated);
        assert!(summary.credential_directory_removed);
        assert_eq!(summary.accounts.len(), 1);
        assert_eq!(
            summary.accounts[0].owner_user_id.as_deref(),
            Some("owner-1")
        );
        assert!(!credential.exists());
        assert!(!directory.path().join(".eggpool-oauth").exists());
        let rewritten = fs::read(&config_path).unwrap();
        assert!(!String::from_utf8_lossy(&rewritten).contains("[oauth]"));
        crate::config::Config::from_toml_bytes(&config_path, &rewritten).unwrap();

        let report = fs::read_to_string(directory.path().join(REPORT_NAME)).unwrap();
        assert!(!report.contains("secret-sentinel"));
        let second = inspect(&config_path).await.unwrap();
        let second = execute_with_server_state(second, false, false, false).unwrap();
        assert_eq!(second.status, "nothing_to_do");
    }

    #[tokio::test]
    async fn interrupted_migration_leaves_a_report_and_never_rewrites_partial_toml() {
        let directory = tempfile::tempdir().unwrap();
        let (config_path, credential, original) = migration_fixture(directory.path());
        let plan = inspect(&config_path).await.unwrap();
        fs::write(
            directory.path().join(".eggpool-oauth/unexpected.txt"),
            b"retain me",
        )
        .unwrap();
        assert!(matches!(
            execute_with_server_state(plan, true, false, false),
            Err(AccountMigrationError::UnsafeCredentialDirectory)
        ));
        assert_eq!(fs::read(&config_path).unwrap(), original);
        assert!(!credential.exists());
        let report: serde_json::Value =
            serde_json::from_slice(&fs::read(directory.path().join(REPORT_NAME)).unwrap()).unwrap();
        assert_eq!(report["status"], "in_progress");
        assert_eq!(report["credential_files_removed"], 1);
        assert!(
            directory
                .path()
                .join(".eggpool-oauth/unexpected.txt")
                .exists()
        );
    }
}
