//! Thin HTTP adapter for the optional finite hosted Eggsearch operation.

use axum::{
    body::Bytes,
    extract::{Extension, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use serde_json::json;

use crate::{
    identity::{AuthenticatedPrincipal, PrincipalKind},
    runtime_lifecycle::GenerationLease,
    server::AppState,
};

pub(super) const PATH: &str = "/api/agent/v1/run";
pub(super) const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_INPUT_BYTES: usize = 48 * 1024;
const MAX_ITERATIONS: usize = 5;
const MAX_TOOL_CALLS: usize = 8;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RunRequest {
    mode: String,
    model: String,
    input: String,
    tools: Vec<String>,
    #[serde(default)]
    max_iterations: Option<usize>,
    #[serde(default)]
    max_tool_calls: Option<usize>,
}

pub(super) async fn run(
    State(state): State<AppState>,
    Extension(lease): Extension<std::sync::Arc<GenerationLease>>,
    Extension(principal): Extension<AuthenticatedPrincipal>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if !state.server.hosted_execution_enabled() {
        return StatusCode::NOT_FOUND.into_response();
    }
    if body.len() > MAX_REQUEST_BYTES {
        return failure(StatusCode::PAYLOAD_TOO_LARGE, "request_too_large");
    }
    if !headers
        .get(axum::http::header::CONTENT_TYPE)
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
        .get(axum::http::header::ACCEPT)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| {
            value.split(',').any(|item| {
                let mime = item.split(';').next().unwrap_or_default().trim();
                mime.eq_ignore_ascii_case("application/json") || mime == "*/*"
            })
        })
    {
        return StatusCode::NOT_ACCEPTABLE.into_response();
    }
    if !principal.scopes.iter().any(|scope| scope == "inference")
        || !principal
            .scopes
            .iter()
            .any(|scope| scope == crate::tool_policy::INVOCATION_SCOPE)
        || principal.kind != PrincipalKind::AgentToken
        || principal.user_id.is_none()
        || principal.credential_id.is_none()
        || principal.auth_epoch.is_none()
    {
        return failure(StatusCode::FORBIDDEN, "hosted_execution_not_authorized");
    }
    let request = match serde_json::from_slice::<RunRequest>(&body) {
        Ok(request) => request,
        Err(_) => return failure(StatusCode::BAD_REQUEST, "invalid_request"),
    };
    if request.mode != "eggsearch"
        || request.model.is_empty()
        || request.model.len() > 256
        || request.input.is_empty()
        || request.input.len() > MAX_INPUT_BYTES
    {
        return failure(StatusCode::BAD_REQUEST, "invalid_request");
    }
    if !state
        .server
        .tool_gateway
        .hosted_model_qualified(&request.model)
    {
        return failure(StatusCode::FORBIDDEN, "hosted_model_not_qualified");
    }
    let limits = crate::agent_orchestration::HostedLimits {
        iterations: request.max_iterations.unwrap_or(3).clamp(1, MAX_ITERATIONS),
        tool_calls: request.max_tool_calls.unwrap_or(4).clamp(1, MAX_TOOL_CALLS),
    };
    let user_id = principal.user_id.as_deref().expect("verified above");
    let Some(_execution_permit) = state.server.tool_gateway.try_acquire_hosted(user_id) else {
        return failure(
            StatusCode::TOO_MANY_REQUESTS,
            "hosted_execution_capacity_reached",
        );
    };
    let orchestration_id = crate::coordinator::new_proxy_request_id();
    match crate::agent_orchestration::run(crate::agent_orchestration::HostedOperation {
        lease,
        database: state.database.clone(),
        gateway: state.server.tool_gateway.clone(),
        process: state.process.clone(),
        principal,
        headers,
        model: request.model,
        input: request.input,
        requested_tools: request.tools,
        limits,
        orchestration_id,
    })
    .await
    {
        Ok(value) => (StatusCode::OK, axum::Json(value)).into_response(),
        Err(error) => {
            let (status, code) = error.http();
            failure(status, code)
        }
    }
}

fn failure(status: StatusCode, code: &str) -> Response {
    (status, axum::Json(json!({"error":{"code":code}}))).into_response()
}
