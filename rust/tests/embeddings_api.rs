use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{Json, Router, extract::State, routing::post};
use eggpool::{
    Config,
    config::{AccountConfig, ProviderConfig, ProviderStaticModelConfig},
    db::{
        AccountConfig as DbAccountConfig, AccountRepository, Database, DatabaseConfig,
        MigrationRunner,
    },
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

struct Fixture {
    _directory: tempfile::TempDir,
    database: Database,
    runtime: ServerRuntime,
    requests: Arc<Mutex<Vec<Value>>>,
    provider_task: tokio::task::JoinHandle<()>,
}

async fn provider(
    State(requests): State<Arc<Mutex<Vec<Value>>>>,
    Json(body): Json<Value>,
) -> Json<Value> {
    requests
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .push(body.clone());
    let count = match &body["input"] {
        Value::Array(values) => values.len(),
        Value::String(_) => 1,
        _ => 0,
    };
    Json(json!({
        "object":"list",
        "model":"upstream-embed",
        "data":(0..count).map(|index| json!({
            "object":"embedding",
            "index":index,
            "embedding":[0.25, 0.5]
        })).collect::<Vec<_>>(),
        "usage":{"prompt_tokens":count as u64 * 3,"total_tokens":count as u64 * 3}
    }))
}

async fn fixture() -> Fixture {
    let provider_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let provider_address = provider_listener.local_addr().unwrap();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let provider_task = {
        let requests = requests.clone();
        tokio::spawn(async move {
            axum::serve(
                provider_listener,
                Router::new()
                    .route("/v1/embeddings", post(provider))
                    .with_state(requests),
            )
            .await
            .unwrap();
        })
    };
    let directory = tempfile::tempdir().unwrap();
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("eggpool.db")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .unwrap();
    MigrationRunner::new(&database).run().await.unwrap();
    let mut config = Config::default();
    config.server.api_key = Some("embeddings-test-key".into());
    config.models.startup_refresh = false;
    let mut provider_config = ProviderConfig {
        id: "fixture".into(),
        base_url: format!("http://{provider_address}/v1"),
        protocols: vec!["openai".into()],
        embeddings_path: Some("/embeddings".into()),
        embedding_models: ["embed-fixture".to_owned()].into_iter().collect(),
        accounts: vec![AccountConfig {
            name: "fixture-account".into(),
            api_key: Some("provider-key".into()),
            ..AccountConfig::default()
        }],
        ..ProviderConfig::default()
    };
    provider_config.static_models = vec![
        ProviderStaticModelConfig {
            id: "embed-fixture".into(),
            protocol: Some("openai".into()),
            ..Default::default()
        },
        ProviderStaticModelConfig {
            id: "chat-only".into(),
            protocol: Some("openai".into()),
            ..Default::default()
        },
    ];
    config.providers.insert("fixture".into(), provider_config);
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: "fixture-account".into(),
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            provider_id: "fixture".into(),
        }])
        .await
        .unwrap();
    database.call(|connection| {
        for model in ["embed-fixture", "chat-only"] {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES (?1, 'openai', 'fixture', 'resolved')",
                [model],
            )?;
        }
        Ok(())
    }).await.unwrap();
    let process = ProcessRuntime::new(database.clone()).unwrap();
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "embeddings-api".into(), 1)
            .await
            .unwrap();
    let manager = Arc::new(RuntimeManager::new(candidate.transfer().unwrap()));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .unwrap();
    Fixture {
        _directory: directory,
        database,
        runtime: ServerRuntime::new(process, manager, config),
        requests,
        provider_task,
    }
}

