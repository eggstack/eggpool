//! Authenticated, bounded bridge to the one operator-configured Eggsearch MCP
//! backend. Downstream sessions bind to an agent credential; backend sessions
//! are short-lived and scoped to one list/call operation.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use bytes::Bytes as HttpBytes;
use eggfetch_core::{Client, NativeRequestOptions, Timeout};
use http::{Method, Request, Uri};
use http_body_util::{BodyExt, Full, Limited};
use serde_json::{Map, Value, json};
use sha2::Digest;
use tokio::sync::Semaphore;

use crate::{
    config::ToolBackendConfig,
    identity::{AuthenticatedPrincipal, PrincipalKind},
    tool_policy::{
        BACKEND_ID, DISCOVERY_SCOPE, INVOCATION_SCOPE, KNOWN_TOOLS, ToolAction, ToolPolicyService,
    },
};

pub(super) const PATH: &str = "/api/tools/v1/mcp";
const MCP_PROTOCOL: &str = "2025-03-26";
const EGGSEARCH_VERSION: &str = "0.4.3";
const MAX_REQUEST_BYTES: usize = 128 * 1024;
const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_SCHEMA_BYTES: usize = 64 * 1024;
const MAX_ARGUMENT_BYTES: usize = 32 * 1024;
const MAX_JSON_DEPTH: usize = 32;
const MAX_SESSIONS: usize = 1024;
const SESSION_TTL: Duration = Duration::from_secs(30 * 60);

#[derive(Clone)]
pub(super) struct Gateway {
    backend: ToolBackendConfig,
    sessions: Arc<Mutex<HashMap<String, GatewaySession>>>,
    permits: Arc<Semaphore>,
}

#[derive(Clone)]
struct GatewaySession {
    user_id: String,
    credential_id: String,
    auth_epoch: i64,
    expires_at: Instant,
    catalog_fingerprint: Option<String>,
}

struct BackendSession {
    client: Client,
    uri: Uri,
    session_id: HeaderValue,
    connect_timeout: Duration,
    read_timeout: Duration,
}

#[derive(Debug)]
enum GatewayError {
    Unavailable,
    Incompatible,
    Malformed,
}

impl Gateway {
    pub(super) fn new(backend: ToolBackendConfig) -> Self {
        Self {
            backend,
            sessions: Arc::new(Mutex::new(HashMap::new())),
            permits: Arc::new(Semaphore::new(16)),
        }
    }

    pub(super) fn enabled(&self) -> bool {
        self.backend.enabled && self.backend.gateway_enabled
    }

