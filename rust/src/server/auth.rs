//! Browser login, sessions, CSRF forms, and forwarded-HTTPS validation.

use super::*;
use axum::{
    Extension, Form,
    http::{HeaderMap, HeaderValue, header},
    response::{Html, Redirect},
};
use serde::Deserialize;
use std::net::IpAddr;

#[derive(Deserialize)]
pub(super) struct LoginForm {
    username: String,
    password: String,
}

#[derive(Deserialize)]
pub(super) struct CsrfForm {
    csrf: String,
}

#[derive(Deserialize)]
pub(super) struct PasswordChangeForm {
    current_password: String,
    new_password: String,
    csrf: String,
}

pub(super) async fn login_page() -> Html<String> {
    Html(login_html(None))
}

pub(super) async fn login_submit(
    State(state): State<AppState>,
    headers: HeaderMap,
    peer: Option<Extension<eggserve_server::interop::ConnectionInfoExt>>,
    Form(form): Form<LoginForm>,
) -> Response {
    let connection = peer.as_ref().map(|extension| &extension.0.0);
    let secure = request_is_secure(connection, &headers, &state.server);
    if !web_auth_transport_allowed(connection, &headers, &state.server) {
        return (
            StatusCode::FORBIDDEN,
            "Browser sign-in requires HTTPS or loopback.",
        )
            .into_response();
    }
    if !same_origin(&headers, secure) {
        return (
            StatusCode::FORBIDDEN,
            "Cross-origin sign-in is not allowed.",
        )
            .into_response();
    }
    let access_state = match crate::identity::access_state(&state.database).await {
        Ok(access_state) => access_state,
        Err(_) => return unavailable(),
    };
    if !matches!(access_state.as_str(), "admin_initialized" | "managed") {
        return (StatusCode::NOT_FOUND, "Browser sign-in is not enabled.").into_response();
    }

    let peer_ip =
        connection.and_then(|connection| connection.remote_addr.map(|address| address.ip()));
    if !crate::web_session::login_allowed(&form.username, peer_ip) {
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Html(login_html(Some("Sign-in failed. Try again later."))),
        )
            .into_response();
    }
    let user = crate::identity::verify_password(&state.database, &form.username, &form.password)
        .await
        .ok()
        .flatten();
    let Some(user) = user else {
        crate::web_session::record_login_failure(&form.username, peer_ip);
        return (
            StatusCode::UNAUTHORIZED,
            Html(login_html(Some("Invalid username or password."))),
        )
            .into_response();
    };
    let session = match crate::web_session::issue(&state.database, &user.id).await {
        Ok(session) => session,
        Err(_) => return unavailable(),
    };
    let mut response = Redirect::to(if user.must_change_password {
        "/account?password=required"
    } else {
        "/account"
    })
    .into_response();
    set_cookie(
        &mut response,
        crate::web_session::session_set_cookie(&session.session_token, secure),
    );
    set_cookie(
        &mut response,
        crate::web_session::csrf_set_cookie(&session.csrf_token, secure),
    );
    response
}

pub(super) async fn account_page(
    Extension(principal): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
) -> Html<String> {
    let csrf = crate::web_session::csrf_cookie_value(&headers)
        .filter(|token| crate::web_session::verify_csrf(&principal, token));
    let csrf = csrf.unwrap_or_default();
    let username = html_escape(&principal.username);
    let forced = principal.must_change_password;
    let notice = if forced {
        "<p>Change your password to continue.</p>"
    } else {
        ""
    };
    Html(format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Account</title><body><main><h1>Account</h1><p>Signed in as {username}.</p>{notice}<form method=\"post\" action=\"/account/password\"><label>Current password <input type=\"password\" name=\"current_password\" autocomplete=\"current-password\" required></label><label>New password <input type=\"password\" name=\"new_password\" autocomplete=\"new-password\" minlength=\"12\" maxlength=\"1024\" required></label><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\"><button type=\"submit\">Change password</button></form><form method=\"post\" action=\"/logout\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\"><button type=\"submit\">Log out</button></form></main></body></html>"
    ))
}

