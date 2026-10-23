//! Browser login, sessions, CSRF forms, and forwarded-HTTPS validation.

use super::*;
use axum::{
    Extension, Form,
    http::{HeaderMap, HeaderValue, header},
    response::{Html, Redirect},
};
use serde::Deserialize;
use std::net::IpAddr;

/// The member landing page. A member who signs in lands here rather than on a
/// bare password form, so the member surface is reachable without knowing a
/// URL. The path is registered next to the other `/account` routes.
pub(super) const MEMBER_LANDING_PATH: &str = "/account/landing";

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

pub(super) async fn login_page(
    State(state): State<AppState>,
    headers: HeaderMap,
    peer: Option<Extension<eggserve_server::interop::ConnectionInfoExt>>,
) -> Response {
    let connection = peer.as_ref().map(|extension| &extension.0.0);
    let (status, html) = login_page_for_transport(connection, &headers, &state.server);
    sign_in_response(status, html)
}

fn login_page_for_transport(
    connection: Option<&eggserve_primitives::connection_info::ConnectionInfo>,
    headers: &HeaderMap,
    server: &ServerState,
) -> (StatusCode, String) {
    if web_auth_transport_allowed(connection, headers, server) {
        (StatusCode::OK, login_html(None))
    } else {
        (StatusCode::FORBIDDEN, unsafe_login_transport_html())
    }
}

/// The refusal shown when the connection is neither local nor fronted by a
/// trusted HTTPS proxy.
///
/// It names the SSH port forward and the reverse-proxy path because that text
/// is operator-facing documentation of the supported ways to reach the console,
/// not a generic error, and it never asks for a credential.
fn unsafe_login_transport_html() -> String {
    session_document(
        "Secure sign-in required",
        &format!(
            "<p class=\"status\" role=\"alert\">{}</p>\
             <p>This connection is not local and is not protected by a trusted HTTPS proxy. EggPool did not request a username or password.</p>\
             <p>For a solo headless device, use an SSH port forward such as <code>ssh -L 11300:127.0.0.1:11300 operator@device</code> and open <code>http://127.0.0.1:11300/login</code>.</p>\
             <p>For a team server, use an operator-managed HTTPS reverse proxy or VPN.</p>",
            html_escape("Browser sign-in requires HTTPS or loopback.")
        ),
    )
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
        return sign_in_response(StatusCode::FORBIDDEN, unsafe_login_transport_html());
    }
    if !same_origin(&headers, secure) {
        return sign_in_failure(
            StatusCode::FORBIDDEN,
            "Cross-origin sign-in is not allowed.",
        );
    }
    let peer_ip = throttle_peer_ip(connection, &headers, &state.server);
    // The account bucket is a hard refusal consulted before the credential
    // check, so an exhausted account budget is not broken by a correct
    // password.
    if !crate::web_session::login_allowed(&form.username, peer_ip) {
        return sign_in_failure(
            StatusCode::TOO_MANY_REQUESTS,
            "Sign-in failed. Try again later.",
        );
    }
    let access_state = match crate::identity::access_state(&state.database).await {
        Ok(access_state) => access_state,
        Err(_) => return sign_in_unavailable(),
    };
    if !matches!(access_state.as_str(), "admin_initialized" | "managed") {
        return no_store(
            (StatusCode::NOT_FOUND, "Browser sign-in is not enabled.").into_response(),
        );
    }

    let user = crate::identity::verify_password(&state.database, &form.username, &form.password)
        .await
        .ok()
        .flatten();
    let Some(user) = user else {
        crate::web_session::record_login_failure(&form.username, peer_ip);
        return sign_in_failure(StatusCode::UNAUTHORIZED, "Invalid username or password.");
    };
    // A successful credential check clears the account's failure bucket, so a
    // run of failures that does not end in a successful sign-in is what
    // consumes the account's budget. The per-peer bucket is deliberately left
    // alone: clearing it would let any valid account reset an address's
    // cross-account failure budget and multiply that address's brute-force
    // throughput without bound.
    crate::web_session::clear_login_failures(&form.username);
    let session = match crate::web_session::issue(&state.database, &user.id).await {
        Ok(session) => session,
        Err(_) => return sign_in_unavailable(),
    };
    let mut response = no_store(
        Redirect::to(if user.must_change_password {
            "/account?password=required"
        } else if user.role == "admin" {
            "/account"
        } else {
            MEMBER_LANDING_PATH
        })
        .into_response(),
    );
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

