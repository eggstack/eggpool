//! Opaque browser sessions and bounded local login throttling.

use std::{
    collections::HashMap,
    net::IpAddr,
    sync::OnceLock,
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio_rusqlite::rusqlite::OptionalExtension;

use crate::db::Database;

const SESSION_PREFIX: &str = "eps1_";
const LOGIN_WINDOW: Duration = Duration::from_secs(15 * 60);
const LOGIN_USERNAME_LIMIT: u8 = 10;
const LOGIN_IP_LIMIT: u8 = 30;
const PERSONAL_TOKEN_IP_LIMIT: u8 = 30;
const LOGIN_BUCKET_LIMIT: usize = 2048;

static LOGIN_FAILURES: OnceLock<std::sync::Mutex<HashMap<String, LoginBucket>>> = OnceLock::new();

type SessionRow = (String, String, String, String, i64, i64, String, String);

#[derive(Debug, Error)]
pub enum WebSessionError {
    #[error("session entropy unavailable")]
    Entropy,
    #[error("session worker failed")]
    Worker,
    #[error(transparent)]
    Database(#[from] crate::db::DatabaseError),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Principal {
    pub session_id: String,
    pub user_id: String,
    pub username: String,
    pub role: String,
    pub must_change_password: bool,
    pub auth_epoch: i64,
    csrf_digest: String,
}

/// Transaction-time fence for browser-originated mutations. It binds a
/// mutation to the exact session and privilege snapshot admitted by
/// middleware, so revocation, password reset, role change, or disablement
/// before commit makes the write fail closed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SessionFence {
    session_id: String,
    user_id: String,
    role: String,
    auth_epoch: i64,
    must_change_password: bool,
}

impl SessionFence {
    pub(crate) fn from_principal(principal: &Principal) -> Self {
        Self {
            session_id: principal.session_id.clone(),
            user_id: principal.user_id.clone(),
            role: principal.role.clone(),
            auth_epoch: principal.auth_epoch,
            must_change_password: principal.must_change_password,
        }
    }

    pub(crate) fn validates_actor(
        &self,
        connection: &tokio_rusqlite::rusqlite::Connection,
        actor_id: &str,
        require_admin: bool,
    ) -> Result<bool, tokio_rusqlite::rusqlite::Error> {
        if self.must_change_password
            || actor_id != self.user_id
            || (require_admin && self.role != "admin")
        {
            return Ok(false);
        }
        self.validates_current_session(connection, actor_id)
    }

    pub(crate) fn validates_current_session(
        &self,
        connection: &tokio_rusqlite::rusqlite::Connection,
        actor_id: &str,
    ) -> Result<bool, tokio_rusqlite::rusqlite::Error> {
        if actor_id != self.user_id {
            return Ok(false);
        }
        connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM web_sessions s \
             JOIN users u ON u.id = s.user_id \
             WHERE s.id = ?1 AND s.user_id = ?2 AND s.revoked_at IS NULL \
             AND s.expires_at > CURRENT_TIMESTAMP \
             AND s.last_used_at > datetime('now', '-12 hours') \
             AND u.state = 'active' AND u.role = ?3 \
             AND u.must_change_password = ?4 AND u.auth_epoch = ?5)",
            (
                &self.session_id,
                &self.user_id,
                &self.role,
                i64::from(self.must_change_password),
                self.auth_epoch,
            ),
            |row| row.get(0),
        )
    }

    pub(crate) fn user_id(&self) -> &str {
        &self.user_id
    }
}

pub struct IssuedSession {
    pub session_token: String,
    pub csrf_token: String,
}

#[derive(Clone, Copy)]
struct LoginBucket {
    failures: u8,
    started: Instant,
}

