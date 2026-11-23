use std::{
    io::{self, Write},
    sync::{Arc, Mutex},
};

use bytes::Bytes;
use http::HeaderMap;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

use crate::{
    Config,
    catalog::RefreshOutcome,
    config::{
        AccountAuthMode, AccountConfig, ProviderAuthConfig, ProviderConfig,
        ProviderModelsEndpointConfig,
    },
    coordinator::{
        DownstreamResult, StreamRequest, WireResolver, WireResolverConfig,
        build_inference_state_with_shared, compile_provider_profiles,
    },
    db::{
        AccountConfig as DbAccountConfig, AccountRepository, Database, DatabaseConfig,
        MigrationRunner,
    },
    model_router::ModelRouterAffinity,
    providers::ProviderClientPool,
    request::StaticRoutingFacts,
    wire::ir::ClientSurface,
};

const ACCESS_TOKEN: &str = "oauth-access-dispatch-sentinel";
const USER_ID: &str = "oauth-owner";
const ACCOUNT_NAME: &str = "oauth-account";
const MODEL_ID: &str = "gpt-oauth-fixture";

#[derive(Clone)]
struct CaptureLogs(Arc<Mutex<Vec<u8>>>);

impl Write for CaptureLogs {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0
            .lock()
            .map_err(|_| io::Error::other("log capture lock poisoned"))?
            .extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for CaptureLogs {
    type Writer = Self;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

struct ProviderFixture {
    base_url: String,
    requests: Arc<Mutex<Vec<String>>>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl ProviderFixture {
    async fn start() -> Self {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .expect("provider fixture listener");
        let base_url = format!("http://{}", listener.local_addr().expect("fixture address"));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&requests);
        let task = tokio::spawn(async move {
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().await.expect("provider request");
                let request = read_request(&mut stream).await;
                captured
                    .lock()
                    .expect("request capture lock")
                    .push(request.clone());
                let (status, content_type, body) = if request.starts_with("GET ")
                    && request.contains("/models")
                {
                    (
                        "200 OK",
                        "application/json",
                        r#"{"data":[{"id":"gpt-oauth-fixture","object":"model"}]}"#,
                    )
                } else if request.starts_with("POST ") && request.contains("chat/completions") {
                    (
                        "200 OK",
                        "text/event-stream",
                        concat!(
                            "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\n",
                            "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                            "data: [DONE]\n\n"
                        ),
                    )
                } else {
                    (
                        "404 Not Found",
                        "application/json",
                        r#"{"error":"not found"}"#,
                    )
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream
                    .write_all(response.as_bytes())
                    .await
                    .expect("provider fixture response");
            }
        });
        Self {
            base_url,
            requests,
            task: Some(task),
        }
    }

    async fn finish(mut self) -> Vec<String> {
        self.task
            .take()
            .expect("provider fixture task")
            .await
            .expect("provider fixture completes");
        captured_requests(&self.requests)
    }
}

impl Drop for ProviderFixture {
    fn drop(&mut self) {
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}

fn captured_requests(requests: &Arc<Mutex<Vec<String>>>) -> Vec<String> {
    requests.lock().expect("request capture lock").clone()
}

async fn read_request(stream: &mut TcpStream) -> String {
    let mut request = Vec::new();
    let mut chunk = [0_u8; 2048];
    let mut request_end = None;
    loop {
        let read = stream
            .read(&mut chunk)
            .await
            .expect("provider request read");
        if read == 0 {
            break;
        }
        request.extend_from_slice(&chunk[..read]);
        if request_end.is_none()
            && let Some(headers_end) = request.windows(4).position(|part| part == b"\r\n\r\n")
        {
            let headers = String::from_utf8_lossy(&request[..headers_end]);
            let body_length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            request_end = Some(headers_end + 4 + body_length);
        }
        if request_end.is_some_and(|end| request.len() >= end) {
            break;
        }
    }
    String::from_utf8_lossy(&request).into_owned()
}

async fn database(path: &std::path::Path) -> Database {
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("database migrations");
    database
}

async fn database_contains(database: &Database, secret: &'static str) -> bool {
    let secret = secret.as_bytes().to_vec();
    database
        .call(move |connection| {
            let tables = {
                let mut statement = connection.prepare(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
                )?;
                statement
                    .query_map([], |row| row.get::<_, String>(0))?
                    .collect::<Result<Vec<_>, _>>()?
            };
            for table in tables {
                let escaped = table.replace('"', "\"\"");
                let mut statement = connection.prepare(&format!("SELECT * FROM \"{escaped}\""))?;
                let columns = statement.column_count();
                let mut rows = statement.query([])?;
                while let Some(row) = rows.next()? {
                    for index in 0..columns {
                        let bytes = match row.get_ref(index)? {
                            tokio_rusqlite::rusqlite::types::ValueRef::Text(value)
                            | tokio_rusqlite::rusqlite::types::ValueRef::Blob(value) => Some(value),
                            _ => None,
                        };
                        if bytes.is_some_and(|value| {
                            value.windows(secret.len()).any(|window| window == secret.as_slice())
                        }) {
                            return Ok(true);
                        }
                    }
                }
            }
            Ok(false)
        })
        .await
        .expect("database secret scan")
}

#[tokio::test(flavor = "current_thread")]
async fn catalog_and_streaming_dispatch_use_owner_oauth_token_without_persisting_it() {
    let captured_logs = Arc::new(Mutex::new(Vec::new()));
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(tracing::Level::TRACE)
        .with_ansi(false)
        .with_writer(CaptureLogs(Arc::clone(&captured_logs)))
        .finish();
    let _log_guard = tracing::subscriber::set_default(subscriber);
    let fixture = ProviderFixture::start().await;
    let directory = tempfile::tempdir().expect("test directory");
    let database = database(&directory.path().join("state.sqlite3")).await;

    let mut config = Config::default();
    config.providers.clear();
    let account = AccountConfig {
        name: ACCOUNT_NAME.into(),
        auth_mode: AccountAuthMode::OAuth,
        ..Default::default()
    };
    let mut provider = ProviderConfig {
        id: "xai".into(),
        base_url: fixture.base_url.clone(),
        protocols: vec!["openai".into()],
        openai_path: "/chat/completions".into(),
        models_method: "GET".into(),
        models_path: "/models".into(),
        auth: ProviderAuthConfig::default(),
        accounts: vec![account],
        ..Default::default()
    };
    provider.models_endpoint = Some(ProviderModelsEndpointConfig {
        method: "GET".into(),
        path: "/models".into(),
        ..Default::default()
    });
    config.providers.insert("xai".into(), provider);
    config.oauth.enabled = true;
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: ACCOUNT_NAME.into(),
            api_key_env: "UNUSED_OAUTH_KEY".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "xai".into(),
        }])
        .await
        .expect("account row");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings (provider_id, account_name, user_id, state, actor_id)
                 VALUES ('xai', 'oauth-account', 'oauth-owner', 'active', 'fixture')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("active owner binding");

