use super::*;

pub(in crate::server::dashboard) fn render_accounts_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    show_disabled: bool,
    health_snapshots: &[crate::health::AccountHealthSnapshot],
) -> String {
    let detail_headers = [
        "Reserved",
        "Resv.",
        "5h rate",
        "7d rate",
        "30d rate",
        "BW received",
        "BW emitted",
        "Over budget",
        "Upstream backoff",
        "Backoff until",
        "Failures",
        "Auth fail",
        "Disabled",
        "Est. cost",
        "Cache R",
        "Cache W",
        "Reasoning",
        "Avg cost/req",
        "Avg cost/1k tok",
    ]
    .iter()
    .map(|label| format!("<th data-priority=\"3\">{label}</th>"))
    .collect::<String>();
    let disabled_count = data
        .accounts
        .iter()
        .filter(|account| !account.enabled)
        .count();
    if data
        .accounts
        .iter()
        .all(|account| !show_disabled && !account.enabled)
    {
        let empty_message = if disabled_count > 0 {
            format!(
                "No enabled accounts. {disabled_count} disabled account{} hidden — <a href=\"?show_disabled=1\">show them</a>.",
                if disabled_count == 1 { "" } else { "s" }
            )
        } else {
            "No accounts configured.".to_owned()
        };
        return format!(
            "<h2>Accounts</h2><form method=\"get\" class=\"period-selector account-filters\" data-period-selector aria-label=\"Account filters\"><label for=\"period\">Period: </label><select id=\"period\" name=\"period\" data-auto-submit=\"1\">{}</select><label for=\"show_disabled\">Disabled: </label><select id=\"show_disabled\" name=\"show_disabled\" data-auto-submit=\"1\"><option value=\"0\"{}>Hide disabled accounts</option><option value=\"1\"{}>Show disabled accounts</option></select><input type=\"hidden\" name=\"theme\" value=\"{}\"></form><section class=\"panel\"><p class=\"empty\">{empty_message}</p></section>",
            period_options(period),
            if show_disabled {
                ""
            } else {
                " selected=\"selected\""
            },
            if show_disabled {
                " selected=\"selected\""
            } else {
                ""
            },
            html_escape(theme)
        );
    }
    let rows = data
        .accounts
        .iter()
        .filter(|row| show_disabled || row.enabled)
        .map(|row| {
            let live_health = health_snapshots
                .iter()
                .find(|snapshot| snapshot.account_name == row.name);
            let health_state = live_health
                .map(|snapshot| snapshot.health_state.as_str())
                .or_else(|| {
                    data.pings
                        .iter()
                        .find(|ping| ping.account_name == row.name)
                        .map(|ping| {
                            if ping
                                .status_code
                                .is_some_and(|status| (200..300).contains(&status))
                            {
                                "healthy"
                            } else if ping.status_code.is_some() || ping.error.is_some() {
                                "unhealthy"
                            } else {
                                "unknown"
                            }
                        })
                })
                .unwrap_or("unknown");
            let exactness = exactness_badge(
                row.exact_count,
                row.derived_count,
                row.partial_count,
                row.estimated_count,
                row.unknown_count,
                row.provider_reported_count,
            );
            let tokens_per_second = if row.avg_latency_ms > 0.0 && row.requests > 0 {
                format!(
                    "{:.1} tok/s",
                    row.output_tokens as f64 * 1_000.0
                        / (row.avg_latency_ms * row.requests as f64)
                )
            } else {
                "0.0 tok/s".to_owned()
            };
            let authentication_failed = live_health.map_or("—", |snapshot| {
                if snapshot.health_state == "authentication_failed" {
                    "yes"
                } else {
                    "no"
                }
            });
            let operator_disabled = live_health.map_or("—", |snapshot| {
                if snapshot
                    .disabled_until
                    .is_some_and(|until| until > snapshot.last_check)
                {
                    "yes"
                } else {
                    "no"
                }
            });
            let auth_class = if authentication_failed == "—" {
                String::new()
            } else {
                format!(" class=\"{authentication_failed}\"")
            };
            let disabled_class = if operator_disabled == "—" {
                String::new()
            } else {
                format!(" class=\"{operator_disabled}\"")
            };
            let detail_cells = format!(
                "<td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td>",
                format_microdollars(row.reserved_microdollars),
                row.active_reservations,
                format_microdollars(row.utilization_5h),
                format_microdollars(row.utilization_7d),
                format_microdollars(row.utilization_30d),
                format_bytes(row.bytes_received),
                format_bytes(row.bytes_emitted),
                live_health.map_or(0, |snapshot| i64::from(snapshot.consecutive_failures)),
                auth_class,
                authentication_failed,
                disabled_class,
                operator_disabled,
                format_ratio_percent(Some(row.estimated_cost_fraction)),
                format_ratio_percent(row.cache_read_ratio),
                format_ratio_percent(row.cache_write_ratio),
                format_ratio_percent(row.reasoning_output_ratio),
                row.avg_cost_per_request
                    .map(format_microdollars)
                    .unwrap_or_else(|| "—".to_owned()),
                row.avg_cost_per_1k_tokens
                    .map(format_microdollars)
                    .unwrap_or_else(|| "—".to_owned()),
            );
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{}\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\" class=\"{}\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td>{}</tr>",
                html_escape(&row.name),
                html_escape(&row.provider_id),
                if row.enabled { "yes" } else { "no" },
                if row.enabled { "yes" } else { "no" },
                row.requests,
                format_microdollars(row.cost_microdollars),
                html_escape(health_state),
                html_escape(health_state),
                row.errors,
                format_tokens(row.input_tokens),
                format_tokens(row.output_tokens),
                format_tokens(row.input_tokens + row.output_tokens),
                format_latency(row.avg_latency_ms),
                tokens_per_second,
                exactness,
                detail_cells,
            )
        })
        .collect::<String>();
    let high_spend_rows = data
        .accounts
        .iter()
        .filter_map(|row| {
            let estimated_microdollars = row.cost_microdollars as f64 * row.estimated_cost_fraction;
            (estimated_microdollars >= 10_000_000.0).then(|| {
                format!(
                    "<li><code>{}</code>: ~${:.2} estimated ({}% of total)</li>",
                    html_escape(&row.name),
                    estimated_microdollars / 1_000_000.0,
                    (row.estimated_cost_fraction * 100.0).round() as i64
                )
            })
        })
        .collect::<String>();
    let pricing_warning = if high_spend_rows.is_empty() {
        String::new()
    } else {
        format!(
            "<div class=\"panel warn pricing-warning\"><strong>Pricing warning:</strong> the following accounts have substantial cost on estimated (non-exact) pricing in the selected period:<ul>{high_spend_rows}</ul></div>"
        )
    };
    format!(
        "<h2>Accounts</h2><form method=\"get\" class=\"period-selector account-filters\" data-period-selector aria-label=\"Account filters\"><label for=\"period\">Period: </label><select id=\"period\" name=\"period\" data-auto-submit=\"1\">{}</select><label for=\"show_disabled\">Disabled: </label><select id=\"show_disabled\" name=\"show_disabled\" data-auto-submit=\"1\"><option value=\"0\"{}>Hide disabled accounts</option><option value=\"1\"{}>Show disabled accounts</option></select><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>{pricing_warning}<section class=\"panel\"><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Account</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Enabled</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Health</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Input tokens</th><th data-priority=\"2\">Output tokens</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">TPS</th><th data-priority=\"2\">Exactness</th>{}</tr></thead><tbody>{rows}</tbody></table></div></section>",
        period_options(period),
        if show_disabled {
            ""
        } else {
            " selected=\"selected\""
        },
        if show_disabled {
            " selected=\"selected\""
        } else {
            ""
        },
        html_escape(theme),
        detail_headers
    )
}

pub(in crate::server::dashboard) fn exactness_badge(
    exact: i64,
    derived: i64,
    partial: i64,
    estimated: i64,
    unknown: i64,
    provider_reported: i64,
) -> String {
    let total = exact + derived + partial + estimated + unknown + provider_reported;
    if total == 0 {
        return "<span class=\"exactness-badge empty\">—</span>".to_owned();
    }
    let class = if estimated == total || unknown == total {
        "est-major"
    } else if estimated + unknown + partial > 0 {
        "partial-mix"
    } else {
        "derived"
    };
    let label = format!(
        "u:{provider_reported},e:{exact},d:{derived},p:{partial},~:{estimated},?:{unknown}"
    );
    format!(
        "<span class=\"exactness-badge {class}\" data-tooltip=\"{label}\" aria-label=\"{label}\">{label}</span>"
    )
}
