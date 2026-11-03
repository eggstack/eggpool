use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{HeaderMap, Response, StatusCode},
    routing::{get, post},
};
use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
    tool_policy::{GrantEffect, KNOWN_TOOLS, ToolPolicyService},
};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::Notify,
};

const PROTOCOL: &str = "2025-03-26";

#[derive(Clone)]
struct Fixture {
    calls: Arc<AtomicUsize>,
    schema_version: Arc<AtomicUsize>,
    response_mode: Arc<AtomicUsize>,
    call_started: Arc<Notify>,
    call_dropped: Arc<Notify>,
    version: &'static str,
}

struct CallDropSignal(Arc<Notify>);

impl Drop for CallDropSignal {
    fn drop(&mut self) {
        self.0.notify_one();
    }
}

async fn health() -> Json<Value> {
    Json(
        json!({"service":"eggsearch","status":"ready","version":"0.4.3","protocol":"streamable-http"}),
    )
}

async fn mcp(
    State(fixture): State<Fixture>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> Response<Body> {
    let method = request
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if method == "notifications/initialized" {
        return Response::builder()
            .status(StatusCode::ACCEPTED)
            .body(Body::empty())
            .unwrap();
    }
    if method == "tools/call" && fixture.response_mode.load(Ordering::SeqCst) == 1 {
        return Response::builder()
            .status(StatusCode::OK)
            .header("content-type", "application/octet-stream")
            .body(Body::from("not an MCP response"))
            .unwrap();
    }
    if method == "tools/call" && fixture.response_mode.load(Ordering::SeqCst) == 2 {
        return Response::builder()
            .status(StatusCode::OK)
            .header("content-type", "application/json")
            .body(Body::from(" ".repeat(1024 * 1024 + 1)))
            .unwrap();
    }
    if method == "tools/call" && fixture.response_mode.load(Ordering::SeqCst) == 3 {
        fixture.call_started.notify_one();
        let _drop_signal = CallDropSignal(fixture.call_dropped.clone());
        std::future::pending::<()>().await;
    }
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let payload = match method {
        "initialize" => {
            json!({"jsonrpc":"2.0","id":id,"result":{"protocolVersion":PROTOCOL,"capabilities":{"tools":{}},"serverInfo":{"name":"eggsearch","version":fixture.version}}})
        }
        "tools/list" => {
            assert_eq!(
                headers
                    .get("mcp-session-id")
                    .and_then(|value| value.to_str().ok()),
                Some("fixture-session")
            );
            let tools = KNOWN_TOOLS.iter().map(|name| {
                let (properties, required) = if *name == "web_search" {
                    (json!({"query":{"type":"string","minLength":1,"maxLength":256},"response_detail":{"type":"string","enum":["compact","full"]}}), json!(["query"]))
                } else { (json!({}), json!([])) };
                json!({"name":name,"description":format!("bounded fixture tool v{}", fixture.schema_version.load(Ordering::SeqCst)),"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":false}})
            }).collect::<Vec<_>>();
            json!({"jsonrpc":"2.0","id":id,"result":{"tools":tools}})
        }
        "tools/call" => {
            fixture.calls.fetch_add(1, Ordering::SeqCst);
            let detail = request
                .get("params")
                .and_then(|params| params.get("arguments"))
                .and_then(|arguments| arguments.get("response_detail"))
                .cloned()
                .unwrap_or(Value::Null);
            json!({"jsonrpc":"2.0","id":id,"result":{"content":[{"type":"text","text":"fixture result"}],"structuredContent":{"trust":"external_untrusted","source_id":"source-1","response_detail":detail},"isError":false}})
        }
        _ => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"unknown method"}}),
    };
    let mut response = Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/event-stream")
        .body(Body::from(format!(
            "data: {}\n\n",
            serde_json::to_string(&payload).unwrap()
        )))
        .unwrap();
    if method == "initialize" {
        response
            .headers_mut()
            .insert("mcp-session-id", "fixture-session".parse().unwrap());
    } else if headers.get("mcp-session-id").is_none() {
        return Response::builder()
            .status(StatusCode::BAD_REQUEST)
            .body(Body::empty())
            .unwrap();
    }
    response
}

