//! Owner-scoped OAuth credential custody and proactive account token resolution.
//!
//! Token files live beside the active configuration in a private directory;
//! SQLite stores only the existing personal-upstream owner binding. Every
//! account operation holds the stable sibling restore lock and its per-identity
//! OS lock across load, refresh, and atomic replacement so restore and rotating
//! refresh tokens cannot race across EggPool processes.

use std::{
    collections::BTreeSet,
    fmt,
    fs::{self, File, OpenOptions},
    future::Future,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

pub use eggpass_auth::Credentials as OAuthCredentials;
use eggpass_auth::{
    CredentialStore as EggpassCredentialStore, Credentials, OAuthClient, OAuthProviderConfig,
    StorageError, TokenManager,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use zeroize::Zeroizing;

use crate::{
    config::{AccountAuthMode, Config, SecretString},
    personal_upstream::PersonalUpstreamSnapshot,
    providers::ProviderClientPool,
};

const CREDENTIAL_FILE_VERSION: u32 = 1;
const MAX_CREDENTIAL_FILE_BYTES: u64 = 1024 * 1024;
static TEMP_FILE_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Stable OAuth account failures. Variants and display text never contain
/// provider response bodies, file paths, account tokens, or OAuth parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum OAuthAccountError {
    #[error("OAuth account service is disabled")]
    Disabled,
    #[error("OAuth account is not configured")]
    AccountNotConfigured,
    #[error("OAuth account owner is missing or invalid")]
    InvalidOwner,
    #[error("OAuth provider is not supported")]
    UnsupportedProvider,
    #[error("OAuth account transport is unavailable")]
    ClientUnavailable,
    #[error("OAuth credentials require re-authentication")]
    NeedsReauthentication,
    #[error("OAuth credential storage failed")]
    Storage,
    #[error("OAuth credentials are invalid for this provider")]
    InvalidCredentials,
    #[error("OAuth credentials could not be refreshed")]
    RefreshFailed,
}

/// Per-user/provider/account credential identity. The file name is a
/// deterministic hash of this tuple; the tuple is also checked inside the file
/// so copying a credential file cannot change its owner binding.
#[derive(Clone, PartialEq, Eq)]
struct CredentialIdentity {
    user_id: String,
    provider_id: String,
    account_name: String,
}

impl CredentialIdentity {
    fn new(
        user_id: &str,
        provider_id: &str,
        account_name: &str,
    ) -> Result<Self, OAuthAccountError> {
        if [user_id, provider_id, account_name]
            .into_iter()
            .any(|value| value.is_empty() || value.chars().any(char::is_control))
        {
            return Err(OAuthAccountError::InvalidOwner);
        }
        Ok(Self {
            user_id: user_id.to_owned(),
            provider_id: provider_id.to_owned(),
            account_name: account_name.to_owned(),
        })
    }

    fn file_name(&self) -> String {
        let mut digest = Sha256::new();
        digest.update(self.user_id.as_bytes());
        digest.update([0]);
        digest.update(self.provider_id.as_bytes());
        digest.update([0]);
        digest.update(self.account_name.as_bytes());
        let digest = digest.finalize();
        let mut name = String::with_capacity(64 + 5);
        for byte in digest {
            use fmt::Write as _;
            let _ = write!(name, "{byte:02x}");
        }
        name.push_str(".json");
        name
    }
}

/// Recompute the storage name when validating credential files in a backup.
pub(crate) fn credential_filename(
    user_id: &str,
    provider_id: &str,
    account_name: &str,
) -> Option<String> {
    CredentialIdentity::new(user_id, provider_id, account_name)
        .ok()
        .map(|identity| identity.file_name())
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CredentialFile {
    format_version: u32,
    user_id: String,
    provider_id: String,
    account_name: String,
    credentials: Credentials,
}

/// Both OS locks remain held for the complete account operation. The stable
/// sibling lock serializes it with backup/restore; the per-identity lock keeps
/// refresh-token rotation single-flight across EggPool processes.
struct OAuthCredentialLocks {
    _restore: File,
    _account: File,
}

/// Generation-owned OAuth account service. Each token manager is scoped to a
/// single account operation.
#[derive(Clone)]
pub struct OAuthAccountManager {
    enabled: bool,
    credential_root: PathBuf,
    configured_accounts: BTreeSet<(String, String)>,
    personal_bindings: PersonalUpstreamSnapshot,
    clients: ProviderClientPool,
}

impl fmt::Debug for OAuthAccountManager {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("OAuthAccountManager")
            .field("enabled", &self.enabled)
            .field("configured_account_count", &self.configured_accounts.len())
            .finish()
    }
}

impl OAuthAccountManager {
    pub(crate) fn new(
        config: &Config,
        credential_root: PathBuf,
        clients: ProviderClientPool,
        personal_bindings: PersonalUpstreamSnapshot,
    ) -> Self {
        let configured_accounts = config
            .providers
            .iter()
            .flat_map(|(provider_id, provider)| {
                provider
                    .accounts
                    .iter()
                    .filter(|account| account.auth_mode == AccountAuthMode::OAuth)
                    .map(|account| (provider_id.clone(), account.name.clone()))
            })
            .collect();
        Self {
            enabled: config.oauth.enabled,
            credential_root,
            configured_accounts,
            personal_bindings,
            clients,
        }
    }

    /// Return a proactive, expiry-checked access token for exactly one managed
    /// user and configured provider account. This is called before synchronous
    /// borrowed attempt preparation; callers must never retry a submitted 401
    /// through this method.
    pub async fn access_token_for_user(
        &self,
        user_id: &str,
        provider_id: &str,
        account_name: &str,
    ) -> Result<SecretString, OAuthAccountError> {
        if !self.enabled {
            return Err(OAuthAccountError::Disabled);
        }
        let identity = self.identity(user_id, provider_id, account_name)?;
        let client = self.client_for_account(provider_id, account_name)?;
        self.access_token_for_identity(identity, client).await
    }

    async fn access_token_for_identity(
        &self,
        identity: CredentialIdentity,
        client: OAuthClient,
    ) -> Result<SecretString, OAuthAccountError> {
        let lock = self.lock(&identity).await?;
        let store = FileCredentialStore::new(
            self.credential_root.clone(),
            identity,
            Some(client.provider().issuer().to_owned()),
        );
        let error_store = store.clone();
        // Detach the complete refresh transaction from request cancellation.
        // The OS lock stays owned by this task until Eggpass's atomic save has
        // completed, so another process cannot reuse a rotating token.
        tokio::spawn(async move {
            let _lock = lock;
            TokenManager::new(client, store)
                .access_token()
                .await
                .map(SecretString::from)
                .map_err(|error| {
                    map_access_error(error, error_store.invalid_load_requires_reauthentication())
                })
        })
        .await
        .map_err(|_| OAuthAccountError::RefreshFailed)?
    }

    /// Atomically replace the complete credential set for one owner/account.
    /// Login surfaces call this only after authenticating the owner and
    /// establishing the existing personal-upstream binding.
    pub async fn commit_credentials_for_user(
        &self,
        user_id: &str,
        provider_id: &str,
        account_name: &str,
        credentials: OAuthCredentials,
    ) -> Result<(), OAuthAccountError> {
        if !self.enabled {
            return Err(OAuthAccountError::Disabled);
        }
        let identity = self.identity(user_id, provider_id, account_name)?;
        let client = self.client_for_account(provider_id, account_name)?;
        if credentials.issuer() != client.provider().issuer() {
            return Err(OAuthAccountError::InvalidCredentials);
        }
        let lock = self.lock(&identity).await?;
        let store = FileCredentialStore::new(
            self.credential_root.clone(),
            identity,
            Some(client.provider().issuer().to_owned()),
        );
        // Keep the account lock alive if a caller disconnects during commit.
        tokio::spawn(async move {
            let _lock = lock;
            TokenManager::new(client, store)
                .save(credentials)
                .await
                .map_err(map_eggpass_error)
        })
        .await
        .map_err(|_| OAuthAccountError::Storage)?
    }

    /// Return only the issued OpenAI client ID for this verified owner/account.
    /// Access, refresh, and ID tokens remain inside the credential store.
    pub async fn reauthorization_client_id_for_user(
        &self,
        user_id: &str,
        provider_id: &str,
        account_name: &str,
    ) -> Result<Option<String>, OAuthAccountError> {
        if !self.enabled {
            return Err(OAuthAccountError::Disabled);
        }
        if provider_id != "openai" {
            return Err(OAuthAccountError::UnsupportedProvider);
        }
        let identity = self.identity(user_id, provider_id, account_name)?;
        let client = self.client_for_account(provider_id, account_name)?;
        let lock = self.lock(&identity).await?;
        let store = FileCredentialStore::new(
            self.credential_root.clone(),
            identity,
            Some(client.provider().issuer().to_owned()),
        );
        tokio::spawn(async move {
            let _lock = lock;
            let loaded = tokio::task::spawn_blocking(move || store.load_sync())
                .await
                .map_err(|_| OAuthAccountError::Storage)?;
            match loaded {
                Ok(Some(credentials)) => {
                    let client_id = credentials.client_id();
                    if client_id.is_empty()
                        || client_id.len() > 1024
                        || client_id.chars().any(char::is_control)
                    {
                        return Err(OAuthAccountError::InvalidCredentials);
                    }
                    Ok(Some(client_id.to_owned()))
                }
                Ok(None) => Ok(None),
                Err(error) if error.kind() == io::ErrorKind::InvalidData => {
                    Err(OAuthAccountError::InvalidCredentials)
                }
                Err(_) => Err(OAuthAccountError::Storage),
            }
        })
        .await
        .map_err(|_| OAuthAccountError::Storage)?
    }

    /// Remove only this owner's credential file. Explicit logout/removal stays
    /// available while operator OAuth is disabled; disabling alone never calls
    /// this method and retains all stored bytes.
    pub async fn clear_credentials_for_user(
        &self,
        user_id: &str,
        provider_id: &str,
        account_name: &str,
    ) -> Result<(), OAuthAccountError> {
        let identity = self.identity(user_id, provider_id, account_name)?;
        let lock = self.lock(&identity).await?;
        let store = FileCredentialStore::new(self.credential_root.clone(), identity, None);
        tokio::spawn(async move {
            let _lock = lock;
            tokio::task::spawn_blocking(move || store.clear_sync())
                .await
                .map_err(|_| OAuthAccountError::Storage)?
                .map_err(|_| OAuthAccountError::Storage)
        })
        .await
        .map_err(|_| OAuthAccountError::Storage)?
    }

    fn identity(
        &self,
        user_id: &str,
        provider_id: &str,
        account_name: &str,
    ) -> Result<CredentialIdentity, OAuthAccountError> {
        if !self
            .configured_accounts
            .contains(&(provider_id.to_owned(), account_name.to_owned()))
        {
            return Err(OAuthAccountError::AccountNotConfigured);
        }
        if !matches!(provider_id, "openai" | "xai") {
            return Err(OAuthAccountError::UnsupportedProvider);
        }
        if !self
            .personal_bindings
            .is_active_owner(provider_id, account_name, user_id)
        {
            return Err(OAuthAccountError::InvalidOwner);
        }
        CredentialIdentity::new(user_id, provider_id, account_name)
    }

    fn client_for_account(
        &self,
        provider_id: &str,
        account_name: &str,
    ) -> Result<OAuthClient, OAuthAccountError> {
        let profile = oauth_provider_profile(provider_id)?;
        let client = self
            .clients
            .get_client(provider_id, Some(account_name))
            .map_err(|_| OAuthAccountError::ClientUnavailable)?;
        Ok(OAuthClient::new(client.oauth_eggfetch_client(), profile))
    }

    async fn lock(
        &self,
        identity: &CredentialIdentity,
    ) -> Result<OAuthCredentialLocks, OAuthAccountError> {
        let root = self.credential_root.clone();
        let lock_path = root.join(identity.file_name().replace(".json", ".lock"));
        tokio::task::spawn_blocking(move || {
            // Lock order is always stable-root first, then account-specific.
            let restore = lock_oauth_credential_root(&root)?;
            ensure_private_directory_locked(&root)?;
            let mut options = OpenOptions::new();
            options.read(true).write(true).create(true).truncate(false);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let account = options
                .open(lock_path)
                .map_err(|_| OAuthAccountError::Storage)?;
            set_private_file(&account).map_err(|_| OAuthAccountError::Storage)?;
            account.lock().map_err(|_| OAuthAccountError::Storage)?;
            Ok(OAuthCredentialLocks {
                _restore: restore,
                _account: account,
            })
        })
        .await
        .map_err(|_| OAuthAccountError::Storage)?
    }
}

/// Store root for a service configured with an explicit config path. OAuth
/// files are deliberately config-adjacent so the supported backup service can
/// include them without placing them in SQLite.
pub(crate) fn credential_root_for_config(config_path: Option<&Path>) -> PathBuf {
    config_path
        .and_then(Path::parent)
        .map(|parent| parent.join(".eggpool-oauth"))
        .unwrap_or_else(|| {
            crate::operations::paths::RuntimePaths::resolve()
                .data_dir
                .join("oauth")
        })
}

fn oauth_provider_profile(provider_id: &str) -> Result<OAuthProviderConfig, OAuthAccountError> {
    match provider_id {
        "openai" => Ok(OAuthProviderConfig::openai()),
        "xai" => Ok(OAuthProviderConfig::xai()),
        _ => Err(OAuthAccountError::UnsupportedProvider),
    }
}

fn map_access_error(
    error: eggpass_auth::Error,
    invalid_credential_file: bool,
) -> OAuthAccountError {
    if invalid_credential_file && matches!(&error, eggpass_auth::Error::Storage(_)) {
        OAuthAccountError::NeedsReauthentication
    } else {
        map_eggpass_error(error)
    }
}

fn map_eggpass_error(error: eggpass_auth::Error) -> OAuthAccountError {
    match error {
        eggpass_auth::Error::NotLoggedIn | eggpass_auth::Error::NoRefreshToken => {
            OAuthAccountError::NeedsReauthentication
        }
        eggpass_auth::Error::Storage(_) => OAuthAccountError::Storage,
        _ => OAuthAccountError::RefreshFailed,
    }
}

#[derive(Clone)]
struct FileCredentialStore {
    root: PathBuf,
    identity: CredentialIdentity,
    expected_issuer: Option<String>,
    invalid_credential_file: Arc<AtomicBool>,
}

impl FileCredentialStore {
    fn new(root: PathBuf, identity: CredentialIdentity, expected_issuer: Option<String>) -> Self {
        Self {
            root,
            identity,
            expected_issuer,
            invalid_credential_file: Arc::new(AtomicBool::new(false)),
        }
    }

    fn invalid_load_requires_reauthentication(&self) -> bool {
        self.invalid_credential_file.load(Ordering::Acquire)
    }

    fn file_path(&self) -> PathBuf {
        self.root.join(self.identity.file_name())
    }

    fn load_sync(&self) -> io::Result<Option<Credentials>> {
        let path = self.file_path();
        let before = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if !before.file_type().is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid credential file",
            ));
        }
        verify_private_metadata(&before)?;
        if before.len() > MAX_CREDENTIAL_FILE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "credential file too large",
            ));
        }
        let file = File::open(path)?;
        let opened = file.metadata()?;
        if !same_file(&before, &opened) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "credential file changed",
            ));
        }
        verify_private_metadata(&opened)?;
        let mut bytes = Zeroizing::new(Vec::with_capacity(before.len() as usize));
        file.take(MAX_CREDENTIAL_FILE_BYTES + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_CREDENTIAL_FILE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "credential file too large",
            ));
        }
        let record: CredentialFile = serde_json::from_slice(&bytes)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid credential file"))?;
        if record.format_version != CREDENTIAL_FILE_VERSION
            || record.user_id != self.identity.user_id
            || record.provider_id != self.identity.provider_id
            || record.account_name != self.identity.account_name
            || self
                .expected_issuer
                .as_deref()
                .is_some_and(|issuer| record.credentials.issuer() != issuer)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "credential owner mismatch",
            ));
        }
        Ok(Some(record.credentials))
    }

    fn save_sync(&self, credentials: Credentials) -> io::Result<()> {
        ensure_private_directory_locked(&self.root).map_err(storage_io_error)?;
        let path = self.file_path();
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if !metadata.file_type().is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid credential file",
                ));
            }
            verify_private_metadata(&metadata)?;
        }
        let record = CredentialFile {
            format_version: CREDENTIAL_FILE_VERSION,
            user_id: self.identity.user_id.clone(),
            provider_id: self.identity.provider_id.clone(),
            account_name: self.identity.account_name.clone(),
            credentials,
        };
        let bytes =
            Zeroizing::new(serde_json::to_vec(&record).map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid credential set")
            })?);
        if bytes.len() as u64 > MAX_CREDENTIAL_FILE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "credential file too large",
            ));
        }
        write_atomic_private(&path, &bytes)
    }

    fn clear_sync(&self) -> io::Result<()> {
        let path = self.file_path();
        match fs::symlink_metadata(&path) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
            Ok(metadata) => {
                if !metadata.file_type().is_file() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "invalid credential file",
                    ));
                }
                verify_private_metadata(&metadata)?;
            }
        }
        fs::remove_file(path)?;
        sync_parent(&self.root)
    }
}

