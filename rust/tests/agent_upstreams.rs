use std::{
    io,
    net::SocketAddr,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use eggpool::providers::{ProviderClientPool, ProviderClientPoolError};
use eggpool::{
    Config,
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
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
};
use eggress_protocol_reverse::{
    client::{ReverseClient, ReverseClientConfig},
    tls::ReverseClientTlsConfig,
};
use serde_json::Value;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::oneshot,
    task::JoinHandle,
};

const OWNER_MODEL: &str = "agent-owner-model";
const UNBOUND_MODEL: &str = "agent-unbound-model";
const AGENT_A_KEY: &str = "agent-a-upstream-key";
const AGENT_B_KEY: &str = "agent-b-upstream-key";
const FALLBACK_KEY: &str = "shared-fallback-key";
const AGENT_A_TRANSPORT: &str = "agent-a-transport-secret";
const AGENT_B_TRANSPORT: &str = "agent-b-transport-secret";
const AGENT_C_KEY: &str = "agent-c-upstream-key";
const AGENT_C_TRANSPORT: &str = "agent-c-transport-secret";

#[derive(Debug, Clone, PartialEq, Eq)]
struct UpstreamObservation {
    path: String,
    authorization: Option<String>,
    body: String,
}

struct FakeHttpService {
    address: SocketAddr,
    observations: Arc<Mutex<Vec<UpstreamObservation>>>,
    reject_authentication: Arc<AtomicBool>,
    stop: Option<oneshot::Sender<()>>,
    task: JoinHandle<()>,
}

impl FakeHttpService {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("fake provider listener binds");
        let address = listener.local_addr().expect("fake provider address");
        let observations = Arc::new(Mutex::new(Vec::new()));
        let reject_authentication = Arc::new(AtomicBool::new(false));
        let (stop, mut stopped) = oneshot::channel();
        let task_observations = Arc::clone(&observations);
        let task_reject = Arc::clone(&reject_authentication);
        let task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = &mut stopped => return,
                    accepted = listener.accept() => {
                        let (mut stream, _) = accepted.expect("fake provider accepts connection");
                        let observation = match read_upstream_request(&mut stream).await {
                            Ok(observation) => observation,
                            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => continue,
                            Err(error) => panic!("fake provider reads request: {error}"),
                        };
                        let denied = task_reject.load(Ordering::Acquire);
                        task_observations
                            .lock()
                            .expect("request observation lock")
                            .push(observation.clone());
                        write_upstream_response(
                            &mut stream,
                            denied,
                            request_streams(&observation.body),
                        )
                        .await
                        .expect("fake provider writes response");
                    }
                }
            }
        });
        Self {
            address,
            observations,
            reject_authentication,
            stop: Some(stop),
            task,
        }
    }

    fn observations(&self) -> Vec<UpstreamObservation> {
        self.observations
            .lock()
            .expect("request observation lock")
            .clone()
    }

    fn reject_authentication(&self) {
        self.reject_authentication.store(true, Ordering::Release);
    }

    async fn shutdown(mut self) {
        let _ = self.stop.take().expect("stop sender").send(());
        self.task.await.expect("fake provider task joins");
    }
}

struct ReverseClientTask {
    cancel: Box<dyn Fn() + Send + Sync>,
    task: JoinHandle<Result<(), eggress_protocol_reverse::ProtocolError>>,
}

impl ReverseClientTask {
    fn cancel(&self) {
        (self.cancel)();
    }

    async fn shutdown(mut self) {
        self.cancel();
        match tokio::time::timeout(Duration::from_secs(2), &mut self.task).await {
            Ok(result) => result
                .expect("reverse client task joins")
                .expect("reverse client shuts down cleanly"),
            Err(_) => {
                // The client is test infrastructure, not the server lifecycle
                // under test. Close a stuck test socket so the server can prove
                // its active-control counter returns to zero below.
                self.task.abort();
                assert!(
                    self.task
                        .await
                        .expect_err("aborted reverse client")
                        .is_cancelled()
                );
            }
        }
    }
}