async fn fixture_server() -> (
    std::net::SocketAddr,
    tokio::task::JoinHandle<()>,
    Arc<AtomicUsize>,
    Arc<AtomicUsize>,
    Arc<AtomicUsize>,
    Arc<Notify>,
    Arc<Notify>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let schema_version = Arc::new(AtomicUsize::new(1));
    let response_mode = Arc::new(AtomicUsize::new(0));
    let call_started = Arc::new(Notify::new());
    let call_dropped = Arc::new(Notify::new());
    let state = Fixture {
        calls: calls.clone(),
        schema_version: schema_version.clone(),
        response_mode: response_mode.clone(),
        call_started: call_started.clone(),
        call_dropped: call_dropped.clone(),
        version: "0.4.3",
    };
    let router = Router::new()
        .route("/healthz", get(health))
        .route("/mcp", post(mcp))
        .with_state(state);
    let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    (
        address,
        task,
        calls,
        schema_version,
        response_mode,
        call_started,
        call_dropped,
    )
}

async fn runtime_fixture(
    backend_url: &str,
    grant_tool: &str,
    gateway_enabled: bool,
) -> (tempfile::TempDir, Database, ServerRuntime, Vec<String>) {
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
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) =
        identity::bootstrap_admin(&database, "admin", "MCP fixture administrator password")
            .await
            .unwrap();
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    identity::create_member_without_profile(
        &database,
        &admin.id,
        "allowed",
        "MCP fixture allowed password",
    )
    .await
    .unwrap();
    identity::create_member_without_profile(
        &database,
        &admin.id,
        "denied",
        "MCP fixture denied password",
    )
    .await
    .unwrap();
    ToolPolicyService::new(database.clone())
        .set_user_rule("allowed", grant_tool, Some(GrantEffect::Allow), &admin.id)
        .await
        .unwrap();
    let allowed = identity::issue_api_token_with_tool_scopes(
        &database,
        "allowed",
        "gateway-test",
        true,
        true,
    )
    .await
    .unwrap();
    let denied =
        identity::issue_api_token_with_tool_scopes(&database, "denied", "gateway-test", true, true)
            .await
            .unwrap();

    let mut config = Config::default();
    config.server.api_key = Some("gateway-test-server-key".to_owned());
    config.tools.enabled = true;
    config.tools.gateway_enabled = gateway_enabled;
    config.tools.base_url = backend_url.to_owned();
    let process = ProcessRuntime::new(database.clone()).unwrap();
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "mcp-gateway-test".to_owned(),
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
    (
        directory,
        database,
        runtime,
        vec![allowed.token, denied.token],
    )
}

async fn raw_request(
    address: std::net::SocketAddr,
    token: &str,
    method: &str,
    headers: &[(&str, &str)],
    body: &str,
) -> String {
    raw_request_with_content_type(address, token, method, headers, "application/json", body).await
}

async fn raw_request_with_content_type(
    address: std::net::SocketAddr,
    token: &str,
    method: &str,
    headers: &[(&str, &str)],
    content_type: &str,
    body: &str,
) -> String {
    let mut stream = TcpStream::connect(address).await.unwrap();
    let extra = headers
        .iter()
        .map(|(name, value)| format!("{name}: {value}\r\n"))
        .collect::<String>();
    let request = format!(
        "{method} /api/tools/v1/mcp HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {token}\r\nContent-Type: {content_type}\r\nAccept: application/json, text/event-stream\r\n{extra}Connection: close\r\nContent-Length: {}\r\n\r\n{body}",
        address.port(),
        body.len()
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(4), stream.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    String::from_utf8(response).unwrap()
}

async fn raw_health_request(address: std::net::SocketAddr) -> String {
    let mut stream = TcpStream::connect(address).await.unwrap();
    let request = format!(
        "GET /v1/healthz HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        address.port()
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(4), stream.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    String::from_utf8(response).unwrap()
}

fn response_body(response: &str) -> &str {
    response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .unwrap()
}

fn session_id(response: &str) -> String {
    response
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("mcp-session-id")
                .then(|| value.trim().to_owned())
        })
        .unwrap()
}

