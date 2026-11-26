use std::{
    fs, io,
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener},
    path::PathBuf,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    agents::AgentTunnelSet,
    config::{
        AccountAuthMode, AccountConfig, AgentAccountConfig, AgentTlsConfig, ProviderConfig,
        ProviderStaticModelConfig,
    },
    db::{
        AccountConfig as DbAccountConfig, AccountRepository, Database, DatabaseConfig,
        MigrationRunner,
    },
    identity,
    operations::{
        control::{self, ControlRequest, ControlResponse, ControlServerHandle},
        paths::RuntimePaths,
    },
    personal_upstream::{self, GrantEffect},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ServerRuntimeHandle, ShutdownReason},
};
use eggress_protocol_reverse::{
    client::{ReverseClient, ReverseClientConfig},
    tls::ReverseClientTlsConfig,
};
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener as TokioTcpListener, TcpStream},
    sync::{mpsc as async_mpsc, oneshot},
    task::JoinHandle,
};

#[path = "common/agent_ticket.rs"]
mod agent_ticket;

use agent_ticket::{SERVER_NAME, TestTicketProducer, TicketVariant};
use zeroize::{Zeroize, Zeroizing};

const PROVIDER: &str = "opencode-go";
const ENROLLMENT_MODEL: &str = "agent-enrollment-model";
const MEMBER_PASSWORD: &str = "M002 temporary member password";
const ENROLLMENT_USERNAME: &str = "enroll";

#[derive(Clone)]
struct Browser {
    user_id: String,
    cookies: String,
    csrf: String,
}

struct TestApp {
    _directory: tempfile::TempDir,
    database: Database,
    config: Config,
    config_path: PathBuf,
    env_path: PathBuf,
    control_port: u16,
    producer: TestTicketProducer,
    member: Browser,
    other_member: Browser,
    admin: Browser,
    api_token: String,
    fail_reload: Arc<AtomicBool>,
    address: SocketAddr,
    origin: String,
    process: ProcessRuntime,
    handle: ServerRuntimeHandle,
    server_task: JoinHandle<Result<eggpool::server::ShutdownReport, eggpool::server::ServerError>>,
    control_server: Option<ControlServerHandle>,
}

impl TestApp {
    async fn start(
        with_control_socket: bool,
        fail_reload: bool,
        agents_enabled: bool,
        existing_agent: bool,
    ) -> Self {
        let directory = tempfile::tempdir().expect("isolated enrollment directory");
        let config_path = directory.path().join("config.toml");
        let env_path = directory.path().join(".env");
        let producer = TestTicketProducer::new();
        let fail_reload_state = Arc::new(AtomicBool::new(fail_reload));
        let ca_path = directory.path().join("agent-ca.pem");
        let cert_path = directory.path().join("agent-server.pem");
        let key_path = directory.path().join("agent-server-key.pem");
        fs::write(&ca_path, producer.ca_pem()).expect("write test CA");
        fs::write(&cert_path, producer.server_cert_pem()).expect("write test server certificate");
        fs::write(&key_path, producer.server_key_pem()).expect("write test server key");

        let control_port = unused_loopback_port();
        let env_var = personal_upstream::agent_env_var(PROVIDER, "existing-agent");
        let retained_secret = "retained-agent-credential-for-disabled-gate";
        let mut config = Config::default();
        config.server.host = "127.0.0.1".to_owned();
        config.server.api_key = None;
        config.dashboard.enabled = true;
        config.dashboard.public = true;
        config.models.startup_refresh = false;
        config.agents.enabled = agents_enabled;
        config.agents.control_bind_host = "127.0.0.1".to_owned();
        config.agents.advertised_control_host = SERVER_NAME.to_owned();
        config.agents.control_port_min = control_port;
        config.agents.control_port_max = control_port;
        config.agents.single_host = true;
        config.agents.tls = Some(AgentTlsConfig {
            cert_path: cert_path.display().to_string(),
            key_path: key_path.display().to_string(),
            server_ca_path: Some(ca_path.display().to_string()),
            ..AgentTlsConfig::default()
        });
        let existing_accounts = if existing_agent {
            let external_port = loop {
                let candidate = unused_loopback_port();
                if candidate != control_port {
                    break candidate;
                }
            };
            let account_env = personal_upstream::agent_env_var(PROVIDER, "existing-agent");
            fs::write(&env_path, format!("{account_env}={retained_secret}\n"))
                .expect("write pre-existing credential");
            vec![AccountConfig {
                name: "existing-agent".to_owned(),
                auth_mode: AccountAuthMode::Agent,
                api_key_env: account_env.clone(),
                base_url: Some(format!("http://127.0.0.1:{external_port}/v1")),
                agent: Some(AgentAccountConfig {
                    control_bind: format!("127.0.0.1:{control_port}"),
                    control_endpoint: Some(format!("{}:{control_port}", SERVER_NAME)),
                    server_name: Some(SERVER_NAME.to_owned()),
                    external_bind: format!("127.0.0.1:{external_port}"),
                    transport_secret_env: account_env,
                    tls: config.agents.tls.clone(),
                    ..AgentAccountConfig::default()
                }),
                ..AccountConfig::default()
            }]
        } else {
            Vec::new()
        };
        config.providers.insert(
            PROVIDER.to_owned(),
            ProviderConfig {
                id: PROVIDER.to_owned(),
                base_url: "https://opencode.ai/zen/go/v1".to_owned(),
                protocols: vec!["openai".to_owned()],
                accounts: existing_accounts,
                static_models: vec![ProviderStaticModelConfig {
                    id: ENROLLMENT_MODEL.to_owned(),
                    protocol: Some("openai".to_owned()),
                    ..ProviderStaticModelConfig::default()
                }],
                ..ProviderConfig::default()
            },
        );
        let config_bytes = toml::to_string(&config)
            .expect("config serializes")
            .into_bytes();
        fs::write(&config_path, &config_bytes).expect("write isolated config");
        if !existing_agent {
            assert!(!env_path.exists(), "fresh enrollment starts without .env");
        }

        let database = Database::open(DatabaseConfig {
            path: directory
                .path()
                .join("eggpool.sqlite3")
                .display()
                .to_string(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("database opens");
        MigrationRunner::new(&database)
            .run()
            .await
            .expect("database migrates");
        identity::mark_fresh_uninitialized(&database)
            .await
            .expect("initialize managed identity");
        let (admin, _) = identity::bootstrap_admin(
            &database,
            "m002-admin",
            "M002 administrator fixture password",
        )
        .await
        .expect("create bootstrap administrator");
        identity::enable_managed(&database, &admin.id)
            .await
            .expect("enable managed identities");
        let member = identity::create_member(&database, &admin.id, "m002-member", MEMBER_PASSWORD)
            .await
            .expect("create first managed member");
        let other =
            identity::create_member(&database, &admin.id, "m002-other-member", MEMBER_PASSWORD)
                .await
                .expect("create second managed member");
        for user_id in [&member.id, &other.id] {
            let user_id = user_id.clone();
            database
                .call(move |connection| {
                    connection.execute(
                        "UPDATE users SET must_change_password = 0 WHERE id = ?1",
                        [user_id.as_str()],
                    )?;
                    Ok(())
                })
                .await
                .expect("fixture members may sign in directly");
        }
        personal_upstream::set_instance_grant(&database, &admin.id, GrantEffect::Allow)
            .await
            .expect("grant member self-service");
        let model_policy = eggpool::model_policy::ModelPolicyService::new(database.clone());
        model_policy
            .set_user_override(&member.id, ENROLLMENT_MODEL, "allow", &admin.id)
            .await
            .expect("allow enrollment fixture model");
        database
            .call(|connection| {
                connection.execute(
                    "INSERT INTO models(model_id, protocol, provider_id, resolution_status)
                     VALUES (?1, 'openai', ?2, 'resolved')",
                    (ENROLLMENT_MODEL, PROVIDER),
                )?;
                Ok(())
            })
            .await
            .expect("seed enrollment inference model");
        let member_api_token =
            identity::issue_api_token(&database, "m002-member", "agent-enrollment-inference")
                .await
                .expect("issue member inference token");
        if existing_agent {
            AccountRepository::new(&database)
                .sync_from_config(vec![DbAccountConfig {
                    name: "existing-agent".to_owned(),
                    api_key_env: env_var,
                    enabled: true,
                    weight: 1.0,
                    provider_id: PROVIDER.to_owned(),
                }])
                .await
                .expect("sync retained Agent account");
        }

        let process =
            ProcessRuntime::with_config_path_and_config(database.clone(), &config_path, &config)
                .expect("process runtime uses isolated config path");
        let candidate = RuntimeGenerationFactory::prepare(
            &process,
            config.clone(),
            "agent-enrollment-test".to_owned(),
            1,
        )
        .await
        .expect("initial runtime generation prepares");
        let manager = Arc::new(RuntimeManager::new(
            candidate.transfer().expect("candidate transfers"),
        ));
        process
            .install_initial_tasks((*manager).clone(), &config)
            .await
            .expect("runtime tasks install");

        let control_server = if with_control_socket {
            let reload_service = process.reload_service((*manager).clone());
            let path = config_path.clone();
            let fail_reload_for_control = Arc::clone(&fail_reload_state);
            Some(
                control::start(
                    RuntimePaths::resolve().control_socket,
                    move |request: ControlRequest| {
                        let reload_service = reload_service.clone();
                        let path = path.clone();
                        let fail_reload = Arc::clone(&fail_reload_for_control);
                        async move {
                            if fail_reload.load(Ordering::Acquire) {
                                ControlResponse::error(
                                    request.request_id,
                                    "test_failure",
                                    "test reload failure",
                                )
                            } else {
                                let result = reload_service
                                    .reload_path(path, request.validated_digest)
                                    .await;
                                ControlResponse::from_reload(request.request_id, result)
                            }
                        }
                    },
                )
                .await
                .expect("isolated reload control socket starts"),
            )
        } else {
            None
        };

        let runtime = ServerRuntime::new(process.clone(), manager, config.clone());
        let handle = runtime.handle();
        let listener = TokioTcpListener::bind("127.0.0.1:0")
            .await
            .expect("HTTP listener binds");
        let address = listener.local_addr().expect("HTTP listener address");
        let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });
        let origin = format!("http://{address}");
        let member = sign_in(address, &origin, "m002-member", &member.id).await;
        let other_member = sign_in(address, &origin, "m002-other-member", &other.id).await;
        let admin_login = sign_in_with_password(
            address,
            &origin,
            "m002-admin",
            &admin.id,
            "M002 administrator fixture password",
        )
        .await;
        Self {
            _directory: directory,
            database,
            config,
            config_path,
            env_path,
            control_port,
            producer,
            member: Browser {
                user_id: member.user_id,
                cookies: member.cookies,
                csrf: member.csrf,
            },
            other_member: Browser {
                user_id: other_member.user_id,
                cookies: other_member.cookies,
                csrf: other_member.csrf,
            },
            admin: Browser {
                user_id: admin_login.user_id,
                cookies: admin_login.cookies,
                csrf: admin_login.csrf,
            },
            api_token: member_api_token.token,
            fail_reload: fail_reload_state,
            address,
            origin,
            process,
            handle,
            server_task,
            control_server,
        }
    }

    fn ticket(&self, variant: TicketVariant) -> String {
        self.producer
            .ticket(unix_time(), self.control_port, variant)
    }

    async fn enroll(&self, browser: &Browser, account_name: &str, ticket: &str) -> String {
        let body = format!(
            "provider={PROVIDER}&account_name={account_name}&ticket={ticket}&csrf={}",
            browser.csrf
        );
        browser_form_post(
            self.address,
            "/my-account/agents/enroll",
            &body,
            &browser.cookies,
            &self.origin,
        )
        .await
    }

    async fn revoke(&self, browser: &Browser, account_name: &str) -> String {
        let body = format!(
            "provider={PROVIDER}&account_name={account_name}&csrf={}",
            browser.csrf
        );
        browser_form_post(
            self.address,
            "/my-account/agents/revoke",
            &body,
            &browser.cookies,
            &self.origin,
        )
        .await
    }

    async fn shutdown(self) {
        self.handle.request_shutdown(ShutdownReason::Requested);
        tokio::time::timeout(Duration::from_secs(10), self.server_task)
            .await
            .expect("HTTP server shuts down")
            .expect("HTTP server task joins")
            .expect("HTTP server exits cleanly");
        if let Some(control_server) = self.control_server {
            control_server.close().await.expect("control server closes");
        }
        self.process.task_supervisor().shutdown().await;
        self.database.close().await.expect("test database closes");
    }
}

struct Login {
    user_id: String,
    cookies: String,
    csrf: String,
}

async fn sign_in(address: SocketAddr, origin: &str, username: &str, user_id: &str) -> Login {
    sign_in_with_password(address, origin, username, user_id, MEMBER_PASSWORD).await
}

async fn sign_in_with_password(
    address: SocketAddr,
    origin: &str,
    username: &str,
    user_id: &str,
    password: &str,
) -> Login {
    let body = format!(
        "username={username}&password={}",
        password.replace(' ', "+")
    );
    let response = browser_form_post(address, "/login", &body, "", origin).await;
    assert!(
        response.starts_with("HTTP/1.1 303"),
        "browser login failed: {response}"
    );
    let session = cookie(&response, "eggpool_session");
    let csrf = cookie(&response, "eggpool_csrf");
    Login {
        user_id: user_id.to_owned(),
        cookies: format!("eggpool_session={session}; eggpool_csrf={csrf}"),
        csrf,
    }
}

async fn browser_form_post(
    address: SocketAddr,
    path: &str,
    body: &str,
    cookies: &str,
    origin: &str,
) -> String {
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to EggPool");
    let mut request = format!(
        "POST {path} HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nReferer: {origin}/\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    );
    if !cookies.is_empty() {
        request.push_str(&format!("Cookie: {cookies}\r\n"));
    }
    request.push_str("\r\n");
    request.push_str(body);
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write request");
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .await
        .expect("read response");
    String::from_utf8_lossy(&response).into_owned()
}

fn spawn_enrollment_request(
    app: &TestApp,
    browser: &Browser,
    account_name: &str,
    ticket: &str,
) -> JoinHandle<String> {
    let body = format!(
        "provider={PROVIDER}&account_name={account_name}&ticket={ticket}&csrf={}",
        browser.csrf
    );
    let address = app.address;
    let origin = app.origin.clone();
    let cookies = browser.cookies.clone();
    tokio::spawn(async move {
        browser_form_post(
            address,
            "/my-account/agents/enroll",
            &body,
            &cookies,
            &origin,
        )
        .await
    })
}

async fn authenticated_status(address: SocketAddr, cookies: &str) -> String {
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect for live status request");
    let request = format!(
        "GET /api/status HTTP/1.1\r\nHost: {address}\r\nCookie: {cookies}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write live status request");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(5), stream.read_to_end(&mut response))
        .await
        .expect("live status response completes")
        .expect("read live status response");
    String::from_utf8_lossy(&response).into_owned()
}

async fn browser_get(address: SocketAddr, path: &str, cookies: &str) -> String {
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect for member page request");
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {address}\r\nCookie: {cookies}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write member page request");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(5), stream.read_to_end(&mut response))
        .await
        .expect("member page response completes")
        .expect("read member page response");
    String::from_utf8_lossy(&response).into_owned()
}

fn live_agent_status(response: &str, account_name: &str) -> (String, Option<String>) {
    assert!(
        response.starts_with("HTTP/1.1 200"),
        "live status request failed: {}",
        response.lines().next().unwrap_or("<empty response>")
    );
    let (_, body) = response
        .split_once("\r\n\r\n")
        .expect("live status response has a body");
    let document: serde_json::Value = serde_json::from_str(body).expect("live status is JSON");
    let agent = document["agent_tunnels"]
        .as_array()
        .expect("live status includes Agent tunnel rows")
        .iter()
        .find(|row| row["account_name"].as_str() == Some(account_name))
        .expect("live status includes the expected Agent account");
    let state = agent["state"]
        .as_str()
        .expect("Agent state is a string")
        .to_owned();
    let reason_code = agent["reason_code"].as_str().map(str::to_owned);
    if let Some(reason) = &reason_code {
        assert!(
            reason.chars().count() <= eggpool::operations::status::MAX_REASON_CODE_CHARS,
            "Agent status reason is bounded"
        );
    }
    (state, reason_code)
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
        .unwrap_or_else(|| panic!("{name} cookie missing: {response}"))
}

fn response_summary(response: &str) -> String {
    let status = response.lines().next().unwrap_or("<empty>");
    let error_code = response
        .split_once("\r\n\r\n")
        .and_then(|(_, body)| serde_json::from_str::<serde_json::Value>(body).ok())
        .and_then(|body| {
            body.get("error_code")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_else(|| "none".to_owned());
    format!("{status} ({error_code})")
}

fn unused_loopback_port() -> u16 {
    let listener = TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0))
        .expect("reserve a loopback port");
    listener.local_addr().expect("reserved address").port()
}

fn unix_time() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs().min(i64::MAX as u64) as i64)
}

