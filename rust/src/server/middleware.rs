use super::inference::error_body_response;
use super::*;

pub(super) async fn authenticate(
    State(state): State<AppState>,
    request: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    if !requires_auth(request.uri().path(), &state.server) {
        return next.run(request).await;
    }
    if state
        .server
        .api_key
        .as_deref()
        .is_none_or(|key| verify_api_key(request.headers(), key))
    {
        return next.run(request).await;
    }
    (
        StatusCode::UNAUTHORIZED,
        axum::Json(json!({
            "detail": "Invalid or missing API key"
        })),
    )
        .into_response()
}

pub(super) fn requires_auth(path: &str, server: &ServerState) -> bool {
    if path == "/v1/healthz" || path == "/v1/readyz" || path.starts_with("/static/") {
        return false;
    }
    if path == "/api/stats/runtime" {
        return true;
    }
    if path == "/api/stats/update" {
        return true;
    }
    if path.starts_with("/v1/") {
        return true;
    }
    let dashboard_page = path == "/"
        || matches!(
            path,
            "/accounts"
                | "/models"
                | "/latency"
                | "/events"
                | "/timeseries"
                | "/bandwidth"
                | "/pings"
                | "/reliability"
                | "/routing"
                | "/traces"
                | "/runtime"
                | "/cache"
        )
        || path.starts_with("/models/");
    server.dashboard_enabled
        && !server.dashboard_public
        && (dashboard_page || path.starts_with("/api/"))
}

/// Acquire the active generation before collecting an inference body. Axum's
/// `Bytes` extractor is intentionally downstream of this middleware, so a
/// live per-generation limit is enforced while the body is still streaming.
/// The lease is moved into request extensions and consumed by the handler,
/// keeping body admission and routing on one generation.
pub(super) async fn admit_inference_body(
    State(state): State<AppState>,
    mut request: axum::http::Request<Body>,
    next: Next,
) -> Response {
    if !is_inference_path(request.uri().path()) {
        return next.run(request).await;
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(error) => {
            return error_body_response(
                StatusCode::SERVICE_UNAVAILABLE,
                ClientSurface::ChatCompletions,
                format!(r#"{{"detail":"{}"}}"#, error).into_bytes(),
            );
        }
    };
    let limit = usize::try_from(lease.generation().config().server.max_request_body_bytes)
        .unwrap_or(usize::MAX);
    let body = std::mem::replace(request.body_mut(), Body::empty());
    let collected = match Limited::new(body, limit).collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => {
            return json_response(
                StatusCode::PAYLOAD_TOO_LARGE,
                json!({
                    "detail": "Request body too large"
                }),
            );
        }
    };
    request.extensions_mut().insert(Arc::new(lease));
    *request.body_mut() = Body::from(collected);
    next.run(request).await
}

pub(super) fn is_inference_path(path: &str) -> bool {
    matches!(
        path,
        "/v1/chat/completions" | "/v1/messages" | "/v1/responses"
    )
}

pub(super) fn validate_server_key(config: &Config) -> Result<(), ServerError> {
    let key = config.resolved_server_api_key();
    if !is_loopback_host(&config.server.host) && key.is_none() {
        return Err(ServerError::InvalidApiKey);
    }
    if key.as_deref().is_some_and(|value| !valid_key_shape(value)) {
        return Err(ServerError::InvalidApiKey);
    }
    Ok(())
}

pub(super) fn valid_key_shape(value: &str) -> bool {
    (8..=512).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

pub(super) fn map_generation_error(error: GenerationBuildError) -> ServerError {
    match error {
        GenerationBuildError::ProviderPool(error) => ServerError::ProviderPool(error),
        error => ServerError::Generation(error),
    }
}

pub(super) fn verify_api_key(headers: &HeaderMap, expected: &str) -> bool {
    let authorization = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim();
    let provided = authorization
        .strip_prefix("Bearer ")
        .or_else(|| authorization.strip_prefix("bearer "))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            headers
                .get("x-api-key")
                .and_then(|value| value.to_str().ok())
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or("");
    let left = fixed_key(provided);
    let right = fixed_key(expected);
    let different = left
        .iter()
        .zip(right.iter())
        .fold(0u8, |accumulator, (a, b)| accumulator | (a ^ b));
    valid_key_shape(provided) && valid_key_shape(expected) && different == 0
}

pub(super) fn fixed_key(value: &str) -> [u8; 512] {
    let mut result = [0u8; 512];
    let bytes = value.as_bytes();
    if bytes.len() > result.len() {
        return [0xff; 512];
    }
    result[..bytes.len()].copy_from_slice(bytes);
    result
}

pub(super) fn is_loopback_host(host: &str) -> bool {
    let normalized = host.trim().trim_matches(['[', ']']);
    normalized == "localhost"
        || normalized == "127.0.0.1"
        || normalized == "::1"
        || normalized == "0:0:0:0:0:0:0:1"
}