    async fn handle(
        &self,
        database: crate::db::Database,
        principal: AuthenticatedPrincipal,
        headers: HeaderMap,
        body: Bytes,
    ) -> Response {
        if !self.enabled() {
            return StatusCode::NOT_FOUND.into_response();
        }
        if !valid_origin_host(&headers) {
            return StatusCode::FORBIDDEN.into_response();
        }
        if !headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                value
                    .split(';')
                    .next()
                    .is_some_and(|mime| mime.trim().eq_ignore_ascii_case("application/json"))
            })
        {
            return StatusCode::UNSUPPORTED_MEDIA_TYPE.into_response();
        }
        if !headers
            .get(header::ACCEPT)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                value.split(',').any(|item| {
                    let mime = item.split(';').next().unwrap_or_default().trim();
                    mime.eq_ignore_ascii_case("application/json")
                        || mime.eq_ignore_ascii_case("text/event-stream")
                        || mime == "*/*"
                })
            })
        {
            return StatusCode::NOT_ACCEPTABLE.into_response();
        }
        if principal.kind != PrincipalKind::AgentToken {
            return StatusCode::FORBIDDEN.into_response();
        }
        if principal.user_id.is_none()
            || principal.credential_id.is_none()
            || principal.auth_epoch.is_none()
        {
            return StatusCode::FORBIDDEN.into_response();
        }
        if body.len() > MAX_REQUEST_BYTES {
            return StatusCode::PAYLOAD_TOO_LARGE.into_response();
        }
        let Ok(request) = serde_json::from_slice::<Value>(&body) else {
            return json_rpc_error(Value::Null, -32700, "Parse error");
        };
        if !json_depth_within(&request, MAX_JSON_DEPTH) {
            return json_rpc_error(
                request.get("id").cloned().unwrap_or(Value::Null),
                -32600,
                "Invalid request",
            );
        }
        let Some(method) = request.get("method").and_then(Value::as_str) else {
            return json_rpc_error(
                request.get("id").cloned().unwrap_or(Value::Null),
                -32600,
                "Invalid request",
            );
        };
        if request.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || request.as_object().is_none_or(|object| {
                object
                    .keys()
                    .any(|key| !matches!(key.as_str(), "jsonrpc" | "id" | "method" | "params"))
            })
        {
            return json_rpc_error(
                request.get("id").cloned().unwrap_or(Value::Null),
                -32600,
                "Invalid request",
            );
        }
        if method == "notifications/initialized" && request.get("id").is_none() {
            return if self.session_for(&principal, &headers).is_some() {
                StatusCode::ACCEPTED.into_response()
            } else {
                StatusCode::BAD_REQUEST.into_response()
            };
        }
        let Some(id) = valid_request_id(&request) else {
            return json_rpc_error(Value::Null, -32600, "Invalid request");
        };

        match method {
            "initialize" => self.initialize(&principal, &headers, id, &request),
            "notifications/initialized" => json_rpc_error(id, -32600, "Invalid notification"),
            "tools/list" => {
                if request
                    .get("params")
                    .is_some_and(|params| !params.as_object().is_some_and(Map::is_empty))
                {
                    return json_rpc_error(id, -32602, "Unknown tools/list parameters");
                }
                if let Err(error) = self.check_protocol(&headers) {
                    return protocol_error(id, error);
                }
                let Some(session_id) = self.session_for(&principal, &headers) else {
                    return StatusCode::BAD_REQUEST.into_response();
                };
                self.list(database, principal, id, session_id).await
            }
            "tools/call" => {
                if let Err(error) = self.check_protocol(&headers) {
                    return protocol_error(id, error);
                }
                let Some(session_id) = self.session_for(&principal, &headers) else {
                    return StatusCode::BAD_REQUEST.into_response();
                };
                self.call(database, principal, id, request.get("params"), session_id)
                    .await
            }
            _ => json_rpc_error(id, -32601, "Method not found"),
        }
    }

    fn initialize(
        &self,
        principal: &AuthenticatedPrincipal,
        headers: &HeaderMap,
        id: Value,
        request: &Value,
    ) -> Response {
        if headers.get("mcp-session-id").is_some()
            || !principal
                .scopes
                .iter()
                .any(|scope| scope == DISCOVERY_SCOPE || scope == INVOCATION_SCOPE)
        {
            return StatusCode::FORBIDDEN.into_response();
        }
        let Some(params) = request.get("params").and_then(Value::as_object) else {
            return json_rpc_error(id, -32602, "Invalid initialize parameters");
        };
        if params.get("protocolVersion").and_then(Value::as_str) != Some(MCP_PROTOCOL)
            || params.keys().any(|key| {
                !matches!(
                    key.as_str(),
                    "protocolVersion" | "capabilities" | "clientInfo"
                )
            })
        {
            return json_rpc_error(id, -32602, "Unsupported protocol version or parameters");
        }
        let (Some(user_id), Some(credential_id), Some(auth_epoch)) = (
            principal.user_id.clone(),
            principal.credential_id.clone(),
            principal.auth_epoch,
        ) else {
            return StatusCode::FORBIDDEN.into_response();
        };
        let mut session_bytes = [0u8; 32];
        if getrandom::fill(&mut session_bytes).is_err() {
            return StatusCode::SERVICE_UNAVAILABLE.into_response();
        }
        let session_id = hex(&session_bytes);
        let now = Instant::now();
        let mut sessions = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        sessions.retain(|_, session| session.expires_at > now);
        if sessions.len() >= MAX_SESSIONS {
            return StatusCode::SERVICE_UNAVAILABLE.into_response();
        }
        sessions.insert(
            session_id.clone(),
            GatewaySession {
                user_id,
                credential_id,
                auth_epoch,
                expires_at: now + SESSION_TTL,
                catalog_fingerprint: None,
            },
        );
        let mut response = json_response(json!({
            "jsonrpc":"2.0",
            "id":id,
            "result":{
                "protocolVersion":MCP_PROTOCOL,
                "capabilities":{"tools":{"listChanged":false}},
                "serverInfo":{"name":"eggpool-tools","version":env!("CARGO_PKG_VERSION")}
            }
        }));
        if let Ok(value) = HeaderValue::from_str(&session_id) {
            response.headers_mut().insert("mcp-session-id", value);
            response.headers_mut().insert(
                "mcp-protocol-version",
                HeaderValue::from_static(MCP_PROTOCOL),
            );
        }
        response
    }

    fn check_protocol(&self, headers: &HeaderMap) -> Result<(), GatewayError> {
        if headers
            .get("mcp-protocol-version")
            .and_then(|value| value.to_str().ok())
            == Some(MCP_PROTOCOL)
        {
            Ok(())
        } else {
            Err(GatewayError::Incompatible)
        }
    }

    fn session_for(
        &self,
        principal: &AuthenticatedPrincipal,
        headers: &HeaderMap,
    ) -> Option<String> {
        let supplied = headers.get("mcp-session-id")?.to_str().ok()?;
        let user_id = principal.user_id.as_deref()?;
        let credential_id = principal.credential_id.as_deref()?;
        let auth_epoch = principal.auth_epoch?;
        let now = Instant::now();
        let mut sessions = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        sessions.retain(|_, session| session.expires_at > now);
        let session = sessions.get_mut(supplied)?;
        if session.user_id != user_id
            || session.credential_id != credential_id
            || session.auth_epoch != auth_epoch
        {
            return None;
        }
        session.expires_at = now + SESSION_TTL;
        Some(supplied.to_owned())
    }

    async fn list(
        &self,
        database: crate::db::Database,
        principal: AuthenticatedPrincipal,
        id: Value,
        session_id: String,
    ) -> Response {
        let Ok(_permit) = Arc::clone(&self.permits).acquire_owned().await else {
            return json_rpc_error(id, -32000, "Tool backend unavailable");
        };
        let catalog = match self.fetch_catalog().await {
            Ok(catalog) => catalog,
            Err(error) => return protocol_error(id, error),
        };
        let policy = ToolPolicyService::new(database);
        let mut visible = Vec::new();
        for tool in &catalog.tools {
            let Some(name) = tool.get("name").and_then(Value::as_str) else {
                return json_rpc_error(id, -32603, "Invalid tool catalog");
            };
            match policy
                .authorize_tool(&principal, BACKEND_ID, name, ToolAction::Discover)
                .await
            {
                Ok(true) => visible.push(tool.clone()),
                Ok(false) => {}
                Err(_) => return json_rpc_error(id, -32000, "Tool authorization unavailable"),
            }
        }
        if !self.remember_catalog(&session_id, &catalog.fingerprint) {
            return StatusCode::BAD_REQUEST.into_response();
        }
        json_response(json!({"jsonrpc":"2.0","id":id,"result":{"tools":visible}}))
    }

    async fn call(
        &self,
        database: crate::db::Database,
        principal: AuthenticatedPrincipal,
        id: Value,
        params: Option<&Value>,
        session_id: String,
    ) -> Response {
        let Some(params) = params.and_then(Value::as_object) else {
            return json_rpc_error(id, -32602, "Invalid tool parameters");
        };
        if params
            .keys()
            .any(|key| !matches!(key.as_str(), "name" | "arguments"))
        {
            return json_rpc_error(id, -32602, "Unknown tool parameters");
        }
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return json_rpc_error(id, -32602, "Invalid tool name");
        };
        let Some(arguments) = params.get("arguments").and_then(Value::as_object) else {
            return json_rpc_error(id, -32602, "Tool arguments must be an object");
        };
        if !KNOWN_TOOLS.contains(&name)
            || serde_json::to_vec(arguments).map_or(true, |bytes| bytes.len() > MAX_ARGUMENT_BYTES)
            || !json_depth_within(&Value::Object(arguments.clone()), MAX_JSON_DEPTH)
        {
            return json_rpc_error(id, -32602, "Invalid tool arguments");
        }
        let policy = ToolPolicyService::new(database);
        match policy
            .authorize_tool(&principal, BACKEND_ID, name, ToolAction::Invoke)
            .await
        {
            Ok(true) => {}
            Ok(false) => return json_rpc_error(id, -32003, "Tool is not authorized"),
            Err(_) => return json_rpc_error(id, -32000, "Tool authorization unavailable"),
        }
        let Ok(_permit) = Arc::clone(&self.permits).acquire_owned().await else {
            return json_rpc_error(id, -32000, "Tool backend unavailable");
        };
        let mut upstream = match self.open_backend().await {
            Ok(upstream) => upstream,
            Err(error) => return protocol_error(id, error),
        };
        let catalog = match upstream.list().await {
            Ok(catalog) => catalog,
            Err(error) => return protocol_error(id, error),
        };
        let Some(tool) = catalog
            .tools
            .iter()
            .find(|tool| tool.get("name").and_then(Value::as_str) == Some(name))
        else {
            return json_rpc_error(id, -32602, "Unknown tool");
        };
        let Some(schema) = tool.get("inputSchema") else {
            return json_rpc_error(id, -32603, "Invalid tool catalog");
        };
        if !schema_accepts(schema, &Value::Object(arguments.clone()), 0) {
            return json_rpc_error(id, -32602, "Tool arguments do not match the current schema");
        }
        if self.catalog_changed(&session_id, &catalog.fingerprint) {
            return json_rpc_error(id, -32001, "Tool catalog changed; refresh discovery");
        }
        let result = match upstream.call(name, Value::Object(arguments.clone())).await {
            Ok(result) => result,
            Err(error) => return protocol_error(id, error),
        };
        json_response(json!({"jsonrpc":"2.0","id":id,"result":result}))
    }

    fn remember_catalog(&self, session_id: &str, fingerprint: &str) -> bool {
        let mut sessions = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let Some(session) = sessions.get_mut(session_id) else {
            return false;
        };
        session.catalog_fingerprint = Some(fingerprint.to_owned());
        true
    }

    fn catalog_changed(&self, session_id: &str, fingerprint: &str) -> bool {
        let sessions = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        sessions
            .get(session_id)
            .and_then(|session| session.catalog_fingerprint.as_deref())
            .is_some_and(|previous| previous != fingerprint)
    }

    async fn fetch_catalog(&self) -> Result<Catalog, GatewayError> {
        self.open_backend().await?.list().await
    }

    async fn open_backend(&self) -> Result<BackendSession, GatewayError> {
        let uri = self
            .backend
            .base_url
            .parse::<Uri>()
            .map_err(|_| GatewayError::Incompatible)?;
        let client = Client::new();
        let connect_timeout = Duration::from_secs_f64(self.backend.connect_timeout_s);
        let read_timeout = Duration::from_secs_f64(self.backend.read_timeout_s);
        let request = json!({
            "jsonrpc":"2.0","id":1,"method":"initialize",
            "params":{"protocolVersion":MCP_PROTOCOL,"capabilities":{},"clientInfo":{"name":"eggpool-gateway","version":env!("CARGO_PKG_VERSION")}}
        });
        let (status, headers, body) =
            send_json(&client, &uri, request, connect_timeout, read_timeout, None).await?;
        if !status.is_success() {
            return Err(GatewayError::Unavailable);
        }
        let payload = parse_payload(&headers, &body)?;
        let result = payload.get("result").ok_or(GatewayError::Incompatible)?;
        let protocol = result.get("protocolVersion").and_then(Value::as_str);
        let server = result
            .get("serverInfo")
            .and_then(|v| v.get("name"))
            .and_then(Value::as_str);
        let version = result
            .get("serverInfo")
            .and_then(|v| v.get("version"))
            .and_then(Value::as_str);
        if payload.get("id").and_then(Value::as_u64) != Some(1)
            || payload.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || protocol != Some(MCP_PROTOCOL)
            || server != Some(BACKEND_ID)
            || version != Some(EGGSEARCH_VERSION)
        {
            return Err(GatewayError::Incompatible);
        }
        let session_id = headers
            .get("mcp-session-id")
            .cloned()
            .ok_or(GatewayError::Incompatible)?;
        let notify = json!({"jsonrpc":"2.0","method":"notifications/initialized"});
        let (status, _, _) = send_json(
            &client,
            &uri,
            notify,
            connect_timeout,
            read_timeout,
            Some(&session_id),
        )
        .await?;
        if !status.is_success() {
            return Err(GatewayError::Incompatible);
        }
        Ok(BackendSession {
            client,
            uri,
            session_id,
            connect_timeout,
            read_timeout,
        })
    }
}