fn unused_loopback_address(excluded: &[SocketAddr]) -> SocketAddr {
    loop {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("reserve loopback port");
        let address = listener.local_addr().expect("reserved loopback address");
        drop(listener);
        if !excluded.contains(&address) {
            return address;
        }
    }
}

fn agent_account(
    name: &str,
    external_bind: SocketAddr,
    control_bind: SocketAddr,
    api_key: &str,
    transport_secret_env: &str,
    cert_path: &Path,
    key_path: &Path,
) -> AccountConfig {
    AccountConfig {
        name: name.to_owned(),
        auth_mode: AccountAuthMode::Agent,
        api_key: Some(api_key.into()),
        base_url: Some(format!("http://{external_bind}/v1")),
        agent: Some(AgentAccountConfig {
            control_bind: control_bind.to_string(),
            external_bind: external_bind.to_string(),
            transport_secret_env: transport_secret_env.to_owned(),
            tls: Some(AgentTlsConfig {
                cert_path: cert_path.to_string_lossy().into_owned(),
                key_path: key_path.to_string_lossy().into_owned(),
                ..AgentTlsConfig::default()
            }),
            ..AgentAccountConfig::default()
        }),
        weight: 100.0,
        ..AccountConfig::default()
    }
}

fn make_control_tls_material() -> (String, String, String) {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let mut ca_params = rcgen::CertificateParams::new(vec!["agent-control-test-ca".to_owned()])
        .expect("test CA parameters");
    ca_params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca_key = rcgen::KeyPair::generate().expect("test CA key");
    let ca_certificate = ca_params.self_signed(&ca_key).expect("test CA certificate");
    let ca_pem = ca_certificate.pem();
    let issuer = rcgen::Issuer::new(ca_params, ca_key);

    let mut server_params =
        rcgen::CertificateParams::new(vec!["localhost".to_owned(), "127.0.0.1".to_owned()])
            .expect("test server certificate parameters");
    server_params
        .extended_key_usages
        .push(rcgen::ExtendedKeyUsagePurpose::ServerAuth);
    let server_key = rcgen::KeyPair::generate().expect("test server key");
    let server_certificate = server_params
        .signed_by(&server_key, &issuer)
        .expect("test server certificate");
    (server_certificate.pem(), server_key.serialize_pem(), ca_pem)
}

fn start_reverse_client(
    control: SocketAddr,
    service: SocketAddr,
    username: &str,
    password: &str,
    ca_pem: Vec<u8>,
) -> ReverseClientTask {
    let client = ReverseClient::new(ReverseClientConfig {
        server_addr: control,
        auth_username: Some(username.to_owned()),
        auth_password: Some(password.to_owned()),
        reconnect_initial_ms: 5,
        reconnect_max_ms: 20,
        default_target_host: Some(service.ip().to_string()),
        default_target_port: Some(service.port()),
        tls: Some(ReverseClientTlsConfig {
            ca_pem: Some(ca_pem),
            server_name: "localhost".to_owned(),
            client_cert_pem: None,
            client_key_pem: None,
        }),
        ..ReverseClientConfig::default()
    });
    let token = client.cancel_token();
    let task = tokio::spawn(async move { client.run().await });
    ReverseClientTask {
        cancel: Box::new(move || token.cancel()),
        task,
    }
}

