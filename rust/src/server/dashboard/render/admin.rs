use super::*;

pub(in crate::server::dashboard) struct UserAdminFacts {
    pub profile_id: Option<String>,
    pub overrides: Vec<(String, String)>,
    pub rules: Vec<crate::user_budget::UserBudgetRule>,
    pub credentials: Vec<crate::identity::CredentialSummary>,
    /// Stored self-service effect: `allow`, `deny`, or `inherit`.
    pub upstream_grant: String,
    /// Provider, account name, and binding state. No secret and no env var.
    pub upstreams: Vec<(String, String, String)>,
}

pub(in crate::server::dashboard) fn render_users_admin(
    users: &[crate::identity::UserSummary],
    profiles: &[crate::model_policy::PolicyProfileSummary],
    instance_rules: &[crate::instance_budget::InstanceBudgetRule],
    facts: &std::collections::BTreeMap<String, UserAdminFacts>,
    csrf: &str,
    instance_grant: &str,
) -> String {
    let default_profile = profiles
        .iter()
        .find(|profile| profile.is_default_member_profile);
    let initial_profile = default_profile.map_or_else(
        || "<option value=\"\">No instance default (deny all)</option>".to_owned(),
        |profile| {
            format!(
                "<option value=\"\">Use instance default: {}</option>",
                html_escape(&profile.name)
            )
        },
    );
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
            let Some(facts) = facts.get(&user.id) else {
                return format!(
                    "<tr><th scope=\"row\">{}</th><td>{}</td><td>{}</td><td>{}</td><td>Roster details could not be loaded.</td></tr>",
                    html_escape(&user.username),
                    html_escape(&user.role),
                    html_escape(&user.state),
                    if user.must_change_password {
                        "Must change password"
                    } else {
                        "—"
                    },
                );
            };
            let options = profiles
                .iter()
                .map(|profile| {
                    let selected = if facts.profile_id.as_deref() == Some(profile.id.as_str()) {
                        " selected"
                    } else {
                        ""
                    };
                    format!(
                        "<option value=\"{}\"{selected}>{}</option>",
                        html_escape(&profile.id),
                        html_escape(&profile.name)
                    )
                })
                .collect::<String>();
            let profile_status = match facts.profile_id.as_deref() {
                Some(profile_id) => {
                    let name = profiles
                        .iter()
                        .find(|profile| profile.id == profile_id)
                        .map_or(profile_id, |profile| profile.name.as_str());
                    format!("Assigned profile: {}", html_escape(name))
                }
                None if user.role == "member" => {
                    "Unassigned. Models are denied until a profile is assigned.".to_owned()
                }
                None => "No profile assignment.".to_owned(),
            };
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
                "<details><summary>Set budget</summary><form method=\"post\" action=\"/admin/users/budget\"><label>Exact model (blank for all) <input name=\"model_id\" maxlength=\"512\"></label><label>Window <select name=\"window\"><option value=\"day\">UTC day</option><option value=\"month\">UTC month</option></select></label><label>Mode <select name=\"enforcement\"><option value=\"estimated\">Estimated (approximate hold)</option><option value=\"hard\">Hard (prove worst-case bound or deny)</option></select></label><label>Max requests <input type=\"number\" min=\"1\" name=\"max_requests\"></label><label>Max tokens <input type=\"number\" min=\"1\" name=\"max_tokens\"></label><label>Max microdollars (integer microdollars) <input type=\"number\" min=\"1\" name=\"max_microdollars\"></label><label>Max concurrent <input type=\"number\" min=\"1\" name=\"max_concurrent\"></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Save budget</button></form></details><p>Hard token rules admit only requests with an explicit caller output cap and reserve a worst-case bound; hard USD rules additionally require an operator tariff in provider configuration. Estimated rules use approximate holds and are never a hard ceiling. The stored unit is microdollars.</p>",
                html_escape(csrf), html_escape(&user.id)
            );
            let override_list = if facts.overrides.is_empty() {
                "<p>No overrides.</p>".to_owned()
            } else {
                let items = facts
                    .overrides
                    .iter()
                    .map(|(model, effect)| {
                        format!(
                            "<li>{} {}</li>",
                            html_escape(model),
                            html_escape(effect)
                        )
                    })
                    .collect::<String>();
                format!("<ul>{items}</ul>")
            };
            let budget_list = if facts.rules.is_empty() {
                "<p>No budget rules.</p>".to_owned()
            } else {
                let items = facts
                    .rules
                    .iter()
                    .map(|rule| {
                        format!(
                            "<li>{} {} requests {} tokens {} concurrent {}</li>",
                            html_escape(rule.model_id.as_deref().unwrap_or("All models")),
                            html_escape(rule.window.as_str()),
                            optional_count(rule.max_requests),
                            optional_money(rule.max_microdollars),
                            optional_count(rule.max_concurrent)
                        )
                    })
                    .collect::<String>();
                format!("<ul>{items}</ul>")
            };
            let credential_list = if facts.credentials.is_empty() {
                "<p>No personal credentials.</p>".to_owned()
            } else {
                let items = facts
                    .credentials
                    .iter()
                    .map(|credential| {
                        format!(
                            "<li>{} created {} last used {}</li>",
                            html_escape(&credential.label),
                            html_escape(&credential.created_at),
                            html_escape(credential.last_used_at.as_deref().unwrap_or("never"))
                        )
                    })
                    .collect::<String>();
                format!("<ul>{items}</ul>")
            };
            let token_form = format!(
                "<form method=\"post\" action=\"/admin/users/token\"><label>One-time token label <input name=\"label\" maxlength=\"80\" required></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Issue token</button></form><form method=\"post\" action=\"/admin/users/revoke-tokens\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Revoke all tokens</button></form>",
                html_escape(csrf), html_escape(&user.id), html_escape(csrf), html_escape(&user.id)
            );
            let upstream_rows = if facts.upstreams.is_empty() {
                "<p>No personal upstream accounts.</p>".to_owned()
            } else {
                facts
                    .upstreams
                    .iter()
                    .map(|(provider, account, state)| {
                        format!(
                            "<li>{} {} {} <form method=\"post\" action=\"/admin/users/upstream/remove\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><input type=\"hidden\" name=\"provider\" value=\"{}\"><input type=\"hidden\" name=\"name\" value=\"{}\"><button type=\"submit\">Remove</button></form></li>",
                            html_escape(provider),
                            html_escape(account),
                            html_escape(state),
                            html_escape(csrf),
                            html_escape(&user.id),
                            html_escape(provider),
                            html_escape(account)
                        )
                    })
                    .collect::<String>()
            };
            let grant = facts.upstream_grant.as_str();
            let grant_form = format!(
                "<form method=\"post\" action=\"/admin/users/upstream/grant\"><label>Self-service <select name=\"effect\" aria-label=\"Personal upstream grant for {}\"><option value=\"inherit\"{}>Inherit instance default</option><option value=\"allow\"{}>Allow</option><option value=\"deny\"{}>Deny</option></select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"user_id\" value=\"{}\"><button type=\"submit\">Save grant</button></form>",
                html_escape(&user.username),
                if grant == "inherit" { " selected" } else { "" },
                if grant == "allow" { " selected" } else { "" },
                if grant == "deny" { " selected" } else { "" },
                html_escape(csrf),
                html_escape(&user.id)
            );
            let actions = format!(
                "<section><h3>Profile</h3><p>{profile_status}</p>{profile_form}<h3>Overrides</h3>{override_list}{override_form}{remove_override_form}<h3>Budget rules</h3>{budget_list}{budget_form}<h3>Credentials</h3>{credential_list}{token_form}<h3>Personal upstream</h3><ul>{upstream_rows}</ul>{grant_form}<p>Changing the grant does not remove an account. Adding an account for this user stays a command-line action.</p><h3>Account</h3>{state_form}{password_form}</section>"
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
        "<section class=\"panel\"><h2>Instance budget ceilings</h2><p>One instance-wide ceiling intersected with every per-user rule: an admitted call must satisfy all applicable instance and user limits. Absent rules mean no additional cap. Members never see other users' usage here. Hard token rules need an explicit caller output cap; hard USD rules need an operator tariff in provider configuration.</p><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Instance budget rules\">{DASHBOARD_TABLE_OPEN}<thead><tr><th>Model</th><th>Window</th><th>Mode</th><th>Max requests</th><th>Max tokens</th><th>Max microdollars</th><th>Max concurrent</th><th>Actions</th></tr></thead><tbody>{instance_rows}</tbody></table></div><details><summary>Set instance ceiling</summary><form method=\"post\" action=\"/admin/budget/instance\"><label>Exact model (blank for all) <input name=\"model_id\" maxlength=\"512\"></label><label>Window <select name=\"window\"><option value=\"day\">UTC day</option><option value=\"month\">UTC month</option></select></label><label>Mode <select name=\"enforcement\"><option value=\"estimated\">Estimated (approximate hold)</option><option value=\"hard\">Hard (prove worst-case bound or deny)</option></select></label><label>Max requests <input type=\"number\" min=\"1\" name=\"max_requests\"></label><label>Max tokens <input type=\"number\" min=\"1\" name=\"max_tokens\"></label><label>Max microdollars <input type=\"number\" min=\"1\" name=\"max_microdollars\"></label><label>Max concurrent <input type=\"number\" min=\"1\" name=\"max_concurrent\"></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Save instance ceiling</button></form></details></section>",
        html_escape(csrf)
    );
    let grant_section = format!(
        "<section class=\"panel\"><h2>Personal upstream self-service</h2><p>The instance default is deny until an administrator changes it. Allow lets that member submit one bundled-provider key from My account. Deny does not withdraw an account that is already bound.</p><form method=\"post\" action=\"/admin/users/upstream/default\"><label>Instance default <select name=\"effect\"><option value=\"deny\"{}>Deny</option><option value=\"allow\"{}>Allow</option></select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Save default</button></form></section>",
        if instance_grant == "deny" {
            " selected"
        } else {
            ""
        },
        if instance_grant == "allow" {
            " selected"
        } else {
            ""
        },
        html_escape(csrf)
    );
    format!(
        "{instance_section}{grant_section}<section class=\"panel\"><h2>Users</h2><p>Create local member accounts and assign policy profiles. The instance default applies only to new members; existing assignments are unchanged. There is no default budget template: user and instance budget rules stay independent. Temporary passwords are shown only to the administrator who enters them; users must change them at first login.</p><form method=\"post\" action=\"/admin/users/create\"><label>Username <input name=\"username\" minlength=\"1\" maxlength=\"64\" required autocomplete=\"off\"></label><label>Temporary password <input type=\"password\" name=\"password\" minlength=\"12\" maxlength=\"1024\" required autocomplete=\"new-password\"></label><label>Initial profile <select name=\"profile_id\">{initial_profile}<option value=\"__none\">No profile (deny all)</option>{options}</select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Add member</button></form></section><section class=\"panel\"><h2>Local accounts</h2><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Local users\">{DASHBOARD_TABLE_OPEN}<thead><tr><th>Username</th><th>Role</th><th>State</th><th>Password</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
        html_escape(csrf)
    )
}