struct Catalog {
    tools: Vec<Value>,
    fingerprint: String,
}

impl BackendSession {
    async fn list(&mut self) -> Result<Catalog, GatewayError> {
        let request = json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}});
        let (status, headers, body) = send_json(
            &self.client,
            &self.uri,
            request,
            self.connect_timeout,
            self.read_timeout,
            Some(&self.session_id),
        )
        .await?;
        if !status.is_success() {
            return Err(GatewayError::Incompatible);
        }
        let payload = parse_payload(&headers, &body)?;
        let tools = payload
            .get("result")
            .and_then(|result| result.get("tools"))
            .and_then(Value::as_array)
            .ok_or(GatewayError::Malformed)?;
        if payload.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || payload.get("id").and_then(Value::as_u64) != Some(2)
            || payload
                .get("result")
                .and_then(|result| result.get("nextCursor"))
                .is_some()
            || tools.len() != KNOWN_TOOLS.len()
        {
            return Err(GatewayError::Incompatible);
        }
        let mut names = std::collections::BTreeSet::new();
        let mut normalized = Vec::with_capacity(tools.len());
        for tool in tools {
            let Some(object) = tool.as_object() else {
                return Err(GatewayError::Malformed);
            };
            let Some(name) = object.get("name").and_then(Value::as_str) else {
                return Err(GatewayError::Malformed);
            };
            if !KNOWN_TOOLS.contains(&name) || !names.insert(name.to_owned()) {
                return Err(GatewayError::Incompatible);
            }
            let Some(schema) = object.get("inputSchema") else {
                return Err(GatewayError::Malformed);
            };
            if !schema.is_object()
                || !json_depth_within(schema, MAX_JSON_DEPTH)
                || serde_json::to_vec(schema).map_or(true, |bytes| bytes.len() > MAX_SCHEMA_BYTES)
            {
                return Err(GatewayError::Malformed);
            }
            let mut safe = Map::new();
            safe.insert("name".into(), Value::String(name.to_owned()));
            for field in ["description", "inputSchema", "outputSchema", "annotations"] {
                if let Some(value) = object.get(field) {
                    if field == "description" && value.as_str().is_none_or(|text| text.len() > 8192)
                    {
                        return Err(GatewayError::Malformed);
                    }
                    if !json_depth_within(value, MAX_JSON_DEPTH) {
                        return Err(GatewayError::Malformed);
                    }
                    safe.insert(field.into(), value.clone());
                }
            }
            normalized.push(Value::Object(safe));
        }
        if KNOWN_TOOLS.iter().any(|name| !names.contains(*name)) {
            return Err(GatewayError::Incompatible);
        }
        normalized.sort_by(|left, right| left["name"].as_str().cmp(&right["name"].as_str()));
        let bytes = serde_json::to_vec(&normalized).map_err(|_| GatewayError::Malformed)?;
        if bytes.len() > MAX_RESPONSE_BYTES {
            return Err(GatewayError::Malformed);
        }
        let fingerprint = hex(&sha2::Sha256::digest(bytes));
        Ok(Catalog {
            tools: normalized,
            fingerprint,
        })
    }

    async fn call(&mut self, name: &str, arguments: Value) -> Result<Value, GatewayError> {
        let request = json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":name,"arguments":arguments}});
        let (status, headers, body) = send_json(
            &self.client,
            &self.uri,
            request,
            self.connect_timeout,
            self.read_timeout,
            Some(&self.session_id),
        )
        .await?;
        if !status.is_success() {
            return Err(GatewayError::Unavailable);
        }
        let payload = parse_payload(&headers, &body)?;
        if payload.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || payload.get("id").and_then(Value::as_u64) != Some(3)
        {
            return Err(GatewayError::Malformed);
        }
        if payload.get("error").is_some() {
            return Err(GatewayError::Unavailable);
        }
        let result = payload
            .get("result")
            .cloned()
            .ok_or(GatewayError::Malformed)?;
        if !json_depth_within(&result, MAX_JSON_DEPTH)
            || serde_json::to_vec(&result).map_or(true, |bytes| bytes.len() > MAX_RESPONSE_BYTES)
        {
            return Err(GatewayError::Malformed);
        }
        Ok(result)
    }
}

