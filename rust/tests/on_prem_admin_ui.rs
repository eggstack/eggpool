use std::{sync::Arc, time::Duration};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    model_policy::ModelPolicyService,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ServerRuntimeHandle, ShutdownReason},
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
    request_with_provenance(address, method, path, body, cookies, origin, None, bearer).await
}

/// A same-origin browser form POST sends `Origin` and `Referer` together, which
/// is the header set the raw helper above never produces.
#[allow(clippy::too_many_arguments)]
async fn browser_form_post(
    address: std::net::SocketAddr,
    path: &str,
    body: &str,
    cookies: Option<&str>,
    origin: &str,
    referer: &str,
) -> String {
    request_with_provenance(
        address,
        "POST",
        path,
        body,
        cookies,
        Some(origin),
        Some(referer),
        None,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn request_with_provenance(
    address: std::net::SocketAddr,
    method: &str,
    path: &str,
    body: &str,
    cookies: Option<&str>,
    origin: Option<&str>,
    referer: Option<&str>,
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
    if let Some(referer) = referer {
        request.push_str(&format!("Referer: {referer}\r\n"));
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
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();

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

    let default_form = format!("profile_id=team&csrf={csrf}");
    let default_saved = http_request(
        address,
        "POST",
        "/admin/profiles/default",
        &default_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(default_saved.starts_with("HTTP/1.1 303"), "{default_saved}");
    let profiles_page = http_request(
        address,
        "GET",
        "/profiles",
        "",
        Some(&admin_cookies),
        None,
        None,
    )
    .await;
    assert!(
        profiles_page.contains("Default for newly created members"),
        "{profiles_page}"
    );
    assert!(
        profiles_page.contains("Clear default for future members"),
        "{profiles_page}"
    );

    let create_form =
        format!("username=member&password=M006+temporary+member+password&profile_id=&csrf={csrf}");
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
    let assigned_default: bool = database
        .call(|connection| {
            connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM user_profile_assignments a JOIN users u ON u.id = a.user_id WHERE u.username = 'member' AND a.profile_id = 'team')",
                [],
                |row| row.get(0),
            )
        })
        .await
        .unwrap();
    assert!(
        assigned_default,
        "browser creation applies the selected instance default"
    );
    let explicit_none_form = format!(
        "username=without-profile&password=M006+temporary+member+password&profile_id=__none&csrf={csrf}"
    );
    let explicit_none_created = http_request(
        address,
        "POST",
        "/admin/users/create",
        &explicit_none_form,
        Some(&admin_cookies),
        Some(&origin),
        None,
    )
    .await;
    assert!(
        explicit_none_created.starts_with("HTTP/1.1 303"),
        "{explicit_none_created}"
    );
    let explicitly_unassigned: bool = database
        .call(|connection| {
            connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM user_profile_assignments a JOIN users u ON u.id = a.user_id WHERE u.username = 'without-profile')",
                [],
                |row| row.get(0),
            )
        })
        .await
        .unwrap();
    assert!(
        !explicitly_unassigned,
        "explicit no-profile choice bypasses default"
    );
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

/// Start the managed dashboard on an ephemeral loopback port.
async fn managed_server(
    database: &Database,
) -> (
    std::net::SocketAddr,
    ServerRuntimeHandle,
    tokio::task::JoinHandle<()>,
) {
    managed_server_with_proxies(database, Vec::new()).await
}

/// Start the managed dashboard with a trusted-proxy list.
async fn managed_server_with_proxies(
    database: &Database,
    trusted_proxies: Vec<String>,
) -> (
    std::net::SocketAddr,
    ServerRuntimeHandle,
    tokio::task::JoinHandle<()>,
) {
    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = None;
    config.dashboard.enabled = true;
    config.dashboard.public = true;
    config.security.trusted_proxies = trusted_proxies;
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "browser-signin".into(), 1)
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
    let task = tokio::spawn(async move {
        let _ = runtime.serve_listener(listener).await;
    });
    (address, handle, task)
}