impl EggpassCredentialStore for FileCredentialStore {
    fn load(&self) -> impl Future<Output = Result<Option<Credentials>, StorageError>> + Send {
        let store = self.clone();
        let invalid_credential_file = Arc::clone(&self.invalid_credential_file);
        async move {
            let result = tokio::task::spawn_blocking(move || store.load_sync())
                .await
                .map_err(|_| StorageError::new("credential storage worker failed"))?;
            if result
                .as_ref()
                .is_err_and(|error| error.kind() == io::ErrorKind::InvalidData)
            {
                invalid_credential_file.store(true, Ordering::Release);
            }
            result.map_err(|_| StorageError::new("credential file is unavailable or invalid"))
        }
    }

    fn save(
        &self,
        credentials: Credentials,
    ) -> impl Future<Output = Result<(), StorageError>> + Send {
        let store = self.clone();
        async move {
            tokio::task::spawn_blocking(move || store.save_sync(credentials))
                .await
                .map_err(|_| StorageError::new("credential storage worker failed"))?
                .map_err(|_| StorageError::new("credential file could not be committed"))
        }
    }

    fn clear(&self) -> impl Future<Output = Result<(), StorageError>> + Send {
        let store = self.clone();
        async move {
            tokio::task::spawn_blocking(move || store.clear_sync())
                .await
                .map_err(|_| StorageError::new("credential storage worker failed"))?
                .map_err(|_| StorageError::new("credential file could not be removed"))
        }
    }
}

