use super::*;

pub(in crate::server::dashboard) fn render_reliability_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
) -> String {
    let attempts: i64 = data.retries.iter().map(|row| row.attempts).sum();
    let failures: i64 = data.retries.iter().map(|row| row.failures).sum();
    let successes: i64 = data.retries.iter().map(|row| row.successes).sum();
    let retry_rows = data
        .retries
        .iter()
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.category),
                row.attempts,
                row.retry_outcomes,
                row.successes,
                row.failures,
                format_latency(row.avg_latency_ms),
            )
        })
        .collect::<String>();
    let retry_attempts: i64 = data.retries.iter().map(|row| row.retry_outcomes).sum();
    // Scoped to the `initial` bucket for the same reason as the overview card:
    // successes/attempts across every category counted retried requests that
    // eventually succeeded as first-attempt successes.
    let (initial_attempts, initial_successes) = data
        .retries
        .iter()
        .filter(|row| row.category == "initial")
        .fold((0_i64, 0_i64), |(attempts, successes), row| {
            (attempts + row.attempts, successes + row.successes)
        });
    let first_attempt_rate = if initial_attempts > 0 {
        initial_successes as f64 * 100.0 / initial_attempts as f64
    } else {
        0.0
    };
    let retry_rate = if attempts > 0 {
        retry_attempts as f64 * 100.0 / attempts as f64
    } else {
        0.0
    };
    let average_attempt_latency = if attempts > 0 {
        data.retries
            .iter()
            .map(|row| row.avg_latency_ms * row.attempts as f64)
            .sum::<f64>()
            / attempts as f64
    } else {
        0.0
    };
    let operational_summary = if data.operational_summary.is_empty() {
        "<p class=\"empty\">No operational events in this window.</p>".to_owned()
    } else {
        let rows = data.operational_summary.iter().map(|row| format!(
            "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
            html_escape(&row.event_type), row.event_count, html_escape(&row.last_seen),
            row.interrupted_requests, row.released_reservations)).collect::<String>();
        format!(
            "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Event type</th><th data-priority=\"1\">Count</th><th data-priority=\"2\">Last seen</th><th data-priority=\"2\">Interrupted</th><th data-priority=\"3\">Released</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let recent_operational_events = if data.recent_operational_events.is_empty() {
        "<p class=\"empty\">No recent operational events.</p>".to_owned()
    } else {
        let rows = data.recent_operational_events.iter().map(|row| format!(
            "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td></tr>",
            html_escape(&row.occurred_at), html_escape(&row.event_type), html_escape(row.details.chars().take(200).collect::<String>()))).collect::<String>();
        format!(
            "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">When</th><th data-priority=\"1\">Type</th><th data-priority=\"2\">Details</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let attempts_chart = format!(
        "<div class=\"chart-wrap\" style=\"height: 280px;\"><canvas id=\"reliability-attempts-by-provider\"></canvas></div><script type=\"application/json\" class=\"static-chart-data\" data-chart-id=\"reliability-attempts-by-provider\">{{\"type\":\"bar\",\"labels\":[\"Success\",\"Retry\",\"Failed\"],\"datasets\":[{{\"label\":\"Attempts\",\"data\":[{successes},{retry_attempts},{failures}],\"backgroundColor\":[\"rgba(75, 192, 120, 0.7)\",\"rgba(255, 159, 64, 0.7)\",\"rgba(255, 99, 132, 0.7)\"]}}],\"options\":{{\"responsive\":true,\"maintainAspectRatio\":false,\"plugins\":{{\"legend\":{{\"display\":false}}}},\"scales\":{{\"y\":{{\"beginAtZero\":true,\"title\":{{\"display\":true,\"text\":\"Count\"}}}}}}}}}}</script>"
    );
    let distribution = if retry_rows.is_empty() {
        "<p class=\"empty\">No attempt data for this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Category</th><th data-priority=\"1\">Attempts</th><th data-priority=\"2\">Retry outcomes</th><th data-priority=\"2\">Successes</th><th data-priority=\"2\">Failures</th><th data-priority=\"3\">Avg attempt latency</th></tr></thead><tbody>{retry_rows}</tbody></table></div>"
        )
    };
    let pending_subtext = if data.pending_requests == 0 {
        "oldest — · stale 0"
    } else {
        "oldest — · stale count unavailable"
    };
    format!(
        "<h2>Reliability</h2>{}<section class=\"cards\"><div class=\"card\" data-tooltip=\"Total upstream attempts in the selected period, including retries.\" data-tooltip-pos=\"bottom\" aria-label=\"Total upstream attempts in the selected period, including retries.\"><h3>Total attempts</h3><p class=\"metric\">{attempts}</p><p class=\"sub\">{period}</p></div><div class=\"card\" data-tooltip=\"Attempts that completed successfully. The subtext highlights the first-attempt success rate.\" data-tooltip-pos=\"bottom\" aria-label=\"Attempts that completed successfully. The subtext highlights the first-attempt success rate.\"><h3>Success attempts</h3><p class=\"metric\">{successes}</p><p class=\"sub\">first-attempt success rate {first_attempt_rate:.1}%</p></div><div class=\"card\" data-tooltip=\"Attempts that were retries rather than initial tries.\" data-tooltip-pos=\"bottom\" aria-label=\"Attempts that were retries rather than initial tries.\"><h3>Retry attempts</h3><p class=\"metric\">{retry_attempts}</p><p class=\"sub\">retry rate {retry_rate:.1}%</p></div><div class=\"card\" data-tooltip=\"Attempts that ended in failure. The subtext shows average attempt latency.\" data-tooltip-pos=\"bottom\" aria-label=\"Attempts that ended in failure. The subtext shows average attempt latency.\"><h3>Failed attempts</h3><p class=\"metric\">{failures}</p><p class=\"sub\">avg attempt latency {average_attempt_latency:.1} ms</p></div></section><section class=\"panel\"><h3>Attempts by provider (aggregated)</h3>{attempts_chart}</section><section class=\"cards system-health\"><div class=\"card\" data-tooltip=\"Requests still in progress. Subtext shows the oldest pending age.\" data-tooltip-pos=\"bottom\" aria-label=\"Requests still in progress. Subtext shows the oldest pending age.\"><h3>Pending requests</h3><p class=\"metric\">{}</p><p class=\"sub\">{}</p></div><div class=\"card\" data-tooltip=\"Active quota or spend reservations for in-flight work.\" data-tooltip-pos=\"bottom\" aria-label=\"Active quota or spend reservations for in-flight work.\"><h3>Active reservations</h3><p class=\"metric\">{}</p><p class=\"sub\">reserved {} · oldest —</p></div><div class=\"card\" data-tooltip=\"Explanation of the pending-request snapshot and stale threshold used by the reliability view.\" data-tooltip-pos=\"bottom\" aria-label=\"Explanation of the pending-request snapshot and stale threshold used by the reliability view.\"><h3>Pending window</h3><p class=\"sub\">stale &gt; 15 minutes are flagged for cleanup</p><p class=\"sub\">snapshot is instantaneous; reload to refresh</p></div></section><section class=\"panel\"><h3>Retry distribution</h3>{distribution}</section><section class=\"panel\"><h3>Operational events (summary)</h3>{operational_summary}</section><section class=\"panel\"><h3>Operational events (recent)</h3>{recent_operational_events}</section>",
        dashboard_period_selector(period, theme),
        data.pending_requests,
        pending_subtext,
        data.active_reservations,
        format_microdollars(data.active_reserved_microdollars),
    )
}

