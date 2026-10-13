//! Local downstream-user identity and credential storage.
//!
//! Hashing is deliberately performed before entering SQLite's single gate.

use std::{
    collections::VecDeque,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};

use argon2::{
    Algorithm, Argon2, Params, PasswordHash, PasswordHasher, PasswordVerifier, Version,
    password_hash::SaltString,
};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::Semaphore;
use tokio_rusqlite::rusqlite::OptionalExtension;

use crate::db::Database;

const MAX_PASSWORD_BYTES: usize = 1024;
const MIN_PASSWORD_BYTES: usize = 12;
const HASH_CONCURRENCY: usize = 2;

static HASH_SLOTS: OnceLock<Semaphore> = OnceLock::new();
static FAILED_TOKEN_DIGESTS: OnceLock<Mutex<VecDeque<(String, Instant)>>> = OnceLock::new();
static DUMMY_PASSWORD_HASH: tokio::sync::OnceCell<String> = tokio::sync::OnceCell::const_new();
const DUMMY_PASSWORD: &str = "eggpool-invalid-login-dummy-password";
const FAILED_TOKEN_CAPACITY: usize = 256;
const FAILED_TOKEN_TTL: Duration = Duration::from_secs(1);

#[derive(Debug, Error)]
pub enum IdentityError {
    #[error("username must be 1 to 64 ASCII letters, digits, dots, underscores, or hyphens")]
    InvalidUsername,
    #[error("password must contain between 12 and 1024 bytes")]
    InvalidPassword,
    #[error("credential label must contain between 1 and 80 bytes")]
    InvalidCredentialLabel,
    #[error("an administrator is already initialized")]
    AdminAlreadyInitialized,
    #[error("identity access state cannot be moved to managed")]
    InvalidAccessTransition,
    #[error("user was not found")]
    UserNotFound,
    #[error("credential entropy unavailable")]
    Entropy,
    #[error("password hashing worker failed")]
    HashWorker,
    #[error("password hash is invalid")]
    InvalidHash,
    #[error(transparent)]
    Database(#[from] crate::db::DatabaseError),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserSummary {
    pub id: String,
    pub username: String,
    pub role: String,
    pub state: String,
    pub must_change_password: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserRequestSummary {
    pub proxy_request_id: Option<String>,
    pub model_id: String,
    pub provider_id: String,
    pub status: String,
    pub started_at: String,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_microdollars: i64,
}

/// Return a bounded, content-free request history for one user. Internal
/// selector children are excluded so downstream callers see logical requests.
pub async fn recent_user_requests(
    database: &Database,
    user_id: &str,
    limit: usize,
) -> Result<Vec<UserRequestSummary>, IdentityError> {
    let user_id = user_id.to_owned();
    let limit = limit.clamp(1, 500) as i64;
    database
        .call(move |connection| {
            let mut statement = connection.prepare(
                "SELECT proxy_request_id, model_id, provider_id, status, started_at,\
                        COALESCE(input_tokens, 0), COALESCE(output_tokens, 0),\
                        COALESCE(cost_microdollars, 0)\
                 FROM requests WHERE user_id = ?1 AND request_kind = 'logical'\
                 ORDER BY started_at DESC, id DESC LIMIT ?2",
            )?;
            let rows = statement.query_map((user_id, limit), |row| {
                Ok(UserRequestSummary {
                    proxy_request_id: row.get(0)?,
                    model_id: row.get(1)?,
                    provider_id: row.get(2)?,
                    status: row.get(3)?,
                    started_at: row.get(4)?,
                    input_tokens: row.get(5)?,
                    output_tokens: row.get(6)?,
                    cost_microdollars: row.get(7)?,
                })
            })?;
            rows.collect::<Result<Vec<_>, _>>()
        })
        .await
        .map_err(IdentityError::Database)
}

#[derive(Clone, PartialEq, Eq)]
pub struct IssuedCredential {
    pub user_id: String,
    pub token: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum PrincipalKind {
    AgentToken,
    BrowserSession,
    LegacySharedKey,
}

impl PrincipalKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AgentToken => "agent_token",
            Self::BrowserSession => "browser_session",
            Self::LegacySharedKey => "legacy_shared_key",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatedPrincipal {
    pub user_id: Option<String>,
    pub credential_id: Option<String>,
    pub role: Option<String>,
    pub scopes: Vec<String>,
    pub auth_epoch: Option<i64>,
    pub kind: PrincipalKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum AttributedRequestKind {
    Logical,
    InternalSelector,
}

impl AttributedRequestKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Logical => "logical",
            Self::InternalSelector => "internal_selector",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestAttribution {
    pub user_id: Option<String>,
    pub credential_id: Option<String>,
    pub auth_epoch: Option<i64>,
    pub principal_kind: PrincipalKind,
    pub request_kind: AttributedRequestKind,
    pub parent_proxy_request_id: Option<String>,
}

impl RequestAttribution {
    pub fn selector_child(&self, parent_proxy_request_id: &str) -> Self {
        Self {
            request_kind: AttributedRequestKind::InternalSelector,
            parent_proxy_request_id: Some(parent_proxy_request_id.to_owned()),
            ..self.clone()
        }
    }
}

impl AuthenticatedPrincipal {
    pub fn legacy_shared_key() -> Self {
        Self {
            user_id: None,
            credential_id: None,
            role: None,
            scopes: vec!["inference".to_owned()],
            auth_epoch: None,
            kind: PrincipalKind::LegacySharedKey,
        }
    }

    pub fn is_user_principal(&self) -> bool {
        self.user_id.is_some()
    }

    pub fn request_attribution(&self) -> RequestAttribution {
        RequestAttribution {
            user_id: self.user_id.clone(),
            credential_id: self.credential_id.clone(),
            auth_epoch: self.auth_epoch,
            principal_kind: self.kind,
            request_kind: AttributedRequestKind::Logical,
            parent_proxy_request_id: None,
        }
    }

    pub fn selector_attribution(&self, parent_proxy_request_id: &str) -> RequestAttribution {
        RequestAttribution {
            request_kind: AttributedRequestKind::InternalSelector,
            parent_proxy_request_id: Some(parent_proxy_request_id.to_owned()),
            ..self.request_attribution()
        }
    }
}

/// Create the sole initial administrator and an inference-only personal key
/// in one transaction. The password hash is computed before acquiring SQLite.
pub async fn bootstrap_admin(
    database: &Database,
    username: &str,
    password: &str,
) -> Result<(UserSummary, IssuedCredential), IdentityError> {
    let username = validate_username(username)?;
    let password_hash = hash_password(password).await?;
    let id = random_hex(16)?;
    let credential_id = random_hex(16)?;
    let selector = random_hex(8)?;
    let secret = random_hex(32)?;
    let token = format!("epk1_{selector}_{secret}");
    let digest = digest(&token);
    let username_for_db = username.clone();
    let id_for_db = id.clone();
    let selector_for_db = selector.clone();
    let password_hash_for_db = password_hash.clone();

    let transaction_result = database
        .with_transaction(move |connection| {
            let user_count: i64 = connection.query_row("SELECT COUNT(*) FROM users", [], |row| row.get(0))?;
            if user_count != 0 {
                return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows);
            }
            connection.execute(
                "INSERT INTO users(id, username, normalized_username, role, password_hash) VALUES (?1, ?2, ?3, 'admin', ?4)",
                (&id_for_db, &username_for_db, username_for_db.to_ascii_lowercase(), &password_hash_for_db),
            )?;
            connection.execute(
                "INSERT INTO user_credentials(id, user_id, label, selector, digest, scopes) VALUES (?1, ?2, 'initial admin client', ?3, ?4, 'inference,discovery,integrations')",
                (&credential_id, &id_for_db, &selector_for_db, &digest),
            )?;
            connection.execute(
                "UPDATE user_access_state SET state = 'admin_initialized', updated_at = CURRENT_TIMESTAMP WHERE singleton = 1",
                [],
            )?;
            Ok(())
        })
        .await;
    if let Err(error) = transaction_result {
        let initialized = database
            .call(|connection| {
                connection.query_row("SELECT EXISTS(SELECT 1 FROM users)", [], |row| {
                    row.get::<_, bool>(0)
                })
            })
            .await
            .unwrap_or(false);
        if initialized {
            return Err(IdentityError::AdminAlreadyInitialized);
        }
        return Err(IdentityError::Database(error));
    }

    Ok((
        UserSummary {
            id: id.clone(),
            username,
            role: "admin".to_owned(),
            state: "active".to_owned(),
            must_change_password: false,
        },
        IssuedCredential { user_id: id, token },
    ))
}

pub async fn list_users(database: &Database) -> Result<Vec<UserSummary>, IdentityError> {
    database
        .call(|connection| {
            let mut statement = connection.prepare(
                "SELECT id, username, role, state, must_change_password FROM users ORDER BY username COLLATE NOCASE",
            )?;
            let rows = statement.query_map([], |row| {
                Ok(UserSummary {
                    id: row.get(0)?,
                    username: row.get(1)?,
                    role: row.get(2)?,
                    state: row.get(3)?,
                    must_change_password: row.get::<_, i64>(4)? != 0,
                })
            })?;
            rows.collect()
        })
        .await
        .map_err(IdentityError::Database)
}

pub async fn access_state(database: &Database) -> Result<String, IdentityError> {
    database
        .call(|connection| {
            connection.query_row(
                "SELECT state FROM user_access_state WHERE singleton = 1",
                [],
                |row| row.get(0),
            )
        })
        .await
        .map_err(IdentityError::Database)
}

pub async fn enable_managed(database: &Database) -> Result<(), IdentityError> {
    let changed = database
        .with_transaction(|connection| {
            connection.execute(
                "UPDATE user_access_state SET state = 'managed', updated_at = CURRENT_TIMESTAMP \
                 WHERE singleton = 1 AND state = 'admin_initialized' \
                 AND EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND state = 'active')",
                [],
            )?;
            Ok(connection.changes())
        })
        .await?;
    if changed == 1 || access_state(database).await?.as_str() == "managed" {
        Ok(())
    } else {
        Err(IdentityError::InvalidAccessTransition)
    }
}

pub async fn mark_fresh_uninitialized(database: &Database) -> Result<(), IdentityError> {
    database
        .with_transaction(|connection| {
            connection.execute(
                "UPDATE user_access_state SET state = 'fresh_uninitialized', updated_at = CURRENT_TIMESTAMP WHERE singleton = 1 AND state = 'legacy_unmigrated' AND NOT EXISTS (SELECT 1 FROM users)",
                [],
            )?;
            Ok(())
        })
        .await
        .map_err(IdentityError::Database)
}

/// Replace a password and revoke all the user's client credentials. The
/// browser-session migration adds session revocation to this same boundary.
pub async fn reset_password(
    database: &Database,
    username: &str,
    password: &str,
) -> Result<(), IdentityError> {
    let normalized = validate_username(username)?.to_ascii_lowercase();
    let password_hash = hash_password(password).await?;
    let normalized_for_error = normalized.clone();
    let result = database
        .with_transaction(move |connection| {
            let changed = connection.execute(
                "UPDATE users SET password_hash = ?1, must_change_password = 0, auth_epoch = auth_epoch + 1, updated_at = CURRENT_TIMESTAMP WHERE normalized_username = ?2 AND state = 'active'",
                (&password_hash, &normalized),
            )?;
            if changed == 0 {
                return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows);
            }
            connection.execute(
                "UPDATE user_credentials SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = (SELECT id FROM users WHERE normalized_username = ?1) AND revoked_at IS NULL",
                [&normalized],
            )?;
            connection.execute(
                "UPDATE web_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = (SELECT id FROM users WHERE normalized_username = ?1) AND revoked_at IS NULL",
                [&normalized],
            )?;
            Ok(())
        })
        .await;
    match result {
        Ok(()) => Ok(()),
        Err(error) => {
            if active_user_exists(database, normalized_for_error).await? {
                Err(IdentityError::Database(error))
            } else {
                Err(IdentityError::UserNotFound)
            }
        }
    }
}

pub async fn issue_api_token(
    database: &Database,
    username: &str,
    label: &str,
) -> Result<IssuedCredential, IdentityError> {
    let normalized = validate_username(username)?.to_ascii_lowercase();
    let label = label.trim().to_owned();
    if label.is_empty() || label.len() > 80 {
        return Err(IdentityError::InvalidCredentialLabel);
    }
    let id = random_hex(16)?;
    let selector = random_hex(8)?;
    let secret = random_hex(32)?;
    let token = format!("epk1_{selector}_{secret}");
    let digest = digest(&token);
    let (id, user_id) = (id.clone(), normalized.clone());
    let normalized_for_error = normalized.clone();
    match database
        .with_transaction(move |connection| {
            let user_id: Option<String> = connection
                .query_row(
                    "SELECT id FROM users WHERE normalized_username = ?1 AND state = 'active'",
                    [&user_id],
                    |row| row.get(0),
                )
                .optional()?;
            let Some(user_id) = user_id else {
                return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows);
            };
            connection.execute(
                "INSERT INTO user_credentials(id, user_id, label, selector, digest, scopes) VALUES (?1, ?2, ?3, ?4, ?5, 'inference,discovery,integrations')",
                (&id, &user_id, &label, &selector, &digest),
            )?;
            Ok(user_id)
        })
        .await
    {
        Ok(user_id) => Ok(IssuedCredential { user_id, token }),
        Err(error) => {
            if active_user_exists(database, normalized_for_error).await? {
                Err(IdentityError::Database(error))
            } else {
                Err(IdentityError::UserNotFound)
            }
        }
    }
}

pub async fn revoke_api_tokens(database: &Database, username: &str) -> Result<u64, IdentityError> {
    let normalized = validate_username(username)?.to_ascii_lowercase();
    let normalized_for_error = normalized.clone();
    match database
        .with_transaction(move |connection| {
            let exists: bool = connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM users WHERE normalized_username = ?1)",
                [&normalized],
                |row| row.get(0),
            )?;
            if !exists {
                return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows);
            }
            let changed = connection.execute(
                "UPDATE user_credentials SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = (SELECT id FROM users WHERE normalized_username = ?1) AND revoked_at IS NULL",
                [&normalized],
            )?;
            Ok(changed as u64)
        })
        .await
    {
        Ok(count) => Ok(count),
        Err(error) => {
            if active_user_exists(database, normalized_for_error).await? {
                Err(IdentityError::Database(error))
            } else {
                Err(IdentityError::UserNotFound)
            }
        }
    }
}

