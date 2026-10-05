use super::inference::{error_body_response, surface_for_path};
use super::*;
use crate::coordinator::endpoint_error_body;

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
    // EggPool-specific integration profile is always authenticated. It must
    // never inherit a dashboard/public exemption even when the dashboard is
    // public; the returned object is sanitized but the endpoint is not public.
    if path.starts_with("/api/integrations/") {
        return true;
    }
    if path == "/api/stats/runtime" {
        return true;
    }
    if path == "/api/stats/update" {
        return true;
    }
    if path == "/api/status" {
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
    let surface = surface_for_path(request.uri().path());
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(error) => {
            return error_body_response(
                StatusCode::SERVICE_UNAVAILABLE,
                surface,
                endpoint_error_body(surface, &error.to_string()),
            );
        }
    };
    let Ok(limit) = usize::try_from(lease.generation().config().server.max_request_body_bytes)
    else {
        return error_body_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            surface,
            endpoint_error_body(surface, "Invalid body limit"),
        );
    };
    let ceiling = crate::request::resource_budget::effective_ceiling(limit);
    // Malformed Content-Length is a client error, not an absent value: fail
    // closed with 400 instead of granting the cheap 32 KiB reservation path.
    let declared_length = match request.headers().get(header::CONTENT_LENGTH) {
        None => None,
        Some(value) => match value.to_str().ok().and_then(|v| v.parse::<usize>().ok()) {
            Some(length) => Some(length),
            None => {
                return error_body_response(
                    StatusCode::BAD_REQUEST,
                    surface,
                    endpoint_error_body(surface, "Invalid Content-Length"),
                );
            }
        },
    };
    if declared_length.is_some_and(|length| length > limit) {
        return error_body_response(
            StatusCode::PAYLOAD_TOO_LARGE,
            surface,
            endpoint_error_body(surface, "Request body too large"),
        );
    }
    // Small initial reservation avoids exhausting the process budget on
    // missing/large declarations; the actual byte count is accounted after
    // collection so lying declarations cannot under-reserve.
    const INITIAL_RESERVATION_BYTES: usize = 32 * 1024;
    let initial_reservation = declared_length
        .map(|length| length.min(INITIAL_RESERVATION_BYTES))
        .unwrap_or(INITIAL_RESERVATION_BYTES);
    let Some(mut reservation) = crate::request::resource_budget::RawBodyReservation::try_acquire(
        initial_reservation,
        ceiling,
    ) else {
        return backpressure_response(
            surface,
            endpoint_error_body(surface, "Service busy: body budget exhausted"),
        );
    };
    let body = std::mem::replace(request.body_mut(), Body::empty());
    let collected = match Limited::new(body, limit).collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => {
            return error_body_response(
                StatusCode::PAYLOAD_TOO_LARGE,
                surface,
                endpoint_error_body(surface, "Request body too large"),
            );
        }
    };
    let overage = collected.len().saturating_sub(initial_reservation);
    if !reservation.try_grow(overage, ceiling) {
        return backpressure_response(
            surface,
            endpoint_error_body(surface, "Service busy: body budget exhausted"),
        );
    }
    request.extensions_mut().insert(Arc::new(lease));
    request.extensions_mut().insert(Arc::new(reservation));
    *request.body_mut() = Body::from(collected);
    next.run(request).await
}

pub(super) fn is_inference_path(path: &str) -> bool {
    matches!(
        path,
        "/v1/chat/completions" | "/v1/messages" | "/v1/responses" | "/v1/responses/compact"
    )
}

/// Backpressure (body-budget exhaustion) is retryable client backoff, not an
/// outage: 429 with `Retry-After` so clients back off instead of treating it
/// as a 503 outage.
fn backpressure_response(surface: crate::wire::ir::ClientSurface, detail: Vec<u8>) -> Response {
    let mut response = error_body_response(StatusCode::TOO_MANY_REQUESTS, surface, detail);
    response
        .headers_mut()
        .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    response
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
    // Always fold over the full 512-byte pads before combining with the
    // shape bits, so shape validity does not short-circuit the compare.
    let different = left
        .iter()
        .zip(right.iter())
        .fold(0u8, |accumulator, (a, b)| accumulator | (a ^ b));
    let provided_ok = u8::from(valid_key_shape(provided));
    let expected_ok = u8::from(valid_key_shape(expected));
    let match_ok = u8::from(different == 0);
    (provided_ok & expected_ok & match_ok) == 1
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
    crate::config::is_loopback_host(host)
}