fn recover_interrupted_oauth_restore(path: &Path) -> io::Result<()> {
    let parent = path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "credential directory has no parent",
        )
    })?;
    match fs::symlink_metadata(path) {
        Ok(_) => return Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let target_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid credential directory name",
            )
        })?;
    let rollback_prefix = format!("{target_name}-restore-old-");
    let entries = match fs::read_dir(parent) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let mut rollback = None;
    for entry in entries {
        let entry = entry?;
        if entry
            .file_name()
            .to_str()
            .is_some_and(|name| name.starts_with(&rollback_prefix))
            && rollback.replace(entry.path()).is_some()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "ambiguous OAuth restore recovery state",
            ));
        }
    }
    let Some(rollback) = rollback else {
        return Ok(());
    };
    let metadata = fs::symlink_metadata(&rollback)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid OAuth restore recovery directory",
        ));
    }
    verify_private_metadata(&metadata)?;
    match fs::rename(&rollback, path) {
        Ok(()) => sync_parent(parent),
        Err(error) if path.exists() && error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn lock_oauth_credential_root(path: &Path) -> Result<File, OAuthAccountError> {
    let parent = path.parent().ok_or(OAuthAccountError::Storage)?;
    crate::operations::backup::acquire_oauth_restore_lock(parent)
        .map_err(|_| OAuthAccountError::Storage)
}

/// Recovery core for callers that already own the stable sibling restore lock.
pub(crate) fn recover_interrupted_restore_directory_locked(
    path: &Path,
) -> Result<(), OAuthAccountError> {
    recover_interrupted_oauth_restore(path).map_err(|_| OAuthAccountError::Storage)
}

#[cfg(test)]
fn ensure_private_directory(path: &Path) -> Result<(), OAuthAccountError> {
    let _restore_lock = lock_oauth_credential_root(path)?;
    ensure_private_directory_locked(path)
}

/// Directory setup/recovery core; callers must hold the stable restore lock.
fn ensure_private_directory_locked(path: &Path) -> Result<(), OAuthAccountError> {
    recover_interrupted_restore_directory_locked(path)?;
    fs::create_dir_all(path).map_err(|_| OAuthAccountError::Storage)?;
    let metadata = fs::symlink_metadata(path).map_err(|_| OAuthAccountError::Storage)?;
    if !metadata.file_type().is_dir() {
        return Err(OAuthAccountError::Storage);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if metadata.uid() != nix::unistd::geteuid().as_raw() {
            return Err(OAuthAccountError::Storage);
        }
        if metadata.permissions().mode() & 0o077 != 0 {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))
                .map_err(|_| OAuthAccountError::Storage)?;
        }
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        return Err(OAuthAccountError::Storage);
    }
    Ok(())
}