async fn send_json(
    client: &Client,
    uri: &Uri,
    value: Value,
    connect: Duration,
    read: Duration,
    session_id: Option<&HeaderValue>,
) -> Result<(StatusCode, HeaderMap, Vec<u8>), GatewayError> {
    let mut builder = Request::builder()
        .method(Method::POST)
        .uri(uri.clone())
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::ACCEPT, "application/json, text/event-stream")
        .header("mcp-protocol-version", MCP_PROTOCOL);
    if let Some(session_id) = session_id {
        builder = builder.header("mcp-session-id", session_id);
    }
    let bytes = serde_json::to_vec(&value).map_err(|_| GatewayError::Malformed)?;
    let request = builder
        .body(Full::new(HttpBytes::from(bytes)))
        .map_err(|_| GatewayError::Malformed)?;
    let options = NativeRequestOptions::default().timeout(Timeout {
        connect: Some(connect),
        read: Some(read),
        total: Some(connect.saturating_add(read)),
        ..Timeout::default()
    });
    let response = client
        .execute_http_body(request, options)
        .await
        .map_err(|_| GatewayError::Unavailable)?;
    let status = response.status();
    let headers = response.headers().clone();
    let body = Limited::new(response, MAX_RESPONSE_BYTES)
        .collect()
        .await
        .map_err(|_| GatewayError::Malformed)?
        .to_bytes()
        .to_vec();
    Ok((status, headers, body))
}