#[tokio::test]
async fn browser_sign_in_accepts_the_origin_and_referer_pair_a_real_browser_sends() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) =
        identity::bootstrap_admin(&database, "admin", "M011 strong admin password passphrase")
            .await
            .unwrap();
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    let (address, handle, task) = managed_server(&database).await;
    let origin = format!("http://{address}");
    let form = "username=admin&password=M011+strong+admin+password+passphrase";

    // Chrome sends both headers on a same-origin form POST; rejecting that pair
    // would make the admin and member sign-in screens unusable in any browser.
    let signed_in = browser_form_post(
        address,
        "/login",
        form,
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    assert!(signed_in.contains("eggpool_session="), "{signed_in}");
    let (_, _csrf, cookies) = session_pair(&signed_in);

    let users = http_request(address, "GET", "/users", "", Some(&cookies), None, None).await;
    assert!(users.starts_with("HTTP/1.1 200"), "{users}");

    // The same pair must still fail closed when the referer is cross-site.
    let refused = browser_form_post(
        address,
        "/login",
        form,
        None,
        &origin,
        "https://attacker.test/login",
    )
    .await;
    assert!(refused.starts_with("HTTP/1.1 403"), "{refused}");
    assert!(!refused.contains("eggpool_session="), "{refused}");

    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(10), task)
        .await
        .expect("server shutdown completes")
        .expect("server task joins");
    database.close().await.unwrap();
}

const ADMIN_PASSWORD: &str = "M024 strong admin password passphrase";
const MEMBER_PASSWORD: &str = "M024 member password passphrase";
const MEMBER_TEMPORARY_PASSWORD: &str = "M024 temporary member password";

/// The full `Set-Cookie` header for one cookie, so the attributes can be
/// asserted rather than only the value.
fn set_cookie_header(response: &str, name: &str) -> String {
    response
        .lines()
        .find_map(|line| {
            let (header, value) = line.split_once(':')?;
            if !header.eq_ignore_ascii_case("set-cookie") {
                return None;
            }
            value
                .trim()
                .strip_prefix(&format!("{name}="))
                .map(str::to_owned)
        })
        .expect("cookie is set")
}

/// One response header value, for the redirect targets.
fn response_header(response: &str, name: &str) -> Option<String> {
    response.lines().find_map(|line| {
        let (header, value) = line.split_once(':')?;
        header
            .eq_ignore_ascii_case(name)
            .then(|| value.trim().to_owned())
    })
}

/// The value of the page's hidden CSRF field.
fn hidden_csrf(body: &str) -> String {
    let marker = "name=\"csrf\" value=\"";
    let start = body
        .find(marker)
        .expect("the page carries a hidden csrf field")
        + marker.len();
    let end = start + body[start..].find('"').expect("the csrf field is closed");
    body[start..end].to_owned()
}

/// A managed instance with an administrator and a member.
///
/// `forced_change` keeps the member's forced password change, so the sign-in
/// lands on the forced-change redirect rather than on the member landing
/// page.
async fn managed_member_instance(
    forced_change: bool,
) -> (
    tempfile::TempDir,
    Database,
    std::net::SocketAddr,
    ServerRuntimeHandle,
    tokio::task::JoinHandle<()>,
) {
    let (directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) = identity::bootstrap_admin(&database, "admin", ADMIN_PASSWORD)
        .await
        .unwrap();
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    identity::create_member_without_profile(
        &database,
        &admin.id,
        "member",
        MEMBER_TEMPORARY_PASSWORD,
    )
    .await
    .unwrap();
    if !forced_change {
        identity::reset_password(&database, "member", MEMBER_PASSWORD)
            .await
            .unwrap();
    }
    let (address, handle, task) = managed_server(&database).await;
    (directory, database, address, handle, task)
}

async fn shutdown(handle: ServerRuntimeHandle, task: tokio::task::JoinHandle<()>) {
    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(10), task)
        .await
        .expect("server shutdown completes")
        .expect("server task joins");
}

/// The account and the client address the sign-in throttle test spends.
///
/// The throttle table is process-local, so the test claims an account and a
/// client address of its own and keeps its budget out of the other tests in
/// this binary.
const THROTTLE_ACCOUNT: &str = "throttle-account";
const THROTTLE_PASSWORD: &str = "M024 throttle account password";
const THROTTLE_CLIENT: &str = "198.51.100.77";