async fn active_user_exists(
    database: &Database,
    normalized: String,
) -> Result<bool, IdentityError> {
    database
        .call(move |connection| {
            connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM users WHERE normalized_username = ?1 AND state = 'active')",
                [&normalized],
                |row| row.get(0),
            )
        })
        .await
        .map_err(IdentityError::Database)
}

pub async fn verify_password(
    database: &Database,
    username: &str,
    password: &str,
) -> Result<Option<UserSummary>, IdentityError> {
    if password.len() > MAX_PASSWORD_BYTES {
        return Ok(None);
    }
    let dummy_hash = DUMMY_PASSWORD_HASH
        .get_or_try_init(|| async { hash_password(DUMMY_PASSWORD).await })
        .await?
        .clone();
    let normalized = validate_username(username)?.to_ascii_lowercase();
    let account: Option<(String, UserSummary)> = database.call(move |connection| {
        connection.query_row(
            "SELECT id, username, role, state, must_change_password, password_hash FROM users WHERE normalized_username = ?1",
            [&normalized],
            |row| {
                let summary = UserSummary {
                    id: row.get(0)?, username: row.get(1)?, role: row.get(2)?, state: row.get(3)?,
                    must_change_password: row.get::<_, i64>(4)? != 0,
                };
                Ok((row.get(5)?, summary))
            },
        ).optional()
    }).await.map_err(IdentityError::Database)?;
    let (password_hash, user) = account
        .map(|(password_hash, user)| (password_hash, Some(user)))
        .unwrap_or((dummy_hash, None));
    let permit = HASH_SLOTS
        .get_or_init(|| Semaphore::new(HASH_CONCURRENCY))
        .acquire()
        .await
        .map_err(|_| IdentityError::HashWorker)?;
    let password = password.to_owned();
    let verified = tokio::task::spawn_blocking(move || {
        let parsed = PasswordHash::new(&password_hash).map_err(|_| IdentityError::InvalidHash)?;
        if parsed.algorithm.as_str() != "argon2id"
            || parsed.version != Some(19)
            || parsed.params.get_decimal("m") != Some(19_456)
            || parsed.params.get_decimal("t") != Some(2)
            || parsed.params.get_decimal("p") != Some(1)
        {
            return Err(IdentityError::InvalidHash);
        }
        Ok(Argon2::default()
            .verify_password(password.as_bytes(), &parsed)
            .is_ok())
    })
    .await
    .map_err(|_| IdentityError::HashWorker)??;
    drop(permit);
    Ok(verified
        .then_some(user)
        .flatten()
        .filter(|user| user.state == "active"))
}

