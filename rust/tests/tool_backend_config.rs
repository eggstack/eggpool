use std::{collections::BTreeSet, sync::Arc};

use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{HeaderMap, Response, StatusCode},
    routing::{get, post},
};
use eggpool::{
    Config,
    operations::tools::{BackendStatus, doctor},
};
use serde_json::{Value, json};
use tokio::net::TcpListener;

#[derive(Clone)]
struct Fixture {
    service: &'static str,
    protocol: &'static str,
    version: &'static str,
    catalog: Vec<&'static str>,
    schema_padding: usize,
    health_status: StatusCode,
    health_delay_ms: u64,
}

const TOOLS: &[&str] = &[
    "web_search",
    "web_fetch",
    "batch_fetch",
    "provider_status",
    "repo_search",
    "repo_fetch",
    "repo_map",
    "security_search",
    "research_search",
    "build_evidence_bundle",
];

async fn health(State(fixture): State<Arc<Fixture>>) -> Response<Body> {
    if fixture.health_delay_ms > 0 {
        tokio::time::sleep(std::time::Duration::from_millis(fixture.health_delay_ms)).await;
    }
    let body = json!({
        "service": fixture.service,
        "status": "ready",
        "version": fixture.version,
        "protocol": fixture.protocol,
    });
    Response::builder()
        .status(fixture.health_status)
        .header("content-type", "application/json")
        .body(Body::from(
            serde_json::to_vec(&body).expect("serialize health"),
        ))
        .expect("health response")
}

async fn mcp(
    State(fixture): State<Arc<Fixture>>,
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
            .expect("response");
    }
    let result = match method {
        "initialize" => json!({
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": fixture.service, "version": fixture.version},
        }),
        "tools/list" => {
            json!({"tools": fixture.catalog.iter().map(|name| json!({"name":name,"description":"x".repeat(fixture.schema_padding),"inputSchema":{"type":"object"}})).collect::<Vec<_>>() })
        }
        _ => json!({"error":{"code":-32601,"message":"unknown method"}}),
    };
    let body = json!({"jsonrpc":"2.0","id":request.get("id"),"result":result});
    let payload = format!(
        "data: {}\n\n",
        serde_json::to_string(&body).expect("serialize")
    );
    let mut response = Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/event-stream")
        .body(Body::from(payload))
        .expect("response");
    if method == "initialize" {
        response
            .headers_mut()
            .insert("mcp-session-id", "fixture-session".parse().expect("header"));
    } else if headers.get("mcp-session-id").is_none() {
        return Response::builder()
            .status(StatusCode::BAD_REQUEST)
            .body(Body::empty())
            .expect("response");
    }
    response
}

async fn run_fixture(fixture: Fixture) -> (BackendStatus, Option<String>, Option<usize>) {
    run_fixture_with_timeout(fixture, 3.0, 2.0).await
}

async fn run_fixture_with_timeout(
    fixture: Fixture,
    read_timeout_s: f64,
    connect_timeout_s: f64,
) -> (BackendStatus, Option<String>, Option<usize>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind fixture");
    let address = listener.local_addr().expect("fixture address");
    let state = Arc::new(fixture);
    let app = Router::new()
        .route("/healthz", get(health))
        .route("/mcp", post(mcp))
        .with_state(state);
    let server =
        tokio::spawn(async move { axum::serve(listener, app).await.expect("serve fixture") });
    let mut config = Config::default();
    config.tools.enabled = true;
    config.tools.base_url = format!("http://127.0.0.1:{}/mcp", address.port());
    config.tools.read_timeout_s = read_timeout_s;
    config.tools.connect_timeout_s = connect_timeout_s;
    let result = doctor(&config).await;
    server.abort();
    (result.status, result.service_version, result.tool_count)
}