pub async fn issue(database: &Database, user_id: &str) -> Result<IssuedSession, WebSessionError> {
    let id = random_hex(16)?;
    let selector = random_hex(8)?;
    let secret = random_hex(32)?;
    let session_token = format!("{SESSION_PREFIX}{selector}_{secret}");
    let token_digest = digest(&session_token);
    let csrf_token = random_hex(32)?;
    let csrf_digest = digest(&csrf_token);
    let id_for_db = id.clone();
    let selector_for_db = selector.clone();
    let user_id = user_id.to_owned();
    database
        .with_transaction(move |connection| {
            connection.execute(
                "INSERT INTO web_sessions(id, user_id, selector, digest, csrf_digest, expires_at) \
                 SELECT ?1, id, ?2, ?3, ?4, datetime('now', '+7 days') FROM users WHERE id = ?5 AND state = 'active'",
                (&id_for_db, &selector_for_db, &token_digest, &csrf_digest, &user_id),
            )?;
            if connection.changes() != 1 {
                return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows);
            }
            Ok(())
        })
        .await?;
    Ok(IssuedSession {
        session_token,
        csrf_token,
    })
}

pub async fn authenticate(
    database: &Database,
    session_token: &str,
) -> Result<Option<Principal>, WebSessionError> {
    let Some((selector, _secret)) = parse_session_token(session_token) else {
        return Ok(None);
    };
    let selector = selector.to_owned();
    let expected = digest(session_token);
    database
        .with_transaction(move |connection| {
            let row: Option<SessionRow> = connection.query_row(
                "SELECT s.id, u.id, u.username, u.role, u.must_change_password, u.auth_epoch, s.digest, s.csrf_digest \
                 FROM web_sessions s JOIN users u ON u.id = s.user_id \
                 WHERE s.selector = ?1 AND s.revoked_at IS NULL \
                 AND s.expires_at > CURRENT_TIMESTAMP \
                 AND s.last_used_at > datetime('now', '-12 hours') AND u.state = 'active'",
                [&selector],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?, row.get(6)?, row.get(7)?)),
            ).optional()?;
            let Some((session_id, user_id, username, role, must_change_password, auth_epoch, stored, csrf_digest)) = row else {
                return Ok(None);
            };
            if !constant_time_equal(stored.as_bytes(), expected.as_bytes()) {
                return Ok(None);
            }
            connection.execute(
                "UPDATE web_sessions SET last_used_at = CURRENT_TIMESTAMP \
                 WHERE selector = ?1 AND last_used_at < datetime('now', '-15 minutes')",
                [&selector],
            )?;
            Ok(Some(Principal {
                session_id,
                user_id,
                username,
                role,
                must_change_password: must_change_password != 0,
                auth_epoch,
                csrf_digest,
            }))
        })
        .await
        .map_err(WebSessionError::Database)
}

pub fn verify_csrf(principal: &Principal, csrf_token: &str) -> bool {
    let supplied = digest(csrf_token);
    constant_time_equal(principal.csrf_digest.as_bytes(), supplied.as_bytes())
}

pub async fn revoke(database: &Database, session_token: &str) -> Result<(), WebSessionError> {
    let Some((selector, _secret)) = parse_session_token(session_token) else {
        return Ok(());
    };
    let selector = selector.to_owned();
    let expected = digest(session_token);
    database
        .with_transaction(move |connection| {
            let stored: Option<String> = connection.query_row(
                "SELECT digest FROM web_sessions WHERE selector = ?1 AND revoked_at IS NULL",
                [&selector],
                |row| row.get(0),
            ).optional()?;
            if stored.as_deref().is_some_and(|stored| constant_time_equal(stored.as_bytes(), expected.as_bytes())) {
                connection.execute(
                    "UPDATE web_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE selector = ?1 AND revoked_at IS NULL",
                    [&selector],
                )?;
            }
            Ok(())
        })
        .await?;
    Ok(())
}

