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
    let peer_ip = throttle_peer_ip(connection, &headers, &state.server);
    if !crate::web_session::login_allowed(&form.username, peer_ip) {
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Html(login_html(Some("Sign-in failed. Try again later."))),
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
    match crate::identity::reset_password_from_browser(
        &state.database,
        &principal,
        &form.new_password,
    )
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
    if !trusted_proxy(peer, server) {
        return false;
    }
    let mut values = headers.get_all("x-forwarded-proto").iter();
    let Some(value) = values.next().and_then(|value| value.to_str().ok()) else {
        return false;
    };
    values.next().is_none() && value.trim().eq_ignore_ascii_case("https")
}

pub(super) fn throttle_peer_ip(
    connection: Option<&eggserve_primitives::connection_info::ConnectionInfo>,
    headers: &HeaderMap,
    server: &ServerState,
) -> Option<IpAddr> {
    let peer = connection
        .and_then(|connection| connection.remote_addr)
        .map(|address| address.ip())?;
    if !trusted_proxy(peer, server) {
        return Some(peer);
    }
    let mut values = headers.get_all("x-forwarded-for").iter();
    let Some(value) = values.next().and_then(|value| value.to_str().ok()) else {
        return Some(peer);
    };
    if values.next().is_some() {
        return Some(peer);
    }
    Some(value.trim().parse().unwrap_or(peer))
}

fn trusted_proxy(peer: IpAddr, server: &ServerState) -> bool {
    server
        .trusted_proxy_ips
        .iter()
        .any(|proxy| proxy.parse::<IpAddr>().ok() == Some(peer))
}

pub(super) fn same_origin(headers: &HeaderMap, secure: bool) -> bool {
    let mut host_values = headers.get_all(header::HOST).iter();
    let Some(host) = host_values.next().and_then(|value| value.to_str().ok()) else {
        return false;
    };
    if host_values.next().is_some() {
        return false;
    }
    let expected_scheme = if secure { "https" } else { "http" };
    let Ok(expected_authority) = host.parse::<http::uri::Authority>() else {
        return false;
    };
    // A same-origin browser form POST carries `Origin` and `Referer` together, so
    // every provenance header that is present has to agree. Requiring exactly one
    // of them rejected real browsers while still accepting a bare `Referer`.
    let mut seen = false;
    for name in [header::ORIGIN, header::REFERER] {
        let referer = name == header::REFERER;
        let mut values = headers.get_all(&name).iter();
        let Some(first) = values.next() else { continue };
        seen = true;
        // A duplicated header is ambiguous provenance; fail closed rather than
        // silently preferring one of the values.
        if values.next().is_some() {
            return false;
        }
        let Ok(value) = first.to_str() else {
            return false;
        };
        let allowed = if referer {
            referer_authority(value)
        } else {
            origin_authority(value)
        };
        let Some((scheme, authority)) = allowed else {
            return false;
        };
        if !scheme.eq_ignore_ascii_case(expected_scheme) {
            return false;
        }
        let Ok(provenance_authority) = authority.parse::<http::uri::Authority>() else {
            return false;
        };
        if !expected_authority
            .host()
            .eq_ignore_ascii_case(provenance_authority.host())
            || effective_port(expected_authority.port_u16(), expected_scheme)
                != effective_port(provenance_authority.port_u16(), expected_scheme)
        {
            return false;
        }
    }
    seen
}

fn origin_authority(value: &str) -> Option<(&str, &str)> {
    let (scheme, remainder) = value.split_once("://")?;
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") {
        return None;
    }
    let authority = remainder.split(['/', '?', '#']).next()?;
    if authority.is_empty()
        || authority.contains('@')
        || remainder[authority.len()..].contains(['?', '#'])
    {
        return None;
    }
    Some((scheme, authority))
}