pub(in crate::server::dashboard) fn render_routing_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    trace: &RoutingTraceSnapshot,
) -> String {
    let decisions: i64 = data.routing.iter().map(|row| row.decisions).sum();
    let avg_eligible = if data.routing.is_empty() {
        0.0
    } else {
        data.routing.iter().map(|row| row.avg_eligible).sum::<f64>() / data.routing.len() as f64
    };
    let distinct = data
        .routing
        .iter()
        .map(|row| row.distinct_accounts)
        .sum::<i64>();
    let rows = data
        .routing
        .iter()
        .map(|row| {
            let tooltip = format!("Open model info for {}", row.model_id);
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&tooltip),
                html_escape(&tooltip),
                html_escape(&row.model_id),
            );
            format!(
            "<tr><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{:.2}</td><td data-priority=\"2\">{:.2}</td><td data-priority=\"2\">{:.2}</td><td data-priority=\"3\">{:.3}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.provider_id),
                row.decisions,
                row.avg_eligible,
                row.avg_scored,
                row.avg_excluded,
                row.avg_score,
                row.distinct_accounts,
            )
        })
        .collect::<String>();
    let distribution = if rows.is_empty() {
        "<p class=\"empty\">No routing decisions in this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Model</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Decisions</th><th data-priority=\"2\">Avg eligible</th><th data-priority=\"2\">Avg scored</th><th data-priority=\"2\">Avg excluded</th><th data-priority=\"3\">Avg score</th><th data-priority=\"3\">Distinct accounts</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let mut selected_by_account = std::collections::BTreeMap::<&str, i64>::new();
    for row in &data.routing_selection {
        *selected_by_account.entry(&row.account_name).or_default() += row.selection_count;
    }
    let mut account_selections = selected_by_account.into_iter().collect::<Vec<_>>();
    account_selections
        .sort_by(|left, right| left.1.cmp(&right.1).then_with(|| right.0.cmp(left.0)));
    let selection_total = account_selections
        .iter()
        .map(|(_, count)| count)
        .sum::<i64>();
    let selection_skew = if let (Some((least_name, least)), Some((most_name, most))) =
        (account_selections.first(), account_selections.last())
    {
        if selection_total > 0 {
            let ratio = if *least == 0 {
                0.0
            } else {
                *most as f64 / *least as f64
            };
            let warning = ratio > 3.0 && selection_total > 10;
            format!(
                "<div class=\"card{}\" data-tooltip=\"Selection skew\" data-tooltip-pos=\"bottom\" aria-label=\"Selection skew\"><h3>Selection skew</h3><p class=\"metric\">{ratio:.1}x</p><p class=\"sub\">max/min ratio ({} / {})</p><p class=\"sub\">{} selections across {} accounts</p></div>",
                if warning { " warning" } else { "" },
                html_escape(most_name),
                html_escape(least_name),
                selection_total,
                account_selections.len(),
            )
        } else {
            String::new()
        }
    } else {
        String::new()
    };
    let selection_rows = data
        .routing_selection
        .iter()
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{:.2}</td><td data-priority=\"3\">{:.3}</td><td data-priority=\"3\">{:.2}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.account_name),
                html_escape(&row.provider_id),
                row.selection_count,
                row.last_selected_score.map_or_else(|| "—".to_owned(), |v| format!("{v:.3}")),
                row.last_selected_tier.map_or_else(|| "—".to_owned(), |v| v.to_string()),
                row.avg_selected_tier,
                row.avg_selected_score,
                row.avg_eligible_count,
                html_escape(row.last_selected_at.chars().take(19).collect::<String>()),
            )
        })
        .collect::<String>();
    let selection_table = if selection_rows.is_empty() {
        "<p class=\"empty\">No selection data in this period.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Account</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Selections</th><th data-priority=\"2\">Last score</th><th data-priority=\"2\">Last tier</th><th data-priority=\"3\">Avg tier</th><th data-priority=\"3\">Avg score</th><th data-priority=\"3\">Avg eligible</th><th data-priority=\"3\">Last selected</th></tr></thead><tbody>{selection_rows}</tbody></table></div>"
        )
    };
    let trace_status = format!(
        "<div class=\"card\" data-tooltip=\"Trace status\" data-tooltip-pos=\"bottom\" aria-label=\"Trace status\"><h3>Trace status</h3><p class=\"metric\">{}</p><p class=\"sub\">{} accepted, {} written</p></div>",
        html_escape(&trace.status),
        trace.accepted,
        trace.written,
    );
    let queue_warning =
        trace.queue_capacity > 0 && trace.queue_depth as f64 / trace.queue_capacity as f64 > 0.8;
    let trace_panel = format!(
        "<section class=\"panel\"><h3>Routing trace observability</h3><p class=\"sub\">Diagnostic traces are written by a background writer off the request path. Queue overload or writer failure never delays dispatch.</p><section class=\"cards\"><div class=\"card\" data-tooltip=\"Trace mode\" data-tooltip-pos=\"bottom\" aria-label=\"Trace mode\"><h3>Trace mode</h3><p class=\"metric\">{}</p><p class=\"sub\">sample rate {:.1}%</p></div>{trace_status}<div class=\"card{}\" data-tooltip=\"Dropped traces\" data-tooltip-pos=\"bottom\" aria-label=\"Dropped traces\"><h3>Dropped traces</h3><p class=\"metric\">{}</p><p class=\"sub\">across all drop reasons</p></div><div class=\"card{}\" data-tooltip=\"Queue depth\" data-tooltip-pos=\"bottom\" aria-label=\"Queue depth\"><h3>Queue depth</h3><p class=\"metric\">{}/{}</p><p class=\"sub\">current / capacity</p></div></section></section>",
        html_escape(&trace.mode),
        trace.sample_rate * 100.0,
        if trace.dropped > 0 { " warning" } else { "" },
        trace.dropped,
        if queue_warning { " warning" } else { "" },
        trace.queue_depth,
        trace.queue_capacity,
    );
    format!(
        "<h2>Routing</h2>{}<section class=\"cards\"><div class=\"card\" data-tooltip=\"Total routing decisions recorded in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Total routing decisions recorded in the selected period.\"><h3>Routing decisions</h3><p class=\"metric\">{decisions}</p><p class=\"sub\">in selected period</p></div><div class=\"card\" data-tooltip=\"Average number of accounts that remained eligible for each routing decision.\" data-tooltip-pos=\"bottom\" aria-label=\"Average number of accounts that remained eligible for each routing decision.\"><h3>Avg eligible / decision</h3><p class=\"metric\">{avg_eligible:.2}</p><p class=\"sub\">candidate accounts per decision</p></div><div class=\"card\" data-tooltip=\"Count of different accounts chosen across routing decisions in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Count of different accounts chosen across routing decisions in the selected period.\"><h3>Distinct selected accounts</h3><p class=\"metric\">{distinct}</p><p class=\"sub\">across all (model, provider) groups</p></div>{selection_skew}</section>{trace_panel}<section class=\"panel\"><h3>Exclusion taxonomy</h3><p class=\"empty\">No exclusion data in this period.</p></section><section class=\"panel\"><h3>Routing distribution</h3>{distribution}</section><section class=\"panel\"><h3>Account selection breakdown</h3>{selection_table}</section><section class=\"panel\"><h3>Account exclusions</h3><p class=\"empty\">No exclusion data in this period.</p></section>",
        dashboard_period_selector(period, theme),
    )
}

