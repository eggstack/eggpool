use std::{sync::Arc, time::Duration};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
    web_session,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

async fn database() -> (tempfile::TempDir, Database) {
    let directory = tempfile::tempdir().expect("temporary identity directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("eggpool.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    (directory, database)
}

async fn create_admin(database: &Database) -> (String, String) {
    identity::mark_fresh_uninitialized(database).await.unwrap();
    let (user, credential) =
        identity::bootstrap_admin(database, "admin", "M002 strong test passphrase")
            .await
            .expect("administrator bootstraps");
    (user.id, credential.token)
}

#[tokio::test]
async fn session_and_csrf_values_are_digest_only_and_revocable() {
    let (_directory, database) = database().await;
    let (user_id, _) = create_admin(&database).await;
    let issued = web_session::issue(&database, &user_id).await.unwrap();
    let principal = web_session::authenticate(&database, &issued.session_token)
        .await
        .unwrap()
        .expect("session authenticates");
    assert_eq!(principal.user_id, user_id);
    assert!(web_session::verify_csrf(&principal, &issued.csrf_token));
    assert!(!web_session::verify_csrf(&principal, "forged-token"));

    let stored = database
        .call(|connection| {
            connection.query_row("SELECT digest, csrf_digest FROM web_sessions", [], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
        })
        .await
        .unwrap();
    assert_ne!(stored.0, issued.session_token);
    assert_ne!(stored.1, issued.csrf_token);

    web_session::revoke(&database, &issued.session_token)
        .await
        .unwrap();
    assert!(
        web_session::authenticate(&database, &issued.session_token)
            .await
            .unwrap()
            .is_none()
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn password_reset_revokes_browser_sessions() {
    let (_directory, database) = database().await;
    let (user_id, _) = create_admin(&database).await;
    let issued = web_session::issue(&database, &user_id).await.unwrap();
    identity::reset_password(&database, "admin", "M002 replacement password 456")
        .await
        .expect("password reset succeeds");
    assert!(
        web_session::authenticate(&database, &issued.session_token)
            .await
            .unwrap()
            .is_none()
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn revoked_admin_session_cannot_commit_privileged_identity_or_budget_writes() {
    let (_directory, database) = database().await;
    let (admin_id, _) = create_admin(&database).await;
    identity::enable_managed(&database).await.unwrap();
    let issued = web_session::issue(&database, &admin_id).await.unwrap();
    let principal = web_session::authenticate(&database, &issued.session_token)
        .await
        .unwrap()
        .expect("admin session is initially active");
    let member = identity::create_member(
        &database,
        &admin_id,
        "fenced-member",
        "M015 fenced member temporary password",
    )
    .await
    .unwrap();
    let credential =
        identity::issue_personal_api_token(&database, &admin_id, &member.id, "fenced member token")
            .await
            .unwrap();
    web_session::revoke(&database, &issued.session_token)
        .await
        .unwrap();

    assert!(
        identity::create_member_from_browser(
            &database,
            &principal,
            "must-not-exist",
            "M015 stale session temporary password",
        )
        .await
        .is_err()
    );
    assert!(
        identity::create_member_with_profile_from_browser(
            &database,
            &principal,
            "must-not-exist-with-profile",
            "M015 stale session temporary password",
            "default",
        )
        .await
        .is_err()
    );
    assert!(
        identity::set_user_enabled_from_browser(&database, &principal, &member.id, false,)
            .await
            .is_err()
    );
    assert!(
        identity::admin_reset_password_from_browser(
            &database,
            &principal,
            &member.id,
            "M015 stale session replacement password",
        )
        .await
        .is_err()
    );
    assert!(
        identity::issue_personal_api_token_from_browser(
            &database,
            &principal,
            &member.id,
            "must-not-be-issued",
        )
        .await
        .is_err()
    );
    assert!(
        identity::revoke_user_api_tokens_from_browser(&database, &principal, &member.id,)
            .await
            .is_err()
    );
    assert!(
        identity::revoke_user_credential_from_browser(
            &database,
            &principal,
            &member.id,
            "missing-credential",
        )
        .await
        .is_err()
    );
    assert!(
        identity::reset_password_from_browser(
            &database,
            &principal,
            "M015 stale session password replacement",
        )
        .await
        .is_err()
    );

    let limits = eggpool::user_budget::BudgetLimits {
        requests: Some(1),
        tokens: None,
        microdollars: None,
        concurrent: None,
        enforcement: eggpool::user_budget::BudgetEnforcement::Estimated,
    };
    let user_budgets = eggpool::user_budget::UserBudgetService::new(database.clone())
        .with_browser_admin_session(&principal);
    assert!(
        user_budgets
            .set_rule(
                &admin_id,
                &member.id,
                None,
                eggpool::user_budget::BudgetWindow::Day,
                limits,
            )
            .await
            .is_err()
    );
    let instance_budgets = eggpool::instance_budget::InstanceBudgetService::new(database.clone())
        .with_browser_admin_session(&principal);
    assert!(
        instance_budgets
            .set_rule(
                &admin_id,
                None,
                eggpool::user_budget::BudgetWindow::Day,
                limits,
            )
            .await
            .is_err()
    );

    let users = identity::list_users(&database).await.unwrap();
    assert!(!users.iter().any(|user| {
        matches!(
            user.username.as_str(),
            "must-not-exist" | "must-not-exist-with-profile"
        )
    }));
    assert_eq!(
        users
            .iter()
            .find(|user| user.id == member.id)
            .expect("member remains present")
            .state,
        "active"
    );
    assert!(
        identity::verify_api_token_principal(&database, &credential.token)
            .await
            .unwrap()
            .is_some(),
        "stale admin requests do not revoke member credentials"
    );
    assert!(user_budgets.rules(&member.id).await.unwrap().is_empty());
    assert!(instance_budgets.rules().await.unwrap().is_empty());
    database.close().await.unwrap();
}

#[tokio::test]
async fn expired_sessions_and_disabled_users_are_rejected() {
    let (_directory, database) = database().await;
    let (user_id, _) = create_admin(&database).await;
    let expired = web_session::issue(&database, &user_id).await.unwrap();
    database
        .call(|connection| {
            connection.execute(
                "UPDATE web_sessions SET expires_at = datetime('now', '-1 second')",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    assert!(
        web_session::authenticate(&database, &expired.session_token)
            .await
            .unwrap()
            .is_none()
    );

    let active = web_session::issue(&database, &user_id).await.unwrap();
    database
        .call(move |connection| {
            connection.execute(
                "UPDATE users SET state = 'disabled' WHERE id = ?1",
                [&user_id],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    assert!(
        web_session::authenticate(&database, &active.session_token)
            .await
            .unwrap()
            .is_none()
    );
    database.close().await.unwrap();
}

#[test]
fn login_throttle_is_bounded_by_username_and_peer() {
    let username = format!("login-test-{}", std::process::id());
    let peer = Some("192.0.2.19".parse().unwrap());
    let other_peer = Some("198.51.100.19".parse().unwrap());
    assert!(web_session::login_allowed(&username, peer));
    for _ in 0..10 {
        web_session::record_login_failure(&username, peer);
    }
    assert!(!web_session::login_allowed(&username, peer));
    assert!(!web_session::login_allowed(&format!(" {username} "), peer));
    assert!(web_session::login_allowed(&username, other_peer));
}

#[test]
fn personal_token_failures_are_bounded_per_peer_before_database_lookup() {
    let peer = Some("203.0.113.71".parse().unwrap());
    let other_peer = Some("203.0.113.72".parse().unwrap());
    for _ in 0..30 {
        assert!(web_session::personal_token_attempt_allowed(peer));
        web_session::record_personal_token_failure(peer);
    }
    assert!(!web_session::personal_token_attempt_allowed(peer));
    assert!(web_session::personal_token_attempt_allowed(other_peer));
}

async fn http_request(address: std::net::SocketAddr, raw: &str) -> String {
    let target = raw.split_whitespace().nth(1).unwrap_or("unknown");
    let mut stream = TcpStream::connect(address).await.expect("connect");
    stream
        .write_all(raw.as_bytes())
        .await
        .expect("write request");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(10), stream.read_to_end(&mut response))
        .await
        .unwrap_or_else(|_| panic!("request to {target} did not finish"))
        .expect("read response");
    String::from_utf8_lossy(&response).into_owned()
}

fn cookie(response: &str, name: &str) -> String {
    response
        .lines()
        .find_map(|line| {
            let (header, value) = line.split_once(':')?;
            if !header.eq_ignore_ascii_case("set-cookie") {
                return None;
            }
            value
                .trim()
                .split(';')
                .next()?
                .strip_prefix(&format!("{name}="))
                .map(str::to_owned)
        })
        .expect("cookie is set")
}

#[tokio::test]
async fn managed_browser_login_account_csrf_and_logout_work_over_loopback() {
    let (_directory, database) = database().await;
    let (_user_id, agent_token) = create_admin(&database).await;
    identity::enable_managed(&database).await.unwrap();

    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = None;
    config.dashboard.enabled = true;
    config.dashboard.public = true;
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "session-test".to_owned(), 1)
            .await
            .expect("initial generation");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfer"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("tasks install");
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("listener");
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let origin = format!("http://{address}");

    let public_dashboard = http_request(
        address,
        &format!("GET / HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"),
    )
    .await;
    assert!(
        public_dashboard.starts_with("HTTP/1.1 401"),
        "{public_dashboard}"
    );
    let agent_to_account = http_request(address, &format!(
        "GET /account HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {agent_token}\r\nConnection: close\r\n\r\n"
    )).await;
    assert!(
        agent_to_account.starts_with("HTTP/1.1 401"),
        "{agent_to_account}"
    );
    let agent_to_admin_api = http_request(address, &format!(
        "GET /api/status HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {agent_token}\r\nConnection: close\r\n\r\n"
    )).await;
    assert!(
        agent_to_admin_api.starts_with("HTTP/1.1 401"),
        "{agent_to_admin_api}"
    );

    let login_page = http_request(
        address,
        &format!("GET /login HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"),
    )
    .await;
    assert!(login_page.starts_with("HTTP/1.1 200"), "{login_page}");
    assert!(login_page.contains("name=\"password\""), "{login_page}");

    let form = "username=admin&password=M002+strong+test+passphrase";
    let login = http_request(address, &format!(
        "POST /login HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{form}", form.len()
    )).await;
    assert!(login.starts_with("HTTP/1.1 303"), "{login}");
    assert!(login.to_ascii_lowercase().contains("httponly"), "{login}");
    assert!(
        login.to_ascii_lowercase().contains("samesite=lax"),
        "{login}"
    );
    let session = cookie(&login, "eggpool_session");
    let csrf = cookie(&login, "eggpool_csrf");
    let cookie_header = format!("eggpool_session={session}; eggpool_csrf={csrf}");

    let account = http_request(address, &format!("GET /account HTTP/1.1\r\nHost: {address}\r\nCookie: {cookie_header}\r\nConnection: close\r\n\r\n")).await;
    assert!(account.starts_with("HTTP/1.1 200"), "{account}");
    assert!(
        account
            .to_ascii_lowercase()
            .contains("cache-control: no-store"),
        "authenticated account pages are not cacheable: {account}"
    );
    assert!(account.contains("Change password"), "{account}");

    // A forced first-login password change is available without JavaScript
    // and invalidates the browser session when completed.
    database
        .call(|connection| {
            connection.execute(
                "UPDATE users SET must_change_password = 1 WHERE username = 'admin'",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    let forced = http_request(address, &format!("GET / HTTP/1.1\r\nHost: {address}\r\nCookie: {cookie_header}\r\nConnection: close\r\n\r\n")).await;
    assert!(forced.starts_with("HTTP/1.1 303"), "{forced}");
    assert!(forced.contains("/account?password=required"), "{forced}");
    let forced_account = http_request(address, &format!("GET /account HTTP/1.1\r\nHost: {address}\r\nCookie: {cookie_header}\r\nConnection: close\r\n\r\n")).await;
    assert!(
        forced_account.contains("Change your password to continue."),
        "{forced_account}"
    );

    let new_password = "M002 new test password 789";
    let change_body = format!(
        "current_password=M002+strong+test+passphrase&new_password=M002+new+test+password+789&csrf={csrf}"
    );
    let changed = http_request(address, &format!(
        "POST /account/password HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nCookie: {cookie_header}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{change_body}", change_body.len()
    )).await;
    assert!(changed.starts_with("HTTP/1.1 303"), "{changed}");
    assert!(
        web_session::authenticate(&database, &session)
            .await
            .unwrap()
            .is_none(),
        "password change revokes current session"
    );
    let relogin_form = format!("username=admin&password={}", new_password.replace(' ', "+"));
    let relogin = http_request(address, &format!(
        "POST /login HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{relogin_form}", relogin_form.len()
    )).await;
    assert!(relogin.starts_with("HTTP/1.1 303"), "{relogin}");

    let session = cookie(&relogin, "eggpool_session");
    let csrf = cookie(&relogin, "eggpool_csrf");
    let cookie_header = format!("eggpool_session={session}; eggpool_csrf={csrf}");

    let missing_body = "csrf=";
    let missing_csrf = http_request(address, &format!(
        "POST /logout HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nCookie: {cookie_header}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{missing_body}", missing_body.len()
    )).await;
    assert!(missing_csrf.starts_with("HTTP/1.1 403"), "{missing_csrf}");

    let logout_body = format!("csrf={csrf}");
    let logout = http_request(address, &format!(
        "POST /logout HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nCookie: {cookie_header}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{logout_body}", logout_body.len()
    )).await;
    assert!(logout.starts_with("HTTP/1.1 303"), "{logout}");

    let stale = http_request(address, &format!("GET /account HTTP/1.1\r\nHost: {address}\r\nCookie: {cookie_header}\r\nConnection: close\r\n\r\n")).await;
    assert!(stale.starts_with("HTTP/1.1 401"), "{stale}");
    handle.request_shutdown(ShutdownReason::Requested);
    task.await.unwrap().unwrap();
    database.close().await.unwrap();
}

#[tokio::test]
async fn trusted_reverse_proxy_can_complete_secure_browser_login() {
    let (_directory, database) = database().await;
    let (_user_id, _) = create_admin(&database).await;
    identity::enable_managed(&database).await.unwrap();

    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = None;
    config.dashboard.enabled = true;
    config.dashboard.public = true;
    config.security.trusted_proxies = vec!["127.0.0.1".to_owned()];
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "proxy-session-test".to_owned(),
        1,
    )
    .await
    .expect("initial generation");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfer"),
    ));
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("listener");
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let origin = format!("https://{address}");
    let form = "username=admin&password=M002+strong+test+passphrase";
    let login = http_request(address, &format!(
        "POST /login HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nX-Forwarded-Proto: https\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{form}", form.len()
    )).await;

    assert!(login.starts_with("HTTP/1.1 303"), "{login}");
    assert!(
        login.to_ascii_lowercase().contains("eggpool_session=")
            && login.to_ascii_lowercase().contains("; secure"),
        "trusted proxy login must issue a Secure session cookie: {login}"
    );
    handle.request_shutdown(ShutdownReason::Requested);
    task.await.unwrap().unwrap();
    database.close().await.unwrap();
}

#[tokio::test]
async fn token_guess_throttle_runs_before_access_state_and_database_lookup() {
    let (_directory, database) = database().await;
    let (_admin_id, _) = create_admin(&database).await;
    identity::enable_managed(&database).await.unwrap();

    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = Some("legacy-shared-key-for-test-only".to_owned());
    config.dashboard.enabled = true;
    config.dashboard.public = true;
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "token-test".to_owned(), 1)
            .await
            .expect("initial generation");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfer"),
    ));
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("listener");
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let before_missing = database.stats().calls;
    let missing = http_request(
        address,
        &format!("GET /v1/models HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"),
    )
    .await;
    assert!(missing.starts_with("HTTP/1.1 401"), "{missing}");
    assert_eq!(
        database.stats().calls,
        before_missing,
        "missing credentials are rejected before the access-state read when the shared key is configured"
    );

    for index in 0..30_u64 {
        let token = format!("epk1_{index:016x}_{:064x}", index + 1);
        let response = http_request(address, &format!(
            "GET /v1/models HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
        )).await;
        assert!(response.starts_with("HTTP/1.1 401"), "{response}");
    }
    let before_blocked = database.stats().calls;
    let blocked_token = format!("epk1_{:016x}_{:064x}", 31_u64, 32_u64);
    let blocked = http_request(address, &format!(
        "GET /v1/models HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {blocked_token}\r\nConnection: close\r\n\r\n"
    )).await;
    assert!(blocked.starts_with("HTTP/1.1 401"), "{blocked}");
    assert_eq!(
        database.stats().calls,
        before_blocked,
        "the exhausted peer bucket rejects before any SQLite operation"
    );

    handle.request_shutdown(ShutdownReason::Requested);
    task.await.unwrap().unwrap();
    database.close().await.unwrap();
}
