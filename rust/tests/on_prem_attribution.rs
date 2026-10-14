use eggpool::{
    Config,
    config::{AccountConfig, ProviderAuthConfig, ProviderConfig, ProviderStaticModelConfig},
    db::{
        AccountConfig as DbAccountConfig, AccountRepository, Database, DatabaseConfig,
        MigrationRunner,
    },
    identity,
    model_policy::ModelPolicyService,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
    web_session,
};
use std::sync::Arc;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

async fn database() -> (tempfile::TempDir, Database) {
    let directory = tempfile::tempdir().expect("temporary attribution directory");
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

#[tokio::test]
async fn credential_and_session_resolve_the_same_user_epoch() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, credential) = identity::bootstrap_admin(
        &database,
        "attribution-admin",
        "M003 attribution test password",
    )
    .await
    .unwrap();

    let agent = identity::verify_api_token_principal(&database, &credential.token)
        .await
        .unwrap()
        .expect("agent token resolves to a typed user principal");
    assert_eq!(agent.user_id.as_deref(), Some(admin.id.as_str()));
    assert_eq!(agent.role.as_deref(), Some("agent"));
    assert!(agent.credential_id.is_some());
    assert!(agent.scopes.contains(&"inference".to_owned()));
    assert_eq!(agent.auth_epoch, Some(1));

    let issued = web_session::issue(&database, &admin.id).await.unwrap();
    let browser = web_session::authenticate(&database, &issued.session_token)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(browser.user_id, admin.id);
    assert_eq!(browser.auth_epoch, 1);

    identity::reset_password(&database, "attribution-admin", "M003 replacement password")
        .await
        .unwrap();
    assert!(
        identity::verify_api_token_principal(&database, &credential.token)
            .await
            .unwrap()
            .is_none(),
        "password reset revokes the old credential"
    );
    assert!(
        web_session::authenticate(&database, &issued.session_token)
            .await
            .unwrap()
            .is_none(),
        "password reset revokes the old session"
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn user_request_history_is_bounded_and_excludes_selector_children() {
    let (_directory, database) = database().await;
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)\
                 VALUES (1, 'attribution-account', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)\
                 VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                [],
            )?;
            for (proxy_id, user_id, request_kind) in [
                ("logical-a", "user-a", "logical"),
                ("selector-a", "user-a", "internal_selector"),
                ("logical-b", "user-b", "logical"),
            ] {
                connection.execute(
                    "INSERT INTO requests\
                     (account_id, model_id, provider_id, proxy_request_id, user_id, request_kind)\
                     VALUES (1, 'model-a', 'provider-a', ?1, ?2, ?3)",
                    (proxy_id, user_id, request_kind),
                )?;
            }
            Ok(())
        })
        .await
        .unwrap();

    let user_a = identity::recent_user_requests(&database, "user-a", 50)
        .await
        .unwrap();
    assert_eq!(user_a.len(), 1);
    assert_eq!(user_a[0].proxy_request_id.as_deref(), Some("logical-a"));
    assert_eq!(user_a[0].model_id, "model-a");
    assert!(
        identity::recent_user_requests(&database, "user-b", 0)
            .await
            .unwrap()
            .iter()
            .all(|row| row.proxy_request_id.as_deref() == Some("logical-b"))
    );
    database.close().await.unwrap();
}

async fn provider_fixture(listener: TcpListener) {
    let (mut stream, _) = listener.accept().await.unwrap();
    let mut request = Vec::new();
    let header_end = loop {
        if let Some(index) = request.windows(4).position(|window| window == b"\r\n\r\n") {
            break index + 4;
        }
        let mut buffer = [0_u8; 2048];
        let read = stream.read(&mut buffer).await.unwrap();
        assert_ne!(read, 0, "provider request closes before headers");
        request.extend_from_slice(&buffer[..read]);
    };
    let headers = String::from_utf8_lossy(&request[..header_end]);
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
        let read = stream.read(&mut buffer).await.unwrap();
        assert_ne!(read, 0, "provider request body ends early");
        request.extend_from_slice(&buffer[..read]);
    }
    let body = br#"{"id":"chatcmpl-attribution","object":"chat.completion","model":"attribution-model","choices":[{"index":0,"message":{"role":"assistant","content":"owned"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}"#;
    let head = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).await.unwrap();
    stream.write_all(body).await.unwrap();
}

async fn inference_request(address: std::net::SocketAddr, token: &str) -> String {
    inference_request_for_model(address, token, "attribution-model").await
}

