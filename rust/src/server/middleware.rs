use super::inference::{error_body_response, surface_for_path};
use super::*;
use crate::coordinator::endpoint_error_body;

pub(super) async fn authenticate(
    State(state): State<AppState>,
    request: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    let path = request.uri().path().to_owned();
    if is_unconditionally_public(&path) {
        return next.run(request).await;
    }
    let supplied_key = provided_api_key(request.headers());
    let token_candidate = supplied_key
        .filter(|_| personal_credential_allowed(&path))
        .filter(|_| {
            !state
                .server
                .api_key
                .as_deref()
                .is_some_and(|server_key| verify_api_key(request.headers(), server_key))
        });
    let connection = request
        .extensions()
        .get::<eggserve_server::interop::ConnectionInfoExt>()
        .map(|extension| &extension.0);
    let token_peer = super::auth::throttle_peer_ip(connection, request.headers(), &state.server);
    if personal_credential_allowed(&path)
        && path != "/api/user-budget"
        && state.server.api_key.is_some()
        && supplied_key.is_none()
    {
        // With the configured shared key present, an absent key cannot be
        // valid in either legacy-key or managed mode. Reject before the
        // access-state SQLite lookup.
        return unauthorized("Invalid or missing API key");
    }
    if token_candidate.is_some() && !crate::web_session::personal_token_attempt_allowed(token_peer)
    {
        return unauthorized("Invalid or missing personal API credential");
    }
    let access_state = match crate::identity::access_state(&state.database).await {
        Ok(access_state) => access_state,
        Err(_) => return auth_unavailable(),
    };
    if legacy_server_key_allowed(Some(&access_state)) {
        if requires_browser_session(&path) {
            return unauthorized("Browser session required");
        }
        if !requires_auth(&path, request.method(), &state.server)
            || state.server.api_key.is_none()
            || state
                .server
                .api_key
                .as_deref()
                .is_some_and(|key| verify_api_key(request.headers(), key))
        {
            let mut request = request;
            if personal_credential_allowed(&path) {
                request
                    .extensions_mut()
                    .insert(crate::identity::AuthenticatedPrincipal::legacy_shared_key());
            }
            return next.run(request).await;
        }
        return unauthorized("Invalid or missing API key");
    }
    if personal_credential_allowed(&path)
        && let Some(token) = token_candidate
    {
        match crate::identity::verify_api_token_principal(&state.database, token).await {
            Ok(Some(principal)) => {
                let mut request = request;
                request.extensions_mut().insert(principal);
                return next.run(request).await;
            }
            Ok(None) => crate::web_session::record_personal_token_failure(token_peer),
            Err(_) => return auth_unavailable(),
        }
    }
    if personal_credential_allowed(&path) && path != "/api/user-budget" {
        return unauthorized("Invalid or missing personal API credential");
    }

    let Some(session_token) = crate::web_session::session_cookie_value(request.headers()) else {
        return unauthorized("Sign in required");
    };
    let principal = match crate::web_session::authenticate(&state.database, &session_token).await {
        Ok(Some(principal)) => principal,
        Ok(None) => return unauthorized("Sign in required"),
        Err(_) => return auth_unavailable(),
    };
    let account_surface = matches!(
        path.as_str(),
        "/account"
            | "/account/landing"
            | "/account/password"
            | "/logout"
            | "/api/user-budget"
            | "/my-account"
            | "/my-account/models"
            | "/usage"
    );
    let account_surface = account_surface || path.starts_with("/account/tokens");
    if principal.role != "admin" && !account_surface {
        // A member is refused an administrative path with a page that names
        // the refusal and points at the member surface the member does have,
        // rather than a bare text body.
        return (
            StatusCode::FORBIDDEN,
            axum::response::Html(super::auth::member_refusal_html()),
        )
            .into_response();
    }
    if principal.must_change_password
        && !matches!(path.as_str(), "/account" | "/account/password" | "/logout")
    {
        return axum::response::Redirect::to("/account?password=required").into_response();
    }
    let connection = request
        .extensions()
        .get::<eggserve_server::interop::ConnectionInfoExt>();
    let secure = super::auth::request_is_secure(
        connection.map(|extension| &extension.0),
        request.headers(),
        &state.server,
    );
    if !super::auth::web_auth_transport_allowed(
        connection.map(|extension| &extension.0),
        request.headers(),
        &state.server,
    ) || (is_unsafe_method(request.method())
        && !super::auth::same_origin(request.headers(), secure))
    {
        return (
            StatusCode::FORBIDDEN,
            "Cross-origin or insecure request rejected.",
        )
            .into_response();
    }
    let mut request = request;
    request
        .extensions_mut()
        .insert(crate::identity::AuthenticatedPrincipal {
            user_id: Some(principal.user_id.clone()),
            credential_id: Some(principal.session_id.clone()),
            role: Some(principal.role.clone()),
            scopes: vec!["inference".to_owned(), "discovery".to_owned()],
            auth_epoch: Some(principal.auth_epoch),
            kind: crate::identity::PrincipalKind::BrowserSession,
        });
    request.extensions_mut().insert(principal);
    let mut response = next.run(request).await;
    response.headers_mut().insert(
        axum::http::header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("no-store, max-age=0"),
    );
    response
}