/// The member landing page: where a member lands after a successful sign-in.
///
/// It links only the member destinations and exposes no administrative route
/// and no administrative datum.
pub(super) async fn member_landing_page(
    Extension(principal): Extension<crate::web_session::Principal>,
) -> Html<String> {
    Html(member_landing_html(&principal.username))
}

pub(super) fn member_landing_html(username: &str) -> String {
    let username = html_escape(username);
    session_document(
        "Your account",
        &format!(
            "<p>Signed in as {username}.</p>\
             <div class=\"cards\">\
             <div class=\"card\"><h3>My account</h3><p class=\"sub\">Your personal details, personal credentials, and budget.</p><p><a href=\"/my-account\">Open my account</a></p></div>\
             <div class=\"card\"><h3>My models</h3><p class=\"sub\">The models your profile permits.</p><p><a href=\"/my-account/models\">Open my models</a></p></div>\
             <div class=\"card\"><h3>Usage</h3><p class=\"sub\">Your own request and token usage.</p><p><a href=\"/usage\">Open usage</a></p></div>\
             </div>"
        ),
    )
}

/// The refusal a non-administrator receives for an administrative path.
///
/// It keeps the status code and the message, and it points at the member
/// surface the principal does have instead of leaving a bare text body.
pub(super) fn member_refusal_html() -> String {
    session_document(
        "Administrator access required",
        &format!(
            "<p class=\"status\" role=\"alert\">{}</p>\
             <p>Your account does not have access to the administrator console.</p>\
             <p><a href=\"{MEMBER_LANDING_PATH}\">Continue to your account</a></p>",
            html_escape("Administrator access required.")
        ),
    )
}

pub(super) async fn account_page(
    Extension(principal): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
) -> Html<String> {
    let csrf = crate::web_session::csrf_cookie_value(&headers)
        .filter(|token| crate::web_session::verify_csrf(&principal, token));
    let csrf = csrf.unwrap_or_default();
    Html(account_html(
        &principal.username,
        principal.must_change_password,
        &csrf,
        principal.role == "admin",
    ))
}

/// The account page.
///
/// An administrator is pointed back at the dashboard overview; a member is
/// pointed at the member surface, so a signed-in member always has a way
/// onward that does not require knowing a URL.
fn account_html(username: &str, forced: bool, csrf: &str, admin: bool) -> String {
    let username = html_escape(username);
    let notice = if forced {
        "<p>Change your password to continue.</p>"
    } else {
        ""
    };
    let navigation = if admin {
        "<p><a href=\"/\">Dashboard overview</a></p>"
    } else {
        "<p><a href=\"/my-account\">My account</a> &middot; <a href=\"/my-account/models\">My models</a> &middot; <a href=\"/usage\">Usage</a></p>"
    };
    session_document(
        "Account",
        &format!(
            "<p>Signed in as {username}.</p>{notice}{navigation}\
             <form method=\"post\" action=\"/account/password\">\
             <label>Current password <input type=\"password\" name=\"current_password\" autocomplete=\"current-password\" required></label>\
             <label>New password <input type=\"password\" name=\"new_password\" autocomplete=\"new-password\" minlength=\"12\" maxlength=\"1024\" required></label>\
             <input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">\
             <button type=\"submit\">Change password</button></form>\
             <form method=\"post\" action=\"/logout\">\
             <input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">\
             <button type=\"submit\">Log out</button></form>"
        ),
    )
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

/// The one document shell for the browser-session surfaces.
///
/// The sign-in page, the account page, the account-error page, the transport
/// refusal, the member landing page, and the member refusal all render
/// through it, so an operator meets one product surface instead of an
/// unstyled fragment.
///
/// `dashboard.css` declares no custom properties of its own, so the complete
/// variable set comes from `/static/theme.css`. The theme link carries no
/// query parameter and the route's default theme applies; the sign-in surface
/// has no operator-selected theme because the theme travels in the query
/// string of the dashboard navigation rather than being stored. Both static
/// routes are registered unconditionally, so the stylesheets resolve whether
/// or not the dashboard is enabled.
///
/// The document references no custom property of its own, so it never depends
/// on an unresolved `var(`.
pub(super) fn session_document(title: &str, body: &str) -> String {
    format!(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title}</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><link rel=\"stylesheet\" href=\"/static/theme.css\"></head><body><main class=\"panel\"><div class=\"panel-header\"><h2>{title}</h2></div>{body}</main></body></html>",
        title = html_escape(title)
    )
}