async fn inference_request_for_model(
    address: std::net::SocketAddr,
    token: &str,
    model: &str,
) -> String {
    let body = serde_json::json!({
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": false,
    })
    .to_string();
    let request = format!(
        "POST /v1/chat/completions HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nx-eggpool-user-id: spoofed-user\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let mut stream = TcpStream::connect(address).await.unwrap();
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(
        std::time::Duration::from_secs(15),
        stream.read_to_end(&mut response),
    )
    .await
    .expect("downstream response completes")
    .unwrap();
    String::from_utf8_lossy(&response).into_owned()
}

async fn get_request(address: std::net::SocketAddr, path: &str, token: &str) -> String {
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
    );
    let mut stream = TcpStream::connect(address).await.unwrap();
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(
        std::time::Duration::from_secs(15),
        stream.read_to_end(&mut response),
    )
    .await
    .expect("discovery response completes")
    .unwrap();
    String::from_utf8_lossy(&response).into_owned()
}

#[tokio::test]
async fn managed_inference_ignores_spoofed_user_headers_and_persists_token_owner() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, credential) = identity::bootstrap_admin(
        &database,
        "request-owner",
        "M003 request attribution password",
    )
    .await
    .unwrap();

    let provider_listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let provider_address = provider_listener.local_addr().unwrap();
    let provider_task = tokio::spawn(provider_fixture(provider_listener));
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = None;
    config.integrations.advertise_base_url = Some("https://pool.example/v1".to_owned());
    config.dashboard.public = false;
    config.models.startup_refresh = false;
    let provider = ProviderConfig {
        id: "fixture".to_owned(),
        base_url: format!("http://{provider_address}"),
        protocols: vec!["openai".to_owned()],
        auth: ProviderAuthConfig {
            mode: "none".to_owned(),
            ..ProviderAuthConfig::default()
        },
        accounts: vec![AccountConfig {
            name: "fixture-account".to_owned(),
            ..AccountConfig::default()
        }],
        static_models: vec![ProviderStaticModelConfig {
            id: "attribution-model".to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        }],
        ..ProviderConfig::default()
    };
    config.providers.insert("fixture".to_owned(), provider);
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: "fixture-account".to_owned(),
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            provider_id: "fixture".to_owned(),
        }])
        .await
        .unwrap();
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)\
                 VALUES ('attribution-model', 'openai', 'fixture', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();

    let process = ProcessRuntime::new(database.clone()).unwrap();
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "on-prem-attribution".to_owned(),
        1,
    )
    .await
    .unwrap();
    let manager = Arc::new(RuntimeManager::new(candidate.transfer().unwrap()));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .unwrap();
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let address = listener.local_addr().unwrap();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let response = inference_request(address, &credential.token).await;
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
    ModelPolicyService::new(database.clone())
        .set_instance_denial("attribution-model", &admin.id, true)
        .await
        .unwrap();
    let denied = inference_request(address, &credential.token).await;
    assert!(denied.starts_with("HTTP/1.1 403"), "{denied}");
    let qualified =
        inference_request_for_model(address, &credential.token, "attribution-model/fixture").await;
    assert!(qualified.starts_with("HTTP/1.1 403"), "{qualified}");
    let models = get_request(address, "/v1/models", &credential.token).await;
    assert!(models.starts_with("HTTP/1.1 200"), "{models}");
    assert!(!models.contains("attribution-model"), "{models}");
    let profile = get_request(address, "/api/integrations/v1/profile", &credential.token).await;
    assert!(profile.starts_with("HTTP/1.1 200"), "{profile}");
    assert!(
        profile.to_ascii_lowercase().contains("etag: \""),
        "{profile}"
    );
    assert!(
        profile.contains("-p"),
        "policy revision is in the ETag: {profile}"
    );
    assert!(!profile.contains("attribution-model"), "{profile}");
    let persisted = database
        .call(|connection| {
            connection.query_row(
                "SELECT user_id, credential_id, auth_epoch, principal_kind, request_kind \
                 FROM requests ORDER BY id DESC LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<i64>>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                },
            )
        })
        .await
        .unwrap();
    let credential_id = database
        .call(|connection| {
            connection.query_row(
                "SELECT id FROM user_credentials ORDER BY created_at LIMIT 1",
                [],
                |row| row.get::<_, String>(0),
            )
        })
        .await
        .unwrap();
    assert_eq!(persisted.0.as_deref(), Some(admin.id.as_str()));
    assert_eq!(persisted.1.as_deref(), Some(credential_id.as_str()));
    assert_eq!(persisted.2, Some(1));
    assert_eq!(persisted.3, "agent_token");
    assert_eq!(persisted.4, "logical");

    handle.request_shutdown(ShutdownReason::Requested);
    server_task.await.unwrap().unwrap();
    provider_task.await.unwrap();
    database.close().await.unwrap();
}