struct TicketCredentials {
    enrollment: Zeroizing<String>,
    api_key: Zeroizing<String>,
}

#[derive(serde::Deserialize)]
struct EncodedTicketSecrets {
    enroll: String,
    api_key: String,
}

impl Drop for EncodedTicketSecrets {
    fn drop(&mut self) {
        self.enroll.zeroize();
        self.api_key.zeroize();
    }
}

fn ticket_credentials(ticket: &str) -> TicketCredentials {
    let encoded = ticket
        .strip_prefix("epagent1.")
        .expect("producer ticket prefix");
    let payload = Zeroizing::new(
        URL_SAFE_NO_PAD
            .decode(encoded)
            .expect("producer ticket encoding"),
    );
    let mut fields: EncodedTicketSecrets =
        serde_json::from_slice(payload.as_slice()).expect("producer ticket fields");
    TicketCredentials {
        enrollment: Zeroizing::new(std::mem::take(&mut fields.enroll)),
        api_key: Zeroizing::new(std::mem::take(&mut fields.api_key)),
    }
}

fn real_ticket_from_eggpass_cli(app: &TestApp) -> Zeroizing<String> {
    let binary = std::env::var_os("EGGPASS_BIN")
        .expect("set EGGPASS_BIN when running the ignored producer interoperability test");
    let home = app._directory.path().join("eggpass-cli-home");
    fs::create_dir_all(&home).expect("create isolated EggPass CLI home");
    let ca_path = app
        .config
        .agents
        .tls
        .as_ref()
        .and_then(|tls| tls.server_ca_path.as_ref())
        .expect("test CA path configured");
    let output = Command::new(binary)
        .env("EGGPASS_HOME", &home)
        .arg("eggpool")
        .arg("--control")
        .arg(format!("{SERVER_NAME}:{}", app.control_port))
        .arg("--server-name")
        .arg(SERVER_NAME)
        .arg("--ca")
        .arg(ca_path)
        .arg("--label")
        .arg("producer-cli-interop")
        .output()
        .expect("run EggPass eggpool setup CLI");
    assert!(
        output.status.success(),
        "EggPass producer CLI exits successfully (status {:?})",
        output.status.code()
    );
    let stdout = String::from_utf8(output.stdout).expect("EggPass ticket output is UTF-8");
    let ticket_lines = stdout
        .lines()
        .filter(|line| line.starts_with("epagent1."))
        .collect::<Vec<_>>();
    assert_eq!(
        ticket_lines.len(),
        1,
        "EggPass stdout must contain exactly one ticket line ({} bytes, {} lines)",
        stdout.len(),
        stdout.lines().count()
    );
    let ticket = ticket_lines[0];
    assert!(
        ticket.len() <= 2_048 && !ticket.contains(['\r', '\n']),
        "EggPass ticket line is bounded and single-line"
    );
    Zeroizing::new(ticket.to_owned())
}