/// Resolve a high-entropy personal token by its non-secret selector. Only the
/// digest is stored; the final digest check is fixed length and branchless.
pub async fn verify_api_token(database: &Database, token: &str) -> Result<bool, IdentityError> {
    Ok(verify_api_token_principal(database, token).await?.is_some())
}

pub async fn verify_api_token_principal(
    database: &Database,
    token: &str,
) -> Result<Option<AuthenticatedPrincipal>, IdentityError> {
    let Some(rest) = token.strip_prefix("epk1_") else {
        return Ok(None);
    };
    let Some((selector, _secret)) = rest.split_once('_') else {
        return Ok(None);
    };
    if selector.len() != 16
        || !selector.bytes().all(|byte| byte.is_ascii_hexdigit())
        || token.len() > 128
    {
        return Ok(None);
    }
    let failed_digest = digest(token);
    if failed_token_recently_seen(&failed_digest) {
        return Ok(None);
    }
    let selector = selector.to_owned();
    let expected = failed_digest.clone();
    database.call(move |connection| {
        let stored: Option<(String, String, String, String, i64)> = connection.query_row(
            "SELECT c.digest, c.id, c.user_id, c.scopes, u.auth_epoch FROM user_credentials c JOIN users u ON u.id = c.user_id WHERE c.selector = ?1 AND c.revoked_at IS NULL AND (c.expires_at IS NULL OR c.expires_at > CURRENT_TIMESTAMP) AND u.state = 'active'",
            [&selector],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
        ).optional()?;
        let Some((stored, credential_id, user_id, scopes, auth_epoch)) = stored else {
            remember_failed_token(&failed_digest);
            return Ok(None);
        };
        let matches = constant_time_equal(stored.as_bytes(), expected.as_bytes());
        if matches {
            connection.execute("UPDATE user_credentials SET last_used_at = CURRENT_TIMESTAMP WHERE selector = ?1 AND (last_used_at IS NULL OR last_used_at < datetime('now', '-15 minutes'))", [&selector])?;
            let scopes = scopes
                .split(',')
                .map(str::trim)
                .filter(|scope| !scope.is_empty())
                .map(str::to_owned)
                .collect();
            Ok(Some(AuthenticatedPrincipal {
                user_id: Some(user_id),
                credential_id: Some(credential_id),
                role: Some("agent".to_owned()),
                scopes,
                auth_epoch: Some(auth_epoch),
                kind: PrincipalKind::AgentToken,
            }))
        } else {
            remember_failed_token(&failed_digest);
            Ok(None)
        }
    }).await.map_err(IdentityError::Database)
}