async fn wait_for_active_controls(tunnels: &AgentTunnelSet, expected: usize) {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let snapshot = tunnels.snapshot();
            if snapshot.tunnels.len() == expected
                && snapshot
                    .tunnels
                    .iter()
                    .all(|tunnel| tunnel.active_control > 0)
            {
                return;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("each reverse client establishes an active control connection");
}

async fn read_upstream_request(stream: &mut TcpStream) -> io::Result<UpstreamObservation> {
    let mut request = Vec::new();
    let header_end = loop {
        if let Some(index) = request.windows(4).position(|window| window == b"\r\n\r\n") {
            break index + 4;
        }
        let mut buffer = [0_u8; 2048];
        let read = stream.read(&mut buffer).await?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "request ended early",
            ));
        }
        request.extend_from_slice(&buffer[..read]);
    };
    let headers = String::from_utf8_lossy(&request[..header_end]).into_owned();
    let content_length = headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        })
        .unwrap_or(0);
    while request.len() < header_end + content_length {
        let mut buffer = [0_u8; 2048];
        let read = stream.read(&mut buffer).await?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "request body ended early",
            ));
        }
        request.extend_from_slice(&buffer[..read]);
    }
    let first_line = headers.lines().next().unwrap_or_default();
    let path = first_line
        .split_whitespace()
        .nth(1)
        .unwrap_or_default()
        .to_owned();
    let authorization = headers.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("authorization")
            .then(|| value.trim().to_owned())
    });
    let body =
        String::from_utf8_lossy(&request[header_end..header_end + content_length]).into_owned();
    Ok(UpstreamObservation {
        path,
        authorization,
        body,
    })
}

fn request_streams(body: &str) -> bool {
    serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|value| value.get("stream").and_then(Value::as_bool))
        .unwrap_or(false)
}