pub(in crate::server::dashboard) fn render_member_upstream(
    allowed: bool,
    rows: &[(String, String, String)],
    providers: &[String],
    csrf: &str,
) -> String {
    let listed = if rows.is_empty() {
        "<p>No personal upstream accounts.</p>".to_owned()
    } else {
        let items = rows
            .iter()
            .map(|(provider, account, state)| {
                let remove = if allowed {
                    format!(
                        " <form method=\"post\" action=\"/my-account/upstreams/remove\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"provider\" value=\"{}\"><input type=\"hidden\" name=\"name\" value=\"{}\"><button type=\"submit\">Remove</button></form>",
                        html_escape(csrf),
                        html_escape(provider),
                        html_escape(account)
                    )
                } else {
                    String::new()
                };
                format!(
                    "<li>{} {} {}{remove}</li>",
                    html_escape(provider),
                    html_escape(account),
                    html_escape(state)
                )
            })
            .collect::<String>();
        format!("<ul>{items}</ul>")
    };
    if !allowed {
        return format!(
            "<section><h2>Personal upstream account</h2><p>Personal upstream self-service is not granted. The instance operator can add an account from the command line. Changing a grant does not remove an account that is already bound.</p>{listed}</section>"
        );
    }
    let options = providers
        .iter()
        .map(|provider| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(provider),
                html_escape(provider)
            )
        })
        .collect::<String>();
    format!(
        "<section><h2>Personal upstream account</h2><p>The key is written to the operator environment file and is not stored in the database. The account is selectable only by you.</p><form method=\"post\" action=\"/my-account/upstreams\"><label>Provider <select name=\"provider\" required>{options}</select></label><label>Account name <input name=\"name\" minlength=\"1\" maxlength=\"96\" required autocomplete=\"off\"></label><label>API key <input type=\"password\" name=\"secret\" maxlength=\"4096\" required autocomplete=\"off\"></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Add personal account</button></form>{listed}</section>",
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
            let default_control = if profile.is_default_member_profile {
                format!("<p>Default for newly created members</p><form method=\"post\" action=\"/admin/profiles/default\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"\"><button type=\"submit\">Clear default for future members</button></form>", html_escape(csrf))
            } else {
                format!("<form method=\"post\" action=\"/admin/profiles/default\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><button type=\"submit\">Set as default for new members</button></form>", html_escape(csrf), html_escape(&profile.id))
            };
            format!(
                "<section class=\"panel\"><h3>{}</h3><p>ID: <code>{}</code> · default: {} · version: {} · assigned users: {} · exact model rules: {}</p>{default_control}<form method=\"post\" action=\"/admin/profiles/update\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><input type=\"hidden\" name=\"version\" value=\"{}\"><label>Name <input name=\"name\" value=\"{}\" maxlength=\"64\" required></label><label>Default <select name=\"default_effect\"><option value=\"allow\"{}>Allow</option><option value=\"deny\"{}>Deny</option></select></label><button type=\"submit\">Save profile</button></form><form method=\"post\" action=\"/admin/profiles/rule\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><label>Exact model ID <input name=\"model_id\" maxlength=\"512\" required></label><label>Effect <select name=\"effect\"><option>allow</option><option>deny</option></select></label><button type=\"submit\">Add or update rule</button></form><ul>{profile_rules}</ul><form method=\"post\" action=\"/admin/profiles/prefer\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><label>Preferred model ID <input name=\"model_id\" maxlength=\"512\" value=\"{}\"></label><button type=\"submit\">Save recommendation</button></form><form method=\"post\" action=\"/admin/profiles/delete\"><input type=\"hidden\" name=\"csrf\" value=\"{}\"><input type=\"hidden\" name=\"profile_id\" value=\"{}\"><input type=\"hidden\" name=\"version\" value=\"{}\"><button type=\"submit\">Delete unassigned profile</button></form></section>",
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
        "<section class=\"panel\"><h2>Profiles</h2><p>Profiles control model access and client recommendations. The default member profile affects future members only. Budget rules remain separately configured per user and instance; no budget template is copied. Provider topology and upstream secrets remain in operator-owned configuration.</p><form method=\"post\" action=\"/admin/profiles/create\"><label>Profile ID <input name=\"profile_id\" maxlength=\"64\" required></label><label>Name <input name=\"name\" maxlength=\"64\" required></label><label>Default <select name=\"default_effect\"><option value=\"deny\">Deny unless allowed</option><option value=\"allow\">Allow unless denied</option></select></label><input type=\"hidden\" name=\"csrf\" value=\"{}\"><button type=\"submit\">Create profile</button></form></section>{rows}",
        html_escape(csrf)
    )
}