fn failed_token_recently_seen(token_digest: &str) -> bool {
    let now = Instant::now();
    let mut cache = FAILED_TOKEN_DIGESTS
        .get_or_init(|| Mutex::new(VecDeque::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    cache.retain(|(_, expires)| *expires > now);
    cache.iter().any(|(cached, _)| cached == token_digest)
}

fn remember_failed_token(token_digest: &str) {
    let now = Instant::now();
    let mut cache = FAILED_TOKEN_DIGESTS
        .get_or_init(|| Mutex::new(VecDeque::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    cache.retain(|(_, expires)| *expires > now);
    cache.retain(|(cached, _)| cached != token_digest);
    if cache.len() == FAILED_TOKEN_CAPACITY {
        cache.pop_front();
    }
    cache.push_back((token_digest.to_owned(), now + FAILED_TOKEN_TTL));
}

pub fn validate_username(username: &str) -> Result<String, IdentityError> {
    let username = username.trim();
    if username.is_empty()
        || username.len() > 64
        || !username
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
    {
        return Err(IdentityError::InvalidUsername);
    }
    Ok(username.to_owned())
}

async fn hash_password(password: &str) -> Result<String, IdentityError> {
    if !(MIN_PASSWORD_BYTES..=MAX_PASSWORD_BYTES).contains(&password.len()) {
        return Err(IdentityError::InvalidPassword);
    }
    let permit = HASH_SLOTS
        .get_or_init(|| Semaphore::new(HASH_CONCURRENCY))
        .acquire()
        .await
        .map_err(|_| IdentityError::HashWorker)?;
    let password = password.to_owned();
    let result = tokio::task::spawn_blocking(move || {
        let mut salt = [0_u8; 16];
        getrandom::fill(&mut salt).map_err(|_| IdentityError::Entropy)?;
        let salt = SaltString::encode_b64(&salt).map_err(|_| IdentityError::Entropy)?;
        Argon2::new(
            Algorithm::Argon2id,
            Version::V0x13,
            Params::new(19_456, 2, 1, None).map_err(|_| IdentityError::InvalidHash)?,
        )
        .hash_password(password.as_bytes(), &salt)
        .map(|hash| hash.to_string())
        .map_err(|_| IdentityError::InvalidHash)
    })
    .await
    .map_err(|_| IdentityError::HashWorker)?;
    drop(permit);
    result
}

fn random_hex(length: usize) -> Result<String, IdentityError> {
    let mut bytes = vec![0; length];
    getrandom::fill(&mut bytes).map_err(|_| IdentityError::Entropy)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn digest(value: &str) -> String {
    Sha256::digest(value.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    let mut difference = (left.len() ^ right.len()) as u8;
    for index in 0..left.len().max(right.len()) {
        difference |=
            left.get(index).copied().unwrap_or(0) ^ right.get(index).copied().unwrap_or(0);
    }
    difference == 0
}
