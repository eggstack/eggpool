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

pub(in crate::server) fn apply_html_security_headers(response: &mut Response) {
    let headers = response.headers_mut();
    headers.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    headers.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    headers.insert("x-frame-options", HeaderValue::from_static("DENY"));
    headers.insert(
        "content-security-policy",
        HeaderValue::from_static("frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    );
}

pub(super) fn html_response(body: String) -> Response {
    let mut response = (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        body,
    )
        .into_response();
    apply_html_security_headers(&mut response);
    response
}

#[cfg(test)]
mod tests {
    use super::{html_response, json_response, static_response};
    use axum::http::{StatusCode, header};
    use serde_json::json;

    #[test]
    fn html_security_headers_apply_only_to_html_responses() {
        let html = html_response("<p>dashboard</p>".to_owned());
        let headers = html.headers();
        assert_eq!(headers.get("x-content-type-options").unwrap(), "nosniff");
        assert_eq!(headers.get("referrer-policy").unwrap(), "no-referrer");
        assert_eq!(headers.get("x-frame-options").unwrap(), "DENY");
        assert_eq!(
            headers.get("content-security-policy").unwrap(),
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        );
        assert_eq!(
            headers.get(header::CONTENT_TYPE).unwrap(),
            "text/html; charset=utf-8"
        );

        let json = json_response(StatusCode::OK, json!({"ok": true}));
        let static_asset = static_response(b"asset", "text/css", "public, max-age=60");
        for response in [json, static_asset] {
            for name in [
                "x-content-type-options",
                "referrer-policy",
                "x-frame-options",
                "content-security-policy",
            ] {
                assert!(response.headers().get(name).is_none(), "unexpected {name}");
            }
        }
    }
}