#[tokio::test]
async fn disabled_backend_does_not_probe_and_live_probe_validates_service_protocol_and_catalog() {
    let disabled = doctor(&Config::default()).await;
    assert_eq!(disabled.status, BackendStatus::Disabled);

    let ready = run_fixture(Fixture {
        service: "eggsearch",
        protocol: "streamable-http",
        version: "0.4.3",
        catalog: TOOLS.to_vec(),
        schema_padding: 0,
        health_status: StatusCode::OK,
        health_delay_ms: 0,
    })
    .await;
    assert_eq!(
        ready,
        (
            BackendStatus::Ready,
            Some("0.4.3".into()),
            Some(TOOLS.len())
        )
    );

    let wrong_service = run_fixture(Fixture {
        service: "other-service",
        protocol: "streamable-http",
        version: "1.0.0",
        catalog: TOOLS.to_vec(),
        schema_padding: 0,
        health_status: StatusCode::OK,
        health_delay_ms: 0,
    })
    .await;
    assert_eq!(wrong_service.0, BackendStatus::WrongService);

    let incompatible = run_fixture(Fixture {
        service: "eggsearch",
        protocol: "streamable-http",
        version: "0.4.3",
        catalog: vec!["web_search"],
        schema_padding: 0,
        health_status: StatusCode::OK,
        health_delay_ms: 0,
    })
    .await;
    assert_eq!(incompatible.0, BackendStatus::IncompatibleCatalog);

    let wrong_protocol = run_fixture(Fixture {
        service: "eggsearch",
        protocol: "legacy-sse",
        version: "0.4.3",
        catalog: TOOLS.to_vec(),
        schema_padding: 0,
        health_status: StatusCode::OK,
        health_delay_ms: 0,
    })
    .await;
    assert_eq!(wrong_protocol.0, BackendStatus::IncompatibleProtocol);

    let redirect = run_fixture(Fixture {
        service: "eggsearch",
        protocol: "streamable-http",
        version: "0.4.3",
        catalog: TOOLS.to_vec(),
        schema_padding: 0,
        health_status: StatusCode::FOUND,
        health_delay_ms: 0,
    })
    .await;
    assert_eq!(redirect.0, BackendStatus::Unreachable);

    let slow = run_fixture_with_timeout(
        Fixture {
            service: "eggsearch",
            protocol: "streamable-http",
            version: "0.4.3",
            catalog: TOOLS.to_vec(),
            schema_padding: 0,
            health_status: StatusCode::OK,
            health_delay_ms: 300,
        },
        0.1,
        0.1,
    )
    .await;
    assert_eq!(slow.0, BackendStatus::Unreachable);

    let mut absent = Config::default();
    absent.tools.enabled = true;
    absent.tools.base_url = "http://127.0.0.1:1/mcp".into();
    assert_eq!(doctor(&absent).await.status, BackendStatus::Unreachable);
}

#[tokio::test]
async fn oversized_discovery_response_is_rejected() {
    let oversized = run_fixture(Fixture {
        service: "eggsearch",
        protocol: "streamable-http",
        version: "0.4.3",
        catalog: TOOLS.to_vec(),
        schema_padding: 110_000,
        health_status: StatusCode::OK,
        health_delay_ms: 0,
    })
    .await;
    assert_eq!(oversized.0, BackendStatus::MalformedResponse);
}

#[tokio::test]
async fn live_installed_eggsearch_qualification_when_explicitly_requested() {
    let Ok(base_url) = std::env::var("EGGPOOL_TEST_EGGSEARCH_URL") else {
        return;
    };
    let mut config = Config::default();
    config.tools.enabled = true;
    config.tools.base_url = base_url;
    let result = doctor(&config).await;
    assert_eq!(result.status, BackendStatus::Ready, "{result:?}");
    assert!(result.service_version.is_some());
    assert_eq!(result.tool_count, Some(TOOLS.len()));
}

#[test]
fn expected_tool_contract_has_no_duplicate_names() {
    assert_eq!(
        TOOLS.iter().copied().collect::<BTreeSet<_>>().len(),
        TOOLS.len()
    );
}