/// The row cap the usage queries apply.
///
/// The disclosure on the page is derived from this same number, so the page
/// cannot claim a complete table when the query truncated it.
pub(in crate::server::dashboard) const USAGE_ROW_CAP: usize = 500;

/// The disclosure a truncated usage table carries.
pub(in crate::server::dashboard) fn usage_truncation_notice(row_count: usize) -> String {
    if row_count < USAGE_ROW_CAP {
        return String::new();
    }
    format!(
        "<p class=\"sub\" role=\"status\">Showing the first {USAGE_ROW_CAP} rows returned by the usage query. The usage table is truncated.</p>"
    )
}

/// USD text for a stored microdollar integer. The stored unit does not change.
pub(in crate::server::dashboard) fn format_microdollar_usd(microdollars: i64) -> String {
    let negative = microdollars < 0;
    let value = microdollars.unsigned_abs();
    let whole = value / 1_000_000;
    let fraction = value % 1_000_000;
    let mut text = if fraction == 0 {
        whole.to_string()
    } else {
        let digits = format!("{fraction:06}");
        format!("{whole}.{}", digits.trim_end_matches('0'))
    };
    if negative {
        text.insert(0, '-');
    }
    format!("{text} USD")
}

fn money_cell(microdollars: i64) -> String {
    format!("{microdollars} ({})", format_microdollar_usd(microdollars))
}