/// A managed instance that trusts the loopback listener as a proxy, so a
/// request can name its own client address in `X-Forwarded-For`.
async fn managed_throttle_instance() -> (
    tempfile::TempDir,
    Database,
    std::net::SocketAddr,
    ServerRuntimeHandle,
    tokio::task::JoinHandle<()>,
) {
    let (directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) = identity::bootstrap_admin(&database, "admin", ADMIN_PASSWORD)
        .await
        .unwrap();
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    identity::create_member_without_profile(
        &database,
        &admin.id,
        THROTTLE_ACCOUNT,
        THROTTLE_PASSWORD,
    )
    .await
    .unwrap();
    identity::reset_password(&database, THROTTLE_ACCOUNT, THROTTLE_PASSWORD)
        .await
        .unwrap();
    let (address, handle, task) =
        managed_server_with_proxies(&database, vec!["127.0.0.1".to_owned()]).await;
    (directory, database, address, handle, task)
}

/// A form POST that names its client address in `X-Forwarded-For`.
async fn throttle_form_post(
    address: std::net::SocketAddr,
    path: &str,
    body: &str,
    origin: &str,
    referer: &str,
) -> String {
    let mut stream = TcpStream::connect(address).await.expect("connect");
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nOrigin: {origin}\r\nReferer: {referer}\r\nX-Forwarded-For: {THROTTLE_CLIENT}\r\n\r\n{body}",
        body.len()
    );
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