    let clients = ProviderClientPool::from_config(&config).expect("provider client pool");
    let provider_profiles = compile_provider_profiles(&config).expect("provider wire profiles");
    let config_path = directory.path().join("config.toml");
    let state = build_inference_state_with_shared(
        &config,
        &database,
        clients,
        WireResolver::new(WireResolverConfig::default()),
        Arc::new(ModelRouterAffinity::new()),
        config
            .compile_model_router_registry()
            .expect("model router registry"),
        provider_profiles,
        Some(&config_path),
    )
    .await
    .expect("generation state builds");
    let manager = state
        .oauth_account_manager()
        .expect("generation OAuth manager");
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock")
        .as_secs() as i64;
    let credentials: crate::oauth::OAuthCredentials = serde_json::from_value(serde_json::json!({
        "access_token": ACCESS_TOKEN,
        "refresh_token": "oauth-refresh-dispatch-sentinel",
        "id_token": "oauth-id-dispatch-sentinel",
        "token_type": "Bearer",
        "scope": "openid offline_access",
        "client_id": "test-client",
        "issuer": "https://auth.x.ai",
        "expires_at": now + 3600,
        "obtained_at": now
    }))
    .expect("valid unexpired xAI credentials");
    manager
        .commit_credentials_for_user(USER_ID, "xai", ACCOUNT_NAME, credentials)
        .await
        .expect("owner credential file committed");

    let catalog_result = state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");
    assert_eq!(
        catalog_result.outcomes[ACCOUNT_NAME],
        RefreshOutcome::SuccessAuthoritative
    );
    assert_eq!(catalog_result.live_model_ids, vec![MODEL_ID]);
    assert!(
        !serde_json::to_string(&catalog_result)
            .expect("catalog result serialization")
            .contains(ACCESS_TOKEN)
    );

    let surface = ClientSurface::ChatCompletions;
    let body = Bytes::from(
        serde_json::json!({
            "model": MODEL_ID,
            "stream": true,
            "messages": [{"role": "user", "content": "hello"}]
        })
        .to_string(),
    );
    let mut request = StreamRequest::new(
        "oauth-stream-dispatch",
        body,
        HeaderMap::new(),
        surface,
        StaticRoutingFacts::default(),
    )
    .expect("stream request admission");
    request.routing_facts.provider_id = Some("xai".into());
    request.routing_facts.principal_user_id = Some(USER_ID.into());
    let mut execution = state
        .streaming_coordinator()
        .execute(request)
        .await
        .expect("OAuth streaming dispatch");
    execution.mark_started();
    let mut chunks = Vec::new();
    let mut stream_error = None;
    while let Some(chunk) = execution.next_chunk().await {
        match chunk {
            Ok(chunk) => chunks.extend_from_slice(&chunk),
            Err(error) => {
                stream_error = Some(error);
                break;
            }
        }
    }
    let finalization = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("stream finalization");
    assert!(
        stream_error.is_none(),
        "stream body has no error: {stream_error:?}"
    );
    assert!(!chunks.is_empty(), "stream response is forwarded");
    assert!(
        finalization.progress.completed,
        "stream finalizes successfully"
    );

    let requests = fixture.finish().await;
    assert_eq!(requests.len(), 2);
    for method in ["GET ", "POST "] {
        let request = requests
            .iter()
            .find(|request| request.starts_with(method))
            .unwrap_or_else(|| panic!("fixture observed {method:?} request: {requests:?}"));
        assert!(
            request
                .to_ascii_lowercase()
                .contains(&format!("authorization: bearer {ACCESS_TOKEN}")),
            "{method} uses the owner-scoped token"
        );
    }
    assert!(!format!("{state:?}").contains(ACCESS_TOKEN));
    assert!(!format!("{manager:?}").contains(ACCESS_TOKEN));
    assert!(
        !format!("{:?}", state.streaming_coordinator().diagnostics_snapshot())
            .contains(ACCESS_TOKEN)
    );
    assert!(
        !database_contains(&database, ACCESS_TOKEN).await,
        "no SQLite table stores the OAuth access token"
    );
    let log_text =
        String::from_utf8_lossy(&captured_logs.lock().expect("log capture lock").clone())
            .into_owned();
    assert!(
        !log_text.contains(ACCESS_TOKEN),
        "OAuth access token never enters tracing logs"
    );
}