#[tokio::test]
async fn mcp_sessions_filter_per_user_and_reauthorize_direct_calls() {
    let (backend, backend_task, calls, schema_version, response_mode, call_started, call_dropped) =
        fixture_server().await;
    let backend_url = format!("http://127.0.0.1:{}/mcp", backend.port());
    let (_directory, database, runtime, tokens) =
        runtime_fixture(&backend_url, "web_search", true).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let initialize = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{{\"protocolVersion\":\"{PROTOCOL}\",\"capabilities\":{{}},\"clientInfo\":{{\"name\":\"test\",\"version\":\"1\"}}}}}}"
    );
    let allowed_init = raw_request(address, &tokens[0], "POST", &[], &initialize).await;
    assert!(allowed_init.starts_with("HTTP/1.1 200"), "{allowed_init}");
    let allowed_session = session_id(&allowed_init);
    let list = r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#;
    let batch = r#"[{"jsonrpc":"2.0","id":9,"method":"tools/list"}]"#;
    let batch_response = raw_request(address, &tokens[0], "POST", &[], batch).await;
    assert_eq!(
        serde_json::from_str::<Value>(response_body(&batch_response)).unwrap()["error"]["code"],
        -32600,
        "JSON-RPC batches are rejected"
    );
    let cross_origin = raw_request(
        address,
        &tokens[0],
        "POST",
        &[("Origin", "https://attacker.example")],
        list,
    )
    .await;
    assert!(cross_origin.starts_with("HTTP/1.1 403"));
    let wrong_content_type =
        raw_request_with_content_type(address, &tokens[0], "POST", &[], "text/plain", &initialize)
            .await;
    assert!(wrong_content_type.starts_with("HTTP/1.1 415"));
    let oversized = " ".repeat(129 * 1024);
    let oversized_response = raw_request(address, &tokens[0], "POST", &[], &oversized).await;
    assert!(oversized_response.starts_with("HTTP/1.1 413"));
    let notification = r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#;
    assert!(
        raw_request(
            address,
            &tokens[0],
            "POST",
            &[("Mcp-Session-Id", &allowed_session)],
            notification
        )
        .await
        .starts_with("HTTP/1.1 202")
    );

    let allowed_list = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        list,
    )
    .await;
    let list_json: Value = serde_json::from_str(response_body(&allowed_list)).unwrap();
    assert_eq!(list_json["result"]["tools"].as_array().unwrap().len(), 1);
    assert_eq!(list_json["result"]["tools"][0]["name"], "web_search");
    let unsupported = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        r#"{"jsonrpc":"2.0","id":8,"method":"resources/list","params":{}}"#,
    )
    .await;
    assert_eq!(
        serde_json::from_str::<Value>(response_body(&unsupported)).unwrap()["error"]["code"],
        -32601
    );
    let wrong_protocol = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", "2024-11-05"),
        ],
        list,
    )
    .await;
    assert_eq!(
        serde_json::from_str::<Value>(response_body(&wrong_protocol)).unwrap()["error"]["code"],
        -32002
    );

    let denied_init = raw_request(address, &tokens[1], "POST", &[], &initialize).await;
    assert!(denied_init.starts_with("HTTP/1.1 200"), "{denied_init}");
    let denied_session = session_id(&denied_init);
    let denied_list = raw_request(
        address,
        &tokens[1],
        "POST",
        &[
            ("Mcp-Session-Id", &denied_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        list,
    )
    .await;
    let denied_json: Value = serde_json::from_str(response_body(&denied_list)).unwrap();
    assert_eq!(denied_json["result"]["tools"].as_array().unwrap().len(), 0);

    let denied_call = r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"test"}}}"#;
    let denied_result = raw_request(
        address,
        &tokens[1],
        "POST",
        &[
            ("Mcp-Session-Id", &denied_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        denied_call,
    )
    .await;
    let denied_json: Value = serde_json::from_str(response_body(&denied_result)).unwrap();
    assert_eq!(denied_json["error"]["code"], -32003);
    assert_eq!(
        calls.load(Ordering::SeqCst),
        0,
        "denied direct call never reaches backend"
    );

    let call = r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"test"}}}"#;
    let unknown_argument = r#"{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"test","unlisted":true}}}"#;
    let invalid_arguments = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        unknown_argument,
    )
    .await;
    let invalid_json: Value = serde_json::from_str(response_body(&invalid_arguments)).unwrap();
    assert_eq!(invalid_json["error"]["code"], -32602);
    assert_eq!(
        calls.load(Ordering::SeqCst),
        0,
        "schema-invalid arguments never reach backend"
    );

    schema_version.store(2, Ordering::SeqCst);
    let stale_call = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        call,
    )
    .await;
    let stale_json: Value = serde_json::from_str(response_body(&stale_call)).unwrap();
    assert_eq!(stale_json["error"]["code"], -32001);
    assert_eq!(
        calls.load(Ordering::SeqCst),
        0,
        "catalog drift invalidates prior discovery"
    );
    let refreshed_list = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        list,
    )
    .await;
    assert!(
        serde_json::from_str::<Value>(response_body(&refreshed_list)).unwrap()["result"]["tools"]
            .is_array()
    );
    let allowed_call_headers = [
        ("Mcp-Session-Id", allowed_session.as_str()),
        ("Mcp-Protocol-Version", PROTOCOL),
    ];
    let denied_list_headers = [
        ("Mcp-Session-Id", denied_session.as_str()),
        ("Mcp-Protocol-Version", PROTOCOL),
    ];
    let (result, concurrent_denied_list) = tokio::join!(
        raw_request(address, &tokens[0], "POST", &allowed_call_headers, call,),
        raw_request(address, &tokens[1], "POST", &denied_list_headers, list,),
    );
    let concurrent_denied_json: Value =
        serde_json::from_str(response_body(&concurrent_denied_list)).unwrap();
    assert!(
        concurrent_denied_json["result"]["tools"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let call_json: Value = serde_json::from_str(response_body(&result)).unwrap();
    assert_eq!(
        call_json["result"]["structuredContent"]["trust"],
        "external_untrusted"
    );
    assert_eq!(
        call_json["result"]["structuredContent"]["source_id"],
        "source-1"
    );
    assert_eq!(
        call_json["result"]["structuredContent"]["response_detail"], "compact",
        "the gateway selects Eggsearch's compact projection when available"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);

    response_mode.store(1, Ordering::SeqCst);
    let wrong_mime = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        call,
    )
    .await;
    let wrong_mime_json: Value = serde_json::from_str(response_body(&wrong_mime)).unwrap();
    assert_eq!(wrong_mime_json["error"]["code"], -32603);
    assert!(!wrong_mime.contains("not an MCP response"));

    response_mode.store(2, Ordering::SeqCst);
    let oversized_backend = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        call,
    )
    .await;
    let oversized_json: Value = serde_json::from_str(response_body(&oversized_backend)).unwrap();
    assert_eq!(oversized_json["error"]["code"], -32603);

    response_mode.store(3, Ordering::SeqCst);
    let abort_token = tokens[0].clone();
    let abort_session = allowed_session.clone();
    let abort_body = call.to_owned();
    let aborted_call = tokio::spawn(async move {
        raw_request(
            address,
            &abort_token,
            "POST",
            &[
                ("Mcp-Session-Id", &abort_session),
                ("Mcp-Protocol-Version", PROTOCOL),
            ],
            &abort_body,
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(4), call_started.notified())
        .await
        .expect("backend call reached the cancellation fixture");
    aborted_call.abort();
    let _ = aborted_call.await;
    tokio::time::timeout(Duration::from_secs(4), call_dropped.notified())
        .await
        .expect("client cancellation drops the backend operation");

    identity::revoke_api_tokens(&database, "allowed")
        .await
        .unwrap();
    let revoked = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &allowed_session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        list,
    )
    .await;
    assert!(
        revoked.starts_with("HTTP/1.1 401"),
        "revoked credentials lose existing sessions: {revoked}"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);

    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    backend_task.abort();
    database.close().await.unwrap();
}