struct EnrollmentCallback {
    authorization_valid: bool,
    host_valid: bool,
    request_shape_valid: bool,
    callback_accepted: bool,
    client_cert_pem: String,
    client_key_pem: Zeroizing<String>,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentCallbackBody {
    client_cert_pem: String,
    client_key_pem: String,
}

struct ProviderFixture {
    address: SocketAddr,
    requests: Arc<AtomicUsize>,
    api_host_valid: Arc<AtomicBool>,
    api_authorization_valid: Arc<AtomicBool>,
    callbacks: async_mpsc::UnboundedReceiver<EnrollmentCallback>,
    stop: Option<oneshot::Sender<()>>,
    task: JoinHandle<()>,
}

impl ProviderFixture {
    async fn start_for_agent(enrollment_secret: &str, api_key: &str) -> Self {
        Self::start_with_credentials(Some(enrollment_secret), Some(api_key), true).await
    }

    async fn start_rejecting_callback(enrollment_secret: &str, api_key: &str) -> Self {
        Self::start_with_credentials(Some(enrollment_secret), Some(api_key), false).await
    }

    async fn start_with_credentials(
        enrollment_secret: Option<&str>,
        api_key: Option<&str>,
        callback_acceptance: bool,
    ) -> Self {
        let listener = TokioTcpListener::bind("127.0.0.1:0")
            .await
            .expect("provider fixture binds");
        let address = listener.local_addr().expect("provider fixture address");
        let requests = Arc::new(AtomicUsize::new(0));
        let task_requests = Arc::clone(&requests);
        let api_host_valid = Arc::new(AtomicBool::new(false));
        let task_api_host_valid = Arc::clone(&api_host_valid);
        let api_authorization_valid = Arc::new(AtomicBool::new(false));
        let task_api_authorization_valid = Arc::clone(&api_authorization_valid);
        let expected_enrollment = enrollment_secret.map(|secret| Zeroizing::new(secret.to_owned()));
        let expected_api_key = api_key.map(|secret| Zeroizing::new(secret.to_owned()));
        let (callback_tx, callbacks) = async_mpsc::unbounded_channel();
        let (stop, mut stopped) = oneshot::channel();
        let task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = &mut stopped => return,
                    accepted = listener.accept() => {
                        let (mut stream, _) = accepted.expect("provider fixture accepts");
                        let mut request = match read_provider_request(&mut stream).await {
                            Ok(request) => request,
                            Err(_) => continue,
                        };
                        let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
                            request.zeroize();
                            continue;
                        };
                        let headers = Zeroizing::new(String::from_utf8_lossy(&request[..header_end]).into_owned());
                        let mut lines = headers.lines();
                        let first = lines.next().unwrap_or_default();
                        let mut first_fields = first.split_ascii_whitespace();
                        let method = first_fields.next().unwrap_or_default();
                        let target = first_fields.next().unwrap_or_default();
                        let host = headers.lines().find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("host").then_some(value.trim())
                        }).unwrap_or_default();
                        let authorization = headers.lines().find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("authorization").then_some(value.trim())
                        }).unwrap_or_default();
                        if target == "/v1/eggpool/enroll" {
                            let body_bytes = &request[header_end + 4..];
                            let content_type_valid = headers.lines().any(|line| {
                                line.split_once(':').is_some_and(|(name, value)| {
                                    name.eq_ignore_ascii_case("content-type")
                                        && value.trim().eq_ignore_ascii_case("application/json")
                                })
                            });
                            let payload = serde_json::from_slice::<EnrollmentCallbackBody>(body_bytes).ok();
                            let fields_valid = payload.as_ref().is_some_and(|payload| {
                                !payload.client_cert_pem.is_empty()
                                    && !payload.client_key_pem.is_empty()
                                    && payload.client_cert_pem.len() <= 64 * 1024
                                    && payload.client_key_pem.len() <= 64 * 1024
                            });
                            let authorization_valid = expected_enrollment.as_ref().is_some_and(|expected| {
                                authorization.strip_prefix("Bearer ") == Some(expected.as_str())
                            });
                            let host_valid = host == "127.0.0.1";
                            let request_shape_valid = method == "POST"
                                && content_type_valid
                                && fields_valid
                                && body_bytes.len() <= 132 * 1024;
                            let accepted = callback_acceptance
                                && authorization_valid
                                && host_valid
                                && request_shape_valid;
                            let callback = EnrollmentCallback {
                                authorization_valid,
                                host_valid,
                                request_shape_valid,
                                callback_accepted: accepted,
                                client_cert_pem: payload.as_ref().map_or_else(String::new, |payload| payload.client_cert_pem.clone()),
                                client_key_pem: Zeroizing::new(payload.map_or_else(String::new, |payload| payload.client_key_pem)),
                            };
                            request.zeroize();
                            let (status, body) = if accepted {
                                ("201 Created", br#"{"status":"ok"}"#.as_slice())
                            } else {
                                ("400 Bad Request", br#"{"status":"error"}"#.as_slice())
                            };
                            let response = format!(
                                "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                                body.len()
                            );
                            let _ = stream.write_all(response.as_bytes()).await;
                            let _ = stream.write_all(body).await;
                            let _ = callback_tx.send(callback);
                        } else {
                            let host_valid = host == "127.0.0.1";
                            let authorization_valid = expected_api_key.as_ref().is_none_or(|expected| {
                                authorization.strip_prefix("Bearer ") == Some(expected.as_str())
                            });
                            task_requests.fetch_add(1, Ordering::AcqRel);
                            task_api_host_valid.store(host_valid, Ordering::Release);
                            task_api_authorization_valid.store(authorization_valid, Ordering::Release);
                            request.zeroize();
                            let body = br#"{"id":"enrollment-fixture","object":"chat.completion","model":"agent-enrollment-model","choices":[{"index":0,"message":{"role":"assistant","content":"reverse-agent-ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}"#;
                            let (status, body) = if host_valid && authorization_valid {
                                ("200 OK", body.as_slice())
                            } else {
                                ("401 Unauthorized", br#"{"error":"forwarding rejected"}"#.as_slice())
                            };
                            let response = format!(
                                "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                                body.len()
                            );
                            let _ = stream.write_all(response.as_bytes()).await;
                            let _ = stream.write_all(body).await;
                        }
                    }
                }
            }
        });
        Self {
            address,
            requests,
            api_host_valid,
            api_authorization_valid,
            callbacks,
            stop: Some(stop),
            task,
        }
    }

    async fn next_callback(&mut self) -> EnrollmentCallback {
        tokio::time::timeout(Duration::from_secs(35), self.callbacks.recv())
            .await
            .expect("EggPass callback reaches the fixture")
            .expect("callback fixture channel remains open")
    }

    async fn shutdown(mut self) {
        let _ = self.stop.take().expect("provider stop sender").send(());
        tokio::time::timeout(Duration::from_secs(3), self.task)
            .await
            .expect("provider fixture stops")
            .expect("provider fixture task joins");
    }
}

async fn read_provider_request(stream: &mut TcpStream) -> io::Result<Vec<u8>> {
    const MAX_REQUEST_BYTES: usize = 256 * 1024;
    let mut bytes = Vec::new();
    let mut buffer = [0_u8; 2048];
    let mut request_end = None;
    loop {
        let read = stream.read(&mut buffer).await?;
        if read == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        bytes.extend_from_slice(&buffer[..read]);
        if bytes.len() > MAX_REQUEST_BYTES {
            bytes.zeroize();
            return Err(io::ErrorKind::InvalidData.into());
        }
        if request_end.is_none()
            && let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n")
        {
            let content_length = String::from_utf8_lossy(&bytes[..header_end])
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            request_end = Some(header_end + 4 + content_length);
        }
        if request_end.is_some_and(|end| bytes.len() >= end) {
            return Ok(bytes);
        }
    }
}

struct ReverseClientTask {
    cancel: Box<dyn Fn() + Send + Sync>,
    task: JoinHandle<Result<(), eggress_protocol_reverse::ProtocolError>>,
}

impl ReverseClientTask {
    fn start(
        control: SocketAddr,
        service: SocketAddr,
        username: &str,
        password: &str,
        ca_pem: Vec<u8>,
    ) -> Self {
        Self::start_with_identity(
            control,
            service,
            Some(username.to_owned()),
            Some(password.to_owned()),
            None,
            None,
            ca_pem,
        )
    }

    fn start_mtls(
        control: SocketAddr,
        service: SocketAddr,
        certificate_pem: &str,
        private_key_pem: &str,
        ca_pem: Vec<u8>,
    ) -> Self {
        Self::start_with_identity(
            control,
            service,
            None,
            None,
            Some(certificate_pem.as_bytes().to_vec()),
            Some(private_key_pem.as_bytes().to_vec()),
            ca_pem,
        )
    }

    fn start_with_identity(
        control: SocketAddr,
        service: SocketAddr,
        username: Option<String>,
        password: Option<String>,
        client_cert_pem: Option<Vec<u8>>,
        client_key_pem: Option<Vec<u8>>,
        ca_pem: Vec<u8>,
    ) -> Self {
        let client = ReverseClient::new(ReverseClientConfig {
            server_addr: control,
            auth_username: username,
            auth_password: password,
            reconnect_initial_ms: 5,
            reconnect_max_ms: 20,
            default_target_host: Some(service.ip().to_string()),
            default_target_port: Some(service.port()),
            tls: Some(ReverseClientTlsConfig {
                ca_pem: Some(ca_pem),
                server_name: SERVER_NAME.to_owned(),
                client_cert_pem,
                client_key_pem,
            }),
            ..ReverseClientConfig::default()
        });
        let token = client.cancel_token();
        let task = tokio::spawn(async move { client.run().await });
        Self {
            cancel: Box::new(move || token.cancel()),
            task,
        }
    }

    fn request_cancel(&self) {
        (self.cancel)();
    }

    async fn shutdown(mut self) {
        self.request_cancel();
        match tokio::time::timeout(Duration::from_secs(5), &mut self.task).await {
            Ok(result) => result
                .expect("reverse client task joins")
                .expect("reverse client shuts down cleanly"),
            Err(_) => {
                self.task.abort();
                let _ = self.task.await;
            }
        }
    }
}

fn start_bootstrap_client(
    app: &TestApp,
    provider: &ProviderFixture,
    enrollment_secret: &str,
) -> ReverseClientTask {
    ReverseClientTask::start(
        SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), app.control_port),
        provider.address,
        ENROLLMENT_USERNAME,
        enrollment_secret,
        app.producer.ca_pem().as_bytes().to_vec(),
    )
}