pub(super) async fn password_change(
    State(state): State<AppState>,
    Extension(principal): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    peer: Option<Extension<eggserve_server::interop::ConnectionInfoExt>>,
    Form(form): Form<PasswordChangeForm>,
) -> Response {
    if !crate::web_session::verify_csrf(&principal, &form.csrf)
        || !crate::web_session::csrf_cookie_value(&headers)
            .is_some_and(|cookie| crate::web_session::verify_csrf(&principal, &cookie))
    {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    let authenticated = crate::identity::verify_password(
        &state.database,
        &principal.username,
        &form.current_password,
    )
    .await
    .ok()
    .flatten()
    .is_some_and(|user| user.id == principal.user_id);
    if !authenticated {
        return (
            StatusCode::BAD_REQUEST,
            Html(account_error_html("Current password is incorrect.")),
        )
            .into_response();
    }
    match crate::identity::reset_password(&state.database, &principal.username, &form.new_password)
        .await
    {
        Ok(()) => {
            let mut response = Redirect::to("/login").into_response();
            clear_auth_cookies(
                &mut response,
                request_is_secure(
                    peer.as_ref().map(|extension| &extension.0.0),
                    &headers,
                    &state.server,
                ),
            );
            response
        }
        Err(crate::identity::IdentityError::InvalidPassword) => (
            StatusCode::BAD_REQUEST,
            Html(account_error_html("New password must be 12 to 1024 bytes.")),
        )
            .into_response(),
        Err(_) => unavailable(),
    }
}

pub(super) async fn logout_submit(
    State(state): State<AppState>,
    Extension(principal): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    peer: Option<Extension<eggserve_server::interop::ConnectionInfoExt>>,
    Form(form): Form<CsrfForm>,
) -> Response {
    if !crate::web_session::verify_csrf(&principal, &form.csrf)
        || !crate::web_session::csrf_cookie_value(&headers)
            .is_some_and(|cookie| crate::web_session::verify_csrf(&principal, &cookie))
    {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if let Some(token) = crate::web_session::session_cookie_value(&headers)
        && crate::web_session::revoke(&state.database, &token)
            .await
            .is_err()
    {
        return unavailable();
    }
    let secure = request_is_secure(
        peer.as_ref().map(|extension| &extension.0.0),
        &headers,
        &state.server,
    );
    let mut response = Redirect::to("/login").into_response();
    clear_auth_cookies(&mut response, secure);
    response
}

pub(super) fn request_is_secure(
    connection: Option<&eggserve_primitives::connection_info::ConnectionInfo>,
    headers: &HeaderMap,
    server: &ServerState,
) -> bool {
    connection.is_some_and(|connection| {
        connection.scheme == eggserve_primitives::connection_info::Scheme::Https
    }) || forwarded_https(connection, headers, server)
}

pub(super) fn web_auth_transport_allowed(
    connection: Option<&eggserve_primitives::connection_info::ConnectionInfo>,
    headers: &HeaderMap,
    server: &ServerState,
) -> bool {
    if request_is_secure(connection, headers, server) || server.allow_insecure_web_auth {
        return true;
    }
    connection
        .and_then(|connection| connection.remote_addr)
        .is_some_and(|address| address.ip().is_loopback())
}

fn forwarded_https(
    connection: Option<&eggserve_primitives::connection_info::ConnectionInfo>,
    headers: &HeaderMap,
    server: &ServerState,
) -> bool {
    let Some(peer) = connection
        .and_then(|connection| connection.remote_addr)
        .map(|address| address.ip())
    else {
        return false;
    };
    let trusted = server
        .trusted_proxy_ips
        .iter()
        .any(|proxy| proxy.parse::<IpAddr>().ok() == Some(peer));
    if !trusted {
        return false;
    }
    let mut values = headers.get_all("x-forwarded-proto").iter();
    let Some(value) = values.next().and_then(|value| value.to_str().ok()) else {
        return false;
    };
    values.next().is_none() && value.trim().eq_ignore_ascii_case("https")
}

pub(super) fn same_origin(headers: &HeaderMap, secure: bool) -> bool {
    let mut host_values = headers.get_all(header::HOST).iter();
    let Some(host) = host_values.next().and_then(|value| value.to_str().ok()) else {
        return false;
    };
    if host_values.next().is_some() {
        return false;
    }
    let origin_values = headers.get_all(header::ORIGIN).iter().collect::<Vec<_>>();
    let referer_values = headers.get_all(header::REFERER).iter().collect::<Vec<_>>();
    let origin = if !origin_values.is_empty() {
        if origin_values.len() != 1 || !referer_values.is_empty() {
            return false;
        }
        origin_values[0].to_str().ok()
    } else if referer_values.len() == 1 {
        referer_values[0].to_str().ok()
    } else {
        None
    };
    let Some(origin) = origin else { return false };
    let Some((scheme, authority)) = origin_authority(origin) else {
        return false;
    };
    let expected_scheme = if secure { "https" } else { "http" };
    if !scheme.eq_ignore_ascii_case(expected_scheme) {
        return false;
    }
    let Ok(expected_authority) = host.parse::<http::uri::Authority>() else {
        return false;
    };
    let Ok(origin_authority) = authority.parse::<http::uri::Authority>() else {
        return false;
    };
    expected_authority
        .host()
        .eq_ignore_ascii_case(origin_authority.host())
        && effective_port(expected_authority.port_u16(), expected_scheme)
            == effective_port(origin_authority.port_u16(), expected_scheme)
}

fn origin_authority(value: &str) -> Option<(&str, &str)> {
    let (scheme, remainder) = value.split_once("://")?;
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") {
        return None;
    }
    let authority = remainder.split(['/', '?', '#']).next()?;
    if authority.is_empty() || remainder[authority.len()..].contains(['?', '#']) {
        return None;
    }
    Some((scheme, authority))
}

fn effective_port(port: Option<u16>, scheme: &str) -> Option<u16> {
    port.or(match scheme {
        "https" => Some(443),
        "http" => Some(80),
        _ => None,
    })
}

fn set_cookie(response: &mut Response, value: String) {
    if let Ok(value) = HeaderValue::from_str(&value) {
        response.headers_mut().append(header::SET_COOKIE, value);
    }
}

fn clear_auth_cookies(response: &mut Response, secure: bool) {
    for value in crate::web_session::clear_cookies(secure) {
        set_cookie(response, value);
    }
}

fn login_html(message: Option<&str>) -> String {
    let message = message.map_or(String::new(), |message| {
        format!("<p role=\"alert\">{message}</p>")
    });
    format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Sign in</title><body><main><h1>Sign in</h1>{message}<form method=\"post\" action=\"/login\"><label>Username <input name=\"username\" autocomplete=\"username\" required></label><label>Password <input type=\"password\" name=\"password\" autocomplete=\"current-password\" required></label><button type=\"submit\">Sign in</button></form></main></body></html>"
    )
}