pub async fn revoke_user_sessions(
    database: &Database,
    user_id: &str,
) -> Result<(), WebSessionError> {
    let user_id = user_id.to_owned();
    database
        .with_transaction(move |connection| {
            connection.execute(
                "UPDATE web_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = ?1 AND revoked_at IS NULL",
                [&user_id],
            )?;
            Ok(())
        })
        .await?;
    Ok(())
}

pub fn session_cookie_value(headers: &axum::http::HeaderMap) -> Option<String> {
    cookie_value(headers, "eggpool_session")
}

pub fn csrf_cookie_value(headers: &axum::http::HeaderMap) -> Option<String> {
    cookie_value(headers, "eggpool_csrf")
}

fn cookie_value(headers: &axum::http::HeaderMap, name: &str) -> Option<String> {
    headers
        .get(axum::http::header::COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .filter_map(|part| part.trim().split_once('='))
        .find_map(|(key, value)| (key == name).then(|| value.to_owned()))
}

pub fn session_set_cookie(token: &str, secure: bool) -> String {
    format!(
        "eggpool_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{}",
        if secure { "; Secure" } else { "" }
    )
}

/// The CSRF cookie carries the same `SameSite` attribute as the session
/// cookie.
///
/// `Lax` and `Strict` differ in exactly one browser behavior that matters
/// here: a `Lax` cookie is delivered on a cross-site top-level `GET`
/// navigation and a `Strict` cookie is not. The session cookie was already
/// `Lax`, so a `Strict` CSRF cookie was withheld on exactly the requests that
/// already carried the session: the deep-linked operator's page rendered with
/// an empty hidden CSRF field and every mutation on it was refused. Both
/// attributes withhold the cookie on a cross-site unsafe request, so the
/// server-side same-origin check remains the authoritative defense and the
/// double-submit token remains defense in depth.
pub fn csrf_set_cookie(token: &str, secure: bool) -> String {
    format!(
        "eggpool_csrf={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{}",
        if secure { "; Secure" } else { "" }
    )
}

/// Clear both browser cookies. The CSRF cookie is cleared with the same
/// `SameSite` attribute it is set with, so the set and clear paths stay
/// consistent.
pub fn clear_cookies(secure: bool) -> [String; 2] {
    let secure = if secure { "; Secure" } else { "" };
    [
        format!("eggpool_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"),
        format!("eggpool_csrf=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"),
    ]
}

pub fn request_peer_ip(request: &axum::http::Request<axum::body::Body>) -> Option<IpAddr> {
    request
        .extensions()
        .get::<eggserve_server::interop::ConnectionInfoExt>()
        .and_then(|info| info.0.remote_addr)
        .map(|address| address.ip())
}

pub fn login_allowed(username: &str, peer: Option<IpAddr>) -> bool {
    let username_key = username_throttle_key(username);
    let ip_key = throttle_key(
        "ip",
        &peer.map_or_else(|| "unknown".to_owned(), |ip| ip.to_string()),
    );
    let now = Instant::now();
    let mut failures = LOGIN_FAILURES
        .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    failures.retain(|_, bucket| now.duration_since(bucket.started) < LOGIN_WINDOW);
    failures
        .get(&username_key)
        .is_none_or(|bucket| bucket.failures < LOGIN_USERNAME_LIMIT)
        && failures
            .get(&ip_key)
            .is_none_or(|bucket| bucket.failures < LOGIN_IP_LIMIT)
}

pub fn record_login_failure(username: &str, peer: Option<IpAddr>) {
    let keys = [
        (username_throttle_key(username), LOGIN_USERNAME_LIMIT),
        (
            throttle_key(
                "ip",
                &peer.map_or_else(|| "unknown".to_owned(), |ip| ip.to_string()),
            ),
            LOGIN_IP_LIMIT,
        ),
    ];
    let now = Instant::now();
    let mut failures = LOGIN_FAILURES
        .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    failures.retain(|_, bucket| now.duration_since(bucket.started) < LOGIN_WINDOW);
    for (key, limit) in keys {
        if let Some(bucket) = failures.get_mut(&key) {
            bucket.failures = bucket.failures.saturating_add(1).min(limit);
        } else {
            if failures.len() >= LOGIN_BUCKET_LIMIT
                && let Some(oldest) = failures
                    .iter()
                    .min_by_key(|(_, bucket)| bucket.started)
                    .map(|(key, _)| key.clone())
            {
                failures.remove(&oldest);
            }
            failures.insert(
                key,
                LoginBucket {
                    failures: 1,
                    started: now,
                },
            );
        }
    }
}

pub fn personal_token_attempt_allowed(peer: Option<IpAddr>) -> bool {
    let key = throttle_key(
        "personal-token-ip",
        &peer.map_or_else(|| "unknown".to_owned(), |ip| ip.to_string()),
    );
    let now = Instant::now();
    let mut failures = LOGIN_FAILURES
        .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    failures.retain(|_, bucket| now.duration_since(bucket.started) < LOGIN_WINDOW);
    failures
        .get(&key)
        .is_none_or(|bucket| bucket.failures < PERSONAL_TOKEN_IP_LIMIT)
}

pub fn record_personal_token_failure(peer: Option<IpAddr>) {
    let key = throttle_key(
        "personal-token-ip",
        &peer.map_or_else(|| "unknown".to_owned(), |ip| ip.to_string()),
    );
    let now = Instant::now();
    let mut failures = LOGIN_FAILURES
        .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    failures.retain(|_, bucket| now.duration_since(bucket.started) < LOGIN_WINDOW);
    if let Some(bucket) = failures.get_mut(&key) {
        bucket.failures = bucket
            .failures
            .saturating_add(1)
            .min(PERSONAL_TOKEN_IP_LIMIT);
        return;
    }
    if failures.len() >= LOGIN_BUCKET_LIMIT
        && let Some(oldest) = failures
            .iter()
            .min_by_key(|(_, bucket)| bucket.started)
            .map(|(key, _)| key.clone())
    {
        failures.remove(&oldest);
    }
    failures.insert(
        key,
        LoginBucket {
            failures: 1,
            started: now,
        },
    );
}

/// The account-scoped throttle key.
///
/// The key is the normalized username alone. Folding the peer address into it
/// made the documented "10 failures per username" limit a per
/// (username, source address) limit: every additional source address reset
/// the account's budget, so a distributed attempt against one account met one
/// budget per address instead of one budget.
fn username_throttle_key(username: &str) -> String {
    throttle_key("username", &username.trim().to_ascii_lowercase())
}

/// Clear an account's failure bucket after a successful sign-in.
///
/// Only the account bucket is cleared. The per-peer bucket deliberately
/// survives: clearing it would let any valid account reset an address's
/// cross-account failure budget and thereby multiply that address's
/// brute-force throughput without bound.
pub fn clear_login_failures(username: &str) {
    let username_key = username_throttle_key(username);
    let mut failures = LOGIN_FAILURES
        .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    failures.remove(&username_key);
}

fn throttle_key(kind: &str, value: &str) -> String {
    digest(&format!("{kind}:{value}"))
}

fn parse_session_token(token: &str) -> Option<(&str, &str)> {
    let rest = token.strip_prefix(SESSION_PREFIX)?;
    let (selector, secret) = rest.split_once('_')?;
    if selector.len() != 16
        || !selector.bytes().all(|byte| byte.is_ascii_hexdigit())
        || secret.len() != 64
        || !secret.bytes().all(|byte| byte.is_ascii_hexdigit())
        || token.len() > 128
    {
        return None;
    }
    Some((selector, secret))
}

fn random_hex(length: usize) -> Result<String, WebSessionError> {
    let mut bytes = vec![0; length];
    getrandom::fill(&mut bytes).map_err(|_| WebSessionError::Entropy)?;
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

#[cfg(test)]
mod tests {
    use super::{
        LOGIN_FAILURES, clear_login_failures, login_allowed, record_login_failure, throttle_key,
        username_throttle_key,
    };
    use std::{collections::HashMap, net::IpAddr};

    /// The failure table is process-local and shared by every test in this
    /// binary, so each test claims its own usernames and starts from an empty
    /// table.
    fn reset_failures() {
        LOGIN_FAILURES
            .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clear();
    }

    fn peer(address: &str) -> Option<IpAddr> {
        Some(address.parse().expect("peer address"))
    }

    #[test]
    fn the_account_bucket_is_shared_across_source_addresses() {
        reset_failures();
        let username = "account-scoped-throttle";
        let first = peer("192.0.2.41");
        let second = peer("198.51.100.41");
        let third = peer("203.0.113.41");
        for _ in 0..5 {
            record_login_failure(username, first);
        }
        for _ in 0..5 {
            record_login_failure(username, second);
        }
        // Ten failures for one account exhaust the account budget no matter
        // which source address they came from.
        assert!(!login_allowed(username, first));
        assert!(!login_allowed(username, second));
        assert!(!login_allowed(username, third));
        reset_failures();
    }

    #[test]
    fn one_peers_failures_do_not_consume_another_peers_ip_budget() {
        reset_failures();
        let username = "peer-scoped-throttle";
        let first = peer("192.0.2.42");
        let second = peer("198.51.100.42");
        for _ in 0..10 {
            record_login_failure(username, first);
        }
        // The second address has spent nothing: a different account is still
        // allowed there.
        assert!(login_allowed("other-account", second));
        // The account budget, by contrast, is spent for every address.
        assert!(!login_allowed(username, second));
        // The first address has spent ten of its thirty, so the per-peer
        // budget still holds for it and still admits another account.
        assert!(login_allowed("another-account", first));
        for _ in 0..30 {
            record_login_failure("third-account", second);
        }
        assert!(!login_allowed("fourth-account", second));
        assert!(login_allowed("fourth-account", first));
        reset_failures();
    }

    #[test]
    fn a_successful_sign_in_clears_the_account_failure_counter() {
        reset_failures();
        let username = "success-clears-account";
        let source = peer("192.0.2.43");
        for _ in 0..9 {
            record_login_failure(username, source);
        }
        assert!(login_allowed(username, source));
        clear_login_failures(username);
        for _ in 0..9 {
            record_login_failure(username, source);
        }
        // Without the clear the counter would still stand at ten and the
        // attempt would be refused.
        assert!(login_allowed(username, source));
        reset_failures();
    }

    #[test]
    fn a_successful_sign_in_does_not_clear_the_per_peer_bucket() {
        reset_failures();
        let username = "success-keeps-peer";
        let source = peer("192.0.2.44");
        for _ in 0..30 {
            record_login_failure(username, source);
        }
        clear_login_failures(username);
        // The address's cross-account budget survives the success, so another
        // account is still refused from it.
        assert!(!login_allowed("unrelated-account", source));
        reset_failures();
    }

    #[test]
    fn the_account_bucket_key_does_not_contain_the_peer_address() {
        reset_failures();
        // The account key is the normalized username alone: no peer address
        // takes part in it.
        assert_eq!(
            username_throttle_key("Operator"),
            username_throttle_key("  operator  ")
        );
        assert_ne!(
            username_throttle_key("operator"),
            throttle_key("ip", "192.0.2.45")
        );
        // The peer bucket is a digest of the peer address, so two addresses
        // never share one.
        assert_ne!(
            throttle_key("ip", "192.0.2.45"),
            throttle_key("ip", "198.51.100.45")
        );
        // And the two accounts really do share one bucket.
        let username = "shared-account-bucket";
        for _ in 0..10 {
            record_login_failure(username, peer("192.0.2.45"));
        }
        assert!(!login_allowed(username, peer("198.51.100.45")));
        reset_failures();
    }
}