#[tokio::test]
async fn browser_sign_in_sets_both_cookies_with_the_same_attributes() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) = identity::bootstrap_admin(&database, "admin", ADMIN_PASSWORD)
        .await
        .unwrap();
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    let (address, handle, task) = managed_server(&database).await;
    let origin = format!("http://{address}");

    let signed_in = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");

    // The CSRF cookie is delivered on exactly the requests that carry the
    // session cookie, so both carry the same attributes.
    for name in ["eggpool_session", "eggpool_csrf"] {
        let header = set_cookie_header(&signed_in, name);
        assert!(header.contains("Path=/"), "{name}: {header}");
        assert!(header.contains("HttpOnly"), "{name}: {header}");
        assert!(header.contains("SameSite=Lax"), "{name}: {header}");
        assert!(header.contains("Max-Age=604800"), "{name}: {header}");
        assert!(!header.contains("SameSite=Strict"), "{name}: {header}");
        // The loopback listener is not secure, so `Secure` is not added.
        assert!(!header.contains("Secure"), "{name}: {header}");
    }

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn every_sign_in_failure_is_a_no_store_html_page_with_an_alert() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");

    let refused = browser_form_post(
        address,
        "/login",
        "username=admin&password=not-the-admin-password",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(refused.starts_with("HTTP/1.1 401"), "{refused}");
    assert!(refused.contains("text/html"), "{refused}");
    assert!(refused.contains("role=\"alert\""), "{refused}");
    assert!(
        refused
            .to_ascii_lowercase()
            .contains("cache-control: no-store"),
        "{refused}"
    );

    let cross_origin = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        "http://attacker.test",
        "http://attacker.test/login",
    )
    .await;
    assert!(cross_origin.starts_with("HTTP/1.1 403"), "{cross_origin}");
    assert!(cross_origin.contains("text/html"), "{cross_origin}");
    assert!(cross_origin.contains("role=\"alert\""), "{cross_origin}");
    assert!(
        cross_origin
            .to_ascii_lowercase()
            .contains("cache-control: no-store"),
        "{cross_origin}"
    );
    assert!(
        cross_origin.contains("Cross-origin sign-in is not allowed."),
        "{cross_origin}"
    );

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn a_session_without_the_csrf_cookie_renders_an_empty_field_and_the_mutation_is_refused() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    let (session, _csrf, _cookies) = session_pair(&signed_in);
    let session_only = format!("eggpool_session={session}");

    // The rendered field is derived from the cookie alone, so a request that
    // carries the session but not the CSRF cookie renders an empty field.
    let account = http_request(
        address,
        "GET",
        "/account",
        "",
        Some(&session_only),
        None,
        None,
    )
    .await;
    assert!(account.starts_with("HTTP/1.1 200"), "{account}");
    assert_eq!(hidden_csrf(&account), "");

    let refused = browser_form_post(
        address,
        "/account/password",
        "current_password=M024+strong+admin+password+passphrase&new_password=M024+new+admin+password+passphrase&csrf=",
        Some(&session_only),
        &origin,
        &format!("{origin}/account"),
    )
    .await;
    assert!(refused.starts_with("HTTP/1.1 403"), "{refused}");
    assert!(refused.contains("Invalid request token."), "{refused}");

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn a_session_with_both_cookies_renders_a_verifying_field_and_the_mutation_succeeds() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    let (_session, csrf, cookies) = session_pair(&signed_in);

    let account = http_request(address, "GET", "/account", "", Some(&cookies), None, None).await;
    assert!(account.starts_with("HTTP/1.1 200"), "{account}");
    let field = hidden_csrf(&account);
    assert!(!field.is_empty());
    // The rendered field is the token the session's CSRF digest verifies.
    assert!(field == csrf);

    let changed = browser_form_post(
        address,
        "/account/password",
        &format!("current_password=M024+strong+admin+password+passphrase&new_password=M024+new+admin+password+passphrase&csrf={field}"),
        Some(&cookies),
        &origin,
        &format!("{origin}/account"),
    )
    .await;
    assert!(changed.starts_with("HTTP/1.1 303"), "{changed}");

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn a_successful_sign_in_clears_the_account_failure_counter() {
    let (_directory, database, address, handle, task) = managed_throttle_instance().await;
    let origin = format!("http://{address}");
    let wrong = format!("username={THROTTLE_ACCOUNT}&password=not-the-account-password");
    let right = format!("username={THROTTLE_ACCOUNT}&password=M024+throttle+account+password");

    // Nine failures spend nine of the account's ten attempts.
    for _ in 0..9 {
        let refused = throttle_form_post(
            address,
            "/login",
            &wrong,
            &origin,
            &format!("{origin}/login"),
        )
        .await;
        assert!(refused.starts_with("HTTP/1.1 401"), "{refused}");
    }

    // The successful sign-in clears the account counter.
    let signed_in = throttle_form_post(
        address,
        "/login",
        &right,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");

    // Nine more failures fit inside the account's budget again. Without the
    // clear the counter would already stand at ten and these attempts would
    // be refused as throttled.
    for _ in 0..9 {
        let refused = throttle_form_post(
            address,
            "/login",
            &wrong,
            &origin,
            &format!("{origin}/login"),
        )
        .await;
        assert!(refused.starts_with("HTTP/1.1 401"), "{refused}");
    }

    // The tenth attempt is still inside the budget.
    let allowed = throttle_form_post(
        address,
        "/login",
        &wrong,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(allowed.starts_with("HTTP/1.1 401"), "{allowed}");

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn a_cross_site_unsafe_request_is_rejected_by_the_origin_check_before_the_handler_runs() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    let (_session, _csrf, cookies) = session_pair(&signed_in);

    // A cross-site form POST that carries the session but no CSRF cookie is
    // refused by the server-side origin check. The handler's own token
    // rejection is a different answer, so the two are distinguishable.
    let refused = browser_form_post(
        address,
        "/account/password",
        "current_password=M024+strong+admin+password+passphrase&new_password=M024+new+admin+password+passphrase&csrf=",
        Some(&cookies),
        "http://attacker.test",
        "http://attacker.test/attack",
    )
    .await;
    assert!(refused.starts_with("HTTP/1.1 403"), "{refused}");
    assert!(
        refused.contains("Cross-origin or insecure request rejected."),
        "{refused}"
    );
    assert!(!refused.contains("Invalid request token."), "{refused}");

    // A cross-site form POST that carries no cookie at all is refused by the
    // sign-in surface's own origin check, again before any credential check.
    let anonymous = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        "http://attacker.test",
        "http://attacker.test/attack",
    )
    .await;
    assert!(anonymous.starts_with("HTTP/1.1 403"), "{anonymous}");
    assert!(
        anonymous.contains("Cross-origin sign-in is not allowed."),
        "{anonymous}"
    );
    assert!(!anonymous.contains("Invalid request token."), "{anonymous}");
    assert!(!anonymous.contains("eggpool_session="), "{anonymous}");

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn a_member_lands_on_the_member_landing_page_and_reaches_the_member_surface() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=member&password=M024+member+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    assert_eq!(
        response_header(&signed_in, "location").as_deref(),
        Some("/account/landing")
    );
    let (_session, _csrf, member_cookies) = session_pair(&signed_in);

    let landing = http_request(
        address,
        "GET",
        "/account/landing",
        "",
        Some(&member_cookies),
        None,
        None,
    )
    .await;
    assert!(landing.starts_with("HTTP/1.1 200"), "{landing}");
    for destination in ["/my-account", "/my-account/models", "/usage"] {
        assert!(
            landing.contains(&format!("href=\"{destination}\"")),
            "{landing}"
        );
    }
    // The landing page exposes no administrative route.
    for administrative in ["href=\"/users\"", "href=\"/profiles\"", "href=\"/admin/"] {
        assert!(!landing.contains(administrative), "{landing}");
    }

    // Every member destination is reachable from the landing page.
    for path in ["/my-account", "/my-account/models", "/usage"] {
        let page = http_request(address, "GET", path, "", Some(&member_cookies), None, None).await;
        assert!(page.starts_with("HTTP/1.1 200"), "{path}: {page}");
    }

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn an_administrator_still_lands_on_the_account_page_which_links_the_dashboard() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=admin&password=M024+strong+admin+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    assert_eq!(
        response_header(&signed_in, "location").as_deref(),
        Some("/account")
    );
    let (_session, _csrf, admin_cookies) = session_pair(&signed_in);

    let account = http_request(
        address,
        "GET",
        "/account",
        "",
        Some(&admin_cookies),
        None,
        None,
    )
    .await;
    assert!(account.starts_with("HTTP/1.1 200"), "{account}");
    assert!(account.contains("href=\"/\""), "{account}");

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn a_member_refused_an_administrative_page_gets_an_actionable_html_page() {
    let (_directory, database, address, handle, task) = managed_member_instance(false).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=member&password=M024+member+password+passphrase",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    let (_session, _csrf, member_cookies) = session_pair(&signed_in);

    for path in [
        "/users",
        "/profiles",
        "/accounts",
        "/models",
        "/admin/audit",
        "/admin/users/create",
    ] {
        let refused =
            http_request(address, "GET", path, "", Some(&member_cookies), None, None).await;
        assert!(refused.starts_with("HTTP/1.1 403"), "{path}: {refused}");
        assert!(refused.contains("text/html"), "{path}: {refused}");
        assert!(
            refused.contains("Administrator access required."),
            "{path}: {refused}"
        );
        assert!(
            refused.contains("href=\"/account/landing\""),
            "{path}: {refused}"
        );
    }

    // The member surface itself is unchanged: those paths stay reachable.
    for path in [
        "/account",
        "/account/landing",
        "/my-account",
        "/my-account/models",
        "/usage",
    ] {
        let page = http_request(address, "GET", path, "", Some(&member_cookies), None, None).await;
        assert!(page.starts_with("HTTP/1.1 200"), "{path}: {page}");
    }

    shutdown(handle, task).await;
    database.close().await.unwrap();
}

#[tokio::test]
async fn an_operator_who_must_change_their_password_is_still_redirected_to_the_account_page() {
    let (_directory, database, address, handle, task) = managed_member_instance(true).await;
    let origin = format!("http://{address}");
    let signed_in = browser_form_post(
        address,
        "/login",
        "username=member&password=M024+temporary+member+password",
        None,
        &origin,
        &format!("{origin}/login"),
    )
    .await;
    assert!(signed_in.starts_with("HTTP/1.1 303"), "{signed_in}");
    assert_eq!(
        response_header(&signed_in, "location").as_deref(),
        Some("/account?password=required")
    );
    let (_session, _csrf, member_cookies) = session_pair(&signed_in);

    // The middleware's forced-change redirect is unchanged: the member is
    // sent to the account page from any other page.
    let redirected = http_request(
        address,
        "GET",
        "/my-account",
        "",
        Some(&member_cookies),
        None,
        None,
    )
    .await;
    assert!(redirected.starts_with("HTTP/1.1 303"), "{redirected}");
    assert_eq!(
        response_header(&redirected, "location").as_deref(),
        Some("/account?password=required")
    );

    shutdown(handle, task).await;
    database.close().await.unwrap();
}