fn parse_payload(headers: &HeaderMap, body: &[u8]) -> Result<Value, GatewayError> {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .ok_or(GatewayError::Malformed)?;
    let payload = if content_type.starts_with("application/json") {
        serde_json::from_slice(body).map_err(|_| GatewayError::Malformed)?
    } else if content_type.starts_with("text/event-stream") {
        let text = std::str::from_utf8(body).map_err(|_| GatewayError::Malformed)?;
        text.lines()
            .filter_map(|line| line.strip_prefix("data: "))
            .find_map(|line| serde_json::from_str(line).ok())
            .ok_or(GatewayError::Malformed)?
    } else {
        return Err(GatewayError::Malformed);
    };
    if !json_depth_within(&payload, MAX_JSON_DEPTH) {
        return Err(GatewayError::Malformed);
    }
    Ok(payload)
}

pub(super) async fn gateway(
    State(state): State<super::AppState>,
    axum::Extension(principal): axum::Extension<AuthenticatedPrincipal>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    state
        .server
        .tool_gateway
        .handle(state.database.clone(), principal, headers, body)
        .await
}

pub(super) fn request_limit() -> axum::extract::DefaultBodyLimit {
    axum::extract::DefaultBodyLimit::max(MAX_REQUEST_BYTES)
}

fn json_response(value: Value) -> Response {
    (
        [
            (header::CONTENT_TYPE, "application/json"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        axum::Json(value),
    )
        .into_response()
}

fn json_rpc_error(id: Value, code: i64, message: &str) -> Response {
    json_response(json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}}))
}

