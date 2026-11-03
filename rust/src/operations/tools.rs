//! Explicit, bounded diagnostics for the optional local tool service.

use std::time::Duration;

use bytes::Bytes;
use eggfetch_core::{Client, NativeRequestOptions, Timeout};
use http::{HeaderMap, Method, Request, StatusCode, Uri, header};
use http_body_util::{BodyExt, Full, Limited};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config::{Config, ToolBackendConfig};

const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MCP_PROTOCOL: &str = "2025-03-26";
const EXPECTED_TOOLS: &[&str] = &[
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbeFailure {
    Unreachable,
    Malformed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendStatus {
    Disabled,
    Unreachable,
    WrongService,
    IncompatibleProtocol,
    IncompatibleCatalog,
    MalformedResponse,
    Ready,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendDiagnostic {
    pub schema: &'static str,
    pub status: BackendStatus,
    pub service_version: Option<String>,
    pub tool_count: Option<usize>,
}

impl BackendDiagnostic {
    fn new(status: BackendStatus) -> Self {
        Self {
            schema: "eggpool.tools.doctor/v1",
            status,
            service_version: None,
            tool_count: None,
        }
    }
}

/// Probe the one configured backend. This function is only called by an
/// explicit operator command; it creates no retained client or background task.
pub async fn doctor(config: &Config) -> BackendDiagnostic {
    let backend = &config.tools;
    if !backend.enabled {
        return BackendDiagnostic::new(BackendStatus::Disabled);
    }
    doctor_backend(backend).await
}

async fn doctor_backend(backend: &ToolBackendConfig) -> BackendDiagnostic {
    let Ok(mcp_uri) = backend.base_url.parse::<Uri>() else {
        return BackendDiagnostic::new(BackendStatus::MalformedResponse);
    };
    let Some(authority) = mcp_uri.authority() else {
        return BackendDiagnostic::new(BackendStatus::MalformedResponse);
    };
    let host = authority.host();
    let Some(port) = authority.port_u16() else {
        return BackendDiagnostic::new(BackendStatus::MalformedResponse);
    };
    let health_uri = format!("http://{host}:{port}/healthz");
    let Ok(health_uri) = health_uri.parse::<Uri>() else {
        return BackendDiagnostic::new(BackendStatus::MalformedResponse);
    };
    let client = Client::new();
    let connect = Duration::from_secs_f64(backend.connect_timeout_s);
    let read = Duration::from_secs_f64(backend.read_timeout_s);
    let health = match send_json(&client, Method::GET, health_uri, None, connect, read, None).await
    {
        Ok(response) if response.0 == StatusCode::OK => response.2,
        Ok(_) | Err(ProbeFailure::Unreachable) => {
            return BackendDiagnostic::new(BackendStatus::Unreachable);
        }
        Err(ProbeFailure::Malformed) => {
            return BackendDiagnostic::new(BackendStatus::MalformedResponse);
        }
    };
    let Ok(health) = serde_json::from_slice::<Value>(&health) else {
        return BackendDiagnostic::new(BackendStatus::MalformedResponse);
    };
    let service = health.get("service").and_then(Value::as_str);
    let version = health.get("version").and_then(Value::as_str);
    let protocol = health.get("protocol").and_then(Value::as_str);
    if service != Some("eggsearch") {
        return BackendDiagnostic::new(BackendStatus::WrongService);
    }
    if protocol != Some("streamable-http")
        || version.is_none_or(str::is_empty)
        || health.get("status").and_then(Value::as_str) != Some("ready")
    {
        return BackendDiagnostic::new(BackendStatus::IncompatibleProtocol);
    }

    let initialize = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "eggpool-tools-doctor", "version": env!("CARGO_PKG_VERSION")}
        }
    });
    let initialized = match send_json(
        &client,
        Method::POST,
        mcp_uri.clone(),
        Some(initialize),
        connect,
        read,
        None,
    )
    .await
    {
        Ok((status, headers, body)) if status.is_success() => {
            let Some(payload) = parse_mcp_payload(&headers, &body) else {
                return BackendDiagnostic::new(BackendStatus::MalformedResponse);
            };
            if payload.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
                || payload.get("id").and_then(Value::as_u64) != Some(1)
            {
                return BackendDiagnostic::new(BackendStatus::MalformedResponse);
            }
            let Some(result) = payload.get("result") else {
                return BackendDiagnostic::new(BackendStatus::MalformedResponse);
            };
            let server = result
                .get("serverInfo")
                .and_then(|v| v.get("name"))
                .and_then(Value::as_str);
            if server != Some("eggsearch") {
                return BackendDiagnostic::new(BackendStatus::WrongService);
            }
            let negotiated_version = result
                .get("serverInfo")
                .and_then(|v| v.get("version"))
                .and_then(Value::as_str);
            if negotiated_version != version {
                return BackendDiagnostic::new(BackendStatus::WrongService);
            }
            let negotiated = result.get("protocolVersion").and_then(Value::as_str);
            if negotiated != Some(MCP_PROTOCOL) {
                return BackendDiagnostic::new(BackendStatus::IncompatibleProtocol);
            }
            (
                headers.get("mcp-session-id").cloned(),
                result
                    .get("serverInfo")
                    .and_then(|v| v.get("version"))
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            )
        }
        Ok(_) => return BackendDiagnostic::new(BackendStatus::IncompatibleProtocol),
        Err(ProbeFailure::Unreachable) => {
            return BackendDiagnostic::new(BackendStatus::Unreachable);
        }
        Err(ProbeFailure::Malformed) => {
            return BackendDiagnostic::new(BackendStatus::MalformedResponse);
        }
    };

    let notify = json!({"jsonrpc":"2.0","method":"notifications/initialized"});
    if let Err(error) = send_json(
        &client,
        Method::POST,
        mcp_uri.clone(),
        Some(notify),
        connect,
        read,
        initialized.0.as_ref(),
    )
    .await
    {
        return BackendDiagnostic::new(match error {
            ProbeFailure::Unreachable => BackendStatus::Unreachable,
            ProbeFailure::Malformed => BackendStatus::MalformedResponse,
        });
    }
    let list = json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}});
    let response = match send_json(
        &client,
        Method::POST,
        mcp_uri,
        Some(list),
        connect,
        read,
        initialized.0.as_ref(),
    )
    .await
    {
        Ok((status, headers, body)) if status.is_success() => parse_mcp_payload(&headers, &body),
        Ok(_) => return BackendDiagnostic::new(BackendStatus::IncompatibleCatalog),
        Err(ProbeFailure::Unreachable) => {
            return BackendDiagnostic::new(BackendStatus::Unreachable);
        }
        Err(ProbeFailure::Malformed) => {
            return BackendDiagnostic::new(BackendStatus::MalformedResponse);
        }
    };
    let Some(tools) = response
        .filter(|payload| {
            payload.get("jsonrpc").and_then(Value::as_str) == Some("2.0")
                && payload.get("id").and_then(Value::as_u64) == Some(2)
        })
        .and_then(|payload| payload.get("result").cloned())
        .and_then(|result| result.get("tools").cloned())
        .and_then(|tools| tools.as_array().cloned())
    else {
        return BackendDiagnostic::new(BackendStatus::MalformedResponse);
    };
    let names = tools
        .iter()
        .filter_map(|tool| tool.get("name").and_then(Value::as_str))
        .collect::<std::collections::BTreeSet<_>>();
    if tools.len() != EXPECTED_TOOLS.len()
        || names.len() != tools.len()
        || EXPECTED_TOOLS.iter().any(|name| !names.contains(name))
    {
        return BackendDiagnostic::new(BackendStatus::IncompatibleCatalog);
    }
    BackendDiagnostic {
        schema: "eggpool.tools.doctor/v1",
        status: BackendStatus::Ready,
        service_version: initialized.1.or_else(|| version.map(str::to_owned)),
        tool_count: Some(tools.len()),
    }
}