#[tokio::test]
async fn disabled_gateway_route_is_not_registered() {
    let (backend, backend_task, calls, _, _, _, _) = fixture_server().await;
    let backend_url = format!("http://127.0.0.1:{}/mcp", backend.port());
    let (_directory, database, runtime, tokens) =
        runtime_fixture(&backend_url, "web_search", false).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let response = raw_request(
        address,
        &tokens[0],
        "POST",
        &[],
        r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}"#,
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 404"), "{response}");
    assert_eq!(calls.load(Ordering::SeqCst), 0);

    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    backend_task.abort();
    database.close().await.unwrap();
}

#[tokio::test]
async fn unavailable_backend_is_isolated_from_model_health() {
    let (backend, backend_task, _, _, _, _, _) = fixture_server().await;
    let backend_url = format!("http://127.0.0.1:{}/mcp", backend.port());
    backend_task.abort();
    let (_directory, database, runtime, tokens) =
        runtime_fixture(&backend_url, "web_search", true).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let initialize = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{{\"protocolVersion\":\"{PROTOCOL}\",\"capabilities\":{{}},\"clientInfo\":{{\"name\":\"test\",\"version\":\"1\"}}}}}}"
    );
    let initialized = raw_request(address, &tokens[0], "POST", &[], &initialize).await;
    let session = session_id(&initialized);
    let list = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#,
    )
    .await;
    let list: Value = serde_json::from_str(response_body(&list)).unwrap();
    assert_eq!(list["error"]["code"], -32000);
    assert!(
        raw_health_request(address)
            .await
            .starts_with("HTTP/1.1 200")
    );

    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    database.close().await.unwrap();
}