fn set_private_file(file: &File) -> io::Result<()> {
    let metadata = file.metadata()?;
    verify_owner(&metadata)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
    }
    Ok(())
}

fn verify_private_metadata(metadata: &fs::Metadata) -> io::Result<()> {
    verify_owner(metadata)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "credential file is not private",
            ));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn verify_owner(metadata: &fs::Metadata) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    if metadata.uid() == nix::unistd::geteuid().as_raw() {
        Ok(())
    } else {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "credential file owner mismatch",
        ))
    }
}

#[cfg(not(unix))]
fn verify_owner(_metadata: &fs::Metadata) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "private OAuth storage requires Unix permissions",
    ))
}

#[cfg(unix)]
fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(not(unix))]
fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.len() == right.len() && left.modified().ok() == right.modified().ok()
}

fn write_atomic_private(path: &Path, bytes: &[u8]) -> io::Result<()> {
    write_atomic_private_with_sync_locked(path, bytes, sync_parent)
}

fn write_atomic_private_with_sync_locked<F>(
    path: &Path,
    bytes: &[u8],
    mut sync_directory: F,
) -> io::Result<()>
where
    F: FnMut(&Path) -> io::Result<()>,
{
    let parent = path.parent().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "credential path has no parent")
    })?;
    ensure_private_directory_locked(parent).map_err(storage_io_error)?;
    for _ in 0..16 {
        let sequence = TEMP_FILE_COUNTER.fetch_add(1, Ordering::Relaxed);
        let temp = parent.join(format!(".oauth-{}-{sequence}.tmp", std::process::id()));
        let rollback = parent.join(format!(".oauth-{}-{sequence}.rollback", std::process::id()));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = match options.open(&temp) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        };
        let stage_result = (|| {
            set_private_file(&file)?;
            file.write_all(bytes)?;
            file.sync_all()?;
            drop(file);

            let previous = match fs::symlink_metadata(path) {
                Ok(metadata) => {
                    if !metadata.file_type().is_file() {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "invalid credential file",
                        ));
                    }
                    verify_private_metadata(&metadata)?;
                    // Keep the old inode available until the new directory
                    // entry is synced, so a failed sync can be rolled back.
                    fs::hard_link(path, &rollback)?;
                    let linked = fs::symlink_metadata(&rollback)?;
                    if !same_file(&metadata, &linked) {
                        let _ = fs::remove_file(&rollback);
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "credential file changed",
                        ));
                    }
                    Some(rollback.as_path())
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                Err(error) => return Err(error),
            };

            if previous.is_some()
                && let Err(error) = sync_directory(parent)
            {
                let _ = fs::remove_file(&rollback);
                return Err(error);
            }
            if let Err(error) = fs::rename(&temp, path) {
                if previous.is_some() {
                    let _ = fs::remove_file(&rollback);
                }
                return Err(error);
            }
            if let Err(publish_error) = sync_directory(parent) {
                let rollback_result = match previous {
                    Some(previous) => fs::rename(previous, path),
                    None => fs::remove_file(path),
                };
                if rollback_result.is_err() || sync_directory(parent).is_err() {
                    return Err(io::Error::other("credential publication rollback failed"));
                }
                return Err(publish_error);
            }
            if previous.is_some() && fs::remove_file(&rollback).is_ok() {
                // Publication is already durable. A cleanup sync failure
                // must not turn the completed save into a reported failure.
                let _ = sync_directory(parent);
            }
            Ok(())
        })();
        if stage_result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        return stage_result;
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "credential staging collision",
    ))
}