async fn start_mtls_from_callback(
    app: &TestApp,
    provider: &ProviderFixture,
    bootstrap: ReverseClientTask,
    callback: EnrollmentCallback,
) -> (EnrollmentCallback, ReverseClientTask) {
    assert!(
        callback.authorization_valid,
        "callback uses the enrollment bearer credential"
    );
    assert!(
        callback.host_valid,
        "callback Host is pinned to the loopback provider"
    );
    assert!(
        callback.request_shape_valid,
        "callback has the documented path and JSON body"
    );
    assert!(
        callback.callback_accepted,
        "provider accepted the enrollment callback"
    );
    assert!(
        callback
            .client_cert_pem
            .starts_with("-----BEGIN CERTIFICATE-----")
    );
    assert!(
        callback
            .client_key_pem
            .starts_with("-----BEGIN PRIVATE KEY-----")
    );

    bootstrap.shutdown().await;
    let client = ReverseClientTask::start_mtls(
        SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), app.control_port),
        provider.address,
        &callback.client_cert_pem,
        callback.client_key_pem.as_str(),
        app.producer.ca_pem().as_bytes().to_vec(),
    );
    (callback, client)
}

async fn switch_to_mtls_after_callback(
    app: &TestApp,
    provider: &mut ProviderFixture,
    bootstrap: ReverseClientTask,
) -> (EnrollmentCallback, ReverseClientTask) {
    let callback = provider.next_callback().await;
    start_mtls_from_callback(app, provider, bootstrap, callback).await
}

async fn await_enrollment_request(request: JoinHandle<String>) -> String {
    tokio::time::timeout(Duration::from_secs(45), request)
        .await
        .expect("enrollment request completes after mTLS reconnect")
        .expect("enrollment request task joins")
}

async fn enroll_with_callback(
    app: &TestApp,
    browser: &Browser,
    provider: &mut ProviderFixture,
    account_name: &str,
    ticket: &str,
    enrollment_secret: &str,
) -> (String, EnrollmentCallback, ReverseClientTask) {
    let bootstrap = start_bootstrap_client(app, provider, enrollment_secret);
    let mut request = spawn_enrollment_request(app, browser, account_name, ticket);
    let callback = tokio::select! {
        callback = provider.next_callback() => callback,
        result = &mut request => {
            let response = result.expect("enrollment request task joins");
            panic!("enrollment returned before callback: {}", response_summary(&response));
        }
    };
    let (callback, client) = start_mtls_from_callback(app, provider, bootstrap, callback).await;
    let response = await_enrollment_request(request).await;
    (response, callback, client)
}

async fn wait_for_control_count(tunnels: &AgentTunnelSet, expected: u32) {
    let reached = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let snapshot = tunnels.snapshot();
            if snapshot.tunnels.len() == 1 && snapshot.tunnels[0].active_control == expected {
                return;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .is_ok();
    let snapshot = tunnels.snapshot();
    assert!(
        reached,
        "Agent control count did not reach {expected}; snapshot={snapshot:?}"
    );
}

async fn inference_request(address: SocketAddr, token: &str) -> String {
    let body = serde_json::json!({
        "model": ENROLLMENT_MODEL,
        "messages": [{"role": "user", "content": "hello from the enrollment test"}],
    })
    .to_string();
    let request = format!(
        "POST /v1/chat/completions HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to EggPool");
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write inference request");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(10), stream.read_to_end(&mut response))
        .await
        .expect("inference response completes")
        .expect("read inference response");
    String::from_utf8_lossy(&response).into_owned()
}

async fn port_is_closed(address: SocketAddr) {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if TcpStream::connect(address).await.is_err() {
                return;
            }
            // Poll the observable OS socket state under a bound; no sleep is
            // treated as evidence of task readiness or shutdown.
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("listener closes within the bounded shutdown window");
}

/// Every persisted value in every table, rendered as text.
///
/// The credential-absence invariant must hold against the WHOLE database, not
/// against one projection of one table, so this dump reads every table's rows
/// column by column through SQLite's own quoting.
async fn dump_all_persisted_values(database: &Database) -> String {
    database
        .call(|connection| {
            let tables = connection
                .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")?
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?;
            let mut dump = String::new();
            for table in tables {
                let columns = connection
                    .prepare(&format!("PRAGMA table_info(\"{table}\")"))?
                    .query_map([], |row| row.get::<_, String>(1))?
                    .collect::<Result<Vec<_>, _>>()?;
                if columns.is_empty() {
                    continue;
                }
                let rendered = columns
                    .iter()
                    .map(|column| format!("quote(\"{column}\")"))
                    .collect::<Vec<_>>()
                    .join(" || ' ' || ");
                let mut statement =
                    connection.prepare(&format!("SELECT {rendered} FROM \"{table}\""))?;
                let mut rows = statement.query([])?;
                while let Some(row) = rows.next()? {
                    dump.push_str(&row.get::<_, String>(0)?);
                    dump.push('\n');
                }
            }
            Ok(dump)
        })
        .await
        .expect("persisted values dump")
}

#[tokio::test(flavor = "current_thread")]
async fn enrollment_and_revocation_enforce_csrf_role_and_session_boundaries() {
    let app = TestApp::start(true, false, true, false).await;
    let ticket = app.ticket(TicketVariant::Valid);
    let bad_csrf =
        format!("provider={PROVIDER}&account_name=csrf-agent&ticket={ticket}&csrf=wrong-token");
    let csrf_denied = browser_form_post(
        app.address,
        "/my-account/agents/enroll",
        &bad_csrf,
        &app.member.cookies,
        &app.origin,
    )
    .await;
    assert!(csrf_denied.starts_with("HTTP/1.1 403"), "{csrf_denied}");
    assert!(csrf_denied.contains("csrf_invalid"));

    let admin_enroll = app.enroll(&app.admin, "admin-agent", &ticket).await;
    assert!(admin_enroll.starts_with("HTTP/1.1 403"), "{admin_enroll}");
    assert!(admin_enroll.contains("administrator_not_permitted"));
    let admin_revoke = app.revoke(&app.admin, "stranded-agent").await;
    assert!(admin_revoke.starts_with("HTTP/1.1 403"), "{admin_revoke}");
    assert!(admin_revoke.contains("administrator_not_permitted"));

    let anonymous_body =
        format!("provider={PROVIDER}&account_name=anonymous-agent&ticket={ticket}&csrf=unused");
    let anonymous = browser_form_post(
        app.address,
        "/my-account/agents/enroll",
        &anonymous_body,
        "",
        &app.origin,
    )
    .await;
    assert!(
        !anonymous.starts_with("HTTP/1.1 200"),
        "anonymous request passed: {anonymous}"
    );
    assert!(
        !app.env_path.exists(),
        "denied requests stage no credential"
    );
    app.shutdown().await;
}

#[cfg(unix)]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn initiating_session_revoked_during_enrollment_is_rejected() {
    let app = TestApp::start(true, false, true, false).await;
    let original_config = fs::read(&app.config_path).expect("original config reads");
    let ca_path = PathBuf::from(
        app.config
            .agents
            .tls
            .as_ref()
            .unwrap()
            .server_ca_path
            .as_ref()
            .unwrap(),
    );
    fs::remove_file(&ca_path).expect("remove regular CA file before FIFO setup");
    let fifo = Command::new("mkfifo")
        .arg(&ca_path)
        .status()
        .expect("create CA FIFO");
    assert!(fifo.success(), "mkfifo creates CA FIFO");

    let ticket = app.ticket(TicketVariant::Valid);
    let body = format!(
        "provider={PROVIDER}&account_name=changed-session-agent&ticket={ticket}&csrf={}",
        app.member.csrf
    );
    let address = app.address;
    let cookies = app.member.cookies.clone();
    let origin = app.origin.clone();
    let request = tokio::spawn(async move {
        browser_form_post(
            address,
            "/my-account/agents/enroll",
            &body,
            &cookies,
            &origin,
        )
        .await
    });

    let (opened_tx, opened_rx) = oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let ca_pem = app.producer.ca_pem().to_owned();
    let writer_path = ca_path.clone();
    let writer = tokio::task::spawn_blocking(move || {
        let mut fifo = fs::OpenOptions::new()
            .write(true)
            .open(writer_path)
            .expect("enrollment reaches CA FIFO after initial session validation");
        opened_tx.send(()).expect("signal blocked CA read");
        release_rx.recv().expect("test releases CA read");
        std::io::Write::write_all(&mut fifo, ca_pem.as_bytes()).expect("write pinned CA");
    });
    tokio::time::timeout(Duration::from_secs(5), opened_rx)
        .await
        .expect("enrollment reaches the CA read")
        .expect("FIFO reader is open");

    let user_id = app.member.user_id.clone();
    app.database
        .call(move |connection| {
            connection.execute(
                "UPDATE web_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = ?1",
                [user_id.as_str()],
            )?;
            Ok(())
        })
        .await
        .expect("revoke the initiating session while enrollment is suspended");
    release_tx
        .send(())
        .expect("release the CA read after session revocation");
    tokio::time::timeout(Duration::from_secs(5), writer)
        .await
        .expect("CA FIFO writer completes")
        .expect("CA FIFO writer joins");

    let response = tokio::time::timeout(Duration::from_secs(10), request)
        .await
        .expect("enrollment response completes")
        .expect("enrollment request joins");
    assert!(
        response.starts_with("HTTP/1.1 403"),
        "changed session denied: {response}"
    );
    assert!(
        response.contains("session_changed"),
        "second session check rejects: {response}"
    );
    assert_eq!(fs::read(&app.config_path).unwrap(), original_config);
    assert!(
        !app.env_path.exists(),
        "changed session stages no credential"
    );
    assert!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("changed-session binding query succeeds")
            .is_empty()
    );
    port_is_closed(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        app.control_port,
    ))
    .await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn startup_reconciliation_removes_unpublished_agent_binding() {
    let app = TestApp::start(false, false, true, false).await;
    let owner_id = app.member.user_id.clone();
    let env_name = personal_upstream::agent_env_var(PROVIDER, "orphan-agent");
    let orphan_secret = "orphan-agent-credential-crash-window";
    fs::write(&app.env_path, format!("{env_name}=\"{orphan_secret}\"\n"))
        .expect("seed a stranded Agent credential");
    app.database
        .call(move |connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES (?1, 'orphan-agent', ?2, 'active')",
                (PROVIDER, &owner_id),
            )?;
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id)
                 VALUES (?1, 'agent_enroll', 'account', 'orphan-agent')",
                [&owner_id],
            )?;
            Ok(())
        })
        .await
        .expect("seed a stranded Agent binding");
    let missing_path =
        personal_upstream::reconcile_agent_bindings(&app.database, &app.config, None)
            .await
            .expect_err("reconciliation without a config path fails closed");
    assert!(!missing_path.to_string().contains(orphan_secret));
    assert!(
        fs::read_to_string(&app.env_path)
            .unwrap()
            .contains(orphan_secret)
    );
    assert_eq!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("binding remains until files can be reconciled")
            .len(),
        1
    );

    let removed = personal_upstream::reconcile_agent_bindings(
        &app.database,
        &app.config,
        Some(&app.config_path),
    )
    .await
    .expect("startup Agent reconciliation removes the stranded credential");
    assert_eq!(removed, 1);
    let env_text = fs::read_to_string(&app.env_path).expect("environment file remains");
    assert!(!env_text.contains(orphan_secret));
    assert!(!env_text.contains(&env_name));
    assert!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("reconciled bindings load")
            .is_empty()
    );
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn owner_can_revoke_stranded_agent_binding_and_credential() {
    let app = TestApp::start(true, false, true, false).await;
    let secret = "stranded-enrollment-credential-sentinel";
    let env_var = personal_upstream::agent_env_var(PROVIDER, "stranded-agent");
    fs::write(&app.env_path, format!("{env_var}=\"{secret}\"\n"))
        .expect("write stranded Agent credential");
    let owner_id = app.member.user_id.clone();
    app.database
        .call(move |connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES (?1, 'stranded-agent', ?2, 'active')",
                (PROVIDER, &owner_id),
            )?;
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id)
                 VALUES (?1, 'agent_enroll', 'account', 'stranded-agent')",
                [&owner_id],
            )?;
            Ok(())
        })
        .await
        .expect("seed unpublished Agent binding");
    let revoked = app.revoke(&app.member, "stranded-agent").await;
    assert!(revoked.starts_with("HTTP/1.1 200"), "{revoked}");
    assert!(revoked.contains("\"revoked\":true"));
    assert!(
        !fs::read_to_string(&app.config_path)
            .unwrap()
            .contains("stranded-agent")
    );
    let env_text = fs::read_to_string(&app.env_path).expect("environment file remains");
    assert!(!env_text.contains(secret));
    assert!(!env_text.contains(&env_var));
    assert!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("stranded binding removed")
            .is_empty()
    );
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn agent_file_preparation_does_not_publish_before_binding() {
    let app = TestApp::start(false, false, true, false).await;
    let original_config = fs::read(&app.config_path).expect("initial config reads");
    let original_env = fs::read(&app.env_path).ok();
    let env_name = personal_upstream::agent_api_key_env_var(PROVIDER, "staged-agent");
    let secret = "staged-api-key-must-not-be-persisted";
    let agent = AgentAccountConfig {
        control_bind: "127.0.0.1:17443".to_owned(),
        control_endpoint: Some(format!("{}:17443", SERVER_NAME)),
        server_name: Some(SERVER_NAME.to_owned()),
        external_bind: "127.0.0.1:17444".to_owned(),
        transport_secret_env: String::new(),
        tls: app.config.agents.tls.clone(),
        ..AgentAccountConfig::default()
    };

    let prepared = eggpool::operations::config_mutation::prepare_personal_agent_account_files(
        eggpool::operations::config_mutation::PersonalAgentAccountFileWrite {
            config_path: &app.config_path,
            env_path: &app.env_path,
            provider_id: PROVIDER,
            account_name: "staged-agent",
            api_key_env: &env_name,
            api_key: secret,
            base_url: "http://127.0.0.1:17444/v1",
            label: None,
            agent: &agent,
        },
    )
    .expect("prepare Agent files without publishing");

    assert_eq!(
        fs::read(&app.config_path).expect("config remains readable"),
        original_config
    );
    assert_eq!(fs::read(&app.env_path).ok(), original_env);
    drop(prepared);
    assert!(
        !fs::read_to_string(&app.config_path)
            .expect("config remains readable")
            .contains("staged-agent")
    );
    assert!(
        !fs::read_to_string(&app.env_path)
            .unwrap_or_default()
            .contains(secret)
    );
    app.shutdown().await;
}