/// The sign-in document, with an optional operator-facing alert.
fn login_html(message: Option<&str>) -> String {
    let message = message.map_or(String::new(), |message| {
        format!(
            "<p class=\"status\" role=\"alert\">{}</p>",
            html_escape(message)
        )
    });
    session_document(
        "Sign in",
        &format!(
            "{message}\
             <form method=\"post\" action=\"/login\">\
             <label>Username <input name=\"username\" autocomplete=\"username\" required></label>\
             <label>Password <input type=\"password\" name=\"password\" autocomplete=\"current-password\" required></label>\
             <button type=\"submit\">Sign in</button></form>"
        ),
    )
}

/// The account-error document.
fn account_error_html(message: &str) -> String {
    session_document(
        "Account",
        &format!(
            "<p class=\"status\" role=\"alert\">{}</p>\
             <p><a href=\"/account\">Return to account</a></p>",
            html_escape(message)
        ),
    )
}

/// One presentation for every sign-in failure.
fn sign_in_failure(status: StatusCode, message: &str) -> Response {
    sign_in_response(status, sign_in_failure_html(message))
}

/// The document every sign-in failure renders: the shared document with a
/// `role="alert"` message.
fn sign_in_failure_html(message: &str) -> String {
    login_html(Some(message))
}

/// A sign-in response. `/login` is an unconditionally public path, so the
/// middleware returns before it applies the `no-store` header that every
/// session-authenticated response carries. The sign-in surface must set the
/// header itself.
fn sign_in_response(status: StatusCode, html: String) -> Response {
    no_store((status, Html(html)).into_response())
}

fn sign_in_unavailable() -> Response {
    sign_in_failure(
        StatusCode::SERVICE_UNAVAILABLE,
        "Authentication service unavailable.",
    )
}