fn account_error_html(message: &str) -> String {
    format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Account</title><body><main><h1>Account</h1><p role=\"alert\">{}</p><p><a href=\"/account\">Return to account</a></p></main></body></html>",
        html_escape(message)
    )
}

fn html_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        "Authentication service unavailable.",
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_origin_requires_matching_authority_and_scheme() {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, HeaderValue::from_static("example.test"));
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://example.test"),
        );
        assert!(same_origin(&headers, true));
        assert!(!same_origin(&headers, false));
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://attacker.test"),
        );
        assert!(!same_origin(&headers, true));
    }

    #[test]
    fn origin_parser_rejects_opaque_and_ambiguous_values() {
        assert_eq!(
            origin_authority("https://example.test/path"),
            Some(("https", "example.test"))
        );
        assert_eq!(origin_authority("null"), None);
        assert_eq!(origin_authority("https://example.test/?x=1"), None);
    }

    #[test]
    fn forwarded_https_is_accepted_only_from_the_configured_proxy_socket() {
        let mut server = ServerState {
            api_key: None,
            dashboard_enabled: true,
            dashboard_public: false,
            dashboard_theme: "default".to_owned(),
            dashboard_refresh_interval_s: 60,
            configured_server_threads: 1,
            database_path: ":memory:".to_owned(),
            trusted_proxy_ips: vec!["192.0.2.10".to_owned()],
            allow_insecure_web_auth: false,
            started_at: Instant::now(),
        };
        let mut headers = HeaderMap::new();
        headers.insert("x-forwarded-proto", HeaderValue::from_static("https"));
        let make_connection = |remote_addr| {
            eggserve_primitives::connection_info::ConnectionInfo::new(
                None,
                Some(remote_addr),
                eggserve_primitives::connection_info::Scheme::Http,
                None,
            )
        };
        let connection = make_connection("192.0.2.10:443".parse().unwrap());
        assert!(forwarded_https(Some(&connection), &headers, &server));
        let untrusted_connection = make_connection("198.51.100.20:443".parse().unwrap());
        assert!(!forwarded_https(
            Some(&untrusted_connection),
            &headers,
            &server
        ));
        headers.append("x-forwarded-proto", HeaderValue::from_static("http"));
        assert!(!forwarded_https(Some(&connection), &headers, &server));

        server.trusted_proxy_ips.clear();
        assert!(!web_auth_transport_allowed(
            Some(&connection),
            &headers,
            &server
        ));
    }
}
