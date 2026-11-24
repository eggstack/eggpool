use std::{
    io::{self, Write},
    sync::{Arc, Mutex},
};

use bytes::Bytes;
use http::{HeaderMap, StatusCode};
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
        DownstreamResult, FiniteRequest, StreamRequest, WireResolver, WireResolverConfig,
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
const STATIC_ACCOUNT_NAME: &str = "static-fallback-account";
const STATIC_ACCOUNT_KEY: &str = "oauth-fallback-static-key-sentinel";

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
                let mut statement =
                    connection.prepare("SELECT name FROM sqlite_master WHERE type = 'table'")?;
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
                            value
                                .windows(secret.len())
                                .any(|window| window == secret.as_slice())
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

async fn database_table_contains(
    database: &Database,
    table: &'static str,
    secret: &'static str,
) -> bool {
    let table = table.to_owned();
    let secret = secret.as_bytes().to_vec();
    database
        .call(move |connection| {
            let escaped = table.replace('"', r#""""#);
            let mut statement = connection.prepare(&format!(r#"SELECT * FROM "{escaped}""#))?;
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
                        value
                            .windows(secret.len())
                            .any(|window| window == secret.as_slice())
                    }) {
                        return Ok(true);
                    }
                }
            }
            Ok(false)
        })
        .await
        .expect("database attempt secret scan")
}

