use super::*;
use axum::{Extension, Form, extract::State, http::HeaderMap, response::Redirect};
use serde::Deserialize;

#[derive(Deserialize)]
pub(in crate::server) struct CreateUserForm {
    username: String,
    password: String,
    profile_id: Option<String>,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct UserStateForm {
    user_id: String,
    enabled: bool,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct AdminPasswordForm {
    user_id: String,
    password: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct UserProfileForm {
    user_id: String,
    profile_id: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct ProfileCreateForm {
    profile_id: String,
    name: String,
    default_effect: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct ProfileUpdateForm {
    profile_id: String,
    name: String,
    default_effect: String,
    version: i64,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct ProfileRuleForm {
    profile_id: String,
    model_id: String,
    effect: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct ProfilePreferForm {
    profile_id: String,
    model_id: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct ProfileDeleteForm {
    profile_id: String,
    version: i64,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct ProfileRuleDeleteForm {
    profile_id: String,
    model_id: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct UserOverrideForm {
    user_id: String,
    model_id: String,
    effect: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct UserOverrideRemoveForm {
    user_id: String,
    model_id: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct UserBudgetForm {
    user_id: String,
    model_id: String,
    window: String,
    max_requests: String,
    max_tokens: String,
    max_microdollars: String,
    max_concurrent: String,
    /// Optional so older posted forms keep working; absent means estimated.
    enforcement: Option<String>,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct InstanceBudgetForm {
    model_id: String,
    window: String,
    max_requests: String,
    max_tokens: String,
    max_microdollars: String,
    max_concurrent: String,
    /// Optional so older posted forms keep working; absent means estimated.
    enforcement: Option<String>,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct TokenCreateForm {
    label: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct TokenRevokeForm {
    credential_id: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct AdminTokenForm {
    user_id: String,
    label: String,
    csrf: String,
}

#[derive(Deserialize)]
pub(in crate::server) struct AdminRevokeTokensForm {
    user_id: String,
    csrf: String,
}

fn csrf_valid(
    session: &crate::web_session::Principal,
    headers: &HeaderMap,
    supplied: &str,
) -> bool {
    crate::web_session::verify_csrf(session, supplied)
        && crate::web_session::csrf_cookie_value(headers)
            .is_some_and(|cookie| crate::web_session::verify_csrf(session, &cookie))
}

fn admin_only(session: &crate::web_session::Principal) -> bool {
    session.role == "admin" && !session.must_change_password
}

fn bad_request(message: &'static str) -> Response {
    (StatusCode::BAD_REQUEST, message).into_response()
}

fn mutation_redirect(path: &'static str) -> Response {
    Redirect::to(path).into_response()
}

fn mutation_error(error: impl std::fmt::Display, fallback: &'static str) -> Response {
    tracing::warn!(error = %error, "user administration mutation failed");
    (StatusCode::BAD_REQUEST, fallback).into_response()
}

fn one_time_token_page(token: &str, return_path: &str, owner_message: &str) -> Response {
    let mut response = html_response(format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Save personal token</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><main><h1>Save this credential now</h1><p>It will not be shown again. {owner_message}</p><p>Store it in the environment that launches the client as EGGPOOL_API_KEY. The return screen will never contain the credential.</p><pre>{}</pre><p><a href=\"{}\">Return</a></p></main></html>",
        html_escape(token),
        html_escape(return_path)
    ));
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store, max-age=0"),
    );
    response.headers_mut().insert(
        http::HeaderName::from_static("referrer-policy"),
        HeaderValue::from_static("no-referrer"),
    );
    response.headers_mut().insert(
        http::HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    response
}

pub(in crate::server) async fn users_page(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
) -> Response {
    let csrf = crate::web_session::csrf_cookie_value(&headers)
        .filter(|token| crate::web_session::verify_csrf(&session, token))
        .unwrap_or_default();
    let users = match crate::identity::list_users(&state.database).await {
        Ok(users) => users,
        Err(_) => return degraded("user administration unavailable"),
    };
    let policy = crate::model_policy::ModelPolicyService::new(state.database.clone());
    let profiles = match policy.list_profiles().await {
        Ok(profiles) => profiles,
        Err(_) => return degraded("user administration unavailable"),
    };
    let instance_rules =
        match crate::instance_budget::InstanceBudgetService::new(state.database.clone())
            .rules()
            .await
        {
            Ok(rules) => rules,
            Err(_) => return degraded("user administration unavailable"),
        };
    let body = render_users_admin(&users, &profiles, &instance_rules, &csrf);
    dashboard_page_with_body(
        &state.server.dashboard_theme,
        "Users",
        "users",
        Some("24h".to_owned()),
        None,
        state.server.dashboard_refresh_interval_s,
        body,
    )
}

pub(in crate::server) async fn profiles_page(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
) -> Response {
    let csrf = crate::web_session::csrf_cookie_value(&headers)
        .filter(|token| crate::web_session::verify_csrf(&session, token))
        .unwrap_or_default();
    let profiles = match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .list_profiles()
        .await
    {
        Ok(profiles) => profiles,
        Err(_) => return degraded("profile administration unavailable"),
    };
    let rules = match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .list_profile_rules()
        .await
    {
        Ok(rules) => rules,
        Err(_) => return degraded("profile administration unavailable"),
    };
    let body = render_profiles_admin(&profiles, &rules, &csrf);
    dashboard_page_with_body(
        &state.server.dashboard_theme,
        "Profiles",
        "profiles",
        Some("24h".to_owned()),
        None,
        state.server.dashboard_refresh_interval_s,
        body,
    )
}

pub(in crate::server) async fn usage_page(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    Extension(principal): Extension<crate::identity::AuthenticatedPrincipal>,
) -> Response {
    let Some(user_id) = principal.user_id.as_deref() else {
        return (StatusCode::FORBIDDEN, "User account required.").into_response();
    };
    if session.role != "admin" {
        let rules = match crate::user_budget::UserBudgetService::new(state.database.clone())
            .rules(user_id)
            .await
        {
            Ok(rules) => rules,
            Err(_) => return degraded("usage unavailable"),
        };
        let rows = match crate::user_budget::UserBudgetService::new(state.database.clone())
            .usage(user_id, "0000-01-01", 500)
            .await
        {
            Ok(rows) => rows,
            Err(_) => return degraded("usage unavailable"),
        };
        return html_response(render_my_usage(&session.username, &rules, &rows));
    }
    let rows = match state
        .database
        .call(|connection| {
            let mut statement = connection.prepare(
                "SELECT u.username, COALESCE(b.model_id, '(all models)'), \
                        SUM(b.request_count), SUM(b.token_count), SUM(b.cost_microdollars) \
                 FROM user_budget_usage b JOIN users u ON u.id = b.user_id \
                 GROUP BY u.id, b.model_id ORDER BY u.username COLLATE NOCASE, b.model_id LIMIT 500",
            )?;
            statement
                .query_map([], |row| {
                    Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?))
                })?
                .collect::<Result<Vec<(String, String, i64, i64, i64)>, _>>()
        })
        .await
    {
        Ok(rows) => rows,
        Err(_) => return degraded("usage unavailable"),
    };
    html_response(render_usage_admin(&rows, ""))
}

pub(in crate::server) async fn audit_page(State(state): State<AppState>) -> Response {
    let entries = match crate::identity::admin_audit(&state.database, 200).await {
        Ok(entries) => entries,
        Err(_) => return degraded("audit history unavailable"),
    };
    let rows = entries
        .iter()
        .map(|entry| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&entry.created_at),
                html_escape(&entry.actor_id),
                html_escape(&entry.action),
                html_escape(&entry.target_kind),
                html_escape(&entry.target_id),
            )
        })
        .collect::<String>();
    html_response(format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Admin audit</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><main><h1>Administrative audit</h1><table><thead><tr><th>UTC time</th><th>Actor ID</th><th>Action</th><th>Target type</th><th>Target ID</th></tr></thead><tbody>{rows}</tbody></table><p><a href=\"/users\">Users</a></p></main></html>"
    ))
}

pub(in crate::server) async fn my_account_page(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
) -> Response {
    let csrf = crate::web_session::csrf_cookie_value(&headers)
        .filter(|token| crate::web_session::verify_csrf(&session, token))
        .unwrap_or_default();
    let credentials =
        match crate::identity::list_user_credentials(&state.database, &session.user_id).await {
            Ok(credentials) => credentials,
            Err(_) => return degraded("account details unavailable"),
        };
    let rows = credentials.iter().map(|credential| format!(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td><form method=\"post\" action=\"/account/tokens/revoke\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"credential_id\" value=\"{}\"><button type=\"submit\">Revoke</button></form></td></tr>",
        html_escape(&credential.label), html_escape(&credential.created_at),
        html_escape(credential.last_used_at.as_deref().unwrap_or("Never")), html_escape(&csrf), html_escape(&credential.id)
    )).collect::<String>();
    let token_flash = crate::web_session::csrf_cookie_value(&headers)
        .map(|_| "<p>New credentials are displayed only once when created. Store them in your client environment as EGGPOOL_API_KEY.</p>")
        .unwrap_or_default();
    html_response(format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>My account</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><main><h1>My account</h1><p>Signed in as {}</p><p><a href=\"/usage\">My usage</a> · <a href=\"/my-account/models\">My accessible models</a> · <a href=\"/account\">Change password</a></p><section><h2>Personal client credentials</h2>{token_flash}<form method=\"post\" action=\"/account/tokens\"><label>Credential label <input name=\"label\" minlength=\"1\" maxlength=\"80\" required></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Create credential</button></form><table><thead><tr><th>Label</th><th>Created</th><th>Last used</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></section><form method=\"post\" action=\"/logout\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Log out</button></form></main></html>",
        html_escape(&session.username),
        html_escape(&csrf),
        html_escape(&csrf)
    ))
}

pub(in crate::server) async fn my_models_page(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
) -> Response {
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return degraded("model list unavailable"),
    };
    let inference = lease.generation().inference();
    let Some(policy) = inference.model_policy_service() else {
        return degraded("model list unavailable");
    };
    let snapshot = match policy.snapshot(&session.user_id).await {
        Ok(snapshot) => snapshot,
        Err(_) => return degraded("model list unavailable"),
    };
    let models = inference
        .authorized_discovery_model_ids(&snapshot)
        .into_iter()
        .map(|model| format!("<li><code>{}</code></li>", html_escape(&model)))
        .collect::<String>();
    html_response(format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>My models</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><main><h1>My accessible models</h1><p>This list is filtered by your current model policy.</p><ul>{models}</ul><p><a href=\"/my-account\">My account</a></p></main></html>"
    ))
}

pub(in crate::server) async fn create_user(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<CreateUserForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    let user = match crate::identity::create_member(
        &state.database,
        &session.user_id,
        &form.username,
        &form.password,
    )
    .await
    {
        Ok(user) => user,
        Err(error) => return mutation_error(error, "Could not create user."),
    };
    if let Some(profile_id) = form.profile_id.as_deref().filter(|id| !id.is_empty())
        && let Err(error) = crate::model_policy::ModelPolicyService::new(state.database.clone())
            .assign_profile(&user.id, profile_id, &session.user_id)
            .await
    {
        return mutation_error(
            error,
            "User was created with the default-deny policy; profile assignment failed.",
        );
    }
    mutation_redirect("/users")
}

pub(in crate::server) async fn set_user_state(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<UserStateForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::identity::set_user_enabled(
        &state.database,
        &session.user_id,
        &form.user_id,
        form.enabled,
    )
    .await
    {
        Ok(()) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not change user state."),
    }
}

pub(in crate::server) async fn reset_user_password(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<AdminPasswordForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::identity::admin_reset_password(
        &state.database,
        &session.user_id,
        &form.user_id,
        &form.password,
    )
    .await
    {
        Ok(()) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not reset user password."),
    }
}

pub(in crate::server) async fn assign_user_profile(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<UserProfileForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .assign_profile(&form.user_id, &form.profile_id, &session.user_id)
        .await
    {
        Ok(()) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not assign profile."),
    }
}

pub(in crate::server) async fn create_profile(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<ProfileCreateForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .create_profile(
            &form.profile_id,
            &form.name,
            &form.default_effect,
            &session.user_id,
        )
        .await
    {
        Ok(()) => mutation_redirect("/profiles"),
        Err(error) => mutation_error(error, "Could not create profile."),
    }
}

pub(in crate::server) async fn update_profile(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<ProfileUpdateForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .update_profile(
            &form.profile_id,
            &form.name,
            &form.default_effect,
            form.version,
            &session.user_id,
        )
        .await
    {
        Ok(true) => mutation_redirect("/profiles"),
        Ok(false) => (
            StatusCode::CONFLICT,
            "Profile changed since this page loaded.",
        )
            .into_response(),
        Err(error) => mutation_error(error, "Could not update profile."),
    }
}

pub(in crate::server) async fn delete_profile(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<ProfileDeleteForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .delete_profile(&form.profile_id, form.version, &session.user_id)
        .await
    {
        Ok(true) => mutation_redirect("/profiles"),
        Ok(false) => (
            StatusCode::CONFLICT,
            "Profile is assigned or changed since this page loaded.",
        )
            .into_response(),
        Err(error) => mutation_error(error, "Could not delete profile."),
    }
}

pub(in crate::server) async fn set_profile_rule(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<ProfileRuleForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .set_profile_rule(
            &form.profile_id,
            &form.model_id,
            &form.effect,
            &session.user_id,
        )
        .await
    {
        Ok(()) => mutation_redirect("/profiles"),
        Err(error) => mutation_error(error, "Could not save model rule."),
    }
}

pub(in crate::server) async fn remove_profile_rule(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<ProfileRuleDeleteForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .remove_profile_rule(&form.profile_id, &form.model_id, &session.user_id)
        .await
    {
        Ok(_) => mutation_redirect("/profiles"),
        Err(error) => mutation_error(error, "Could not remove profile rule."),
    }
}

pub(in crate::server) async fn set_profile_preference(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<ProfilePreferForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    let model = (!form.model_id.trim().is_empty()).then_some(form.model_id.as_str());
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .set_profile_preference(&form.profile_id, model, &session.user_id)
        .await
    {
        Ok(()) => mutation_redirect("/profiles"),
        Err(error) => mutation_error(error, "Could not save model recommendation."),
    }
}

pub(in crate::server) async fn set_user_override(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<UserOverrideForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .set_user_override(
            &form.user_id,
            &form.model_id,
            &form.effect,
            &session.user_id,
        )
        .await
    {
        Ok(()) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not save user model override."),
    }
}

pub(in crate::server) async fn remove_user_override(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<UserOverrideRemoveForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::model_policy::ModelPolicyService::new(state.database.clone())
        .remove_user_override(&form.user_id, &form.model_id, &session.user_id)
        .await
    {
        Ok(_) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not remove user override."),
    }
}

fn parse_optional_limit(value: &str) -> Result<Option<i64>, ()> {
    if value.trim().is_empty() {
        Ok(None)
    } else {
        value.parse::<i64>().map(Some).map_err(|_| ())
    }
}

pub(in crate::server) async fn set_user_budget(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<UserBudgetForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    let limits = match (
        parse_optional_limit(&form.max_requests),
        parse_optional_limit(&form.max_tokens),
        parse_optional_limit(&form.max_microdollars),
        parse_optional_limit(&form.max_concurrent),
    ) {
        (Ok(requests), Ok(tokens), Ok(microdollars), Ok(concurrent)) => {
            crate::user_budget::BudgetLimits {
                requests,
                tokens,
                microdollars,
                concurrent,
                enforcement: crate::user_budget::BudgetEnforcement::Estimated,
            }
        }
        _ => return bad_request("Budget limits must be positive integers."),
    };
    let enforcement = match form.enforcement.as_deref().map(str::trim) {
        None | Some("") | Some("estimated") => crate::user_budget::BudgetEnforcement::Estimated,
        Some("hard") => crate::user_budget::BudgetEnforcement::Hard,
        Some(_) => return bad_request("Invalid budget enforcement mode."),
    };
    let limits = crate::user_budget::BudgetLimits {
        enforcement,
        ..limits
    };
    let window = match form.window.as_str() {
        "day" => crate::user_budget::BudgetWindow::Day,
        "month" => crate::user_budget::BudgetWindow::Month,
        _ => return bad_request("Invalid budget window."),
    };
    let model_id = (!form.model_id.trim().is_empty()).then_some(form.model_id.as_str());
    match crate::user_budget::UserBudgetService::new(state.database.clone())
        .set_rule(&session.user_id, &form.user_id, model_id, window, limits)
        .await
    {
        Ok(()) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not save budget rule."),
    }
}

fn parse_instance_limits(
    form: &InstanceBudgetForm,
) -> Result<crate::user_budget::BudgetLimits, &'static str> {
    let limits = match (
        parse_optional_limit(&form.max_requests),
        parse_optional_limit(&form.max_tokens),
        parse_optional_limit(&form.max_microdollars),
        parse_optional_limit(&form.max_concurrent),
    ) {
        (Ok(requests), Ok(tokens), Ok(microdollars), Ok(concurrent)) => {
            crate::user_budget::BudgetLimits {
                requests,
                tokens,
                microdollars,
                concurrent,
                enforcement: crate::user_budget::BudgetEnforcement::Estimated,
            }
        }
        _ => return Err("Budget limits must be positive integers."),
    };
    let enforcement = match form.enforcement.as_deref().map(str::trim) {
        None | Some("") | Some("estimated") => crate::user_budget::BudgetEnforcement::Estimated,
        Some("hard") => crate::user_budget::BudgetEnforcement::Hard,
        Some(_) => return Err("Invalid budget enforcement mode."),
    };
    Ok(crate::user_budget::BudgetLimits {
        enforcement,
        ..limits
    })
}

fn parse_instance_window(window: &str) -> Result<crate::user_budget::BudgetWindow, &'static str> {
    match window {
        "day" => Ok(crate::user_budget::BudgetWindow::Day),
        "month" => Ok(crate::user_budget::BudgetWindow::Month),
        _ => Err("Invalid budget window."),
    }
}

pub(in crate::server) async fn set_instance_budget(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<InstanceBudgetForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    let limits = match parse_instance_limits(&form) {
        Ok(limits) => limits,
        Err(message) => return bad_request(message),
    };
    let window = match parse_instance_window(form.window.as_str()) {
        Ok(window) => window,
        Err(message) => return bad_request(message),
    };
    let model_id = (!form.model_id.trim().is_empty()).then_some(form.model_id.as_str());
    match crate::instance_budget::InstanceBudgetService::new(state.database.clone())
        .set_rule(&session.user_id, model_id, window, limits)
        .await
    {
        Ok(()) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not save instance budget rule."),
    }
}

pub(in crate::server) async fn remove_instance_budget(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<InstanceBudgetForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    let window = match parse_instance_window(form.window.as_str()) {
        Ok(window) => window,
        Err(message) => return bad_request(message),
    };
    let model_id = (!form.model_id.trim().is_empty()).then_some(form.model_id.as_str());
    match crate::instance_budget::InstanceBudgetService::new(state.database.clone())
        .remove_rule(&session.user_id, model_id, window)
        .await
    {
        Ok(_) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not remove instance budget rule."),
    }
}

pub(in crate::server) async fn create_personal_token(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<TokenCreateForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    match crate::identity::issue_personal_api_token(
        &state.database,
        &session.user_id,
        &session.user_id,
        &form.label,
    )
    .await
    {
        Ok(credential) => one_time_token_page(
            &credential.token,
            "/my-account",
            "This is your personal credential.",
        ),
        Err(error) => mutation_error(error, "Could not create credential."),
    }
}

pub(in crate::server) async fn revoke_personal_token(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<TokenRevokeForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    match crate::identity::revoke_user_credential(
        &state.database,
        &session.user_id,
        &session.user_id,
        &form.credential_id,
    )
    .await
    {
        Ok(()) => mutation_redirect("/my-account"),
        Err(error) => mutation_error(error, "Could not revoke credential."),
    }
}

pub(in crate::server) async fn issue_admin_token(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<AdminTokenForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::identity::issue_personal_api_token(
        &state.database,
        &session.user_id,
        &form.user_id,
        &form.label,
    )
    .await
    {
        Ok(credential) => one_time_token_page(
            &credential.token,
            "/users",
            "The administrator issued this for the selected user.",
        ),
        Err(error) => mutation_error(error, "Could not create credential."),
    }
}

pub(in crate::server) async fn revoke_user_tokens(
    State(state): State<AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    Form(form): Form<AdminRevokeTokensForm>,
) -> Response {
    if !csrf_valid(&session, &headers, &form.csrf) {
        return (StatusCode::FORBIDDEN, "Invalid request token.").into_response();
    }
    if !admin_only(&session) {
        return (StatusCode::FORBIDDEN, "Administrator access required.").into_response();
    }
    match crate::identity::revoke_user_api_tokens(&state.database, &session.user_id, &form.user_id)
        .await
    {
        Ok(_) => mutation_redirect("/users"),
        Err(error) => mutation_error(error, "Could not revoke credentials."),
    }
}
