use super::*;

pub(in crate::server::dashboard) fn render_users_admin(
    users: &[crate::identity::UserSummary],
    profiles: &[crate::model_policy::PolicyProfileSummary],
    instance_rules: &[crate::instance_budget::InstanceBudgetRule],
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
                "<details><summary>Set budget</summary><form method=\"post\" action=\"/admin/users/budget\"><label>Exact model (blank for all) <input name=\"model_id\" maxlength=\"512\"></label><label>Window <select name=\"window\"><option value=\"day\">UTC day</option><option value=\"month\">UTC month</option></select></label><label>Mode <select name=\"enforcement\"><option value=\"estimated\">Estimated (approximate hold)</option><option value=\"hard\">Hard (prove worst-case bound or deny)</option></select></label><label>Max requests <input type=\"number\" min=\"1\" name=\"max_requests\"></label><label>Max tokens <input type=\"number\" min=\"1\" name=\"max_tokens\"></label><label>Max microdollars <input type=\"number\" min=\"1\" name=\"max_microdollars\"></label><label>Max concurrent <input type=\"number\" min=\"1\" name=\"max_concurrent\"></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Save budget</button></form></details><p>Hard token rules admit only requests with an explicit caller output cap and reserve a worst-case bound; hard USD rules additionally require an operator tariff in provider configuration. Estimated rules use approximate holds and are never a hard ceiling.</p>",
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
    let instance_rows = instance_rules
        .iter()
        .map(|rule| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><form method=\"post\" action=\"/admin/budget/instance/remove\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"model_id\" value=\"{}\"><input type=\"hidden\" name=\"window\" value=\"{}\"><input type=\"hidden\" name=\"max_requests\" value=\"\"><input type=\"hidden\" name=\"max_tokens\" value=\"\"><input type=\"hidden\" name=\"max_microdollars\" value=\"\"><input type=\"hidden\" name=\"max_concurrent\" value=\"\"><button type=\"submit\">Remove</button></form></td></tr>",
                html_escape(rule.model_id.as_deref().unwrap_or("All models")),
                html_escape(rule.window.as_str()),
                html_escape(rule.enforcement.as_str()),
                rule.max_requests
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                rule.max_tokens
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                rule.max_microdollars
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                rule.max_concurrent
                    .map_or_else(|| "—".to_owned(), |value| value.to_string()),
                html_escape(csrf),
                html_escape(rule.model_id.as_deref().unwrap_or("")),
                html_escape(rule.window.as_str()),
            )
        })
        .collect::<String>();
    let instance_section = format!(
        "<section class=\"panel\"><h2>Instance budget ceilings</h2><p>One instance-wide ceiling intersected with every per-user rule: an admitted call must satisfy all applicable instance and user limits. Absent rules mean no additional cap. Members never see other users' usage here. Hard token rules need an explicit caller output cap; hard USD rules need an operator tariff in provider configuration.</p><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Instance budget rules\"><table><thead><tr><th>Model</th><th>Window</th><th>Mode</th><th>Max requests</th><th>Max tokens</th><th>Max microdollars</th><th>Max concurrent</th><th>Actions</th></tr></thead><tbody>{instance_rows}</tbody></table></div><details><summary>Set instance ceiling</summary><form method=\"post\" action=\"/admin/budget/instance\"><label>Exact model (blank for all) <input name=\"model_id\" maxlength=\"512\"></label><label>Window <select name=\"window\"><option value=\"day\">UTC day</option><option value=\"month\">UTC month</option></select></label><label>Mode <select name=\"enforcement\"><option value=\"estimated\">Estimated (approximate hold)</option><option value=\"hard\">Hard (prove worst-case bound or deny)</option></select></label><label>Max requests <input type=\"number\" min=\"1\" name=\"max_requests\"></label><label>Max tokens <input type=\"number\" min=\"1\" name=\"max_tokens\"></label><label>Max microdollars <input type=\"number\" min=\"1\" name=\"max_microdollars\"></label><label>Max concurrent <input type=\"number\" min=\"1\" name=\"max_concurrent\"></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Save instance ceiling</button></form></details></section>",
        html_escape(csrf)
    );
    format!(
        "{instance_section}<section class=\"panel\"><h2>Users</h2><p>Create local member accounts and assign policy profiles. Temporary passwords are shown only to the administrator who enters them; users must change them at first login.</p><form method=\"post\" action=\"/admin/users/create\"><label>Username <input name=\"username\" minlength=\"1\" maxlength=\"64\" required autocomplete=\"off\"></label><label>Temporary password <input type=\"password\" name=\"password\" minlength=\"12\" maxlength=\"1024\" required autocomplete=\"new-password\"></label><label>Initial profile <select name=\"profile_id\"><option value=\"\">No profile</option>{options}</select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Add member</button></form></section><section class=\"panel\"><h2>Local accounts</h2><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Local users\"><table><thead><tr><th>Username</th><th>Role</th><th>State</th><th>Password</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
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

pub(in crate::server::dashboard) fn render_usage_admin(
    rows: &[(String, String, i64, i64, i64)],
    csrf: &str,
) -> String {
    let content = rows.iter().map(|(user, model, requests, tokens, cost)| {
        format!("<tr><td>{}</td><td>{}</td><td>{requests}</td><td>{tokens}</td><td>{cost}</td></tr>", html_escape(user), html_escape(model))
    }).collect::<String>();
    format!(
        "<section class=\"panel\"><h2>Usage</h2><p>Token and microdollar figures are usage estimates; estimated USD is not an invoice or a hard ceiling for unpriced models.</p><table><thead><tr><th>User</th><th>Model</th><th>Requests</th><th>Tokens</th><th>Estimated microdollars</th></tr></thead><tbody>{content}</tbody></table><form method=\"get\" action=\"/admin/audit\"><button type=\"submit\">View admin audit</button></form><input type=\"hidden\" value=\"{}\"></section>",
        html_escape(csrf)
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
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(rule.model_id.as_deref().unwrap_or("All models")),
                html_escape(rule.window.as_str()),
                html_escape(rule.enforcement.as_str()),
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
    format!(
        "<section class=\"panel\"><h1>My usage</h1><p>Account: {}</p><p>Daily/monthly periods use UTC. Rules marked <code>hard</code> enforce a provable worst-case bound (token rules need an explicit caller output cap; USD rules need an operator tariff) and deny otherwise; rules marked <code>estimated</code> use approximate holds and are not a guaranteed cap. Active requests hold budget before dispatch and may reduce admission headroom.</p><h2>Configured limits</h2><table><thead><tr><th>Model</th><th>Window</th><th>Mode</th><th>Max requests</th><th>Max tokens</th><th>Max microdollars</th><th>Max concurrent</th></tr></thead><tbody>{rule_rows}</tbody></table><h2>Settled usage</h2><table><thead><tr><th>Model</th><th>Window start (UTC)</th><th>Requests</th><th>Tokens</th><th>Estimated microdollars</th></tr></thead><tbody>{usage_rows}</tbody></table><p><a href=\"/my-account\">My account</a></p></section>",
        html_escape(user)
    )
}