fn legacy_server_key_allowed(access_state: Option<&str>) -> bool {
    access_state == Some("legacy_unmigrated")
}

fn personal_credential_allowed(path: &str) -> bool {
    (path.starts_with("/v1/") && path != "/v1/healthz" && path != "/v1/readyz")
        || path == "/api/integrations/v1/profile"
        || path == "/api/user-budget"
}

fn requires_browser_session(path: &str) -> bool {
    matches!(
        path,
        "/account"
            | "/account/landing"
            | "/account/password"
            | "/logout"
            | "/my-account"
            | "/my-account/models"
            | "/usage"
            | "/users"
            | "/profiles"
    ) || path.starts_with("/account/tokens")
        || path.starts_with("/admin/")
}

fn is_unconditionally_public(path: &str) -> bool {
    path == "/v1/healthz"
        || path == "/v1/readyz"
        || path.starts_with("/static/")
        || path == "/login"
}

fn is_unsafe_method(method: &http::Method) -> bool {
    matches!(
        *method,
        http::Method::POST | http::Method::PUT | http::Method::PATCH | http::Method::DELETE
    )
}

fn unauthorized(detail: &str) -> Response {
    (
        StatusCode::UNAUTHORIZED,
        axum::Json(json!({ "detail": detail })),
    )
        .into_response()
}

fn auth_unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        "Authentication service unavailable.",
    )
        .into_response()
}