async fn ollama_fixture() -> Fixture {
    let directory = tempfile::tempdir().unwrap();
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("eggpool.db")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .unwrap();
    MigrationRunner::new(&database).run().await.unwrap();
    let mut config = Config::default();
    config.server.api_key = Some("embeddings-test-key".into());
    config.models.startup_refresh = false;
    let mut provider_config = ProviderConfig {
        id: "ollama".into(),
        base_url: "http://127.0.0.1:11434/v1".into(),
        protocols: vec!["openai".into()],
        embeddings_path: Some("/embeddings".into()),
        embedding_models: ["nomic-embed-text:latest".to_owned()].into_iter().collect(),
        accounts: vec![AccountConfig {
            name: "ollama-local".into(),
            ..AccountConfig::default()
        }],
        ..ProviderConfig::default()
    };
    provider_config.auth.mode = "none".into();
    provider_config.static_models = vec![ProviderStaticModelConfig {
        id: "nomic-embed-text:latest".into(),
        protocol: Some("openai".into()),
        ..Default::default()
    }];
    config.providers.insert("ollama".into(), provider_config);
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: "ollama-local".into(),
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            provider_id: "ollama".into(),
        }])
        .await
        .unwrap();
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES ('nomic-embed-text:latest', 'openai', 'ollama', 'resolved')",
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
        "embeddings-ollama-live".into(),
        1,
    )
    .await
    .unwrap();
    let manager = Arc::new(RuntimeManager::new(candidate.transfer().unwrap()));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .unwrap();
    Fixture {
        _directory: directory,
        database,
        runtime: ServerRuntime::new(process, manager, config),
        requests: Arc::new(Mutex::new(Vec::new())),
        provider_task: tokio::spawn(async {}),
    }
}

async fn request(address: std::net::SocketAddr, body: &[u8]) -> (u16, Value) {
    let mut stream = TcpStream::connect(address).await.unwrap();
    let mut request = format!(
        "POST /v1/embeddings HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer embeddings-test-key\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    ).into_bytes();
    request.extend_from_slice(body);
    stream.write_all(&request).await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(10), stream.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    let response = String::from_utf8(response).unwrap();
    let status = response
        .lines()
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let body = response.split_once("\r\n\r\n").unwrap().1;
    (status, serde_json::from_str(body).unwrap())
}

#[tokio::test]
async fn qualified_embedding_models_route_and_return_ordered_openai_vectors() {
    let fixture = fixture().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let runtime = fixture.runtime;
    let runtime_handle = runtime.handle();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let (status, response) = request(
        address,
        br#"{"model":"embed-fixture","input":["first","second"],"dimensions":2}"#,
    )
    .await;
    assert_eq!(status, 200, "{response}");
    assert_eq!(response["object"], "list");
    assert_eq!(response["data"][0]["index"], 0);
    assert_eq!(response["data"][1]["index"], 1);
    assert_eq!(response["model"], "embed-fixture");
    assert_eq!(response["usage"]["prompt_tokens"], 6);
    {
        let upstream = fixture
            .requests
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        assert_eq!(upstream.len(), 1);
        assert_eq!(upstream[0]["model"], "embed-fixture");
        assert_eq!(upstream[0]["input"], json!(["first", "second"]));
    }
    runtime_handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    fixture.provider_task.abort();
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn malformed_or_unqualified_embedding_requests_fail_before_provider_submission() {
    let fixture = fixture().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let runtime = fixture.runtime;
    let runtime_handle = runtime.handle();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let (status, _) = request(address, br#"{"model":"chat-only","input":"hello"}"#).await;
    assert_eq!(status, 501);
    let (status, _) = request(
        address,
        br#"{"model":"embed-fixture","input":[],"stream":true}"#,
    )
    .await;
    assert_eq!(status, 400);
    let (status, _) = request(
        address,
        br#"{"model":"embed-fixture","input":"hello","encoding_format":"base64"}"#,
    )
    .await;
    assert_eq!(status, 400);
    {
        let requests = fixture
            .requests
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        assert!(requests.is_empty());
    }
    runtime_handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    fixture.provider_task.abort();
    fixture.database.close().await.unwrap();
}

#[tokio::test]
#[ignore = "requires Ollama 0.17.7 with nomic-embed-text:latest on loopback"]
async fn qualified_ollama_model_returns_live_embedding_vectors_through_eggpool() {
    let fixture = ollama_fixture().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let runtime = fixture.runtime;
    let runtime_handle = runtime.handle();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let (status, response) = request(
        address,
        br#"{"model":"nomic-embed-text:latest","input":["EggPool live embedding qualification","bounded vectors"]}"#,
    )
    .await;
    assert_eq!(status, 200, "{response}");
    assert_eq!(response["object"], "list");
    assert_eq!(response["data"].as_array().unwrap().len(), 2);
    assert_eq!(response["data"][0]["index"], 0);
    assert_eq!(response["data"][1]["index"], 1);
    assert_eq!(
        response["data"][0]["embedding"].as_array().unwrap().len(),
        768
    );
    assert_eq!(
        response["data"][1]["embedding"].as_array().unwrap().len(),
        768
    );
    runtime_handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    fixture.provider_task.abort();
    fixture.database.close().await.unwrap();
}
