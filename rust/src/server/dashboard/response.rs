use super::*;

pub(super) fn static_response(body: &[u8], content_type: &str, cache_control: &str) -> Response {
    (
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, cache_control),
        ],
        body.to_vec(),
    )
        .into_response()
}

pub(in crate::server) fn json_response(status: StatusCode, value: Value) -> Response {
    (status, axum::Json(value)).into_response()
}

pub(in crate::server) fn degraded(reason: &str) -> Response {
    json_response(
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"status": "degraded", "reason": reason}),
    )
}

pub(super) fn html_response(body: String) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        body,
    )
        .into_response()
}