#[cfg(unix)]
#[tokio::test(flavor = "current_thread")]
async fn config_destination_preflight_failure_leaves_no_credential_file() {
    let app = TestApp::start(false, false, true, false).await;
    use std::os::unix::fs::symlink;

    let config_link = app._directory.path().join("config-link.toml");
    symlink(&app.config_path, &config_link).expect("create config symlink fixture");
    let env_path = app._directory.path().join("must-not-be-created.env");
    let env_name = personal_upstream::agent_api_key_env_var(PROVIDER, "preflight-agent");
    let secret = "api-key-must-not-be-staged";
    let agent = AgentAccountConfig {
        control_bind: "127.0.0.1:17443".to_owned(),
        external_bind: "127.0.0.1:17444".to_owned(),
        transport_secret_env: String::new(),
        tls: app.config.agents.tls.clone(),
        ..AgentAccountConfig::default()
    };
    let result = eggpool::operations::config_mutation::prepare_personal_agent_account_files(
        eggpool::operations::config_mutation::PersonalAgentAccountFileWrite {
            config_path: &config_link,
            env_path: &env_path,
            provider_id: PROVIDER,
            account_name: "preflight-agent",
            api_key_env: &env_name,
            api_key: secret,
            base_url: "http://127.0.0.1:17444/v1",
            label: None,
            agent: &agent,
        },
    );
    assert!(
        result.is_err(),
        "symlink config destination must fail preflight"
    );
    assert!(
        !env_path.exists(),
        "preflight failure writes no credential file"
    );
    app.shutdown().await;
}

#[test]
fn test_ticket_producer_emits_ca_bound_valid_and_rejectable_tickets() {
    use eggpool::agents::{EnrollmentTicket, TicketError};

    let directory = tempfile::tempdir().expect("temporary ticket fixture");
    let producer = TestTicketProducer::new();
    let ca_path = directory.path().join("ca.pem");
    fs::write(&ca_path, producer.ca_pem()).expect("write producer CA");
    let now = unix_time();
    let valid = producer.ticket(now, 7443, TicketVariant::Valid);
    let ticket = EnrollmentTicket::decode(&valid, now).expect("producer makes a valid ticket");
    ticket
        .verify_server_ca(ca_path.to_str())
        .expect("producer pin matches its CA material");
    assert_eq!(ticket.server_name, SERVER_NAME);
    assert_eq!(ticket.enroll_secret(), producer.enroll_secret_token());

    for (variant, expected) in [
        (TicketVariant::WrongVersion, TicketError::Version),
        (TicketVariant::BadBase64, TicketError::Base64),
        (TicketVariant::MalformedJson, TicketError::Malformed),
        (TicketVariant::Oversized, TicketError::TooLarge),
        (TicketVariant::Expired, TicketError::Expired),
        (TicketVariant::MissingRequired, TicketError::MissingField),
        (
            TicketVariant::BadControlAddress,
            TicketError::ControlAddress,
        ),
        (TicketVariant::MalformedExpiry, TicketError::Expiry),
        (
            TicketVariant::ShortEnrollment,
            TicketError::EnrollmentSecret,
        ),
        (
            TicketVariant::LabelMatchesEnrollment,
            TicketError::OptionalField,
        ),
        (TicketVariant::Tampered, TicketError::Base64),
    ] {
        let invalid = producer.ticket(now, 7443, variant);
        let error =
            EnrollmentTicket::decode(&invalid, now).expect_err("fixture variant is rejected");
        assert_eq!(error, expected, "variant {variant:?}");
        assert!(!error.to_string().contains(producer.enroll_secret_token()));
    }
    let bad_ca = producer.ticket(now, 7443, TicketVariant::BadCa);
    let error = EnrollmentTicket::decode(&bad_ca, now)
        .expect("bad-CA pin has a valid ticket envelope")
        .verify_server_ca(ca_path.to_str())
        .expect_err("fixture bad CA does not match");
    assert_eq!(error, TicketError::CaMismatch);
}