fn sync_parent(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

fn storage_io_error(_error: OAuthAccountError) -> io::Error {
    io::Error::new(
        io::ErrorKind::PermissionDenied,
        "private credential directory unavailable",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{BufRead, BufReader},
        process::{Command, Stdio},
        sync::mpsc,
        thread,
        time::{Duration, SystemTime, UNIX_EPOCH},
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let suffix = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            let path =
                std::env::temp_dir().join(format!("eggpool-oauth-{}-{suffix}", std::process::id()));
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

    #[cfg(unix)]
    #[test]
    fn interrupted_restore_recovers_the_previous_oauth_directory() {
        use std::os::unix::fs::PermissionsExt;

        let directory = TestDirectory::new();
        let target = directory.path().join(".eggpool-oauth");
        let rollback = directory.path().join(".eggpool-oauth-restore-old-123-1");
        fs::create_dir(&rollback).unwrap();
        fs::set_permissions(&rollback, fs::Permissions::from_mode(0o700)).unwrap();
        let credential = rollback.join("credential.json");
        fs::write(&credential, b"previous-credential").unwrap();
        set_private_file(&File::open(&credential).unwrap()).unwrap();

        ensure_private_directory(&target).unwrap();

        assert_eq!(
            fs::read(target.join("credential.json")).unwrap(),
            b"previous-credential"
        );
        assert!(!rollback.exists());
    }

    #[cfg(unix)]
    #[test]
    fn ambiguous_interrupted_restores_fail_closed_without_deleting_either_copy() {
        use std::os::unix::fs::PermissionsExt;

        let directory = TestDirectory::new();
        let target = directory.path().join(".eggpool-oauth");
        let first = directory.path().join(".eggpool-oauth-restore-old-123-1");
        let second = directory.path().join(".eggpool-oauth-restore-old-124-1");
        for path in [&first, &second] {
            fs::create_dir(path).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
        }

        assert!(matches!(
            ensure_private_directory(&target),
            Err(OAuthAccountError::Storage)
        ));
        assert!(!target.exists());
        assert!(first.exists());
        assert!(second.exists());
    }

    #[cfg(unix)]
    #[test]
    fn failed_directory_sync_rolls_back_the_published_credential() {
        use std::os::unix::fs::PermissionsExt;

        let directory = TestDirectory::new();
        let path = directory.path().join("credential.json");
        fs::write(&path, b"old-credential").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let mut sync_calls = 0;

        let result = write_atomic_private_with_sync_locked(&path, b"new-credential", |_| {
            sync_calls += 1;
            if sync_calls == 2 {
                Err(io::Error::other("injected directory sync failure"))
            } else {
                Ok(())
            }
        });

        assert!(result.is_err());
        assert_eq!(fs::read(&path).unwrap(), b"old-credential");
        assert_eq!(sync_calls, 3);
        assert_eq!(
            fs::read_dir(directory.path()).unwrap().count(),
            1,
            "rollback links and temporary files are removed after recovery"
        );
    }

    fn credentials() -> Credentials {
        serde_json::from_str(
            r#"{
                "access_token":"access-secret",
                "refresh_token":"refresh-secret",
                "id_token":"id-secret",
                "token_type":"Bearer",
                "scope":"openid offline_access",
                "client_id":"test-client",
                "issuer":"https://auth.x.ai",
                "expires_at":4102444800,
                "obtained_at":1
            }"#,
        )
        .unwrap()
    }

    fn expired_credentials(issuer: &str) -> Credentials {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() as i64;
        serde_json::from_value(serde_json::json!({
            "access_token": "expired-access-secret",
            "refresh_token": "old-refresh-secret",
            "id_token": "old-id-secret",
            "token_type": "Bearer",
            "scope": "openid offline_access",
            "client_id": "test-client",
            "issuer": issuer,
            "expires_at": now - 60,
            "obtained_at": now - 3600
        }))
        .unwrap()
    }

    fn manager_for_test(root: PathBuf, base_url: &str, enabled: bool) -> OAuthAccountManager {
        let mut config = Config::default();
        config.oauth.enabled = enabled;
        config.providers.insert(
            "xai".to_owned(),
            crate::config::ProviderConfig {
                id: "xai".to_owned(),
                base_url: base_url.to_owned(),
                auth: crate::config::ProviderAuthConfig {
                    mode: "none".to_owned(),
                    ..Default::default()
                },
                accounts: vec![crate::config::AccountConfig {
                    name: "user-account".to_owned(),
                    auth_mode: AccountAuthMode::OAuth,
                    ..Default::default()
                }],
                ..Default::default()
            },
        );
        let clients = crate::providers::ProviderClientPool::from_config(&config)
            .expect("local OAuth provider client pool");
        OAuthAccountManager::new(&config, root, clients, PersonalUpstreamSnapshot::default())
    }

    fn local_profile(base_url: &str) -> OAuthProviderConfig {
        OAuthProviderConfig::custom("Local OAuth test", base_url, "test-client")
            .authorization_endpoint(format!("{base_url}/authorize"))
            .token_endpoint(format!("{base_url}/token"))
            .scopes(["offline_access"])
            .build()
            .expect("local OAuth profile")
    }

    async fn start_token_stub(
        status: u16,
        response_body: &'static str,
    ) -> (String, tokio::task::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("OAuth stub listener");
        let base_url = format!(
            "http://{}",
            listener.local_addr().expect("OAuth stub address")
        );
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("OAuth refresh connection");
            let mut request = Vec::new();
            let mut chunk = [0_u8; 1024];
            let mut expected_len = None;
            loop {
                let read = stream.read(&mut chunk).await.expect("OAuth request read");
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..read]);
                if expected_len.is_none()
                    && let Some(headers_end) =
                        request.windows(4).position(|part| part == b"\r\n\r\n")
                {
                    let headers = String::from_utf8_lossy(&request[..headers_end]);
                    let body_len = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    expected_len = Some(headers_end + 4 + body_len);
                }
                if expected_len.is_some_and(|length| request.len() >= length) {
                    break;
                }
            }
            let request = String::from_utf8_lossy(&request).into_owned();
            let reason = if status == 200 { "OK" } else { "Unauthorized" };
            let response = format!(
                "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response_body}",
                response_body.len()
            );
            stream
                .write_all(response.as_bytes())
                .await
                .expect("OAuth stub response");
            request
        });
        (base_url, task)
    }

    fn identity(user_id: &str) -> CredentialIdentity {
        CredentialIdentity::new(user_id, "xai", "user-account").unwrap()
    }

    #[tokio::test]
    async fn expired_credentials_rotate_atomically_and_restart_with_new_token() {
        let directory = TestDirectory::new();
        let (base_url, server) = start_token_stub(
            200,
            r#"{"access_token":"rotated-access-secret","refresh_token":"rotated-refresh-secret","id_token":"rotated-id-secret","token_type":"Bearer","expires_in":3600,"scope":"offline_access"}"#,
        )
        .await;
        let manager = manager_for_test(directory.path().to_owned(), &base_url, true);
        let owner = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            owner.clone(),
            Some(base_url.clone()),
        );
        store.save_sync(expired_credentials(&base_url)).unwrap();
        let profile = local_profile(&base_url);
        let client_a = manager
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let client_b = manager
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let first = manager.access_token_for_identity(
            owner.clone(),
            OAuthClient::new(client_a.oauth_eggfetch_client(), profile.clone()),
        );
        let second = manager.access_token_for_identity(
            owner.clone(),
            OAuthClient::new(client_b.oauth_eggfetch_client(), profile.clone()),
        );
        let (first, second) = tokio::join!(first, second);
        let first = first.expect("first refresh");
        let second = second.expect("single-flight refresh");
        assert_eq!(first.expose_secret(), "rotated-access-secret");
        assert_eq!(second.expose_secret(), "rotated-access-secret");
        assert!(!format!("{first:?}").contains("rotated-access-secret"));

        let request = server.await.expect("OAuth stub task");
        assert!(request.starts_with("POST /token "));
        assert!(request.contains("refresh_token=old-refresh-secret"));
        let rotated = store.load_sync().unwrap().expect("rotated credentials");
        assert_eq!(rotated.access_token(), "rotated-access-secret");
        assert_eq!(rotated.refresh_token(), Some("rotated-refresh-secret"));
        assert_eq!(rotated.id_token(), Some("rotated-id-secret"));
        let committed_bytes = fs::read(store.file_path()).unwrap();

        // A new generation observes the complete committed token set and does
        // not contact the now-closed local token endpoint again.
        drop(manager);
        let restarted = manager_for_test(directory.path().to_owned(), &base_url, true);
        let restarted_client = restarted
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let cached = restarted
            .access_token_for_identity(
                owner,
                OAuthClient::new(restarted_client.oauth_eggfetch_client(), profile),
            )
            .await
            .expect("restart loads rotated credentials");
        assert_eq!(cached.expose_secret(), "rotated-access-secret");
        assert_eq!(fs::read(store.file_path()).unwrap(), committed_bytes);
    }

    #[tokio::test]
    async fn refresh_denial_leaves_previous_credentials_and_redacts_provider_body() {
        let directory = TestDirectory::new();
        let (base_url, server) = start_token_stub(
            401,
            r#"{"error":"invalid_grant","error_description":"provider-response-secret"}"#,
        )
        .await;
        let manager = manager_for_test(directory.path().to_owned(), &base_url, true);
        let owner = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            owner.clone(),
            Some(base_url.clone()),
        );
        store.save_sync(expired_credentials(&base_url)).unwrap();
        let before = fs::read(store.file_path()).unwrap();
        let client = manager
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let error = manager
            .access_token_for_identity(
                owner,
                OAuthClient::new(client.oauth_eggfetch_client(), local_profile(&base_url)),
            )
            .await
            .expect_err("denied refresh must fail");
        assert_eq!(error, OAuthAccountError::RefreshFailed);
        assert!(!format!("{error:?}").contains("provider-response-secret"));
        assert!(server.await.unwrap().contains("old-refresh-secret"));
        assert_eq!(fs::read(store.file_path()).unwrap(), before);
    }

    #[tokio::test]
    async fn refresh_without_new_refresh_token_preserves_the_current_refresh_token() {
        let directory = TestDirectory::new();
        let (base_url, server) = start_token_stub(
            200,
            r#"{"access_token":"fresh-access-secret","token_type":"Bearer","expires_in":3600,"scope":"offline_access"}"#,
        )
        .await;
        let manager = manager_for_test(directory.path().to_owned(), &base_url, true);
        let owner = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            owner.clone(),
            Some(base_url.clone()),
        );
        store.save_sync(expired_credentials(&base_url)).unwrap();
        let client = manager
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let access = manager
            .access_token_for_identity(
                owner,
                OAuthClient::new(client.oauth_eggfetch_client(), local_profile(&base_url)),
            )
            .await
            .expect("refresh without token rotation");
        assert_eq!(access.expose_secret(), "fresh-access-secret");
        assert!(server.await.unwrap().contains("old-refresh-secret"));
        let saved = store.load_sync().unwrap().expect("saved credentials");
        assert_eq!(saved.refresh_token(), Some("old-refresh-secret"));
        assert_eq!(saved.access_token(), "fresh-access-secret");
    }

    #[tokio::test]
    async fn refresh_transport_failure_keeps_previous_credentials() {
        let directory = TestDirectory::new();
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("closed OAuth endpoint");
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        drop(listener);
        let manager = manager_for_test(directory.path().to_owned(), &base_url, true);
        let owner = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            owner.clone(),
            Some(base_url.clone()),
        );
        store.save_sync(expired_credentials(&base_url)).unwrap();
        let before = fs::read(store.file_path()).unwrap();
        let client = manager
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let error = manager
            .access_token_for_identity(
                owner,
                OAuthClient::new(client.oauth_eggfetch_client(), local_profile(&base_url)),
            )
            .await
            .expect_err("unreachable token endpoint must fail closed");
        assert_eq!(error, OAuthAccountError::RefreshFailed);
        assert_eq!(fs::read(store.file_path()).unwrap(), before);
    }

    #[tokio::test]
    async fn canceled_refresh_finishes_atomic_commit_before_releasing_lock() {
        let directory = TestDirectory::new();
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("OAuth cancellation stub");
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let (request_sender, request_receiver) = tokio::sync::oneshot::channel();
        let (resume_sender, resume_receiver) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("OAuth refresh connection");
            let mut request = Vec::new();
            let mut chunk = [0_u8; 1024];
            let mut expected_len = None;
            loop {
                let read = stream.read(&mut chunk).await.expect("OAuth request read");
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..read]);
                if expected_len.is_none()
                    && let Some(headers_end) =
                        request.windows(4).position(|part| part == b"\r\n\r\n")
                {
                    let headers = String::from_utf8_lossy(&request[..headers_end]);
                    let body_len = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    expected_len = Some(headers_end + 4 + body_len);
                }
                if expected_len.is_some_and(|length| request.len() >= length) {
                    break;
                }
            }
            request_sender
                .send(String::from_utf8_lossy(&request).into_owned())
                .expect("refresh request observer");
            resume_receiver.await.expect("resume OAuth response");
            let body = r#"{"access_token":"canceled-caller-rotated-access","refresh_token":"canceled-caller-rotated-refresh","token_type":"Bearer","expires_in":3600}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .await
                .expect("OAuth cancellation response");
        });
        let manager = manager_for_test(directory.path().to_owned(), &base_url, true);
        let owner = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            owner.clone(),
            Some(base_url.clone()),
        );
        store.save_sync(expired_credentials(&base_url)).unwrap();
        let client = manager
            .clients
            .get_client("xai", Some("user-account"))
            .unwrap();
        let caller_manager = manager.clone();
        let caller_owner = owner.clone();
        let caller = tokio::spawn(async move {
            caller_manager
                .access_token_for_identity(
                    caller_owner,
                    OAuthClient::new(client.oauth_eggfetch_client(), local_profile(&base_url)),
                )
                .await
        });
        let request = tokio::time::timeout(Duration::from_secs(10), request_receiver)
            .await
            .expect("refresh request observation timed out")
            .expect("refresh reached stub");
        assert!(request.contains("refresh_token=old-refresh-secret"));
        caller.abort();
        let _ = caller.await;
        resume_sender.send(()).expect("release OAuth response");
        tokio::time::timeout(Duration::from_secs(10), server)
            .await
            .expect("OAuth cancellation fixture timed out")
            .expect("OAuth cancellation fixture");

        // Taking the same OS lock is the observable commit boundary: the
        // detached refresh transaction releases it only after atomic save.
        // Try-lock polling keeps the synchronization bounded without leaving a
        // blocking worker stuck if the detached refresh fails to release it.
        let lock_path = directory
            .path()
            .join(owner.file_name().replace(".json", ".lock"));
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .open(lock_path)
            .expect("OAuth lock file");
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                match lock.try_lock() {
                    Ok(()) => break,
                    Err(std::fs::TryLockError::WouldBlock) => {
                        tokio::task::yield_now().await;
                    }
                    Err(std::fs::TryLockError::Error(error)) => {
                        panic!("OAuth refresh lock wait failed: {error}");
                    }
                }
            }
        })
        .await
        .expect("timed out waiting for detached refresh commit");
        let committed = store.load_sync().unwrap().expect("detached refresh saved");
        assert_eq!(committed.access_token(), "canceled-caller-rotated-access");
        assert_eq!(
            committed.refresh_token(),
            Some("canceled-caller-rotated-refresh")
        );
    }

    #[tokio::test]
    async fn disabled_oauth_rejects_refresh_without_changing_stored_credentials() {
        let directory = TestDirectory::new();
        let manager = manager_for_test(directory.path().to_owned(), "http://127.0.0.1:1", false);
        let owner = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            owner,
            Some("https://auth.x.ai".to_owned()),
        );
        store.save_sync(credentials()).unwrap();
        let before = fs::read(store.file_path()).unwrap();
        let error = manager
            .access_token_for_user("user-a", "xai", "user-account")
            .await
            .expect_err("disabled gate rejects token use");
        assert_eq!(error, OAuthAccountError::Disabled);
        assert_eq!(fs::read(store.file_path()).unwrap(), before);
    }

    #[tokio::test]
    async fn corrupted_credentials_require_reauthentication() {
        let directory = TestDirectory::new();
        let identity = identity("user-a");
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            identity,
            Some("https://auth.x.ai".to_owned()),
        );
        ensure_private_directory(directory.path()).unwrap();
        fs::write(store.file_path(), b"not-a-credential-record").unwrap();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(store.file_path())
            .unwrap();
        set_private_file(&file).unwrap();

        assert!(EggpassCredentialStore::load(&store).await.is_err());
        assert!(store.invalid_load_requires_reauthentication());
        assert_eq!(
            map_access_error(
                eggpass_auth::Error::Storage(StorageError::new("invalid file")),
                store.invalid_load_requires_reauthentication(),
            ),
            OAuthAccountError::NeedsReauthentication
        );
        assert_eq!(
            map_access_error(
                eggpass_auth::Error::Storage(StorageError::new("unavailable")),
                false,
            ),
            OAuthAccountError::Storage
        );
    }

    #[test]
    fn credentials_are_private_atomic_and_owner_scoped() {
        let directory = TestDirectory::new();
        let store = FileCredentialStore::new(
            directory.path().to_owned(),
            identity("user-a"),
            Some("https://auth.x.ai".to_owned()),
        );
        store.save_sync(credentials()).unwrap();
        let path = store.file_path();
        let metadata = fs::metadata(&path).unwrap();
        verify_private_metadata(&metadata).unwrap();
        assert_eq!(
            store.load_sync().unwrap().unwrap().access_token(),
            "access-secret"
        );

        let copied = directory.path().join(identity("user-b").file_name());
        fs::copy(path, copied).unwrap();
        let other_owner = FileCredentialStore::new(
            directory.path().to_owned(),
            identity("user-b"),
            Some("https://auth.x.ai".to_owned()),
        );
        assert!(other_owner.load_sync().is_err());

        let debug = format!("{:?}", store.load_sync().unwrap().unwrap());
        for secret in ["access-secret", "refresh-secret", "id-secret"] {
            assert!(!debug.contains(secret));
        }
        store.clear_sync().unwrap();
        assert!(store.load_sync().unwrap().is_none());
    }

    #[test]
    fn file_identity_is_bound_to_provider_and_account_as_well_as_user() {
        let original = CredentialIdentity::new("user", "xai", "account-a").unwrap();
        let other_provider = CredentialIdentity::new("user", "openai", "account-a").unwrap();
        let other_account = CredentialIdentity::new("user", "xai", "account-b").unwrap();
        assert_ne!(original.file_name(), other_provider.file_name());
        assert_ne!(original.file_name(), other_account.file_name());
        assert!(CredentialIdentity::new("", "xai", "account-a").is_err());
    }

    #[test]
    fn cross_process_lock_helper() {
        let Ok(path) = std::env::var("EGGPOOL_OAUTH_TEST_LOCK_PATH") else {
            return;
        };
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .unwrap();
        file.lock().unwrap();
        println!("OAUTH_LOCK_ACQUIRED");
        std::io::Write::flush(&mut std::io::stdout()).unwrap();
        let mut release = String::new();
        std::io::stdin().read_line(&mut release).unwrap();
    }

    #[test]
    fn identity_lock_is_exclusive_across_processes() {
        let directory = TestDirectory::new();
        let lock_path = directory.path().join("identity.lock");
        OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&lock_path)
            .unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "oauth::tests::cross_process_lock_helper",
                "--nocapture",
            ])
            .env("EGGPOOL_OAUTH_TEST_LOCK_PATH", &lock_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        let (sender, receiver) = mpsc::channel();
        thread::spawn(move || {
            for line in stdout.lines() {
                if sender.send(line.unwrap_or_default()).is_err() {
                    break;
                }
            }
        });
        loop {
            match receiver.recv_timeout(Duration::from_secs(10)) {
                Ok(line) if line.contains("OAUTH_LOCK_ACQUIRED") => break,
                Ok(_) => continue,
                Err(error) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    panic!("child did not acquire OAuth lock: {error}");
                }
            }
        }
        let contender = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        let exclusive = matches!(contender.try_lock(), Err(std::fs::TryLockError::WouldBlock));
        let mut child_stdin = child.stdin.take().unwrap();
        std::io::Write::write_all(&mut child_stdin, b"release\n").unwrap();
        let status = child.wait().unwrap();
        assert!(status.success(), "lock holder child exited unsuccessfully");
        assert!(exclusive, "another process acquired the same OAuth lock");
    }
}