async fn database_account_scoped_rows(
    database: &Database,
    account_name: &'static str,
) -> Vec<(String, i64)> {
    let account_name = account_name.to_owned();
    database
        .call(move |connection| {
            let account_id = connection.query_row(
                "SELECT id FROM accounts WHERE name = ?1",
                [&account_name],
                |row| row.get::<_, i64>(0),
            )?;
            let tables = {
                let mut statement =
                    connection.prepare("SELECT name FROM sqlite_master WHERE type = 'table'")?;
                statement
                    .query_map([], |row| row.get::<_, String>(0))?
                    .collect::<Result<Vec<_>, _>>()?
            };
            let mut counts = Vec::new();
            for table in tables {
                let escaped = table.replace('"', r#""""#);
                let columns = {
                    let mut statement =
                        connection.prepare(&format!(r#"PRAGMA table_info("{escaped}")"#))?;
                    statement
                        .query_map([], |row| row.get::<_, String>(1))?
                        .collect::<Result<Vec<_>, _>>()?
                };
                let usage_or_attempt_table = table.contains("usage")
                    || table.contains("attempt")
                    || table.contains("reservation");
                if usage_or_attempt_table && columns.iter().any(|column| column == "account_id") {
                    let count = connection.query_row(
                        &format!(r#"SELECT COUNT(*) FROM "{escaped}" WHERE account_id = ?1"#),
                        [account_id],
                        |row| row.get::<_, i64>(0),
                    )?;
                    if count > 0 {
                        counts.push((table, count));
                    }
                }
            }
            Ok(counts)
        })
        .await
        .expect("database account row scan")
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
const STALE_ACCESS_TOKEN: &str = "oauth-stale-access-sentinel";
const ROTATED_ACCESS_TOKEN: &str = "oauth-rotated-access-sentinel";

/// A provider fixture that serves the OAuth token endpoint and can reject the
/// first upstream chat requests with HTTP 401, so the owner-scoped credential
/// refresh and its single same-account retry are observable.
struct RejectingProviderFixture {
    base_url: String,
    requests: Arc<Mutex<Vec<String>>>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl RejectingProviderFixture {
    async fn start(rejected_requests: usize) -> Self {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .expect("provider fixture listener");
        let base_url = format!("http://{}", listener.local_addr().expect("fixture address"));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&requests);
        let task = tokio::spawn(async move {
            let mut rejections = 0_usize;
            loop {
                let (mut stream, _) = match listener.accept().await {
                    Ok(connection) => connection,
                    Err(_) => break,
                };
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
                } else if request.starts_with("POST ") && request.contains("/token") {
                    (
                        "200 OK",
                        "application/json",
                        r#"{"access_token":"oauth-rotated-access-sentinel","refresh_token":"oauth-rotated-refresh-sentinel","id_token":"oauth-rotated-id-sentinel","token_type":"Bearer","expires_in":3600,"scope":"offline_access"}"#,
                    )
                } else if request.starts_with("POST ") && request.contains("/embeddings") {
                    if rejections < rejected_requests {
                        rejections += 1;
                        (
                            "401 Unauthorized",
                            "application/json",
                            r#"{"error":{"message":"token expired","type":"invalid_request_error"}}"#,
                        )
                    } else {
                        (
                            "200 OK",
                            "application/json",
                            r#"{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.1,0.2]}],"model":"gpt-oauth-fixture","usage":{"prompt_tokens":1,"total_tokens":1}}"#,
                        )
                    }
                } else if request.starts_with("POST ") && request.contains("chat/completions") {
                    if rejections < rejected_requests {
                        rejections += 1;
                        (
                            "401 Unauthorized",
                            "application/json",
                            r#"{"error":{"message":"token expired","type":"invalid_request_error"}}"#,
                        )
                    } else if request.contains("\"stream\":true") {
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
                            "200 OK",
                            "application/json",
                            r#"{"id":"chatcmpl-oauth-fixture","object":"chat.completion","created":1,"model":"gpt-oauth-fixture","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}"#,
                        )
                    }
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
                if stream.write_all(response.as_bytes()).await.is_err() {
                    break;
                }
            }
        });
        Self {
            base_url,
            requests,
            task: Some(task),
        }
    }

    fn captured(&self) -> Vec<String> {
        captured_requests(&self.requests)
    }

    async fn finish(mut self) -> Vec<String> {
        if let Some(task) = self.task.take() {
            task.abort();
            let _ = task.await;
        }
        captured_requests(&self.requests)
    }
}

impl Drop for RejectingProviderFixture {
    fn drop(&mut self) {
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}

/// An owner-bound generation whose OAuth provider profile points at the
/// fixture, so a credential refresh reaches the stub instead of the
/// production authorization server.
struct OwnerBoundFixture {
    state: crate::coordinator::InferenceState,
    credential_root: std::path::PathBuf,
    database: Database,
}

async fn owner_bound_fixture(
    fixture: &RejectingProviderFixture,
    directory: &std::path::Path,
) -> OwnerBoundFixture {
    owner_bound_fixture_with_accounts(fixture, directory, false).await
}

async fn owner_bound_fixture_with_static_account(
    fixture: &RejectingProviderFixture,
    directory: &std::path::Path,
) -> OwnerBoundFixture {
    owner_bound_fixture_with_accounts(fixture, directory, true).await
}

async fn owner_bound_fixture_with_accounts(
    fixture: &RejectingProviderFixture,
    directory: &std::path::Path,
    include_static_account: bool,
) -> OwnerBoundFixture {
    let database = database(&directory.join("state.sqlite3")).await;
    let mut config = Config::default();
    config.providers.clear();
    let account = AccountConfig {
        name: ACCOUNT_NAME.into(),
        auth_mode: AccountAuthMode::OAuth,
        ..Default::default()
    };
    let mut accounts = vec![account];
    if include_static_account {
        accounts.push(AccountConfig {
            name: STATIC_ACCOUNT_NAME.into(),
            api_key: Some(STATIC_ACCOUNT_KEY.into()),
            ..Default::default()
        });
    }
    let mut provider = ProviderConfig {
        id: "xai".into(),
        base_url: fixture.base_url.clone(),
        protocols: vec!["openai".into()],
        openai_path: "/chat/completions".into(),
        models_method: "GET".into(),
        models_path: "/models".into(),
        auth: ProviderAuthConfig::default(),
        accounts,
        embedding_models: std::collections::BTreeSet::from([MODEL_ID.to_owned()]),
        embeddings_path: Some("/embeddings".into()),
        ..Default::default()
    };
    provider.models_endpoint = Some(ProviderModelsEndpointConfig {
        method: "GET".into(),
        path: "/models".into(),
        ..Default::default()
    });
    config.providers.insert("xai".into(), provider);
    config.oauth.enabled = true;
    let database_accounts = std::iter::once(DbAccountConfig {
        name: ACCOUNT_NAME.into(),
        api_key_env: "UNUSED_OAUTH_KEY".into(),
        enabled: true,
        weight: 1.0,
        provider_id: "xai".into(),
    })
    .chain(include_static_account.then(|| DbAccountConfig {
        name: STATIC_ACCOUNT_NAME.into(),
        api_key_env: "UNUSED_STATIC_KEY".into(),
        enabled: true,
        weight: 1.0,
        provider_id: "xai".into(),
    }))
    .collect();
    AccountRepository::new(&database)
        .sync_from_config(database_accounts)
        .await
        .expect("account rows");
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
    let config_path = directory.join("config.toml");
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
    manager.set_provider_profile(
        "xai",
        eggpass_auth::OAuthProviderConfig::custom(
            "Local OAuth fixture",
            &fixture.base_url,
            "test-client",
        )
        .authorization_endpoint(format!("{}/authorize", fixture.base_url))
        .token_endpoint(format!("{}/token", fixture.base_url))
        .scopes(["offline_access"])
        .build()
        .expect("local OAuth profile"),
    );
    let credential_root = crate::oauth::credential_root_for_config(Some(&config_path));
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock")
        .as_secs() as i64;
    let credentials: crate::oauth::OAuthCredentials = serde_json::from_value(serde_json::json!({
        "access_token": STALE_ACCESS_TOKEN,
        "refresh_token": "oauth-stale-refresh-sentinel",
        "id_token": "oauth-stale-id-sentinel",
        "token_type": "Bearer",
        "scope": "openid offline_access",
        "client_id": "test-client",
        "issuer": fixture.base_url,
        "expires_at": now + 3600,
        "obtained_at": now
    }))
    .expect("valid unexpired credentials");
    manager
        .commit_credentials_for_user(USER_ID, "xai", ACCOUNT_NAME, credentials)
        .await
        .expect("owner credential file committed");
    OwnerBoundFixture {
        state,
        credential_root,
        database,
    }
}

/// The observable outcome of one streaming dispatch. A request that
/// cannot be routed at all is reported as a route error rather than a
/// terminal response, so the test can tell "the account was disabled" apart
/// from "the provider rejected the credential".
struct StreamOutcome {
    chunks: Vec<u8>,
    completed: bool,
    route_error: Option<String>,
}

async fn execute_stream(
    state: &crate::coordinator::InferenceState,
    proxy_id: &str,
) -> StreamOutcome {
    let body = Bytes::from(
        serde_json::json!({
            "model": MODEL_ID,
            "stream": true,
            "messages": [{"role": "user", "content": "hello"}]
        })
        .to_string(),
    );
    let mut request = StreamRequest::new(
        proxy_id,
        body,
        HeaderMap::new(),
        ClientSurface::ChatCompletions,
        StaticRoutingFacts::default(),
    )
    .expect("stream request admission");
    request.routing_facts.provider_id = Some("xai".into());
    request.routing_facts.principal_user_id = Some(USER_ID.into());
    let mut execution = match state.streaming_coordinator().execute(request).await {
        Ok(execution) => execution,
        Err(error) => {
            return StreamOutcome {
                chunks: Vec::new(),
                completed: false,
                route_error: Some(format!("{error:?}")),
            };
        }
    };
    execution.mark_started();
    let mut chunks = Vec::new();
    while let Some(chunk) = execution.next_chunk().await {
        match chunk {
            Ok(chunk) => chunks.extend_from_slice(&chunk),
            Err(_) => break,
        }
    }
    let finalization = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("stream finalization");
    StreamOutcome {
        chunks,
        completed: finalization.progress.completed,
        route_error: None,
    }
}

fn chat_requests(requests: &[String]) -> Vec<&String> {
    requests
        .iter()
        .filter(|request| request.contains("chat/completions"))
        .collect()
}

fn token_requests(requests: &[String]) -> Vec<&String> {
    requests
        .iter()
        .filter(|request| request.contains("/token"))
        .collect()
}

#[tokio::test(flavor = "current_thread")]
async fn oauth_401_refreshes_the_owner_credential_and_retries_the_same_account_once() {
    let fixture = RejectingProviderFixture::start(1).await;
    let directory = tempfile::tempdir().expect("test directory");
    let owner = owner_bound_fixture(&fixture, directory.path()).await;
    owner
        .state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");

    let outcome = execute_stream(&owner.state, "oauth-401-heal").await;
    assert!(
        outcome.route_error.is_none(),
        "the healed request is routed"
    );
    assert!(outcome.completed, "the healed request completes");
    assert!(
        String::from_utf8_lossy(&outcome.chunks).contains("ok"),
        "the healed response body is forwarded"
    );

    let requests = fixture.captured();
    let chat = chat_requests(&requests);
    assert_eq!(chat.len(), 2, "the same account is retried exactly once");
    assert!(
        chat[0]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {STALE_ACCESS_TOKEN}")),
        "the first attempt uses the rejected credential"
    );
    assert!(
        chat[1]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {ROTATED_ACCESS_TOKEN}")),
        "the retry uses the refreshed credential"
    );
    assert_eq!(
        token_requests(&requests).len(),
        1,
        "the credential is refreshed exactly once"
    );

    // The refreshed credential is the one the owner keeps using, and the
    // account is not disabled by a credential that was healed.
    let credential_file = owner.credential_root.join(
        crate::oauth::credential_filename(USER_ID, "xai", ACCOUNT_NAME)
            .expect("credential file name"),
    );
    let stored = std::fs::read(&credential_file).expect("credential file");
    assert!(String::from_utf8_lossy(&stored).contains(ROTATED_ACCESS_TOKEN));
    assert!(!String::from_utf8_lossy(&stored).contains(STALE_ACCESS_TOKEN));

    let outcome = execute_stream(&owner.state, "oauth-401-heal-follow-up").await;
    assert!(
        outcome.completed,
        "the account stays eligible after a healed rejection"
    );
    assert!(outcome.route_error.is_none());
    assert!(String::from_utf8_lossy(&outcome.chunks).contains("ok"));

    let requests = fixture.finish().await;
    assert_eq!(
        chat_requests(&requests).len(),
        3,
        "the follow-up request reaches the same account"
    );
    assert_eq!(token_requests(&requests).len(), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn oauth_401_terminalizes_when_the_refreshed_credential_is_rejected_again() {
    let fixture = RejectingProviderFixture::start(2).await;
    let directory = tempfile::tempdir().expect("test directory");
    let owner = owner_bound_fixture(&fixture, directory.path()).await;
    owner
        .state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");

    let outcome = execute_stream(&owner.state, "oauth-401-terminal").await;
    assert!(
        !String::from_utf8_lossy(&outcome.chunks).contains("\"content\":\"ok\""),
        "the rejected request does not deliver a provider body"
    );

    let requests = fixture.captured();
    assert_eq!(
        chat_requests(&requests).len(),
        2,
        "the account is retried once and never again"
    );
    assert_eq!(
        token_requests(&requests).len(),
        1,
        "the credential is refreshed once and never again"
    );

    // The credential is genuinely rejected, so the account is disabled and the
    // next request never reaches the provider.
    let outcome = execute_stream(&owner.state, "oauth-401-terminal-follow-up").await;
    assert!(
        !outcome.completed,
        "a disabled OAuth account is not routed again"
    );
    assert!(
        outcome
            .route_error
            .as_deref()
            .is_some_and(|error| error.contains("NoEligibleRoute")),
        "the disabled account leaves no eligible route: {:?}",
        outcome.route_error
    );
    let requests = fixture.finish().await;
    assert_eq!(
        chat_requests(&requests).len(),
        2,
        "the disabled account is not dispatched again"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn finite_oauth_401_refreshes_the_owner_credential_and_retries_the_same_account_once() {
    let fixture = RejectingProviderFixture::start(1).await;
    let directory = tempfile::tempdir().expect("test directory");
    let owner = owner_bound_fixture(&fixture, directory.path()).await;
    owner
        .state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");

    let body = Bytes::from(
        serde_json::json!({
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hello"}]
        })
        .to_string(),
    );
    let mut request = FiniteRequest::new(
        "oauth-401-finite",
        body,
        HeaderMap::new(),
        ClientSurface::ChatCompletions,
        StaticRoutingFacts::default(),
    )
    .expect("finite request admission");
    request.routing_facts.provider_id = Some("xai".into());
    request.routing_facts.principal_user_id = Some(USER_ID.into());
    let execution = owner
        .state
        .finite_coordinator()
        .execute(request)
        .await
        .expect("OAuth finite dispatch");
    assert_eq!(execution.response.status, StatusCode::OK);
    assert!(
        String::from_utf8_lossy(&execution.response.body).contains("ok"),
        "the healed finite body is forwarded"
    );

    let requests = fixture.finish().await;
    let chat = chat_requests(&requests);
    assert_eq!(chat.len(), 2, "the same account is retried exactly once");
    assert!(
        chat[1]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {ROTATED_ACCESS_TOKEN}")),
        "the retry uses the refreshed credential"
    );
    assert_eq!(token_requests(&requests).len(), 1);
}

/// A healed owner OAuth rejection stays on its account instead of taking the
/// ordinary static-key 401 failover path.
#[tokio::test(flavor = "current_thread")]
async fn oauth_401_retry_never_falls_back_to_a_static_account() {
    let fixture = RejectingProviderFixture::start(1).await;
    let directory = tempfile::tempdir().expect("test directory");
    let owner = owner_bound_fixture_with_static_account(&fixture, directory.path()).await;
    owner
        .state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");
    // Catalog discovery can contact both configured accounts; only inspect the
    // inference dispatches that follow it.
    let dispatch_start = fixture.captured().len();

    let outcome = execute_stream(&owner.state, "oauth-401-no-static-fallback").await;
    assert!(
        outcome.route_error.is_none(),
        "the healed request is routed"
    );
    assert!(outcome.completed, "the healed request succeeds");
    assert!(
        String::from_utf8_lossy(&outcome.chunks).contains("ok"),
        "the successful response is forwarded"
    );

    let requests = fixture.finish().await;
    let dispatch = &requests[dispatch_start..];
    let chat = chat_requests(dispatch);
    assert_eq!(chat.len(), 2, "the OAuth account is retried exactly once");
    assert_eq!(
        token_requests(dispatch).len(),
        1,
        "one owner token refresh occurs"
    );
    assert!(
        chat[0]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {STALE_ACCESS_TOKEN}")),
        "the rejected first request identifies the OAuth account"
    );
    assert!(
        chat[1]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {ROTATED_ACCESS_TOKEN}")),
        "the retry identifies that same OAuth account with its rotated token"
    );
    assert!(
        !chat[1]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {STALE_ACCESS_TOKEN}")),
        "the retry does not reuse the rejected token"
    );
    assert!(
        dispatch
            .iter()
            .all(|request| !request.contains(STATIC_ACCOUNT_KEY)),
        "the static-key account receives no inference request"
    );
    let static_rows = database_account_scoped_rows(&owner.database, STATIC_ACCOUNT_NAME).await;
    assert!(
        static_rows.is_empty(),
        "the static-key account has no usage or attempt rows: {static_rows:?}"
    );
}

/// Rejected and rotated OAuth access tokens stay out of every SQLite table,
/// persisted attempt details, and coordinator-produced diagnostics.
#[tokio::test(flavor = "current_thread")]
async fn oauth_401_retry_does_not_persist_or_diagnose_either_access_token() {
    let captured_logs = Arc::new(Mutex::new(Vec::new()));
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(tracing::Level::TRACE)
        .with_ansi(false)
        .with_writer(CaptureLogs(Arc::clone(&captured_logs)))
        .finish();
    let _log_guard = tracing::subscriber::set_default(subscriber);
    let fixture = RejectingProviderFixture::start(1).await;
    let directory = tempfile::tempdir().expect("test directory");
    let owner = owner_bound_fixture(&fixture, directory.path()).await;
    owner
        .state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");
    let dispatch_start = fixture.captured().len();

    let outcome = execute_stream(&owner.state, "oauth-401-token-hygiene").await;
    assert!(
        outcome.route_error.is_none(),
        "the healed request is routed"
    );
    assert!(outcome.completed, "the healed request succeeds");
    let requests = fixture.finish().await;
    let dispatch = &requests[dispatch_start..];
    assert_eq!(
        chat_requests(dispatch).len(),
        2,
        "the request heals after one retry"
    );
    assert_eq!(
        token_requests(dispatch).len(),
        1,
        "one token refresh occurs"
    );

    assert!(
        !database_contains(&owner.database, STALE_ACCESS_TOKEN).await,
        "the rejected token is absent from every SQLite table and column"
    );
    assert!(
        !database_contains(&owner.database, ROTATED_ACCESS_TOKEN).await,
        "the rotated token is absent from every SQLite table and column"
    );
    assert!(
        !database_table_contains(&owner.database, "request_attempts", STALE_ACCESS_TOKEN).await,
        "recorded attempt rows contain no rejected credential"
    );
    let diagnostic_text = format!(
        "{:?}",
        owner.state.streaming_coordinator().diagnostics_snapshot()
    );
    assert!(
        !diagnostic_text.contains(STALE_ACCESS_TOKEN),
        "coordinator diagnostics contain no rejected credential"
    );
    assert!(
        !outcome
            .route_error
            .as_deref()
            .unwrap_or_default()
            .contains(STALE_ACCESS_TOKEN),
        "coordinator error text contains no rejected credential"
    );
    let log_text =
        String::from_utf8_lossy(&captured_logs.lock().expect("log capture lock").clone())
            .into_owned();
    assert!(
        !log_text.contains(STALE_ACCESS_TOKEN),
        "coordinator classification and error logs contain no rejected credential"
    );
}

/// The finite embedding-status branch refreshes the owner credential once and
/// retries the same embedding request with that account's rotated token.
#[tokio::test(flavor = "current_thread")]
async fn finite_embedding_oauth_401_refreshes_the_owner_credential_and_retries_once() {
    let fixture = RejectingProviderFixture::start(1).await;
    let directory = tempfile::tempdir().expect("test directory");
    let owner = owner_bound_fixture(&fixture, directory.path()).await;
    owner
        .state
        .catalog_service()
        .expect("catalog service")
        .refresh()
        .await
        .expect("catalog refresh");
    let dispatch_start = fixture.captured().len();

    let body = Bytes::from(
        serde_json::json!({
            "model": MODEL_ID,
            "input": "hello"
        })
        .to_string(),
    );
    let embedding = crate::request::embeddings::EmbeddingRequest::parse(&body)
        .expect("embedding request admission");
    let mut routing_facts =
        crate::routing::RoutingRequestFacts::from_model_id(MODEL_ID, owner.state.known_providers());
    routing_facts.provider_id = Some("xai".into());
    routing_facts.principal_user_id = Some(USER_ID.into());
    routing_facts.requested_protocol = Some("openai".into());
    routing_facts.client_protocol = Some("openai_embeddings".into());
    routing_facts.request_surface = ClientSurface::ChatCompletions.as_str().into();
    let execution = owner
        .state
        .finite_coordinator()
        .execute_embedding_admitted(
            "oauth-401-embedding".into(),
            body,
            HeaderMap::new(),
            embedding,
            routing_facts,
            None,
        )
        .await
        .expect("owner OAuth embedding dispatch");
    assert_eq!(execution.response.status, StatusCode::OK);
    let response: serde_json::Value =
        serde_json::from_slice(&execution.response.body).expect("embedding response JSON");
    assert_eq!(
        response.get("object").and_then(serde_json::Value::as_str),
        Some("list")
    );

    let requests = fixture.finish().await;
    let dispatch = &requests[dispatch_start..];
    let embeddings = dispatch
        .iter()
        .filter(|request| request.starts_with("POST ") && request.contains("/embeddings"))
        .collect::<Vec<_>>();
    assert_eq!(
        embeddings.len(),
        2,
        "the same embedding request is retried once"
    );
    assert_eq!(
        token_requests(dispatch).len(),
        1,
        "one owner token refresh occurs"
    );
    assert!(
        chat_requests(dispatch).is_empty(),
        "the provider receives embeddings, not chat"
    );
    assert!(
        embeddings[0]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {STALE_ACCESS_TOKEN}")),
        "the first embedding attempt uses the rejected owner token"
    );
    assert!(
        embeddings[1]
            .to_ascii_lowercase()
            .contains(&format!("authorization: bearer {ROTATED_ACCESS_TOKEN}")),
        "the embedding retry uses the rotated owner token"
    );
}
