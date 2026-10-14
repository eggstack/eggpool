use super::*;

pub(in crate::server::dashboard) fn render_users_admin(
    users: &[crate::identity::UserSummary],
    profiles: &[crate::model_policy::PolicyProfileSummary],
    csrf: &str,
) -> String {
    let options = profiles
        .iter()
        .map(|profile| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&profile.id),
                html_escape(&profile.name)
            )
        })
        .collect::<String>();
    let rows = users
        .iter()
        .map(|user| {
            let active = user.state == "active";
            let state_label = if active { "Disable" } else { "Reactivate" };
            let state_form = format!(
                "<form method=\"post\" action=\"/admin/users/state\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><input type=\"hidden\" name=\"enabled\" value=\"{}\"><button type=\"submit\">{}</button></form>",
                html_escape(csrf), html_escape(&user.id), !active, state_label
            );
            let password_form = format!(
                "<form method=\"post\" action=\"/admin/users/password\"><label>Temporary password <input type=\"password\" name=\"password\" minlength=\"12\" maxlength=\"1024\" required></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Reset password</button></form>",
                html_escape(csrf), html_escape(&user.id)
            );
            let profile_form = format!(
                "<form method=\"post\" action=\"/admin/users/profile\"><label>Profile <select name=\"profile_id\" aria-label=\"Profile for {}\">{}</select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Assign</button></form>",
                html_escape(&user.username), options, html_escape(csrf), html_escape(&user.id)
            );
            let override_form = format!(
                "<form method=\"post\" action=\"/admin/users/override\"><label>Exact model override <input name=\"model_id\" maxlength=\"512\" required></label><select name=\"effect\" aria-label=\"Model override effect\"><option>allow</option><option>deny</option></select><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Save override</button></form>",
                html_escape(csrf), html_escape(&user.id)
            );
            let remove_override_form = format!(
                "<form method=\"post\" action=\"/admin/users/override/remove\"><label>Exact model override to remove <input name=\"model_id\" maxlength=\"512\" required></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Remove override</button></form>",
                html_escape(csrf), html_escape(&user.id)
            );
            let budget_form = format!(
                "<details><summary>Set budget</summary><form method=\"post\" action=\"/admin/users/budget\"><label>Exact model (blank for all) <input name=\"model_id\" maxlength=\"512\"></label><label>Window <select name=\"window\"><option value=\"day\">UTC day</option><option value=\"month\">UTC month</option></select></label><label>Max requests (hard) <input type=\"number\" min=\"1\" name=\"max_requests\"></label><label>Max tokens (estimated) <input type=\"number\" min=\"1\" name=\"max_tokens\"></label><label>Max microdollars (estimated) <input type=\"number\" min=\"1\" name=\"max_microdollars\"></label><label>Max concurrent (hard) <input type=\"number\" min=\"1\" name=\"max_concurrent\"></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Save budget</button></form></details>",
                html_escape(csrf), html_escape(&user.id)
            );
            let token_form = format!(
                "<form method=\"post\" action=\"/admin/users/token\"><label>One-time token label <input name=\"label\" maxlength=\"80\" required></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Issue token</button></form><form method=\"post\" action=\"/admin/users/revoke-tokens\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Revoke all tokens</button></form>",
                html_escape(csrf), html_escape(&user.id), html_escape(csrf), html_escape(&user.id)
            );
            let actions = format!(
                "{state_form}{password_form}{profile_form}{override_form}{remove_override_form}{budget_form}{token_form}"
            );
            format!(
                "<tr><th scope=\"row\">{}</th><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&user.username),
                html_escape(&user.role),
                html_escape(&user.state),
                if user.must_change_password { "Must change password" } else { "—" },
                actions
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h2>Users</h2><p>Create local member accounts and assign policy profiles. Temporary passwords are shown only to the administrator who enters them; users must change them at first login.</p><form method=\"post\" action=\"/admin/users/create\"><label>Username <input name=\"username\" minlength=\"1\" maxlength=\"64\" required autocomplete=\"off\"></label><label>Temporary password <input type=\"password\" name=\"password\" minlength=\"12\" maxlength=\"1024\" required autocomplete=\"new-password\"></label><label>Initial profile <select name=\"profile_id\"><option value=\"\">No profile</option>{options}</select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Add member</button></form></section><section class=\"panel\"><h2>Local accounts</h2><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Local users\"><table><thead><tr><th>Username</th><th>Role</th><th>State</th><th>Password</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
        html_escape(csrf)
    )
}

pub(in crate::server::dashboard) fn render_profiles_admin(
    profiles: &[crate::model_policy::PolicyProfileSummary],
    rules: &[crate::model_policy::PolicyProfileRuleSummary],
    csrf: &str,
) -> String {
    let rows = profiles
        .iter()
        .map(|profile| {
            let profile_rules = rules
                .iter()
                .filter(|rule| rule.profile_id == profile.id)
                .map(|rule| {
                    format!(
                        "<li><code>{}</code>: {} <form method=\"post\" action=\"/admin/profiles/rule/remove\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><input type=\"hidden\" name=\"model_id\" value=\"{}\"><button type=\"submit\">Remove rule</button></form></li>",
                        html_escape(&rule.model_id), html_escape(&rule.effect), html_escape(csrf),
                        html_escape(&profile.id), html_escape(&rule.model_id)
                    )
                })
                .collect::<String>();
            format!(
                "<section class=\"panel\"><h3>{}</h3><p>ID: <code>{}</code> · default: {} · version: {} · assigned users: {} · exact model rules: {}</p><form method=\"post\" action=\"/admin/profiles/update\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><input type=\"hidden\" name=\"version\" value=\"{}\"><label>Name <input name=\"name\" value=\"{}\" maxlength=\"64\" required></label><label>Default <select name=\"default_effect\"><option value=\"allow\"{}>Allow</option><option value=\"deny\"{}>Deny</option></select></label><button type=\"submit\">Save profile</button></form><form method=\"post\" action=\"/admin/profiles/rule\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><label>Exact model ID <input name=\"model_id\" maxlength=\"512\" required></label><label>Effect <select name=\"effect\"><option>allow</option><option>deny</option></select></label><button type=\"submit\">Add or update rule</button></form><ul>{profile_rules}</ul><form method=\"post\" action=\"/admin/profiles/prefer\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><label>Preferred model ID <input name=\"model_id\" maxlength=\"512\" value=\"{}\"></label><button type=\"submit\">Save recommendation</button></form><form method=\"post\" action=\"/admin/profiles/delete\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><input type=\"hidden\" name=\"version\" value=\"{}\"><button type=\"submit\">Delete unassigned profile</button></form></section>",
                html_escape(&profile.name), html_escape(&profile.id), html_escape(&profile.default_effect),
                profile.version, profile.assigned_users, profile.model_rules,
                html_escape(csrf), html_escape(&profile.id), profile.version, html_escape(&profile.name),
                if profile.default_effect == "allow" { " selected" } else { "" },
                if profile.default_effect == "deny" { " selected" } else { "" },
                html_escape(csrf), html_escape(&profile.id),
                html_escape(csrf), html_escape(&profile.id), html_escape(profile.preferred_model_id.as_deref().unwrap_or("")),
                html_escape(csrf), html_escape(&profile.id), profile.version,
            )
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h2>Profiles</h2><p>Profiles control model access and client recommendations. Provider topology and upstream secrets remain in operator-owned configuration.</p><form method=\"post\" action=\"/admin/profiles/create\"><label>Profile ID <input name=\"profile_id\" maxlength=\"64\" required></label><label>Name <input name=\"name\" maxlength=\"64\" required></label><label>Default <select name=\"default_effect\"><option value=\"deny\">Deny unless allowed</option><option value=\"allow\">Allow unless denied</option></select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Create profile</button></form></section>{rows}",
        html_escape(csrf)
    )
}

/// Standalone document shell for on-prem pages that are not rendered through
/// the shared dashboard layout. The usage pages are role-specific reads rather
/// than dashboard panels, so without a shell they would ship a bare `<section>`
/// fragment: no stylesheet, no viewport declaration, and tables with no way to
/// scroll on a narrow screen.
fn on_prem_page(title: &str, body: &str) -> String {
    format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{}</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><main>{}</main></html>",
        html_escape(title),
        body
    )
}

/// Wrap one table in the shared keyboard-reachable scroll region.
fn scroll_table(label: &str, table: &str) -> String {
    format!(
        "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"{}\">{table}</div>",
        html_escape(label)
    )
}

pub(in crate::server::dashboard) fn render_usage_admin(
    rows: &[(String, String, i64, i64, i64)],
    csrf: &str,
) -> String {
    let content = rows.iter().map(|(user, model, requests, tokens, cost)| {
        format!("<tr><td>{}</td><td>{}</td><td>{requests}</td><td>{tokens}</td><td>{cost}</td></tr>", html_escape(user), html_escape(model))
    }).collect::<String>();
    let limits = scroll_table(
        "Usage by user and model",
        &format!(
            "<table><thead><tr><th>User</th><th>Model</th><th>Requests</th><th>Tokens</th><th>Estimated microdollars</th></tr></thead><tbody>{content}</tbody></table>"
        ),
    );
    on_prem_page(
        "Usage",
        &format!(
            "<section class=\"panel\"><h2>Usage</h2><p>Token and microdollar figures are usage estimates; estimated USD is not an invoice or a hard ceiling for unpriced models.</p>{limits}<form method=\"get\" action=\"/admin/audit\"><button type=\"submit\">View admin audit</button></form><input type=\"hidden\" value=\"{}\"></section>",
            html_escape(csrf)
        ),
    )
}

pub(in crate::server::dashboard) fn render_my_usage(
    user: &str,
    rules: &[crate::user_budget::UserBudgetRule],
    rows: &[crate::user_budget::UserBudgetUsage],
) -> String {
    let rule_rows = rules
        .iter()
        .map(|rule| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(rule.model_id.as_deref().unwrap_or("All models")),
                html_escape(rule.window.as_str()),
                rule.max_requests
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                rule.max_tokens
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                rule.max_microdollars
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                rule.max_concurrent
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
            )
        })
        .collect::<String>();
    let usage_rows = rows
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.model_id),
                html_escape(&row.window_start),
                row.request_count,
                row.token_count,
                row.cost_microdollars
            )
        })
        .collect::<String>();
    let limit_rows = scroll_table(
        "Configured usage limits",
        &format!(
            "<table><thead><tr><th>Model</th><th>Window</th><th>Max requests (hard)</th><th>Max tokens (estimated)</th><th>Max microdollars (estimated)</th><th>Max concurrent (hard)</th></tr></thead><tbody>{rule_rows}</tbody></table>"
        ),
    );
    let usage_rows = scroll_table(
        "Settled usage",
        &format!(
            "<table><thead><tr><th>Model</th><th>Window start (UTC)</th><th>Requests</th><th>Tokens</th><th>Estimated microdollars</th></tr></thead><tbody>{usage_rows}</tbody></table>"
        ),
    );
    on_prem_page(
        "My usage",
        &format!(
            "<section class=\"panel\"><h1>My usage</h1><p>Account: {}</p><p>Daily/monthly periods use UTC. Request and concurrency limits are hard admission caps. Token and microdollar values are estimates; estimated USD is not an invoice or a guaranteed cap for unpriced models. Active requests hold budget before dispatch and may reduce admission headroom.</p><h2>Configured limits</h2>{limit_rows}<h2>Settled usage</h2>{usage_rows}<p><a href=\"/my-account\">My account</a></p></section>",
            html_escape(user)
        ),
    )
}