pub(super) fn requires_auth(path: &str, method: &http::Method, server: &ServerState) -> bool {
    if is_unconditionally_public(path) {
        return false;
    }
    if matches!(
        path,
        "/account"
            | "/account/landing"
            | "/account/password"
            | "/logout"
            | "/my-account"
            | "/my-account/models"
            | "/usage"
    ) || path.starts_with("/account/tokens")
        || path.starts_with("/admin/")
        || matches!(path, "/users" | "/profiles")
    {
        return true;
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
    if path == "/api/user-budget" {
        return true;
    }
    if path.starts_with("/api/") || encoded_api_path(path) {
        return !(server.dashboard_enabled
            && server.dashboard_public
            && *method == http::Method::GET
            && public_dashboard_api_path(path));
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
        && *method == http::Method::GET
        && dashboard_page
}

fn public_dashboard_api_path(path: &str) -> bool {
    matches!(
        path,
        "/api/stats/summary"
            | "/api/stats/transcoding"
            | "/api/stats/cache-observability"
            | "/api/stats/canonical-request-segmentation"
            | "/api/stats/cache-stability"
            | "/api/stats/request-shaping"
            | "/api/timeseries"
            | "/api/timeseries/grouped"
    )
}

fn encoded_api_path(path: &str) -> bool {
    let Some(first_segment) = path.strip_prefix('/') else {
        return false;
    };
    let first_segment = first_segment.split('/').next().unwrap_or_default();
    let bytes = first_segment.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let Some(pair) = bytes.get(index + 1..index + 3) else {
                return false;
            };
            let high = (pair[0] as char).to_digit(16);
            let low = (pair[1] as char).to_digit(16);
            let (Some(high), Some(low)) = (high, low) else {
                return false;
            };
            decoded.push(((high << 4) | low) as u8);
            index += 3;
        } else {
            decoded.push(bytes[index]);
            index += 1;
        }
    }
    let Ok(decoded) = std::str::from_utf8(&decoded) else {
        return false;
    };
    decoded == "api" || decoded.starts_with("api/")
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
    let active_fairness = &lease.generation().config().admission_fairness;
    let fairness_policy = crate::request::AdmissionFairnessPolicy {
        requests_per_minute_per_principal: active_fairness.requests_per_minute_per_principal,
        global_requests_per_minute: active_fairness.global_requests_per_minute,
        max_tracked_principals: active_fairness.max_tracked_principals,
    };
    let principal_id = request
        .extensions()
        .get::<crate::identity::AuthenticatedPrincipal>()
        .and_then(|principal| principal.user_id.as_deref());
    let fairness_headers =
        match state
            .admission_fairness
            .admit_at(fairness_policy, principal_id, Instant::now())
        {
            crate::request::AdmissionFairnessDecision::Disabled => None,
            crate::request::AdmissionFairnessDecision::Allowed { remaining, scope } => {
                Some((remaining, scope))
            }
            crate::request::AdmissionFairnessDecision::Limited {
                retry_after_seconds,
                scope,
            } => {
                return fairness_limited_response(surface, retry_after_seconds, scope);
            }
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
    // Stream the body in frames, growing the process budget incrementally so
    // a lying Content-Length cannot allocate up to `limit` before the 429.
    let mut limited = Limited::new(body, limit);
    let mut collected = Vec::new();
    let mut accounted = initial_reservation;
    loop {
        match limited.frame().await {
            None => break,
            Some(Ok(frame)) => {
                let Some(data) = frame.data_ref() else {
                    continue;
                };
                let chunk_len = data.len();
                let total = collected.len().saturating_add(chunk_len);
                if total > limit {
                    return error_body_response(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        surface,
                        endpoint_error_body(surface, "Request body too large"),
                    );
                }
                if total > accounted {
                    let need = total - accounted;
                    if !reservation.try_grow(need, ceiling) {
                        return backpressure_response(
                            surface,
                            endpoint_error_body(surface, "Service busy: body budget exhausted"),
                        );
                    }
                    accounted = total;
                }
                collected.extend_from_slice(data);
            }
            Some(Err(_)) => {
                return error_body_response(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    surface,
                    endpoint_error_body(surface, "Request body too large"),
                );
            }
        }
    }
    let collected = Bytes::from(collected);
    request.extensions_mut().insert(Arc::new(lease));
    request.extensions_mut().insert(Arc::new(reservation));
    *request.body_mut() = Body::from(collected);
    let mut response = next.run(request).await;
    if let Some((remaining, scope)) = fairness_headers {
        let headers = response.headers_mut();
        headers.insert(
            "x-eggpool-admission-remaining",
            HeaderValue::from_str(&remaining.to_string())
                .unwrap_or_else(|_| HeaderValue::from_static("0")),
        );
        headers.insert("x-eggpool-admission-scope", HeaderValue::from_static(scope));
        headers.insert(
            "x-eggpool-admission-window-seconds",
            HeaderValue::from_static("60"),
        );
    }
    response
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

fn fairness_limited_response(
    surface: crate::wire::ir::ClientSurface,
    retry_after_seconds: u64,
    scope: &'static str,
) -> Response {
    let message = "Short-window inference admission limit reached";
    let body = if surface == crate::wire::ir::ClientSurface::Messages {
        serde_json::json!({
            "type": "error",
            "error": {
                "type": "api_error",
                "message": message,
                "code": "short_window_rate_limited"
            }
        })
    } else {
        serde_json::json!({
            "error": {
                "type": "rate_limit_error",
                "message": message,
                "code": "short_window_rate_limited"
            }
        })
    };
    let bytes = serde_json::to_vec(&body)
        .unwrap_or_else(|_| br#"{"error":{"code":"short_window_rate_limited"}}"#.to_vec());
    let mut headers = HeaderMap::new();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    headers.insert(
        header::RETRY_AFTER,
        HeaderValue::from_str(&retry_after_seconds.to_string())
            .unwrap_or_else(|_| HeaderValue::from_static("1")),
    );
    headers.insert("x-eggpool-admission-scope", HeaderValue::from_static(scope));
    (StatusCode::TOO_MANY_REQUESTS, headers, Bytes::from(bytes)).into_response()
}

pub(super) fn validate_server_key(config: &Config) -> Result<(), ServerError> {
    let key = config.resolved_server_api_key();
    if key.as_deref().is_some_and(|value| !valid_key_shape(value)) {
        return Err(ServerError::InvalidApiKey);
    }
    Ok(())
}

pub(super) fn validate_server_access(
    config: &Config,
    access_state: &str,
) -> Result<(), ServerError> {
    validate_server_key(config)?;
    if access_state == "fresh_uninitialized" && config.resolved_server_api_key().is_none() {
        return Err(ServerError::StartupConflict {
            detail: "fresh database requires local administrator bootstrap before serving"
                .to_owned(),
        });
    }
    if access_state == "legacy_unmigrated"
        && !is_loopback_host(&config.server.host)
        && config.resolved_server_api_key().is_none()
    {
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
    // Non-UTF8 header values map to `""` (fail-closed, same as absent).
    // There is deliberately no per-request log or counter here: this runs on
    // every request, and the outcome is already observable as a 401.
    let provided = provided_api_key(headers).unwrap_or("");
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

fn provided_api_key(headers: &HeaderMap) -> Option<&str> {
    let authorization = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim();
    let provided = strip_authorization_scheme(authorization)
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
    (!provided.is_empty()).then_some(provided)
}

/// Strip the HTTP authentication scheme token from an `Authorization` value.
///
/// The scheme token is case-insensitive by specification, so `Bearer`,
/// `bearer`, and `BEARER` all name the same scheme. Only the scheme token is
/// matched this way: the credential itself is never inspected here, and it is
/// still compared in constant time by [`verify_api_key`].
fn strip_authorization_scheme(value: &str) -> Option<&str> {
    let (scheme, credentials) = value.split_once(' ')?;
    scheme.eq_ignore_ascii_case("bearer").then_some(credentials)
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

#[cfg(test)]
mod tests {
    use super::{
        fixed_key, legacy_server_key_allowed, personal_credential_allowed, provided_api_key,
        requires_auth, verify_api_key,
    };
    use crate::server::ServerState;
    use axum::http::{HeaderMap, HeaderValue, header};
    use std::time::Instant;

    #[test]
    fn global_server_key_is_valid_only_before_identity_cutover() {
        assert!(legacy_server_key_allowed(Some("legacy_unmigrated")));
        assert!(!legacy_server_key_allowed(Some("fresh_uninitialized")));
        assert!(!legacy_server_key_allowed(Some("admin_initialized")));
        assert!(!legacy_server_key_allowed(Some("managed")));
        assert!(!legacy_server_key_allowed(None));
    }

    #[test]
    fn personal_credentials_are_limited_to_agent_surfaces() {
        for path in [
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/responses",
            "/v1/responses/compact",
            "/v1/models",
            "/api/integrations/v1/profile",
        ] {
            assert!(personal_credential_allowed(path), "{path}");
        }
        for path in ["/", "/api/status", "/api/stats/runtime", "/api/users"] {
            assert!(!personal_credential_allowed(path), "{path}");
        }
        for path in [
            "/api/%73tats/summary",
            "/api//stats/summary",
            "/api/../admin/users",
            "/api/stats/accounts",
        ] {
            assert!(!personal_credential_allowed(path), "{path}");
        }
    }

    #[test]
    fn unknown_and_encoded_api_paths_never_inherit_public_dashboard_access() {
        let public = ServerState {
            api_key: Some("test-server-key-12345678".to_owned()),
            dashboard_enabled: true,
            dashboard_public: true,
            dashboard_theme: "default".to_owned(),
            dashboard_refresh_interval_s: 60,
            configured_server_threads: 1,
            database_path: ":memory:".to_owned(),
            trusted_proxy_ips: Vec::new(),
            allow_insecure_web_auth: false,
            started_at: Instant::now(),
        };
        for path in [
            "/api/unknown",
            "/api/stats/accounts",
            "/api/%73tats/summary",
            "/api//stats/summary",
            "/api/../admin/users",
            "/%61pi/stats/summary",
        ] {
            assert!(
                requires_auth(path, &http::Method::GET, &public),
                "{path} must fail closed"
            );
        }
        for path in [
            "/api/stats/summary",
            "/api/stats/transcoding",
            "/api/timeseries",
        ] {
            assert!(
                !requires_auth(path, &http::Method::GET, &public),
                "{path} is an explicit public API"
            );
        }
        assert!(requires_auth(
            "/api/stats/summary",
            &http::Method::POST,
            &public
        ));
    }

    #[test]
    fn authorization_scheme_token_is_case_insensitive() {
        let expected = "test-key-transport";
        for scheme in ["Bearer", "bearer", "BEARER", "BeArEr"] {
            let mut headers = HeaderMap::new();
            headers.insert(
                header::AUTHORIZATION,
                HeaderValue::from_str(&format!("{scheme} {expected}")).expect("header value"),
            );
            assert!(
                verify_api_key(&headers, expected),
                "{scheme} must be accepted"
            );
            assert_eq!(
                provided_api_key(&headers),
                Some(expected),
                "{scheme} must yield the credential"
            );
        }
    }

    #[test]
    fn a_wrong_credential_is_rejected_under_every_scheme_spelling() {
        let expected = "test-key-transport";
        for credential in [
            "wrong-key-transport",
            "test-key-transpor",
            "test-key-transport-extra",
        ] {
            for scheme in ["Bearer", "BEARER"] {
                let mut headers = HeaderMap::new();
                headers.insert(
                    header::AUTHORIZATION,
                    HeaderValue::from_str(&format!("{scheme} {credential}")).expect("header value"),
                );
                assert!(
                    !verify_api_key(&headers, expected),
                    "{scheme} with {credential} must be rejected"
                );
            }
        }
    }

    #[test]
    fn the_credential_comparison_keeps_its_fixed_width_padding() {
        // The constant-time comparison folds over a fixed 512-byte pad, so a
        // wrong credential is rejected on the same path whether it is
        // shorter, longer, or the same length as the expected key.
        let padded = fixed_key("abc");
        assert_eq!(&padded[..3], &b"abc"[..]);
        assert!(padded[3..].iter().all(|byte| *byte == 0));
        // An over-long value can never be truncated into a matching prefix.
        let overflow = fixed_key(&"a".repeat(600));
        assert!(overflow.iter().all(|byte| *byte == 0xff));
    }

    #[test]
    fn the_accepted_credential_headers_are_unchanged() {
        let expected = "test-key-transport";
        let mut headers = HeaderMap::new();
        headers.insert("x-api-key", HeaderValue::from_static("test-key-transport"));
        assert_eq!(provided_api_key(&headers), Some(expected));
        assert!(verify_api_key(&headers, expected));
        // A scheme other than `bearer` is not a credential source.
        let mut basic = HeaderMap::new();
        basic.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Basic test-key-transport"),
        );
        assert_eq!(provided_api_key(&basic), None);
        assert!(!verify_api_key(&basic, expected));
    }
}