#[tokio::test(flavor = "current_thread")]
async fn credential_equal_agent_label_is_not_published_or_rendered() {
    let app = TestApp::start(true, false, true, false).await;
    let secret = app.producer.enroll_secret_token().to_owned();
    let response = app
        .enroll(
            &app.member,
            "credential-label-agent",
            &app.ticket(TicketVariant::LabelMatchesEnrollment),
        )
        .await;
    assert!(
        !response.starts_with("HTTP/1.1 200"),
        "credential-equal display label is refused: {}",
        response_summary(&response)
    );
    assert!(response.contains("ticket_optional_field_invalid"));
    assert!(
        !response.contains(&secret),
        "refusal must not reveal the credential"
    );

    let page = browser_get(app.address, "/my-account/models", &app.member.cookies).await;
    assert!(
        page.starts_with("HTTP/1.1 200"),
        "member model page renders"
    );
    assert!(
        !page.contains(&secret),
        "a credential-equal Agent label must not reach rendered model origins"
    );
    let config = fs::read_to_string(&app.config_path).expect("operator config reads");
    assert!(!config.contains("credential-label-agent"));
    assert!(!config.contains(&secret));
    assert!(
        !app.env_path.exists(),
        "rejected enrollment creates no credential file"
    );
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn rejected_callback_rolls_back_without_consuming_ticket_or_publishing_agent() {
    let app = TestApp::start(true, false, true, false).await;
    let original_config = fs::read(&app.config_path).expect("initial config reads");
    let ticket = app.ticket(TicketVariant::Valid);
    let credentials = ticket_credentials(&ticket);
    let digest: [u8; 32] = Sha256::digest(ticket.as_bytes()).into();
    let mut rejecting_provider = ProviderFixture::start_rejecting_callback(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let bootstrap =
        start_bootstrap_client(&app, &rejecting_provider, credentials.enrollment.as_str());
    let request = spawn_enrollment_request(&app, &app.member, "callback-retry-agent", &ticket);
    let callback = rejecting_provider.next_callback().await;
    assert!(callback.authorization_valid);
    assert!(callback.host_valid);
    assert!(callback.request_shape_valid);
    assert!(!callback.callback_accepted);
    bootstrap.shutdown().await;
    let rejected = await_enrollment_request(request).await;
    assert!(
        rejected.starts_with("HTTP/1.1 503"),
        "callback rejection is bounded: {}",
        response_summary(&rejected)
    );
    assert!(rejected.contains("agent_callback_failed"));
    assert!(!rejected.contains(credentials.enrollment.as_str()));
    assert_eq!(
        fs::read(&app.config_path).expect("config remains unchanged"),
        original_config
    );
    assert!(
        !app.env_path.exists(),
        "rejected callback publishes no API key"
    );
    assert!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("bindings query succeeds")
            .is_empty()
    );
    let ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "callback-retry-agent",
    );
    assert!(
        !ca_path.exists(),
        "rejected callback creates no trust bundle"
    );
    assert!(
        !personal_upstream::agent_ticket_was_consumed(&app.database, digest, unix_time())
            .await
            .expect("replay lookup succeeds")
    );
    rejecting_provider.shutdown().await;

    let mut accepting_provider = ProviderFixture::start_for_agent(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let (accepted, _callback, client) = enroll_with_callback(
        &app,
        &app.member,
        &mut accepting_provider,
        "callback-retry-agent",
        &ticket,
        credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        accepted.starts_with("HTTP/1.1 200"),
        "same ticket retries after rejected callback: {}",
        response_summary(&accepted)
    );
    assert!(
        personal_upstream::agent_ticket_was_consumed(&app.database, digest, unix_time())
            .await
            .expect("accepted ticket persists")
    );
    let revoked = app.revoke(&app.member, "callback-retry-agent").await;
    assert!(revoked.starts_with("HTTP/1.1 200"));
    assert!(!ca_path.exists());
    client.shutdown().await;
    accepting_provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn member_enrollment_is_single_use_owner_bound_and_revocable() {
    let app = TestApp::start(true, false, true, false).await;
    let ticket = app.ticket(TicketVariant::Valid);
    let credentials = ticket_credentials(&ticket);
    let mut provider = ProviderFixture::start_for_agent(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let bootstrap = start_bootstrap_client(&app, &provider, credentials.enrollment.as_str());
    let left_task = spawn_enrollment_request(&app, &app.member, "enrolled-agent", &ticket);
    let right_task = spawn_enrollment_request(&app, &app.member, "enrolled-agent", &ticket);
    let (_callback, mtls_client) =
        switch_to_mtls_after_callback(&app, &mut provider, bootstrap).await;
    let left = await_enrollment_request(left_task).await;
    let right = await_enrollment_request(right_task).await;
    assert_ne!(
        left.starts_with("HTTP/1.1 200"),
        right.starts_with("HTTP/1.1 200"),
        "concurrent enrollment responses: left={}, right={}",
        response_summary(&left),
        response_summary(&right)
    );
    let winner = if left.starts_with("HTTP/1.1 200") {
        &left
    } else {
        &right
    };
    let loser = if left.starts_with("HTTP/1.1 200") {
        &right
    } else {
        &left
    };
    assert!(
        loser.starts_with("HTTP/1.1 409"),
        "one concurrent consumer loses: {loser}"
    );
    assert!(!winner.contains(app.producer.enroll_secret_token()));
    assert!(!loser.contains(app.producer.enroll_secret_token()));

    let replay = app.enroll(&app.member, "replay-agent", &ticket).await;
    assert!(
        replay.starts_with("HTTP/1.1 409"),
        "replay is refused: {replay}"
    );
    assert!(
        replay.contains("ticket_replayed"),
        "replay has a bounded error code: {replay}"
    );
    let tampered = app.ticket(TicketVariant::Tampered);
    let tamper_response = app.enroll(&app.member, "tampered-agent", &tampered).await;
    assert!(
        tamper_response.starts_with("HTTP/1.1 400"),
        "tampered ticket is refused: {tamper_response}"
    );
    assert!(!tamper_response.contains(app.producer.enroll_secret_token()));

    let duplicate = app.ticket(TicketVariant::Valid);
    let conflict = app.enroll(&app.member, "enrolled-agent", &duplicate).await;
    assert!(
        conflict.starts_with("HTTP/1.1 409"),
        "account name collision is typed: {conflict}"
    );
    assert!(conflict.contains("account_conflict"));

    let config_text = fs::read_to_string(&app.config_path).expect("published config reads");
    let published: Config = toml::from_str(&config_text).expect("published config parses");
    let account = published.providers[PROVIDER]
        .accounts
        .iter()
        .find(|account| account.name == "enrolled-agent")
        .expect("enrolled account published");
    let agent = account.agent.as_ref().expect("Agent metadata is present");
    let client_ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "enrolled-agent",
    );
    assert!(client_ca_path.is_file(), "per-agent client CA is persisted");
    assert_eq!(account.auth_mode, AccountAuthMode::Agent);
    assert_ne!(account.api_key_env, agent.transport_secret_env);
    assert!(
        agent.transport_secret_env.is_empty(),
        "mTLS replaces a transport password"
    );
    assert_eq!(
        agent.control_endpoint.as_deref(),
        Some(format!("agent.example:{}", app.control_port).as_str())
    );
    assert!(!config_text.contains(app.producer.enroll_secret_token()));
    let env_text = fs::read_to_string(&app.env_path).expect("credential file exists");
    assert!(env_text.contains(&format!(
        "{}=\"{}\"",
        account.api_key_env,
        credentials.api_key.as_str()
    )));
    assert!(!env_text.contains(credentials.enrollment.as_str()));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(&app.env_path)
            .expect("secret metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "Agent credential file is owner-only");
    }
    let external_bind: SocketAddr = agent.external_bind.parse().expect("external bind address");
    assert!(
        TcpStream::connect((Ipv4Addr::LOCALHOST, app.control_port))
            .await
            .is_ok()
    );
    assert!(TcpStream::connect(external_bind).await.is_ok());
    let bindings = personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
        .await
        .expect("owner bindings load");
    assert_eq!(bindings.len(), 1);
    let binding_owner = app.member.user_id.clone();
    let persisted = app
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT COALESCE(group_concat(provider_id || '|' || account_name || '|' || user_id || '|' || state, ','), '') FROM personal_upstream_bindings",
                [],
                |row| row.get::<_, String>(0),
            )
        })
        .await
        .expect("binding projection reads");
    assert!(persisted.contains(&binding_owner));
    assert!(!persisted.contains(credentials.enrollment.as_str()));
    assert!(!persisted.contains(credentials.api_key.as_str()));
    let secret_bytes = credentials.enrollment.as_bytes();
    for sqlite_path in [
        app._directory.path().join("eggpool.sqlite3"),
        app._directory.path().join("eggpool.sqlite3-wal"),
        app._directory.path().join("eggpool.sqlite3-shm"),
        app._directory.path().join("eggpool.sqlite3-journal"),
    ] {
        let bytes = fs::read(&sqlite_path).unwrap_or_default();
        assert!(
            !bytes
                .windows(secret_bytes.len())
                .any(|window| window == secret_bytes),
            "enrollment credential is absent from SQLite files: {}",
            sqlite_path.display()
        );
    }

    let forged_owner = format!(
        "provider={PROVIDER}&account_name=forged-agent&ticket={}&user_id={}&csrf={}",
        app.ticket(TicketVariant::Valid),
        app.member.user_id,
        app.member.csrf
    );
    let forged = browser_form_post(
        app.address,
        "/my-account/agents/enroll",
        &forged_owner,
        &app.member.cookies,
        &app.origin,
    )
    .await;
    assert!(
        forged.starts_with("HTTP/1.1 400"),
        "request cannot select an owner: {forged}"
    );

    let cross_owner_revoke = app.revoke(&app.other_member, "enrolled-agent").await;
    assert!(
        cross_owner_revoke.starts_with("HTTP/1.1 200"),
        "foreign revoke is an idempotent no-op: {cross_owner_revoke}"
    );
    assert!(
        fs::read_to_string(&app.config_path)
            .unwrap()
            .contains("enrolled-agent")
    );
    assert!(
        TcpStream::connect((Ipv4Addr::LOCALHOST, app.control_port))
            .await
            .is_ok()
    );

    let revoked = app.revoke(&app.member, "enrolled-agent").await;
    assert!(
        revoked.starts_with("HTTP/1.1 200"),
        "owner revokes Agent: {revoked}"
    );
    assert!(
        !fs::read_to_string(&app.config_path)
            .unwrap()
            .contains("enrolled-agent")
    );
    let revoked_env = fs::read_to_string(&app.env_path).unwrap_or_default();
    assert!(!revoked_env.contains(credentials.enrollment.as_str()));
    assert!(!revoked_env.contains(credentials.api_key.as_str()));
    assert!(
        !client_ca_path.exists(),
        "revocation removes per-agent CA trust"
    );
    assert!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("post-revoke bindings load")
            .is_empty()
    );
    port_is_closed(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        app.control_port,
    ))
    .await;
    let no_op = app.revoke(&app.member, "enrolled-agent").await;
    assert!(
        no_op.starts_with("HTTP/1.1 200"),
        "revoke remains idempotent: {no_op}"
    );
    mtls_client.shutdown().await;
    provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn enrolled_agent_routes_inference_through_reverse_client() {
    let app = TestApp::start(true, false, true, false).await;
    let ticket = app.ticket(TicketVariant::Valid);
    let credentials = ticket_credentials(&ticket);
    let mut provider = ProviderFixture::start_for_agent(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let (enrolled, callback, client) = enroll_with_callback(
        &app,
        &app.member,
        &mut provider,
        "inference-agent",
        &ticket,
        credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        enrolled.starts_with("HTTP/1.1 200"),
        "enrollment succeeds: {}",
        response_summary(&enrolled)
    );
    assert!(callback.callback_accepted);

    let published: Config = toml::from_str(
        &fs::read_to_string(&app.config_path).expect("published configuration reads"),
    )
    .expect("published configuration parses");
    let account = published.providers[PROVIDER]
        .accounts
        .iter()
        .find(|account| account.name == "inference-agent")
        .expect("enrolled Agent account is published");
    let agent = account
        .agent
        .as_ref()
        .expect("Agent tunnel settings are present");
    let client_ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "inference-agent",
    );
    assert!(client_ca_path.is_file(), "mTLS trust bundle is present");
    let monitor = app
        .process
        .agent_tunnel_manager()
        .prepare_for_generation(&published, Some(&app.config_path))
        .await
        .expect("monitor shares the published Agent tunnel");

    wait_for_control_count(&monitor, 1).await;
    let online_status = authenticated_status(app.address, &app.admin.cookies).await;
    assert!(!online_status.contains(credentials.enrollment.as_str()));
    let (online_state, online_reason) = live_agent_status(&online_status, "inference-agent");
    assert_eq!(online_state, "online");
    assert_eq!(online_reason, None);

    let inference = inference_request(app.address, &app.api_token).await;
    assert!(
        inference.starts_with("HTTP/1.1 200"),
        "enrolled Agent inference succeeds: {inference}"
    );
    assert!(
        inference.contains("reverse-agent-ok"),
        "reverse origin response is returned: {inference}"
    );
    assert_eq!(provider.requests.load(Ordering::Acquire), 1);
    assert!(provider.api_host_valid.load(Ordering::Acquire));
    assert!(provider.api_authorization_valid.load(Ordering::Acquire));
    assert!(!inference.contains(credentials.enrollment.as_str()));
    assert!(!inference.contains(credentials.api_key.as_str()));

    client.request_cancel();
    let external_address: SocketAddr = agent
        .external_bind
        .parse()
        .expect("Agent external bind is valid");
    let external_stream = TcpStream::connect(external_address)
        .await
        .expect("loopback external listener consumes the idle control session");
    drop(external_stream);
    client.shutdown().await;
    wait_for_control_count(&monitor, 0).await;
    let offline_status = authenticated_status(app.address, &app.admin.cookies).await;
    assert!(!offline_status.contains(credentials.enrollment.as_str()));
    let (offline_state, offline_reason) = live_agent_status(&offline_status, "inference-agent");
    assert_eq!(offline_state, "offline");
    assert_eq!(offline_reason.as_deref(), Some("control_disconnected"));

    let close_report = monitor.close(Duration::from_secs(2)).await;
    assert_eq!(
        close_report.shared, 1,
        "the live generation retains the tunnel"
    );
    let revoked = app.revoke(&app.member, "inference-agent").await;
    assert!(
        revoked.starts_with("HTTP/1.1 200"),
        "fixture enrollment revokes: {revoked}"
    );
    port_is_closed(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        app.control_port,
    ))
    .await;
    assert!(
        !client_ca_path.exists(),
        "revocation removes the mTLS trust bundle"
    );
    provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn enrollment_credential_cannot_replace_the_post_enrollment_mtls_identity() {
    let app = TestApp::start(true, false, true, false).await;
    let ticket = app.ticket(TicketVariant::Valid);
    let credentials = ticket_credentials(&ticket);
    let mut provider = ProviderFixture::start_for_agent(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let (enrolled, _callback, mtls_client) = enroll_with_callback(
        &app,
        &app.member,
        &mut provider,
        "wrong-credential-agent",
        &ticket,
        credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        enrolled.starts_with("HTTP/1.1 200"),
        "enrollment succeeds: {}",
        response_summary(&enrolled)
    );

    let published: Config = toml::from_str(
        &fs::read_to_string(&app.config_path).expect("published configuration reads"),
    )
    .expect("published configuration parses");
    let account = published.providers[PROVIDER]
        .accounts
        .iter()
        .find(|account| account.name == "wrong-credential-agent")
        .expect("enrolled Agent account is published");
    let agent = account
        .agent
        .as_ref()
        .expect("Agent tunnel settings are present");
    let control: SocketAddr = agent.control_bind.parse().expect("control bind is valid");
    let monitor = app
        .process
        .agent_tunnel_manager()
        .prepare_for_generation(&published, Some(&app.config_path))
        .await
        .expect("monitor shares the published Agent tunnel");
    wait_for_control_count(&monitor, 1).await;
    mtls_client.request_cancel();
    let external_address: SocketAddr = agent
        .external_bind
        .parse()
        .expect("Agent external bind is valid");
    let external_stream = TcpStream::connect(external_address)
        .await
        .expect("loopback external listener consumes the idle control session");
    drop(external_stream);
    mtls_client.shutdown().await;
    wait_for_control_count(&monitor, 0).await;

    // Reuse the one-time bootstrap identity after enrollment. The mTLS listener
    // must reject it because the one-time password is no longer an identity.
    let bootstrap_identity = ReverseClientTask::start(
        control,
        provider.address,
        ENROLLMENT_USERNAME,
        credentials.enrollment.as_str(),
        app.producer.ca_pem().as_bytes().to_vec(),
    );
    let rejected_address = app.address;
    let rejected_token = app.api_token.clone();
    let mut rejected_request =
        tokio::spawn(async move { inference_request(rejected_address, &rejected_token).await });
    if let Ok(result) = tokio::time::timeout(Duration::from_secs(1), &mut rejected_request).await {
        let rejected = result.expect("bootstrap-identity inference task joins");
        assert!(
            !rejected.starts_with("HTTP/1.1 200"),
            "bootstrap identity must not serve inference: {}",
            response_summary(&rejected)
        );
    } else {
        rejected_request.abort();
        let _ = rejected_request.await;
    }
    assert_eq!(
        provider.requests.load(Ordering::Acquire),
        0,
        "bootstrap identity never reaches the provider fixture"
    );
    bootstrap_identity.shutdown().await;
    let close_report = monitor.close(Duration::from_secs(2)).await;
    assert_eq!(
        close_report.shared, 1,
        "the live generation still owns the tunnel"
    );
    let ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "wrong-credential-agent",
    );
    let revoked = app.revoke(&app.member, "wrong-credential-agent").await;
    assert!(revoked.starts_with("HTTP/1.1 200"));
    assert!(!ca_path.exists());
    provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn enrollment_credential_rotation_replaces_old_secret_and_remains_unpersisted() {
    let app = TestApp::start(true, false, true, false).await;
    let first_ticket = app.ticket(TicketVariant::Valid);
    let old_credentials = ticket_credentials(&first_ticket);
    let mut first_provider = ProviderFixture::start_for_agent(
        old_credentials.enrollment.as_str(),
        old_credentials.api_key.as_str(),
    )
    .await;
    let (first, _callback, first_client) = enroll_with_callback(
        &app,
        &app.member,
        &mut first_provider,
        "rotated-agent",
        &first_ticket,
        old_credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        first.starts_with("HTTP/1.1 200"),
        "initial enrollment succeeds: {}",
        response_summary(&first)
    );
    let first_ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "rotated-agent",
    );
    assert!(first_ca_path.is_file());
    first_client.shutdown().await;
    let first_revoke = app.revoke(&app.member, "rotated-agent").await;
    assert!(
        first_revoke.starts_with("HTTP/1.1 200"),
        "initial credential revokes: {first_revoke}"
    );
    assert!(
        !fs::read_to_string(&app.env_path)
            .unwrap_or_default()
            .contains(old_credentials.api_key.as_str()),
        "revocation removes the first API key"
    );
    assert!(
        !first_ca_path.exists(),
        "revocation removes the first CA bundle"
    );
    assert!(
        !fs::read_to_string(&app.env_path)
            .unwrap_or_default()
            .contains(old_credentials.enrollment.as_str())
    );
    first_provider.shutdown().await;
    port_is_closed(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        app.control_port,
    ))
    .await;

    let new_secret = URL_SAFE_NO_PAD.encode([0xa5_u8; 32]);
    let rotated_ticket = app.producer.ticket_with_enrollment_secret(
        unix_time(),
        app.control_port,
        TicketVariant::Valid,
        &new_secret,
    );
    let new_credentials = ticket_credentials(&rotated_ticket);
    let mut second_provider = ProviderFixture::start_for_agent(
        new_credentials.enrollment.as_str(),
        new_credentials.api_key.as_str(),
    )
    .await;
    let (second, _callback, second_client) = enroll_with_callback(
        &app,
        &app.member,
        &mut second_provider,
        "rotated-agent",
        &rotated_ticket,
        new_credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        second.starts_with("HTTP/1.1 200"),
        "rotated enrollment succeeds: {}",
        response_summary(&second)
    );
    let env_text = fs::read_to_string(&app.env_path).expect("rotated credential file reads");
    assert!(
        env_text.contains(new_credentials.api_key.as_str()),
        "new API key is installed"
    );
    assert!(!env_text.contains(new_credentials.enrollment.as_str()));
    assert!(!env_text.contains(old_credentials.api_key.as_str()));
    assert!(!env_text.contains(old_credentials.enrollment.as_str()));
    let config_text = fs::read_to_string(&app.config_path).expect("rotated config reads");
    assert!(!config_text.contains(old_credentials.api_key.as_str()));
    assert!(!config_text.contains(new_credentials.api_key.as_str()));
    assert!(!config_text.contains(old_credentials.enrollment.as_str()));
    assert!(!config_text.contains(new_credentials.enrollment.as_str()));
    // The credential must be absent from EVERY persisted table, not from
    // one projection of one table.
    let persisted = dump_all_persisted_values(&app.database).await;
    for secret in [
        old_credentials.enrollment.as_str(),
        old_credentials.api_key.as_str(),
        new_credentials.enrollment.as_str(),
        new_credentials.api_key.as_str(),
    ] {
        assert!(
            !persisted.contains(secret),
            "ticket credentials are absent from every persisted table"
        );
    }

    let second_revoke = app.revoke(&app.member, "rotated-agent").await;
    assert!(
        second_revoke.starts_with("HTTP/1.1 200"),
        "rotated credential revokes: {second_revoke}"
    );
    let final_env = fs::read_to_string(&app.env_path).unwrap_or_default();
    assert!(!final_env.contains(new_credentials.api_key.as_str()));
    assert!(!final_env.contains(new_credentials.enrollment.as_str()));
    let second_ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "rotated-agent",
    );
    assert!(
        !second_ca_path.exists(),
        "revocation removes the rotated CA bundle"
    );
    second_client.shutdown().await;
    second_provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn failed_reload_rolls_back_config_binding_secret_and_control_listener() {
    let app = TestApp::start(true, true, true, false).await;
    let original_config = fs::read(&app.config_path).expect("initial config reads");
    let ticket = app.ticket(TicketVariant::Valid);
    let credentials = ticket_credentials(&ticket);
    let mut provider = ProviderFixture::start_for_agent(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let (failed, _callback, client) = enroll_with_callback(
        &app,
        &app.member,
        &mut provider,
        "rollback-agent",
        &ticket,
        credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        failed.starts_with("HTTP/1.1 503"),
        "reload failure rolls back publication: {}",
        response_summary(&failed)
    );
    client.shutdown().await;
    provider.shutdown().await;
    assert!(
        failed.contains("publication_rolled_back"),
        "rollback is reported with a bounded code: {failed}"
    );
    assert_eq!(
        fs::read(&app.config_path).expect("config remains present"),
        original_config
    );
    assert!(!app.env_path.exists(), "staged secret file is removed");
    assert!(
        personal_upstream::list_bindings(&app.database, Some(&app.member.user_id))
            .await
            .expect("post-rollback bindings load")
            .is_empty(),
        "owner binding is rolled back"
    );
    let ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "rollback-agent",
    );
    assert!(
        !ca_path.exists(),
        "rollback removes unpublished client CA trust"
    );
    let ticket_digest: [u8; 32] = Sha256::digest(ticket.as_bytes()).into();
    assert!(
        personal_upstream::agent_ticket_was_consumed(&app.database, ticket_digest, unix_time())
            .await
            .expect("replay lookup succeeds")
    );
    port_is_closed(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        app.control_port,
    ))
    .await;
    let replay = app.enroll(&app.member, "rollback-agent", &ticket).await;
    assert!(replay.starts_with("HTTP/1.1 409"));
    assert!(replay.contains("ticket_replayed"));

    app.fail_reload.store(false, Ordering::Release);
    let new_enrollment_secret = URL_SAFE_NO_PAD.encode([0x77_u8; 32]);
    let retry_ticket = app.producer.ticket_with_enrollment_secret(
        unix_time(),
        app.control_port,
        TicketVariant::Valid,
        &new_enrollment_secret,
    );
    let retry_credentials = ticket_credentials(&retry_ticket);
    let mut retry_provider = ProviderFixture::start_for_agent(
        retry_credentials.enrollment.as_str(),
        retry_credentials.api_key.as_str(),
    )
    .await;
    let (retried, _callback, retry_client) = enroll_with_callback(
        &app,
        &app.member,
        &mut retry_provider,
        "rollback-agent",
        &retry_ticket,
        retry_credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        retried.starts_with("HTTP/1.1 200"),
        "fresh ticket succeeds after rollback: {}",
        response_summary(&retried)
    );
    let env_text = fs::read_to_string(&app.env_path).expect("retry writes the API key");
    assert!(env_text.contains(retry_credentials.api_key.as_str()));
    assert!(!env_text.contains(retry_credentials.enrollment.as_str()));
    let revoked = app.revoke(&app.member, "rollback-agent").await;
    assert!(
        revoked.starts_with("HTTP/1.1 200"),
        "cleanup revoke: {revoked}"
    );
    retry_client.shutdown().await;
    retry_provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "run through scripts/test-eggpool-interop.sh with a built EggPass CLI"]