fn protocol_error(id: Value, error: GatewayError) -> Response {
    let (code, message) = match error {
        GatewayError::Unavailable => (-32000, "Tool backend unavailable"),
        GatewayError::Incompatible => (-32002, "Tool backend protocol or catalog is incompatible"),
        GatewayError::Malformed => (-32603, "Invalid tool backend response"),
    };
    json_rpc_error(id, code, message)
}

fn valid_request_id(request: &Value) -> Option<Value> {
    let id = request.get("id")?;
    (id.is_string() || id.as_i64().is_some() || id.as_u64().is_some()).then(|| id.clone())
}

fn json_depth_within(value: &Value, remaining: usize) -> bool {
    if remaining == 0 {
        return !value.is_array() && !value.is_object();
    }
    match value {
        Value::Array(items) => items
            .iter()
            .all(|item| json_depth_within(item, remaining - 1)),
        Value::Object(items) => items
            .values()
            .all(|item| json_depth_within(item, remaining - 1)),
        _ => true,
    }
}

fn schema_accepts(schema: &Value, value: &Value, depth: usize) -> bool {
    if depth > MAX_JSON_DEPTH {
        return false;
    }
    if let Some(expected) = schema.get("type") {
        let matches = match expected.as_str() {
            Some("object") => value.is_object(),
            Some("array") => value.is_array(),
            Some("string") => value.is_string(),
            Some("integer") => value.as_i64().is_some() || value.as_u64().is_some(),
            Some("number") => value.is_number(),
            Some("boolean") => value.is_boolean(),
            Some("null") => value.is_null(),
            _ => true,
        };
        if !matches {
            return false;
        }
    }
    if schema
        .get("enum")
        .and_then(Value::as_array)
        .is_some_and(|values| !values.contains(value))
        || schema
            .get("const")
            .is_some_and(|constant| constant != value)
    {
        return false;
    }
    if let Some(object) = value.as_object() {
        let properties = schema.get("properties").and_then(Value::as_object);
        if let Some(required) = schema.get("required").and_then(Value::as_array)
            && required
                .iter()
                .filter_map(Value::as_str)
                .any(|key| !object.contains_key(key))
        {
            return false;
        }
        for (key, item) in object {
            let Some(property_schema) = properties.and_then(|properties| properties.get(key))
            else {
                if properties.is_some()
                    || schema.get("additionalProperties") == Some(&Value::Bool(false))
                {
                    return false;
                }
                continue;
            };
            if !schema_accepts(property_schema, item, depth + 1) {
                return false;
            }
        }
    }
    if let Some(items) = value.as_array() {
        if schema
            .get("minItems")
            .and_then(Value::as_u64)
            .is_some_and(|min| items.len() < min as usize)
            || schema
                .get("maxItems")
                .and_then(Value::as_u64)
                .is_some_and(|max| items.len() > max as usize)
        {
            return false;
        }
        if let Some(item_schema) = schema.get("items")
            && items
                .iter()
                .any(|item| !schema_accepts(item_schema, item, depth + 1))
        {
            return false;
        }
    }
    if let Some(text) = value.as_str()
        && (schema
            .get("minLength")
            .and_then(Value::as_u64)
            .is_some_and(|min| text.chars().count() < min as usize)
            || schema
                .get("maxLength")
                .and_then(Value::as_u64)
                .is_some_and(|max| text.chars().count() > max as usize))
    {
        return false;
    }
    if let Some(number) = value.as_f64()
        && (!number.is_finite()
            || schema
                .get("minimum")
                .and_then(Value::as_f64)
                .is_some_and(|min| number < min)
            || schema
                .get("maximum")
                .and_then(Value::as_f64)
                .is_some_and(|max| number > max))
    {
        return false;
    }
    true
}

fn valid_origin_host(headers: &HeaderMap) -> bool {
    let Some(origin) = headers.get(header::ORIGIN) else {
        return true;
    };
    let Some(host) = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let Ok(origin) = origin.to_str() else {
        return false;
    };
    let Ok(uri) = origin.parse::<Uri>() else {
        return false;
    };
    uri.authority()
        .is_some_and(|authority| authority.as_str().eq_ignore_ascii_case(host))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