/// Split a `Referer`, which unlike `Origin` may carry an ordinary path, query,
/// or fragment, into the scheme and authority that matter for the same-origin
/// comparison.
fn referer_authority(value: &str) -> Option<(&str, &str)> {
    let (scheme, remainder) = value.split_once("://")?;
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") {
        return None;
    }
    let authority = remainder.split(['/', '?', '#']).next()?;
    // Userinfo would let `https://expected.test@attacker.test` read as the
    // expected host, so ambiguous provenance is rejected outright.
    if authority.is_empty() || authority.contains('@') || remainder.contains("://") {
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
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Account</title><body><main><h1>Account</h1><p role=\"alert\">{}</p><p><a href=\"/account\">Return to account</a></p></main></body></html>",
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
    fn same_origin_accepts_the_header_set_a_browser_form_post_sends() {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, HeaderValue::from_static("127.0.0.1:11300"));
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("http://127.0.0.1:11300"),
        );
        headers.insert(
            header::REFERER,
            HeaderValue::from_static("http://127.0.0.1:11300/login"),
        );
        assert!(same_origin(&headers, false));
        assert!(!same_origin(&headers, true));
        // A real browser `Referer` may carry a query or fragment.
        headers.insert(
            header::REFERER,
            HeaderValue::from_static("http://127.0.0.1:11300/login?next=%2Fusers"),
        );
        assert!(same_origin(&headers, false));
    }

    #[test]
    fn same_origin_rejects_disagreeing_or_duplicated_provenance() {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, HeaderValue::from_static("example.test"));
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://example.test"),
        );
        headers.insert(
            header::REFERER,
            HeaderValue::from_static("https://attacker.test/login"),
        );
        assert!(!same_origin(&headers, true));
        headers.insert(
            header::REFERER,
            HeaderValue::from_static("https://example.test/login"),
        );
        assert!(same_origin(&headers, true));
        headers.append(
            header::ORIGIN,
            HeaderValue::from_static("https://example.test"),
        );
        assert!(!same_origin(&headers, true));
        headers = HeaderMap::new();
        headers.insert(header::HOST, HeaderValue::from_static("example.test"));
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
        assert_eq!(
            referer_authority("https://example.test/login?x=1#frag"),
            Some(("https", "example.test"))
        );
        assert_eq!(referer_authority("https://user@host.example.test/x"), None);
        assert_eq!(referer_authority("null"), None);
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

    #[test]
    fn login_throttle_uses_forwarded_client_ip_only_from_a_trusted_proxy() {
        let server = ServerState {
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
        let connection = |remote_addr| {
            eggserve_primitives::connection_info::ConnectionInfo::new(
                None,
                Some(remote_addr),
                eggserve_primitives::connection_info::Scheme::Http,
                None,
            )
        };
        let mut headers = HeaderMap::new();
        headers.insert("x-forwarded-for", HeaderValue::from_static("198.51.100.24"));

        let trusted_peer = connection("192.0.2.10:443".parse().unwrap());
        assert_eq!(
            throttle_peer_ip(Some(&trusted_peer), &headers, &server),
            Some("198.51.100.24".parse().unwrap())
        );

        let untrusted_peer = connection("198.51.100.30:443".parse().unwrap());
        assert_eq!(
            throttle_peer_ip(Some(&untrusted_peer), &headers, &server),
            Some("198.51.100.30".parse().unwrap())
        );
    }

    #[test]
    fn login_throttle_rejects_ambiguous_forwarded_client_ip() {
        let server = ServerState {
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
        let trusted_peer = eggserve_primitives::connection_info::ConnectionInfo::new(
            None,
            Some("192.0.2.10:443".parse().unwrap()),
            eggserve_primitives::connection_info::Scheme::Http,
            None,
        );
        let mut headers = HeaderMap::new();
        headers.append("x-forwarded-for", HeaderValue::from_static("198.51.100.24"));
        headers.append("x-forwarded-for", HeaderValue::from_static("198.51.100.25"));
        assert_eq!(
            throttle_peer_ip(Some(&trusted_peer), &headers, &server),
            Some("192.0.2.10".parse().unwrap())
        );

        headers.remove("x-forwarded-for");
        headers.insert(
            "x-forwarded-for",
            HeaderValue::from_static("198.51.100.24, 198.51.100.25"),
        );
        assert_eq!(
            throttle_peer_ip(Some(&trusted_peer), &headers, &server),
            Some("192.0.2.10".parse().unwrap())
        );
    }
}