fn optional_count(value: Option<i64>) -> String {
    value.map_or_else(|| "—".to_owned(), |value| value.to_string())
}

fn optional_money(value: Option<i64>) -> String {
    value.map_or_else(|| "—".to_owned(), money_cell)
}

/// Wrap one table in the shared keyboard-reachable scroll region.
fn scroll_table(label: &str, table: &str) -> String {
    format!(
        "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"{}\">{table}</div>",
        html_escape(label)
    )
}

pub(in crate::server::dashboard) fn render_usage_admin(
    snapshot: &crate::user_budget::AdminUsageSnapshot,
    csrf: &str,
    theme: &str,
) -> String {
    let current_day = usage_window_table(
        "Current UTC day",
        snapshot
            .rows
            .iter()
            .filter(|row| {
                row.window == crate::user_budget::BudgetWindow::Day
                    && row.window_start == snapshot.day_start
            })
            .collect(),
    );
    let current_month = usage_window_table(
        "Current UTC month",
        snapshot
            .rows
            .iter()
            .filter(|row| {
                row.window == crate::user_budget::BudgetWindow::Month
                    && row.window_start == snapshot.month_start
            })
            .collect(),
    );
    let historical = usage_window_table(
        "Earlier windows",
        snapshot
            .rows
            .iter()
            .filter(|row| {
                !((row.window == crate::user_budget::BudgetWindow::Day
                    && row.window_start == snapshot.day_start)
                    || (row.window == crate::user_budget::BudgetWindow::Month
                        && row.window_start == snapshot.month_start))
            })
            .collect(),
    );
    let truncation = usage_truncation_notice(snapshot.rows.len());
    dashboard_standalone_page(
        "Usage",
        "usage",
        theme,
        &format!(
            "<section class=\"panel\"><h2>Usage</h2><p>Token and microdollar figures are usage estimates; estimated USD is not an invoice or a hard ceiling for unpriced models. Day and month totals are separate windows and are not added together.</p>{truncation}<h2>Current UTC day</h2>{current_day}<h2>Current UTC month</h2>{current_month}<h2>Earlier windows</h2>{historical}<form method=\"get\" action=\"/admin/audit\"><button type=\"submit\">View admin audit</button></form><input type=\"hidden\" value=\"{}\"></section>",
            html_escape(csrf)
        ),
    )
}