async fn write_upstream_response(
    stream: &mut TcpStream,
    unauthorized: bool,
    streaming: bool,
) -> io::Result<()> {
    let (status, content_type, body) = if unauthorized {
        (
            "401 Unauthorized",
            "application/json",
            br#"{"error":{"message":"agent credential rejected","type":"authentication_error","code":"invalid_api_key"}}"#.as_slice(),
        )
    } else if streaming {
        (
            "200 OK",
            "text/event-stream",
            br#"data: {"id":"chatcmpl-agent","object":"chat.completion.chunk","created":0,"model":"agent-owner-model","choices":[{"index":0,"delta":{"role":"assistant","content":"agent-stream-ok"},"finish_reason":null}]}

data: {"id":"chatcmpl-agent","object":"chat.completion.chunk","created":0,"model":"agent-owner-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]

"# .as_slice(),
        )
    } else {
        (
            "200 OK",
            "application/json",
            br#"{"id":"chatcmpl-agent","object":"chat.completion","model":"agent-owner-model","choices":[{"index":0,"message":{"role":"assistant","content":"agent-finite-ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}"#.as_slice(),
        )
    };
    let headers = format!(
        "HTTP/1.1 {status}\r\ncontent-type: {content_type}\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(headers.as_bytes()).await?;
    stream.write_all(body).await
}

async fn inference_request(address: SocketAddr, token: &str, streaming: bool) -> String {
    inference_request_for_model(address, token, streaming, OWNER_MODEL).await
}

async fn inference_request_for_model(
    address: SocketAddr,
    token: &str,
    streaming: bool,
    model: &str,
) -> String {
    let body = serde_json::json!({
        "model": model,
        "messages": [{"role": "user", "content": "hello from an owner"}],
        "stream": streaming,
    })
    .to_string();
    let request = format!(
        "POST /v1/chat/completions HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to EggPool listener");
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write inference request");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(15), stream.read_to_end(&mut response))
        .await
        .expect("inference response completes")
        .expect("read inference response");
    String::from_utf8_lossy(&response).into_owned()
}

async fn temporary_database(path: &Path) -> Database {
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("database migrates");
    database
}

#[test]
fn provider_client_pool_rejects_programmatic_agent_proxy_without_echoing_it() {
    let mut config = Config::default();
    config.providers.insert(
        "agent-provider".to_owned(),
        ProviderConfig {
            id: "agent-provider".to_owned(),
            base_url: "https://provider.example/v1".to_owned(),
            accounts: vec![{
                let mut account = agent_account(
                    "agent-proxy",
                    "127.0.0.1:19082".parse().unwrap(),
                    "127.0.0.1:19081".parse().unwrap(),
                    "local-key",
                    "AGENT_TRANSPORT_SECRET",
                    Path::new("/unused/server.pem"),
                    Path::new("/unused/server-key.pem"),
                );
                account.proxy_url =
                    Some("http://agent-proxy-user:proxy-secret-sentinel@proxy.example".to_owned());
                account
            }],
            ..ProviderConfig::default()
        },
    );

    let error = ProviderClientPool::from_config(&config).expect_err("agent proxy is rejected");
    assert_eq!(
        error,
        ProviderClientPoolError::AgentProxyConfiguration {
            provider_id: "agent-provider".to_owned(),
            account_name: "agent-proxy".to_owned(),
        }
    );
    assert!(!error.to_string().contains("proxy-secret-sentinel"));
}

#[tokio::test(flavor = "current_thread")]
async fn owner_agents_tunnel_finite_and_streaming_requests_and_terminalize_401() {
    let directory = tempfile::tempdir().expect("isolated Agent test directory");
    let database_path = directory.path().join("eggpool.sqlite3");
    let config_path = directory.path().join("config.toml");
    tokio::fs::write(
        directory.path().join(".env"),
        format!("AGENT_A_TRANSPORT={AGENT_A_TRANSPORT}\nAGENT_B_TRANSPORT={AGENT_B_TRANSPORT}\nAGENT_C_TRANSPORT={AGENT_C_TRANSPORT}\n"),
    )
    .await
    .expect("write isolated Agent transport credentials");
    let (server_cert_pem, server_key_pem, ca_pem) = make_control_tls_material();
    let server_cert_path = directory.path().join("agent-control-cert.pem");
    let server_key_path = directory.path().join("agent-control-key.pem");
    tokio::fs::write(&server_cert_path, server_cert_pem.as_bytes())
        .await
        .expect("write test control certificate");
    tokio::fs::write(&server_key_path, server_key_pem.as_bytes())
        .await
        .expect("write test control key");

    let service_a = FakeHttpService::start().await;
    let service_b = FakeHttpService::start().await;
    let service_c = FakeHttpService::start().await;
    let fallback_service = FakeHttpService::start().await;
    let mut used_addresses = Vec::new();
    let control_a = unused_loopback_address(&used_addresses);
    used_addresses.push(control_a);
    let external_a = unused_loopback_address(&used_addresses);
    used_addresses.push(external_a);
    let control_b = unused_loopback_address(&used_addresses);
    used_addresses.push(control_b);
    let external_b = unused_loopback_address(&used_addresses);
    used_addresses.push(external_b);
    let control_c = unused_loopback_address(&used_addresses);
    used_addresses.push(control_c);
    let external_c = unused_loopback_address(&used_addresses);

    let database = temporary_database(&database_path).await;
    identity::mark_fresh_uninitialized(&database)
        .await
        .expect("initialize managed identity");
    let (owner_a, owner_a_credential) = identity::bootstrap_admin(
        &database,
        "agent-owner-a",
        "M001 Agent owner A test password",
    )
    .await
    .expect("create owner A");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO users(id, username, normalized_username, role, password_hash)
                 VALUES ('agent-owner-b', 'agent-owner-b', 'agent-owner-b', 'member', 'test-hash')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("create owner B");
    let owner_b_credential =
        identity::issue_api_token(&database, "agent-owner-b", "agent-upstream-test")
            .await
            .expect("issue owner B token");
    let model_policy = eggpool::model_policy::ModelPolicyService::new(database.clone());
    model_policy
        .set_user_override(&owner_a.id, OWNER_MODEL, "allow", &owner_a.id)
        .await
        .expect("allow the fixture model to owner A");
    model_policy
        .set_user_override("agent-owner-b", OWNER_MODEL, "allow", &owner_a.id)
        .await
        .expect("allow the fixture model to owner B");
    model_policy
        .set_user_override(&owner_a.id, UNBOUND_MODEL, "allow", &owner_a.id)
        .await
        .expect("allow the unbound-agent model to owner A so routing is tested");

    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = None;
    config.models.startup_refresh = false;
    config.agents.enabled = true;
    let agents = ProviderConfig {
        id: "agent-provider".to_owned(),
        base_url: format!("http://{}/v1", fallback_service.address),
        routing_priority: 100,
        accounts: vec![
            agent_account(
                "agent-a",
                external_a,
                control_a,
                AGENT_A_KEY,
                "AGENT_A_TRANSPORT",
                &server_cert_path,
                &server_key_path,
            ),
            agent_account(
                "agent-b",
                external_b,
                control_b,
                AGENT_B_KEY,
                "AGENT_B_TRANSPORT",
                &server_cert_path,
                &server_key_path,
            ),
            AccountConfig {
                name: "shared-fallback".to_owned(),
                api_key: Some(FALLBACK_KEY.into()),
                weight: 0.001,
                ..AccountConfig::default()
            },
        ],
        static_models: vec![ProviderStaticModelConfig {
            id: OWNER_MODEL.to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        }],
        ..ProviderConfig::default()
    };
    config.providers.insert("agent-provider".to_owned(), agents);
    let unbound_agents = ProviderConfig {
        id: "unbound-agent-provider".to_owned(),
        base_url: format!("http://{}/v1", service_c.address),
        routing_priority: 100,
        accounts: vec![agent_account(
            "agent-unbound",
            external_c,
            control_c,
            AGENT_C_KEY,
            "AGENT_C_TRANSPORT",
            &server_cert_path,
            &server_key_path,
        )],
        static_models: vec![ProviderStaticModelConfig {
            id: UNBOUND_MODEL.to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        }],
        ..ProviderConfig::default()
    };
    config
        .providers
        .insert("unbound-agent-provider".to_owned(), unbound_agents);
    AccountRepository::new(&database)
        .sync_from_config(vec![
            DbAccountConfig {
                name: "agent-a".to_owned(),
                api_key_env: String::new(),
                enabled: true,
                weight: 100.0,
                provider_id: "agent-provider".to_owned(),
            },
            DbAccountConfig {
                name: "agent-b".to_owned(),
                api_key_env: String::new(),
                enabled: true,
                weight: 100.0,
                provider_id: "agent-provider".to_owned(),
            },
            DbAccountConfig {
                name: "shared-fallback".to_owned(),
                api_key_env: String::new(),
                enabled: true,
                weight: 0.001,
                provider_id: "agent-provider".to_owned(),
            },
            DbAccountConfig {
                name: "agent-unbound".to_owned(),
                api_key_env: String::new(),
                enabled: true,
                weight: 100.0,
                provider_id: "unbound-agent-provider".to_owned(),
            },
        ])
        .await
        .expect("sync configured accounts");
    let owner_a_id = owner_a.id.clone();
    database
        .call(move |connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES (?1, 'openai', 'agent-provider', 'resolved')",
                [OWNER_MODEL],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES (?1, 'openai', 'unbound-agent-provider', 'resolved')",
                [UNBOUND_MODEL],
            )?;
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES ('agent-provider', 'agent-a', ?1, 'active')",
                [owner_a_id.as_str()],
            )?;
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES ('agent-provider', 'agent-b', 'agent-owner-b', 'active')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("bind each Agent account to its owner");

    let process =
        ProcessRuntime::with_config_path_and_config(database.clone(), &config_path, &config)
            .expect("process runtime builds with isolated config path");
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "agent-upstreams".to_owned(),
        1,
    )
    .await
    .expect("Agent generation prepares before reverse clients start");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("candidate transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    let tunnel_monitor = process
        .agent_tunnel_manager()
        .prepare_for_generation(&config, Some(&config_path))
        .await
        .expect("test monitor shares generation tunnels");
    assert_eq!(tunnel_monitor.snapshot().tunnels.len(), 3);

    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("EggPool listener binds");
    let address = listener.local_addr().expect("EggPool listener address");
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let ca_pem = ca_pem.into_bytes();
    let client_a = start_reverse_client(
        control_a,
        service_a.address,
        "agent-a",
        AGENT_A_TRANSPORT,
        ca_pem.clone(),
    );
    let client_b = start_reverse_client(
        control_b,
        service_b.address,
        "agent-b",
        AGENT_B_TRANSPORT,
        ca_pem.clone(),
    );
    let client_c = start_reverse_client(
        control_c,
        service_c.address,
        "agent-unbound",
        AGENT_C_TRANSPORT,
        ca_pem,
    );
    wait_for_active_controls(&tunnel_monitor, 3).await;

    let finite_a = inference_request(address, &owner_a_credential.token, false).await;
    assert!(finite_a.starts_with("HTTP/1.1 200"), "{finite_a}");
    assert!(finite_a.contains("agent-finite-ok"), "{finite_a}");
    let stream_a = inference_request(address, &owner_a_credential.token, true).await;
    assert!(stream_a.starts_with("HTTP/1.1 200"), "{stream_a}");
    assert!(stream_a.contains("agent-stream-ok"), "{stream_a}");
    let finite_b = inference_request(address, &owner_b_credential.token, false).await;
    assert!(finite_b.starts_with("HTTP/1.1 200"), "{finite_b}");
    assert!(finite_b.contains("agent-finite-ok"), "{finite_b}");

    let unbound_denial =
        inference_request_for_model(address, &owner_a_credential.token, false, UNBOUND_MODEL).await;
    assert!(
        !unbound_denial.starts_with("HTTP/1.1 200"),
        "a user without an active binding must not route to the unbound Agent: {unbound_denial}"
    );

    service_a.reject_authentication();
    let rejected_a = inference_request(address, &owner_a_credential.token, false).await;
    assert!(rejected_a.starts_with("HTTP/1.1 401"), "{rejected_a}");
    assert!(
        rejected_a.contains("agent credential rejected"),
        "the Agent 401 response is preserved: {rejected_a}"
    );

    let observed_a = service_a.observations();
    let observed_b = service_b.observations();
    let observed_c = service_c.observations();
    let observed_fallback = fallback_service.observations();
    assert_eq!(
        observed_a.len(),
        3,
        "owner A used its Agent for finite, stream, and 401"
    );
    assert_eq!(observed_b.len(), 1, "owner B used only its own Agent");
    assert!(
        observed_c.is_empty(),
        "the unbound Agent must not receive owner A traffic: {observed_c:?}"
    );
    assert!(
        observed_fallback.is_empty(),
        "Agent 401 must not refresh/fall back to the shared account: {observed_fallback:?}"
    );
    assert!(observed_a.iter().all(|request| {
        request.authorization.as_deref() == Some("Bearer agent-a-upstream-key")
            && request.path.contains("chat/completions")
    }));
    assert!(observed_b.iter().all(|request| {
        request.authorization.as_deref() == Some("Bearer agent-b-upstream-key")
            && request.path.contains("chat/completions")
    }));
    assert!(
        request_streams(&observed_a[1].body),
        "the second Agent A request streams"
    );
    assert_eq!(
        tunnel_monitor
            .snapshot()
            .tunnels
            .iter()
            .map(|tunnel| tunnel.active_control)
            .collect::<Vec<_>>(),
        vec![1, 1, 1],
        "the fixture observed each live ReverseServerState control counter"
    );

    client_a.cancel();
    client_b.cancel();
    client_c.cancel();
    handle.request_shutdown(ShutdownReason::Requested);
    server_task
        .await
        .expect("EggPool server task joins")
        .expect("EggPool server shuts down cleanly");
    drop(handle);
    let monitor_close = tunnel_monitor.close(Duration::from_secs(1)).await;
    assert_eq!(
        monitor_close.released, 3,
        "server shutdown releases all generation-owned tunnels"
    );
    assert_eq!(monitor_close.shutdown_timed_out, 0);
    client_a.shutdown().await;
    client_b.shutdown().await;
    client_c.shutdown().await;
    service_a.shutdown().await;
    service_b.shutdown().await;
    service_c.shutdown().await;
    fallback_service.shutdown().await;
    database.close().await.expect("test database closes");
}