#[tokio::test]
#[ignore = "requires Eggsearch 0.4.3 at EGGPOOL_TEST_EGGSEARCH_URL"]
async fn live_eggsearch_gateway_initialize_list_and_call() {
    let backend_url = std::env::var("EGGPOOL_TEST_EGGSEARCH_URL")
        .expect("set EGGPOOL_TEST_EGGSEARCH_URL to the qualified loopback Eggsearch endpoint");
    let (_directory, database, runtime, tokens) =
        runtime_fixture(&backend_url, "provider_status", true).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server_task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let initialize = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{{\"protocolVersion\":\"{PROTOCOL}\",\"capabilities\":{{}},\"clientInfo\":{{\"name\":\"live-qualification\",\"version\":\"1\"}}}}}}"
    );
    let initialized = raw_request(address, &tokens[0], "POST", &[], &initialize).await;
    assert!(initialized.starts_with("HTTP/1.1 200"), "{initialized}");
    let session = session_id(&initialized);
    let list = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#,
    )
    .await;
    let list: Value = serde_json::from_str(response_body(&list)).unwrap();
    assert_eq!(list["result"]["tools"].as_array().unwrap().len(), 1);
    assert_eq!(list["result"]["tools"][0]["name"], "provider_status");
    let call = raw_request(
        address,
        &tokens[0],
        "POST",
        &[
            ("Mcp-Session-Id", &session),
            ("Mcp-Protocol-Version", PROTOCOL),
        ],
        r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"provider_status","arguments":{}}}"#,
    )
    .await;
    let call: Value = serde_json::from_str(response_body(&call)).unwrap();
    assert_eq!(call["jsonrpc"], "2.0");
    assert_eq!(call["id"], 3);
    assert!(call["result"]["content"].as_array().is_some());

    handle.request_shutdown(ShutdownReason::Requested);
    tokio::time::timeout(Duration::from_secs(6), server_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    database.close().await.unwrap();
}