async fn real_eggpass_cli_ticket_completes_callback_mtls_and_authenticated_api_request() {
    let app = TestApp::start(true, false, true, false).await;
    let ticket = real_ticket_from_eggpass_cli(&app);
    let credentials = ticket_credentials(ticket.as_str());
    let mut provider = ProviderFixture::start_for_agent(
        credentials.enrollment.as_str(),
        credentials.api_key.as_str(),
    )
    .await;
    let (enrolled, callback, client) = enroll_with_callback(
        &app,
        &app.member,
        &mut provider,
        "producer-cli-agent",
        ticket.as_str(),
        credentials.enrollment.as_str(),
    )
    .await;
    assert!(
        enrolled.starts_with("HTTP/1.1 200"),
        "real producer ticket enrolls: {}",
        response_summary(&enrolled)
    );
    assert!(callback.callback_accepted);

    let published: Config =
        toml::from_str(&fs::read_to_string(&app.config_path).expect("published config reads"))
            .expect("published config parses");
    let account = published.providers[PROVIDER]
        .accounts
        .iter()
        .find(|account| account.name == "producer-cli-agent")
        .expect("real producer account published");
    let agent = account.agent.as_ref().expect("mTLS settings published");
    assert!(agent.transport_secret_env.is_empty());
    assert_ne!(account.api_key_env, agent.transport_secret_env);
    let env_text = fs::read_to_string(&app.env_path).expect("API key file exists");
    assert!(env_text.contains(credentials.api_key.as_str()));
    assert!(!env_text.contains(credentials.enrollment.as_str()));
    let persisted = dump_all_persisted_values(&app.database).await;
    assert!(!persisted.contains(credentials.enrollment.as_str()));
    assert!(!persisted.contains(credentials.api_key.as_str()));

    let client_ca_path = eggpool::operations::config_mutation::agent_client_ca_path(
        &app.config_path,
        PROVIDER,
        "producer-cli-agent",
    );
    assert!(
        client_ca_path.is_file(),
        "issued client CA is trusted by EggPool"
    );
    let inference = inference_request(app.address, &app.api_token).await;
    assert!(
        inference.starts_with("HTTP/1.1 200"),
        "normal API request succeeds: {}",
        response_summary(&inference)
    );
    assert!(inference.contains("reverse-agent-ok"));
    assert_eq!(provider.requests.load(Ordering::Acquire), 1);
    assert!(provider.api_host_valid.load(Ordering::Acquire));
    assert!(provider.api_authorization_valid.load(Ordering::Acquire));

    let replay = app
        .enroll(&app.member, "producer-cli-replay", ticket.as_str())
        .await;
    assert!(replay.starts_with("HTTP/1.1 409"));
    assert!(replay.contains("ticket_replayed"));
    assert!(!replay.contains(credentials.enrollment.as_str()));

    let revoked = app.revoke(&app.member, "producer-cli-agent").await;
    assert!(revoked.starts_with("HTTP/1.1 200"));
    assert!(
        !client_ca_path.exists(),
        "revocation removes producer-backed trust material"
    );
    let final_env = fs::read_to_string(&app.env_path).unwrap_or_default();
    assert!(!final_env.contains(credentials.api_key.as_str()));
    assert!(!final_env.contains(credentials.enrollment.as_str()));
    client.shutdown().await;
    provider.shutdown().await;
    app.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn consumed_ticket_digest_survives_database_restart_without_raw_ticket() {
    let directory = tempfile::tempdir().expect("isolated replay database directory");
    let database_path = directory.path().join("replay.sqlite3");
    let ticket = Zeroizing::new("epagent1.restart-replay-test-sentinel".to_owned());
    let digest: [u8; 32] = Sha256::digest(ticket.as_bytes()).into();
    let now = 1_800_000_000_i64;
    let expires_at = now + 600;
    let database = Database::open(DatabaseConfig {
        path: database_path.display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("replay database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("replay database migrates");
    assert!(
        personal_upstream::record_consumed_agent_ticket(&database, digest, expires_at, now)
            .await
            .expect("ticket digest persists")
    );
    assert!(
        personal_upstream::agent_ticket_was_consumed(&database, digest, now)
            .await
            .expect("ticket digest is found")
    );
    let persisted = dump_all_persisted_values(&database).await;
    assert!(
        !persisted.contains(ticket.as_str()),
        "only the digest is persisted"
    );
    database
        .close()
        .await
        .expect("first replay database closes");

    let reopened = Database::open(DatabaseConfig {
        path: database_path.display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("replay database reopens");
    MigrationRunner::new(&reopened)
        .run()
        .await
        .expect("reopen migration check succeeds");
    assert!(
        personal_upstream::agent_ticket_was_consumed(&reopened, digest, now)
            .await
            .expect("consumed ticket survives restart")
    );
    reopened
        .close()
        .await
        .expect("reopened replay database closes");
    for suffix in ["", "-wal", "-shm", "-journal"] {
        let file = PathBuf::from(format!("{}{suffix}", database_path.display()));
        let bytes = fs::read(file).unwrap_or_default();
        assert!(
            !bytes
                .windows(ticket.len())
                .any(|window| window == ticket.as_bytes()),
            "raw ticket is absent from SQLite files"
        );
    }
}

#[tokio::test(flavor = "current_thread")]
async fn disabled_gate_blocks_enrollment_and_routing_without_deleting_credentials() {
    let app = TestApp::start(false, false, false, true).await;
    let original_config = fs::read(&app.config_path).expect("disabled config reads");
    let original_env = fs::read(&app.env_path).expect("retained credential file reads");
    let credentials = CredentialStore::from_config_near(&app.config, Some(&app.config_path));
    let registry = AccountRegistry::hydrate_from_db(
        &app.config,
        &AccountRepository::new(&app.database),
        &credentials,
    )
    .await
    .expect("disabled Agent registry builds");
    let identity = registry
        .get("existing-agent")
        .expect("configured Agent identity");
    assert!(!identity.enabled, "disabled Agent is not routable");
    assert!(
        identity.has_usable_credentials,
        "disabled gate retains usable credentials"
    );

    let ticket = app.ticket(TicketVariant::Valid);
    let denied = app.enroll(&app.member, "new-agent", &ticket).await;
    assert!(
        denied.starts_with("HTTP/1.1 403"),
        "disabled gate blocks enrollment: {denied}"
    );
    assert!(denied.contains("agents_disabled"));
    assert_eq!(
        fs::read(&app.config_path).expect("config remains present"),
        original_config
    );
    assert_eq!(
        fs::read(&app.env_path).expect("credential file remains present"),
        original_env
    );
    port_is_closed(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        app.control_port,
    ))
    .await;
    app.shutdown().await;
}
