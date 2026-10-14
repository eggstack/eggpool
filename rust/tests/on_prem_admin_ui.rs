use std::{sync::Arc, time::Duration};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    model_policy::ModelPolicyService,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

async fn database() -> (tempfile::TempDir, Database) {
    let directory = tempfile::tempdir().expect("temporary admin directory");
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

async fn http_request(
    address: std::net::SocketAddr,
    method: &str,
    path: &str,
    body: &str,
    cookies: Option<&str>,
    origin: Option<&str>,
    bearer: Option<&str>,
) -> String {
    let mut stream = TcpStream::connect(address).await.expect("connect");
    let mut request =
        format!("{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n");
    if !body.is_empty() {
        request.push_str(&format!(
            "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\n",
            body.len()
        ));
    }
    if let Some(cookies) = cookies {
        request.push_str(&format!("Cookie: {cookies}\r\n"));
    }
    if let Some(origin) = origin {
        request.push_str(&format!("Origin: {origin}\r\n"));
    }
    if let Some(token) = bearer {
        request.push_str(&format!("Authorization: Bearer {token}\r\n"));
    }
    request.push_str("\r\n");
    request.push_str(body);
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write request");
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .await
        .expect("read response");
    response
}

fn session_pair(response: &str) -> (String, String, String) {
    let session = cookie(response, "eggpool_session");
    let csrf = cookie(response, "eggpool_csrf");
    let header = format!("eggpool_session={session}; eggpool_csrf={csrf}");
    (session, csrf, header)
}

#[tokio::test]
async fn profile_edits_use_compare_and_swap_and_are_audited() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) =
        identity::bootstrap_admin(&database, "admin", "M006 strong admin password passphrase")
            .await
            .unwrap();
    let policy = ModelPolicyService::new(database.clone());
    policy
        .create_profile("restricted", "Restricted", "deny", &admin.id)
        .await
        .unwrap();
    let profile = policy.list_profiles().await.unwrap().remove(0);
    assert_eq!(profile.version, 1);
    assert!(
        policy
            .update_profile(
                "restricted",
                "Restricted v2",
                "deny",
                profile.version,
                &admin.id,
            )
            .await
            .unwrap()
    );
    assert!(
        !policy
            .update_profile(
                "restricted",
                "Stale edit",
                "allow",
                profile.version,
                &admin.id,
            )
            .await
            .unwrap()
    );
    policy
        .set_profile_rule("restricted", "model-a", "allow", &admin.id)
        .await
        .unwrap();
    assert_eq!(policy.list_profile_rules().await.unwrap().len(), 1);
    assert!(
        policy
            .remove_profile_rule("restricted", "model-a", &admin.id)
            .await
            .unwrap()
    );
    let audit = identity::admin_audit(&database, 20).await.unwrap();
    assert!(audit.iter().any(|entry| entry.action == "create_profile"));
    assert!(audit.iter().any(|entry| entry.action == "update_profile"));
    assert!(
        audit
            .iter()
            .any(|entry| entry.action == "remove_profile_rule")
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn two_user_browser_journey_fences_admin_data_and_personal_credentials() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, admin_token) =
        identity::bootstrap_admin(&database, "admin", "M006 strong admin password passphrase")
            .await
            .unwrap();
    identity::enable_managed(&database).await.unwrap();

    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = None;
    config.dashboard.enabled = true;
    config.dashboard.public = true;
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "admin-ui".into(), 1)
            .await
            .expect("runtime generation");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfer"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("tasks install");
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let origin = format!("http://{address}");

    let login_form = "username=admin&password=M006+strong+admin+password+passphrase";
    let admin_login = http_request(
        address,
        "POST",
        "/login",
        login_form,
        None,
        Some(&origin),
        None,
    )
    .await;
    assert!(admin_login.starts_with("HTTP/1.1 303"), "{admin_login}");
    let (_, csrf, admin_cookies) = session_pair(&admin_login);
    let personal_admin_token_to_users = http_request(
        address,
        "GET",
        "/users",
        "",
        None,
        None,
        Some(&admin_token.token),
    )
    .await;
    assert!(
        personal_admin_token_to_users.starts_with("HTTP/1.1 401"),
        "{personal_admin_token_to_users}"
    );

    let profile_form = format!("profile_id=team&name=Team&default_effect=deny&csrf={csrf}");
    let profile_created = http_request(
        address,
        "POST",
        "/admin/profiles/create",
        &profile_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        profile_created.starts_with("HTTP/1.1 303"),
        "{profile_created}"
    );

    let create_form = format!(
        "username=member&password=M006+temporary+member+password&profile_id=team&csrf={csrf}"
    );
    let created = http_request(
        address,
        "POST",
        "/admin/users/create",
        &create_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(created.starts_with("HTTP/1.1 303"), "{created}");
    let user_page = http_request(
        address,
        "GET",
        "/users",
        "",
        Some(&admin_cookies),
        None,
        None,
    )
    .await;
    assert!(user_page.starts_with("HTTP/1.1 200"), "{user_page}");
    assert!(user_page.contains("member"), "{user_page}");
    // M008: the budget form distinguishes hard versus estimated enforcement
    // instead of labeling every dimension an estimate.
    assert!(user_page.contains("name=\"enforcement\""), "{user_page}");
    assert!(
        user_page.contains("Hard (prove worst-case bound or deny)"),
        "{user_page}"
    );
    assert!(
        user_page.contains("Estimated (approximate hold)"),
        "{user_page}"
    );

    // M008: hard-mode budget rules are manageable through the admin form,
    // invalid modes are rejected, and members see the mode on their page.
    let budget_member_id = identity::list_users(&database)
        .await
        .unwrap()
        .into_iter()
        .find(|user| user.username == "member")
        .expect("created member is listed")
        .id;
    let hard_budget_form = format!(
        "user_id={budget_member_id}&model_id=&window=day&max_requests=&max_tokens=5000&max_microdollars=&max_concurrent=&enforcement=hard&csrf={csrf}"
    );
    let hard_budget_saved = http_request(
        address,
        "POST",
        "/admin/users/budget",
        &hard_budget_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        hard_budget_saved.starts_with("HTTP/1.1 303"),
        "{hard_budget_saved}"
    );
    let invalid_budget_form = format!(
        "user_id={budget_member_id}&model_id=&window=day&max_requests=&max_tokens=5000&max_microdollars=&max_concurrent=&enforcement=guaranteed&csrf={csrf}"
    );
    let invalid_budget_rejected = http_request(
        address,
        "POST",
        "/admin/users/budget",
        &invalid_budget_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        invalid_budget_rejected.starts_with("HTTP/1.1 400"),
        "{invalid_budget_rejected}"
    );

    // M009: instance ceilings are managed on the same admin page, visible in
    // the global section, and fenced from member credentials.
    assert!(
        user_page.contains("Instance budget ceilings"),
        "{user_page}"
    );
    let instance_form = format!(
        "model_id=&window=day&max_requests=100&max_tokens=&max_microdollars=&max_concurrent=&enforcement=estimated&csrf={csrf}"
    );
    let instance_saved = http_request(
        address,
        "POST",
        "/admin/budget/instance",
        &instance_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        instance_saved.starts_with("HTTP/1.1 303"),
        "{instance_saved}"
    );
    let users_after_instance = http_request(
        address,
        "GET",
        "/users",
        "",
        Some(&admin_cookies),
        None,
        None,
    )
    .await;
    assert!(
        users_after_instance.contains("All models"),
        "{users_after_instance}"
    );
    let instance_remove_form = format!(
        "model_id=&window=day&max_requests=&max_tokens=&max_microdollars=&max_concurrent=&csrf={csrf}"
    );
    let instance_removed = http_request(
        address,
        "POST",
        "/admin/budget/instance/remove",
        &instance_remove_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        instance_removed.starts_with("HTTP/1.1 303"),
        "{instance_removed}"
    );

    let missing_csrf =
        "username=intruder&password=M006+temporary+member+password&profile_id=&csrf=";
    let csrf_rejected = http_request(
        address,
        "POST",
        "/admin/users/create",
        missing_csrf,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(csrf_rejected.starts_with("HTTP/1.1 403"), "{csrf_rejected}");

    let member_login_form = "username=member&password=M006+temporary+member+password";
    let member_login = http_request(
        address,
        "POST",
        "/login",
        member_login_form,
        None,
        Some(&origin),
        None,
    )
    .await;
    assert!(member_login.starts_with("HTTP/1.1 303"), "{member_login}");
    assert!(
        member_login.contains("/account?password=required"),
        "{member_login}"
    );
    let (_, member_csrf, member_cookies) = session_pair(&member_login);
    let forbidden_users = http_request(
        address,
        "GET",
        "/users",
        "",
        Some(&member_cookies),
        None,
        None,
    )
    .await;
    assert!(
        forbidden_users.starts_with("HTTP/1.1 403"),
        "{forbidden_users}"
    );
    let member_admin_post = format!(
        "username=intruder&password=M006+temporary+member+password&profile_id=&csrf={member_csrf}"
    );
    let member_create_forbidden = http_request(
        address,
        "POST",
        "/admin/users/create",
        &member_admin_post,
        Some(&member_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        member_create_forbidden.starts_with("HTTP/1.1 403"),
        "{member_create_forbidden}"
    );
    let member_instance_post = format!(
        "model_id=&window=day&max_requests=1&max_tokens=&max_microdollars=&max_concurrent=&enforcement=estimated&csrf={member_csrf}"
    );
    let member_instance_forbidden = http_request(
        address,
        "POST",
        "/admin/budget/instance",
        &member_instance_post,
        Some(&member_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        member_instance_forbidden.starts_with("HTTP/1.1 403"),
        "{member_instance_forbidden}"
    );

    let change_password = format!(
        "current_password=M006+temporary+member+password&new_password=M006+new+member+password+passphrase&csrf={member_csrf}"
    );
    let changed = http_request(
        address,
        "POST",
        "/account/password",
        &change_password,
        Some(&member_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(changed.starts_with("HTTP/1.1 303"), "{changed}");
    let member_login_form = "username=member&password=M006+new+member+password+passphrase";
    let member_login = http_request(
        address,
        "POST",
        "/login",
        member_login_form,
        None,
        Some(&origin),
        None,
    )
    .await;
    assert!(member_login.starts_with("HTTP/1.1 303"), "{member_login}");
    let (_, member_csrf, member_cookies) = session_pair(&member_login);

    let my_account = http_request(
        address,
        "GET",
        "/my-account",
        "",
        Some(&member_cookies),
        None,
        None,
    )
    .await;
    assert!(my_account.starts_with("HTTP/1.1 200"), "{my_account}");
    assert!(my_account.contains("My accessible models"), "{my_account}");
    assert!(!my_account.contains(&admin.username), "{my_account}");

    let member_usage = http_request(
        address,
        "GET",
        "/usage",
        "",
        Some(&member_cookies),
        None,
        None,
    )
    .await;
    assert!(member_usage.starts_with("HTTP/1.1 200"), "{member_usage}");
    assert!(!member_usage.contains(&admin.username), "{member_usage}");
    assert!(
        member_usage.contains("<td>hard</td>"),
        "member usage page must show the hard enforcement mode: {member_usage}"
    );

    let token_form = format!("label=member+laptop&csrf={member_csrf}");
    let token_created = http_request(
        address,
        "POST",
        "/account/tokens",
        &token_form,
        Some(&member_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(token_created.starts_with("HTTP/1.1 200"), "{token_created}");
    assert!(
        token_created
            .to_ascii_lowercase()
            .contains("cache-control: no-store")
    );
    let token_start = token_created
        .find("epk1_")
        .expect("one-time token appears once in issue response");
    let token = token_created[token_start..]
        .split('<')
        .next()
        .expect("token text is present")
        .to_owned();
    assert!(token.starts_with("epk1_"));
    assert_eq!(token_created.matches(&token).count(), 1);
    let member_models =
        http_request(address, "GET", "/v1/models", "", None, None, Some(&token)).await;
    assert!(member_models.starts_with("HTTP/1.1 200"), "{member_models}");
    let token_to_admin = http_request(address, "GET", "/users", "", None, None, Some(&token)).await;
    assert!(
        token_to_admin.starts_with("HTTP/1.1 401"),
        "{token_to_admin}"
    );
    let member_id = identity::list_users(&database)
        .await
        .unwrap()
        .into_iter()
        .find(|user| user.username == "member")
        .expect("created member is listed")
        .id;
    let member_credential = identity::list_user_credentials(&database, &member_id)
        .await
        .unwrap()
        .remove(0);
    let revoke_form = format!("credential_id={}&csrf={member_csrf}", member_credential.id);
    let revoked = http_request(
        address,
        "POST",
        "/account/tokens/revoke",
        &revoke_form,
        Some(&member_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(revoked.starts_with("HTTP/1.1 303"), "{revoked}");
    let revoked_token =
        http_request(address, "GET", "/v1/models", "", None, None, Some(&token)).await;
    assert!(revoked_token.starts_with("HTTP/1.1 401"), "{revoked_token}");
    let refreshed_account = http_request(
        address,
        "GET",
        "/my-account",
        "",
        Some(&member_cookies),
        None,
        None,
    )
    .await;
    assert!(!refreshed_account.contains(&token), "{refreshed_account}");

    let audit = http_request(
        address,
        "GET",
        "/admin/audit",
        "",
        Some(&admin_cookies),
        None,
        None,
    )
    .await;
    assert!(audit.starts_with("HTTP/1.1 200"), "{audit}");
    assert!(audit.contains("create_user"), "{audit}");
    assert!(audit.contains("assign_profile"), "{audit}");
    assert!(audit.contains("issue_token"), "{audit}");

    assert!(matches!(
        identity::set_user_enabled(&database, &admin.id, &admin.id, false).await,
        Err(identity::IdentityError::LastAdminProtected)
    ));
    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(10), task)
        .await
        .expect("server shutdown completes")
        .unwrap()
        .unwrap();
    database.close().await.unwrap();
}