async fn send_json(
    client: &Client,
    method: Method,
    uri: Uri,
    value: Option<Value>,
    connect: Duration,
    read: Duration,
    session_id: Option<&http::HeaderValue>,
) -> Result<(StatusCode, HeaderMap, Vec<u8>), ProbeFailure> {
    let mut builder = Request::builder().method(method).uri(uri);
    if value.is_some() {
        builder = builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::ACCEPT, "application/json, text/event-stream")
            .header("mcp-protocol-version", MCP_PROTOCOL);
    }
    if let Some(session_id) = session_id {
        builder = builder.header("mcp-session-id", session_id);
    }
    let bytes = value
        .map(|value| Bytes::from(serde_json::to_vec(&value).unwrap_or_default()))
        .unwrap_or_default();
    let request = builder
        .body(Full::new(bytes))
        .map_err(|_| ProbeFailure::Malformed)?;
    let options = NativeRequestOptions::default().timeout(Timeout {
        connect: Some(connect),
        read: Some(read),
        total: Some(connect.saturating_add(read)),
        ..Timeout::default()
    });
    let response = client
        .execute_http_body(request, options)
        .await
        .map_err(|_| ProbeFailure::Unreachable)?;
    let status = response.status();
    let headers = response.headers().clone();
    let limited = Limited::new(response, MAX_RESPONSE_BYTES);
    let body = limited
        .collect()
        .await
        .map_err(|_| ProbeFailure::Malformed)?
        .to_bytes()
        .to_vec();
    Ok((status, headers, body))
}

fn parse_mcp_payload(headers: &HeaderMap, body: &[u8]) -> Option<Value> {
    let content_type = headers.get(header::CONTENT_TYPE)?.to_str().ok()?;
    if content_type.starts_with("application/json") {
        return serde_json::from_slice(body).ok();
    }
    if !content_type.starts_with("text/event-stream") {
        return None;
    }
    let text = std::str::from_utf8(body).ok()?;
    text.lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .find_map(|data| serde_json::from_str(data).ok())
}