fn no_store(mut response: Response) -> Response {
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store, max-age=0"),
    );
    response
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

    fn test_server() -> ServerState {
        ServerState {
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
        }
    }

    #[test]
    fn both_browser_cookies_are_http_only_lax_and_root_scoped() {
        let session = crate::web_session::session_set_cookie("session-value", false);
        assert!(
            session.starts_with(
                "eggpool_session=session-value; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
            ),
            "{session}"
        );
        assert!(!session.contains("Secure"), "{session}");

        let csrf = crate::web_session::csrf_set_cookie("csrf-value", false);
        assert!(
            csrf.starts_with(
                "eggpool_csrf=csrf-value; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
            ),
            "{csrf}"
        );
        assert!(!csrf.contains("Secure"), "{csrf}");

        let secure_session = crate::web_session::session_set_cookie("session-value", true);
        assert!(secure_session.ends_with("; Secure"), "{secure_session}");
        let secure_csrf = crate::web_session::csrf_set_cookie("csrf-value", true);
        assert!(secure_csrf.ends_with("; Secure"), "{secure_csrf}");
    }

    #[test]
    fn signing_out_clears_both_cookies_with_the_attributes_they_were_set_with() {
        let cleared = crate::web_session::clear_cookies(false);
        assert!(
            cleared[0].starts_with("eggpool_session=;"),
            "{}",
            cleared[0]
        );
        assert!(cleared[1].starts_with("eggpool_csrf=;"), "{}", cleared[1]);
        for value in cleared {
            assert!(
                value.contains("Path=/; HttpOnly; SameSite=Lax; Max-Age=0"),
                "{value}"
            );
        }
    }

    #[test]
    fn every_session_document_links_both_stylesheets_and_declares_the_viewport() {
        for html in [
            login_html(None),
            login_html(Some("Invalid username or password.")),
            account_error_html("Current password is incorrect."),
            unsafe_login_transport_html(),
            member_landing_html("member"),
            member_refusal_html(),
        ] {
            assert!(html.starts_with("<!doctype html>"), "{html}");
            assert!(html.contains("<html lang=\"en\">"), "{html}");
            assert!(html.contains("<meta charset=\"utf-8\">"), "{html}");
            assert!(
                html.contains(
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                ),
                "{html}"
            );
            assert!(html.contains("<title>"), "{html}");
            assert!(
                html.contains("<link rel=\"stylesheet\" href=\"/static/dashboard.css\">"),
                "{html}"
            );
            assert!(
                html.contains("<link rel=\"stylesheet\" href=\"/static/theme.css\">"),
                "{html}"
            );
            assert!(html.contains("class=\"panel\""), "{html}");
            // The sign-in surface never depends on a custom property of its
            // own, so it never renders an unresolved `var(`.
            assert!(!html.contains("var("), "{html}");
        }
    }

    #[test]
    fn the_sign_in_form_keeps_its_fields_and_autocomplete_attributes() {
        let html = login_html(None);
        assert!(html.contains("action=\"/login\""), "{html}");
        assert!(html.contains("name=\"username\""), "{html}");
        assert!(html.contains("autocomplete=\"username\""), "{html}");
        assert!(html.contains("name=\"password\""), "{html}");
        assert!(html.contains("autocomplete=\"current-password\""), "{html}");
    }

    #[test]
    fn the_account_page_keeps_its_password_bounds_and_form_actions() {
        let html = account_html("member", false, "", false);
        assert!(html.contains("action=\"/account/password\""), "{html}");
        assert!(html.contains("action=\"/logout\""), "{html}");
        assert!(html.contains("autocomplete=\"current-password\""), "{html}");
        assert!(
            html.contains("autocomplete=\"new-password\" minlength=\"12\" maxlength=\"1024\""),
            "{html}"
        );
    }

    #[test]
    fn the_account_page_points_each_role_at_its_own_surface() {
        let member = account_html("member", false, "", false);
        assert!(member.contains("href=\"/my-account\""), "{member}");
        assert!(member.contains("href=\"/my-account/models\""), "{member}");
        assert!(member.contains("href=\"/usage\""), "{member}");
        assert!(!member.contains("href=\"/\""), "{member}");

        let admin = account_html("admin", false, "", true);
        assert!(admin.contains("href=\"/\""), "{admin}");
    }

    #[test]
    fn the_transport_refusal_names_the_supported_paths() {
        let html = unsafe_login_transport_html();
        assert!(
            html.contains("ssh -L 11300:127.0.0.1:11300 operator@device"),
            "{html}"
        );
        assert!(html.contains("http://127.0.0.1:11300/login"), "{html}");
        assert!(html.contains("HTTPS reverse proxy or VPN"), "{html}");
        assert!(html.contains("role=\"alert\""), "{html}");
        // The refusal never asks for a credential.
        assert!(!html.contains("type=\"password\""), "{html}");
        assert!(!html.contains("name=\"username\""), "{html}");
    }

    #[test]
    fn every_sign_in_failure_is_an_html_alert_that_is_not_cacheable() {
        for html in [
            sign_in_failure_html("Cross-origin sign-in is not allowed."),
            sign_in_failure_html("Sign-in failed. Try again later."),
            sign_in_failure_html("Authentication service unavailable."),
            unsafe_login_transport_html(),
        ] {
            assert!(html.contains("role=\"alert\""), "{html}");
            let response = sign_in_response(StatusCode::FORBIDDEN, html);
            assert_eq!(
                response
                    .headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok()),
                Some("text/html; charset=utf-8")
            );
            assert_eq!(
                response
                    .headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|value| value.to_str().ok()),
                Some("no-store, max-age=0")
            );
        }
    }

    #[test]
    fn the_member_landing_page_links_only_the_member_surface() {
        let html = member_landing_html("member");
        assert!(html.contains("href=\"/my-account\""), "{html}");
        assert!(html.contains("href=\"/my-account/models\""), "{html}");
        assert!(html.contains("href=\"/usage\""), "{html}");
        // No administrative route and no administrative datum.
        for administrative in [
            "href=\"/users\"",
            "href=\"/profiles\"",
            "href=\"/admin/",
            "href=\"/accounts\"",
        ] {
            assert!(!html.contains(administrative), "{administrative}: {html}");
        }
    }

    #[test]
    fn the_member_refusal_keeps_its_message_and_points_at_the_member_surface() {
        let html = member_refusal_html();
        assert!(html.contains("Administrator access required."), "{html}");
        assert!(html.contains("role=\"alert\""), "{html}");
        assert!(html.contains("href=\"/account/landing\""), "{html}");
    }

    #[test]
    fn login_get_never_solicits_credentials_on_untrusted_remote_http() {
        let server = test_server();
        let connection = eggserve_primitives::connection_info::ConnectionInfo::new(
            None,
            Some("198.51.100.20:443".parse().expect("peer address")),
            eggserve_primitives::connection_info::Scheme::Http,
            None,
        );
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, HeaderValue::from_static("trusted.example"));
        headers.insert("x-forwarded-proto", HeaderValue::from_static("https"));
        let (status, html) = login_page_for_transport(Some(&connection), &headers, &server);
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert!(!html.contains("type=\"password\""));
        assert!(!html.contains("name=\"username\""));
        assert!(html.contains("ssh -L 11300:127.0.0.1:11300"));
    }

    #[test]
    fn login_get_fails_closed_for_missing_peer_and_ambiguous_proxy_scheme() {
        let server = test_server();
        let (status, html) = login_page_for_transport(None, &HeaderMap::new(), &server);
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert!(!html.contains("type=\"password\""));

        let proxy = eggserve_primitives::connection_info::ConnectionInfo::new(
            None,
            Some("192.0.2.10:443".parse().expect("proxy peer")),
            eggserve_primitives::connection_info::Scheme::Http,
            None,
        );
        let mut headers = HeaderMap::new();
        headers.append("x-forwarded-proto", HeaderValue::from_static("https"));
        headers.append("x-forwarded-proto", HeaderValue::from_static("https"));
        let (status, html) = login_page_for_transport(Some(&proxy), &headers, &server);
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert!(!html.contains("type=\"password\""));

        headers.clear();
        headers.insert(
            "x-forwarded-proto",
            HeaderValue::from_bytes(b"https\xff").expect("opaque header value"),
        );
        let (status, html) = login_page_for_transport(Some(&proxy), &headers, &server);
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert!(!html.contains("type=\"password\""));
    }

    #[test]
    fn login_get_shows_credentials_only_on_loopback_or_trusted_https_proxy() {
        let server = test_server();
        let make_connection = |peer: &str| {
            eggserve_primitives::connection_info::ConnectionInfo::new(
                None,
                Some(peer.parse().expect("peer address")),
                eggserve_primitives::connection_info::Scheme::Http,
                None,
            )
        };
        let loopback = make_connection("127.0.0.1:3000");
        let (status, html) = login_page_for_transport(Some(&loopback), &HeaderMap::new(), &server);
        assert_eq!(status, StatusCode::OK);
        assert!(html.contains("type=\"password\""));

        let proxy = make_connection("192.0.2.10:443");
        let mut headers = HeaderMap::new();
        headers.insert("x-forwarded-proto", HeaderValue::from_static("https"));
        let (status, html) = login_page_for_transport(Some(&proxy), &headers, &server);
        assert_eq!(status, StatusCode::OK);
        assert!(html.contains("type=\"password\""));
    }

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