fn usage_window_table(label: &str, rows: Vec<&crate::user_budget::AdminUsageWindowRow>) -> String {
    let content = rows
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.username),
                html_escape(&row.model_id),
                html_escape(row.window.as_str()),
                html_escape(&row.window_start),
                row.request_count,
                row.token_count,
                money_cell(row.cost_microdollars)
            )
        })
        .collect::<String>();
    scroll_table(
        label,
        &format!(
            "{DASHBOARD_TABLE_OPEN}<thead><tr><th>User</th><th>Model</th><th>Window</th><th>Window start (UTC)</th><th>Requests</th><th>Tokens</th><th>Estimated microdollars</th></tr></thead><tbody>{content}</tbody></table>"
        ),
    )
}

pub(in crate::server::dashboard) fn render_my_usage(
    user: &str,
    rules: &[crate::user_budget::UserBudgetRule],
    rows: &[crate::user_budget::UserBudgetUsage],
    allowance: &[crate::user_budget::UserBudgetAllowance],
    instance_rules: &[crate::instance_budget::InstanceBudgetRule],
    theme: &str,
) -> String {
    let rule_rows = rules
        .iter()
        .map(|rule| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(rule.model_id.as_deref().unwrap_or("All models")),
                html_escape(rule.window.as_str()),
                html_escape(rule.enforcement.as_str()),
                optional_count(rule.max_requests),
                optional_count(rule.max_tokens),
                optional_money(rule.max_microdollars),
                optional_count(rule.max_concurrent),
            )
        })
        .collect::<String>();
    let usage_rows = rows
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&row.model_id),
                html_escape(row.window.as_str()),
                html_escape(&row.window_start),
                row.request_count,
                row.token_count,
                money_cell(row.cost_microdollars)
            )
        })
        .collect::<String>();
    let limit_table = scroll_table(
        "Configured usage limits",
        &format!(
            "{DASHBOARD_TABLE_OPEN}<thead><tr><th>Model</th><th>Window</th><th>Mode</th><th>Max requests</th><th>Max tokens</th><th>Max microdollars</th><th>Max concurrent</th></tr></thead><tbody>{rule_rows}</tbody></table>"
        ),
    );
    let usage_table = scroll_table(
        "Settled usage",
        &format!(
            "{DASHBOARD_TABLE_OPEN}<thead><tr><th>Model</th><th>Window</th><th>Window start (UTC)</th><th>Requests</th><th>Tokens</th><th>Estimated microdollars</th></tr></thead><tbody>{usage_rows}</tbody></table>"
        ),
    );
    let remaining_rows = allowance
        .iter()
        .map(|row| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(row.model_id.as_deref().unwrap_or("All models")),
                html_escape(row.window.as_str()),
                html_escape(&row.window_start),
                optional_count(row.requests.ceiling),
                optional_count(row.requests.remaining),
                optional_count(row.tokens.ceiling),
                optional_count(row.tokens.remaining),
                optional_money(row.microdollars.ceiling),
                optional_money(row.microdollars.remaining),
                optional_count(row.concurrent.ceiling),
                optional_count(row.concurrent.remaining),
            )
        })
        .collect::<String>();
    let remaining_table = scroll_table(
        "Remaining allowance",
        &format!(
            "{DASHBOARD_TABLE_OPEN}<thead><tr><th>Model</th><th>Window</th><th>Window start (UTC)</th><th>Request ceiling</th><th>Requests remaining</th><th>Token ceiling</th><th>Tokens remaining</th><th>Microdollar ceiling</th><th>Microdollars remaining</th><th>Concurrent ceiling</th><th>Concurrent remaining</th></tr></thead><tbody>{remaining_rows}</tbody></table>"
        ),
    );
    let instance_rows = instance_rules
        .iter()
        .map(|rule| {
            format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(rule.model_id.as_deref().unwrap_or("All models")),
                html_escape(rule.window.as_str()),
                html_escape(rule.enforcement.as_str()),
                optional_count(rule.max_requests),
                optional_count(rule.max_tokens),
                optional_money(rule.max_microdollars),
                optional_count(rule.max_concurrent),
            )
        })
        .collect::<String>();
    let instance_table = scroll_table(
        "Instance ceilings",
        &format!(
            "{DASHBOARD_TABLE_OPEN}<thead><tr><th>Model</th><th>Window</th><th>Mode</th><th>Max requests</th><th>Max tokens</th><th>Max microdollars</th><th>Max concurrent</th></tr></thead><tbody>{instance_rows}</tbody></table>"
        ),
    );
    let truncation = usage_truncation_notice(rows.len());
    member_standalone_page(
        "My usage",
        "usage",
        theme,
        &format!(
            "<section class=\"panel\"><h1>My usage</h1><p>Account: {}</p><p>Daily/monthly periods use UTC. Rules marked <code>hard</code> enforce a provable worst-case bound (token rules need an explicit caller output cap; USD rules need an operator tariff) and deny otherwise; rules marked <code>estimated</code> use approximate holds and are not a guaranteed cap. Active requests hold budget before dispatch and may reduce admission headroom. Remaining allowance is the ceiling minus settled usage minus active holds for that rule. A dimension without a ceiling has no remaining figure.</p><h2>Configured limits</h2>{limit_table}<h2>Remaining allowance</h2>{remaining_table}<h2>Instance ceilings</h2><p>The shared ceiling also applies. Other people's consumption is not shown.</p>{instance_table}<h2>Settled usage</h2>{truncation}{usage_table}<p><a href=\"/my-account\">My account</a></p></section>",
            html_escape(user)
        ),
    )
}