pub(in crate::server::dashboard) fn render_traces_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    limit: usize,
) -> String {
    if data.requests.is_empty() {
        return format!(
            "<h2>Traces</h2><p class=\"sub\">Auth-gated; does not include error_detail or client_ip; for incident debugging only.</p><form method=\"get\" class=\"filter-form\"><label class=\"trace-limit\">Limit: <span class=\"number-stepper\" data-stepper-for=\"limit\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"dec\" aria-label=\"Decrease limit\">−</button><input type=\"number\" name=\"limit\" id=\"limit\" value=\"{}\" min=\"10\" max=\"500\" data-stepper-input=\"1\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"inc\" aria-label=\"Increase limit\">+</button></span></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form>{}<section class=\"panel\"><p class=\"empty\">No recent requests.</p></section>",
            limit,
            html_escape(period),
            html_escape(theme),
            dashboard_period_selector(period, theme)
        );
    }
    let rows = data
        .requests
        .iter()
        .take(limit)
        .map(|row| {
            let status = row.status_code.map_or_else(
                || row.status.clone(),
                |code| format!("{} ({code})", row.status),
            );
            let latency = row
                .latency_ms
                .filter(|value| *value > 0.0)
                .map_or_else(|| "—".to_owned(), format_latency);
            let model_tooltip = format!("Open model info for {}", row.model_id);
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&model_tooltip),
                html_escape(&model_tooltip),
                html_escape(&row.model_id),
            );
            let request_id = row
                .proxy_request_id
                .as_deref()
                .filter(|id| !id.is_empty())
                .map_or_else(|| "—".to_owned(), |id| id.chars().take(8).collect());
            let has_thinking = row.reasoning_tokens > 0 || row.thinking_characters > 0;
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\" class=\"{}\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.started_at),
                html_escape(&row.account_name),
                html_escape(&status),
                latency,
                html_escape(&row.provider_id),
                html_escape(&row.protocol),
                html_escape(row.error_class.as_deref().unwrap_or("—")),
                format_tokens(row.input_tokens),
                format_tokens(row.output_tokens),
                if has_thinking { "yes" } else { "no" },
                if has_thinking { format_tokens(row.reasoning_tokens) } else { "—".to_owned() },
                html_escape(&request_id),
            )
        })
        .collect::<String>();
    format!(
        "<h2>Traces</h2><p class=\"sub\">Auth-gated; does not include error_detail or client_ip; for incident debugging only.</p><form method=\"get\" class=\"filter-form\"><label class=\"trace-limit\">Limit: <span class=\"number-stepper\" data-stepper-for=\"limit\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"dec\" aria-label=\"Decrease limit\">−</button><input type=\"number\" name=\"limit\" id=\"limit\" value=\"{}\" min=\"10\" max=\"500\" data-stepper-input=\"1\"><button type=\"button\" class=\"number-stepper-btn\" data-stepper-action=\"inc\" aria-label=\"Increase limit\">+</button></span></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form>{}<section class=\"panel\"><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Time</th><th data-priority=\"1\">Account</th><th data-priority=\"1\">Model</th><th data-priority=\"1\">Status</th><th data-priority=\"1\">Latency</th><th data-priority=\"2\">Provider</th><th data-priority=\"2\">Protocol</th><th data-priority=\"2\">Error class</th><th data-priority=\"2\">In</th><th data-priority=\"2\">Out</th><th data-priority=\"3\">Thinking</th><th data-priority=\"3\">ID</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
        limit,
        html_escape(period),
        html_escape(theme),
        dashboard_period_selector(period, theme)
    )
}
