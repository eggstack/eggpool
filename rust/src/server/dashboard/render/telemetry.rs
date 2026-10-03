use super::*;

pub(in crate::server::dashboard) fn render_latency_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
) -> String {
    let header = format!(
        "<h2>Latency</h2>{}",
        dashboard_period_selector(period, theme)
    );
    if data.models.iter().all(|row| row.ttft_requests == 0) {
        return format!(
            "{header}<p class=\"empty\">No TTFT data for this period.</p><section class=\"panel\"><h3>Per-model breakdown</h3><p class=\"empty\">No model data for this period.</p></section>"
        );
    }
    let mut provider_totals: Vec<(&str, f64, i64)> = Vec::new();
    for row in data.models.iter().filter(|row| row.ttft_requests > 0) {
        if let Some((_, total, requests)) = provider_totals
            .iter_mut()
            .find(|(provider, _, _)| *provider == row.provider_id)
        {
            *total += row.avg_ttft_ms * row.ttft_requests as f64;
            *requests += row.ttft_requests;
        } else {
            provider_totals.push((
                &row.provider_id,
                row.avg_ttft_ms * row.ttft_requests as f64,
                row.ttft_requests,
            ));
        }
    }
    let cards = provider_totals
        .iter()
        .map(|(provider, total, requests)| {
            let percentiles = data
                .latency_percentiles
                .iter()
                .find(|row| row.provider_id == *provider && row.model_id.is_empty());
            let p50 = percentiles.map_or(0.0, |row| row.p50_ttft_ms);
            let p99 = percentiles.map_or(0.0, |row| row.p99_ttft_ms);
            format!(
            "<div class=\"card\" data-tooltip=\"Provider TTFT summary. The metric is average time to first token; the subtext shows P50, P99, and request count.\" data-tooltip-pos=\"bottom\" aria-label=\"Provider TTFT summary. The metric is average time to first token; the subtext shows P50, P99, and request count.\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">P50 {} · P99 {} · {} reqs</p></div>",
                html_escape(provider),
                format_latency(total / *requests as f64),
                format_latency(p50),
                format_latency(p99),
                requests,
            )
        })
        .collect::<String>();
    let mut latency_models = data
        .models
        .iter()
        .filter(|row| row.ttft_requests > 0)
        .collect::<Vec<_>>();
    latency_models.sort_by(|left, right| {
        left.avg_ttft_ms
            .partial_cmp(&right.avg_ttft_ms)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let rows = latency_models
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
            let percentiles = data
                .latency_percentiles
                .iter()
                .find(|item| item.provider_id == row.provider_id && item.model_id == row.model_id);
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">—</td></tr>",
                html_escape(&row.provider_id),
                row.ttft_requests,
                format_latency(row.avg_ttft_ms),
                format_latency(percentiles.map_or(0.0, |item| item.p50_ttft_ms)),
                format_latency(percentiles.map_or(0.0, |item| item.p99_ttft_ms)),
            )
        })
        .collect::<String>();
    format!(
        "{header}<section class=\"cards\">{cards}</section><section class=\"panel\"><h3>Per-model breakdown</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Model</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Avg TTFT</th><th data-priority=\"2\">P50 TTFT</th><th data-priority=\"2\">P99 TTFT</th><th data-priority=\"3\">Phases ms (c/r/o)</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(in crate::server::dashboard) fn render_events_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    selected_type: &str,
) -> String {
    let visible_events = data
        .events
        .iter()
        .filter(|row| selected_type.is_empty() || row.event_type == selected_type)
        .collect::<Vec<_>>();
    let rows = if visible_events.is_empty() {
        "<p class=\"empty\">No events recorded.</p>".to_owned()
    } else {
        let rows = visible_events
            .iter()
            .map(|row| {
                format!(
                    "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\"><span class=\"event-tag {}\">{}</span></td><td data-priority=\"2\">{}</td></tr>",
                    html_escape(&row.created_at),
                    html_escape(&row.account_name),
                    sanitize_class_name(&row.event_type),
                    html_escape(&row.event_type),
                    html_escape(row.details.chars().take(200).collect::<String>()),
                )
            })
            .collect::<String>();
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">When</th><th data-priority=\"1\">Account</th><th data-priority=\"1\">Type</th><th data-priority=\"2\">Details</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    };
    let types = data
        .events
        .iter()
        .map(|row| row.event_type.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let type_options = types
        .into_iter()
        .map(|event_type| {
            format!(
                "<option value=\"{}\"{}>{}</option>",
                html_escape(event_type),
                if event_type == selected_type {
                    " selected"
                } else {
                    ""
                },
                html_escape(event_type)
            )
        })
        .collect::<String>();
    format!(
        "<h2>Events</h2><form method=\"get\" class=\"filter-form\"><label>Type: <select name=\"type\" data-auto-submit=\"1\"><option value=\"\" selected>(all types)</option>{type_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><noscript><button type=\"submit\">Apply</button></noscript></form>{}<section class=\"panel\">{rows}</section>",
        html_escape(period),
        html_escape(theme),
        dashboard_period_selector(period, theme)
    )
}

pub(in crate::server::dashboard) fn render_timeseries_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    grouped: &Value,
) -> String {
    let grouped_json = escape_script_end_tags(&grouped_timeseries_json(grouped));
    let has_data = !grouped["points"].as_array().is_none_or(Vec::is_empty)
        && !grouped["buckets"].as_array().is_none_or(Vec::is_empty);
    let chart_display = if has_data {
        ""
    } else {
        " style=\"display: none;\""
    };
    let empty_display = if has_data {
        " style=\"display: none;\""
    } else {
        ""
    };
    let account_options = data
        .accounts
        .iter()
        .map(|row| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&row.name),
                html_escape(&row.name)
            )
        })
        .collect::<String>();
    let model_options = data
        .timeseries
        .iter()
        .map(|row| format!("{}/{}", row.model_id, row.provider_id))
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .map(|model| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&model),
                html_escape(&model)
            )
        })
        .collect::<String>();
    let mut by_bucket = std::collections::BTreeMap::<String, [i64; 7]>::new();
    for row in &data.timeseries {
        let totals = by_bucket.entry(row.bucket.clone()).or_default();
        totals[0] += row.requests;
        totals[1] += row.cost_microdollars;
        totals[2] += row.errors;
        totals[3] += row.total_tokens;
        totals[4] += row.input_tokens;
        totals[5] += row.output_tokens;
        totals[6] += row.bytes_received;
    }
    let aggregate_rows = by_bucket.iter().map(|(bucket, totals)| format!(
        "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
        html_escape(bucket), totals[0], format_microdollars(totals[1]), totals[2], format_tokens(totals[3]),
        format_tokens(totals[4]), format_tokens(totals[5]), format_bytes(totals[6]),
        format_bytes(data.timeseries.iter().filter(|row| row.bucket == *bucket).map(|row| row.bytes_emitted).sum::<i64>())
    )).collect::<String>();
    let aggregate_table = if aggregate_rows.is_empty() {
        "<p class=\"empty\">No requests in this window.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Bucket</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"3\">Input tokens</th><th data-priority=\"3\">Output tokens</th><th data-priority=\"3\">BW received</th><th data-priority=\"3\">BW emitted</th></tr></thead><tbody>{aggregate_rows}</tbody></table></div>"
        )
    };
    let usage_rows = data.timeseries.iter().map(|row| {
        let model_link = format!(
            "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"Open model info for {}\" aria-label=\"Open model info for {}\">{}</a>",
            query_component(&row.model_id), query_component(theme), html_escape(&row.model_id),
            html_escape(&row.provider_id), html_escape(&row.model_id), html_escape(&row.model_id),
            html_escape(&row.model_id), html_escape(&row.model_id));
        format!(
            "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{model_link}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
            html_escape(&row.bucket), html_escape(&row.series), html_escape(&row.provider_id),
            row.requests, format_microdollars(row.cost_microdollars), row.errors,
            format_tokens(row.total_tokens), format_latency(row.avg_latency_ms),
            format_tokens(row.input_tokens), format_tokens(row.output_tokens),
            format_tokens(row.cache_read_tokens), format_tokens(row.cache_write_tokens),
            format_tokens(row.reasoning_tokens), format_bytes(row.bytes_received),
            format_bytes(row.bytes_emitted), format_latency(row.avg_ttft_ms))
    }).collect::<String>();
    let usage_table = if usage_rows.is_empty() {
        "<p class=\"empty\">No requests in this window.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Bucket</th><th data-priority=\"1\">Series</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Model</th><th data-priority=\"1\">Requests</th><th data-priority=\"2\">Cost</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"3\">Input tokens</th><th data-priority=\"3\">Output tokens</th><th data-priority=\"3\">Cache read</th><th data-priority=\"3\">Cache write</th><th data-priority=\"3\">Reasoning</th><th data-priority=\"3\">BW received</th><th data-priority=\"3\">BW emitted</th><th data-priority=\"3\">Avg TTFT</th></tr></thead><tbody>{usage_rows}</tbody></table></div>"
        )
    };
    format!(
        "<h2>Timeseries (hour buckets, group by provider_model)</h2>{}<form method=\"get\" class=\"filter-form timeseries-controls\" data-timeseries-controls aria-label=\"Timeseries filters\"><label>Bucket: <select name=\"bucket\"><option value=\"auto\" selected>Auto (period-aware)</option><option value=\"hour\">Hour</option><option value=\"day\">Day</option></select></label><label>Group by: <select name=\"group_by\"><option value=\"provider_model\" selected>Provider / model</option><option value=\"provider\">Provider</option><option value=\"model\">Model</option><option value=\"account\">Account</option></select></label><label>Metric: <select name=\"metric\"><option value=\"tokens\" selected>Tokens</option><option value=\"requests\">Requests</option><option value=\"cost\">Cost</option><option value=\"errors\">Errors</option><option value=\"bytes\">Bandwidth</option></select></label><label>Limit: <select name=\"limit\"><option value=\"6\">Top 6</option><option value=\"8\">Top 8</option><option value=\"12\" selected>Top 12</option><option value=\"16\">Top 16</option><option value=\"20\">Top 20</option><option value=\"25\">Top 25</option></select></label><label>Account: <select name=\"account\"><option value=\"\" selected>(any account)</option>{account_options}</select></label><label>Model: <select name=\"model\"><option value=\"\" selected>(any model)</option>{model_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form><section class=\"panel timeseries-chart-panel\"><h3>Usage breakdown</h3><div class=\"chart-container\"{chart_display}><canvas class=\"grouped-timeseries-chart\" data-chart-id=\"grouped-timeseries-chart\" data-period=\"{}\" data-bucket=\"hour\" data-group-by=\"provider_model\" data-metric=\"tokens\" data-limit=\"12\" data-account=\"\" data-model=\"\"></canvas></div><p class=\"empty grouped-timeseries-empty\"{empty_display}>No requests in this window.</p><script type=\"application/json\" class=\"grouped-timeseries-data\" data-chart-id=\"grouped-timeseries-chart\">{grouped_json}</script></section><section class=\"panel\"><h3>Usage breakdown</h3>{usage_table}</section><section class=\"panel\"><h3>Aggregate per bucket</h3>{aggregate_table}</section>",
        dashboard_period_selector(period, theme),
        html_escape(period),
        html_escape(theme),
        html_escape(period),
    )
}

pub(in crate::server::dashboard) fn render_bandwidth_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
) -> String {
    let account_options = data
        .accounts
        .iter()
        .map(|account| {
            format!(
                "<option value=\"{}\">{}</option>",
                html_escape(&account.name),
                html_escape(&account.name)
            )
        })
        .collect::<String>();
    format!(
        "<h2>Bandwidth</h2><form method=\"get\" class=\"filter-form\"><label>Account: <select name=\"account\" data-auto-submit=\"1\"><option value=\"\" selected>(all accounts)</option>{account_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"bucket\" value=\"hour\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><noscript><button type=\"submit\">Apply</button></noscript></form>{}<section class=\"cards\"><div class=\"card\" data-tooltip=\"Total bytes received from clients by EggPool in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Total bytes received from clients by EggPool in the selected period.\"><h3>Total received</h3><p class=\"metric\">{}</p><p class=\"sub\">client → proxy</p></div><div class=\"card\" data-tooltip=\"Total bytes emitted by EggPool toward clients in the selected period.\" data-tooltip-pos=\"bottom\" aria-label=\"Total bytes emitted by EggPool toward clients in the selected period.\"><h3>Total emitted</h3><p class=\"metric\">{}</p><p class=\"sub\">upstream → proxy</p></div></section><section class=\"panel\"><h3>Bandwidth activity (last 180 days)</h3>{}</section>",
        html_escape(period),
        html_escape(theme),
        dashboard_period_selector(period, theme),
        format_bytes(data.cache.total_bytes_received),
        format_bytes(data.cache.total_bytes_emitted),
        render_bandwidth_heatmap(&data.token_activity, theme),
    )
}

pub(in crate::server::dashboard) fn render_pings_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
) -> String {
    if data.pings.is_empty() {
        return format!(
            "<h2>Provider Pings</h2>{}<p class=\"empty\">No ping data yet. Data appears after the first catalog refresh.</p><section class=\"panel\"><h3>Recent pings</h3><p class=\"empty\">No pings recorded yet.</p></section>",
            dashboard_period_selector(period, theme)
        );
    }
    let mut provider_totals: Vec<(&str, f64, i64, i64)> = Vec::new();
    for row in &data.pings {
        let latency = row.latency_ms.map_or(0.0, |value| value as f64);
        let success = i64::from(
            row.status_code
                .is_some_and(|code| (200..300).contains(&code)),
        );
        if let Some((_, total, count, successes)) = provider_totals
            .iter_mut()
            .find(|(provider, _, _, _)| *provider == row.provider_id)
        {
            if row.latency_ms.is_some() {
                *total += latency;
                *count += 1;
            }
            *successes += success;
        } else {
            provider_totals.push((
                &row.provider_id,
                latency,
                i64::from(row.latency_ms.is_some()),
                success,
            ));
        }
    }
    provider_totals.sort_by(|left, right| left.0.cmp(right.0));
    let cards = provider_totals
        .iter()
        .map(|(provider, total, count, successes)| {
            let ping_count = data.pings.iter().filter(|row| row.provider_id == *provider).count();
            let success_rate = if ping_count == 0 {
                0.0
            } else {
                *successes as f64 * 100.0 / ping_count as f64
            };
            let status = if success_rate >= 90.0 {
                "healthy"
            } else {
                "degraded"
            };
            format!(
                "<div class=\"card\" data-tooltip=\"Provider ping latency summary. The metric is average ping latency; the subtext shows health status, success rate, and last seen model count.\" data-tooltip-pos=\"bottom\" aria-label=\"Provider ping latency summary. The metric is average ping latency; the subtext shows health status, success rate, and last seen model count.\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\"><span class=\"{}\">{}</span> &middot; {success_rate:.1}% success &middot; {} models</p></div>",
                html_escape(provider),
                format_latency(if *count == 0 { 0.0 } else { total / *count as f64 }),
                status,
                status,
                data.pings
                    .iter()
                    .find(|row| row.provider_id == *provider)
                    .map_or(0, |row| row.model_count),
            )
        })
        .collect::<String>();
    let rows = data
        .pings
        .iter()
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.provider_id),
                html_escape(&row.probed_at),
                format_latency(row.latency_ms.unwrap_or_default() as f64),
                row.status_code
                    .map_or_else(|| "—".to_owned(), |v| v.to_string()),
                html_escape(&row.account_name),
                row.model_count,
                html_escape(row.error.as_deref().unwrap_or_default()),
            )
        })
        .collect::<String>();
    format!(
        "<h2>Provider Pings</h2>{}<section class=\"cards\">{cards}</section><section class=\"panel\"><h3>Recent pings</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Time</th><th data-priority=\"1\">Latency</th><th data-priority=\"1\">Status</th><th data-priority=\"2\">Account</th><th data-priority=\"2\">Models</th><th data-priority=\"3\">Error</th></tr></thead><tbody>{rows}</tbody></table></div></section>",
        dashboard_period_selector(period, theme)
    )
}

pub(in crate::server::dashboard) fn render_token_heatmap(
    rows: &[crate::db::repositories::DashboardTokenActivityRow],
    theme: &str,
) -> String {
    if rows.is_empty() {
        return "<p class=\"empty\">No activity data available.</p>".to_owned();
    }
    let values = rows
        .iter()
        .map(|row| (row.day.as_str(), (row.total_tokens, row.requests)))
        .collect::<std::collections::BTreeMap<_, _>>();
    let epoch_days = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
        / 86_400;
    let start_day = epoch_days - 179;
    let weekday_sunday = (start_day + 4).rem_euclid(7);
    let grid_start = start_day - weekday_sunday;
    let weeks = (epoch_days - grid_start) / 7 + 1;
    let step = 16_i64;
    let cell = 13_i64;
    let left = 36_i64;
    let top = 20_i64;
    let svg_width = left + weeks * step + 10;
    let svg_height = top + 7 * step + 10;
    let max_value = (0..180)
        .map(|offset| {
            let (year, month, day) = civil_date_from_days(start_day + offset);
            let key = format!("{year:04}-{month:02}-{day:02}");
            values.get(key.as_str()).map_or(0, |value| value.0)
        })
        .max()
        .unwrap_or(0)
        .max(1);
    let colors = theme_heatmap_colors(theme);
    let day_labels = [(1, "Mon"), (3, "Wed"), (5, "Fri")]
        .into_iter()
        .map(|(day, label)| {
            format!(
                "<text x=\"0\" y=\"{}\" class=\"heatmap-label\" text-anchor=\"start\" dominant-baseline=\"central\">{label}</text>",
                top + day * step + cell / 2
            )
        })
        .collect::<String>();
    let month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let mut month_labels = String::new();
    let mut prior_month = 0;
    for week in 0..weeks {
        let (_, month, _) = civil_date_from_days(grid_start + week * 7);
        if month != prior_month {
            prior_month = month;
            month_labels.push_str(&format!(
                "<text x=\"{}\" y=\"10\" class=\"heatmap-label\" text-anchor=\"start\">{}</text>",
                left + week * step,
                month_names[(month - 1) as usize]
            ));
        }
    }
    let mut cells = format!("{day_labels}{month_labels}");
    let mut hitboxes = String::new();
    for week in 0..weeks {
        for day_of_week in 0..7 {
            let day_number = grid_start + week * 7 + day_of_week;
            if day_number < start_day || day_number > epoch_days {
                hitboxes.push_str("<div class=\"heatmap-hitbox\"></div>");
                continue;
            }
            let (year, month, day) = civil_date_from_days(day_number);
            let key = format!("{year:04}-{month:02}-{day:02}");
            let (token_count, request_count) = values.get(key.as_str()).copied().unwrap_or((0, 0));
            let ratio = token_count as f64 / max_value as f64;
            let level = if token_count == 0 {
                0
            } else if ratio < 0.25 {
                1
            } else if ratio < 0.5 {
                2
            } else if ratio < 0.75 {
                3
            } else {
                4
            };
            let color = &colors[level];
            let x = left + week * step;
            let y = top + day_of_week * step;
            let weekday = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                [(day_number + 4).rem_euclid(7) as usize];
            let tooltip = format!(
                "{weekday}, {} {day} {year}\n{} tokens · {request_count} request{}",
                month_names[(month - 1) as usize],
                format_tokens(token_count),
                if request_count == 1 { "" } else { "s" }
            );
            let title = format!("{key}: {}", tooltip.replace('\n', " — "));
            cells.push_str(&format!(
                "<rect x=\"{x}\" y=\"{y}\" width=\"{cell}\" height=\"{cell}\" rx=\"2\" fill=\"{color}\" class=\"heatmap-cell\" pointer-events=\"none\"><title>{}</title></rect>",
                html_escape(title)
            ));
            let tooltip = html_escape(tooltip);
            hitboxes.push_str(&format!(
                "<div class=\"heatmap-hitbox\" data-tooltip=\"{tooltip}\" aria-label=\"{tooltip}\"></div>"
            ));
        }
    }
    format!(
        "<div class=\"heatmap\"><svg width=\"{svg_width}\" height=\"{svg_height}\" viewBox=\"0 0 {svg_width} {svg_height}\" role=\"img\" aria-label=\"Token activity (last 180 days)\">{cells}</svg><div class=\"heatmap-overlay\" style=\"--heatmap-weeks: {weeks}\" aria-hidden=\"true\">{hitboxes}</div></div>"
    )
}

pub(in crate::server::dashboard) fn render_bandwidth_heatmap(
    rows: &[crate::db::repositories::DashboardTokenActivityRow],
    theme: &str,
) -> String {
    if rows.is_empty() {
        return "<p class=\"empty\">No activity data available.</p>".to_owned();
    }
    let values = rows
        .iter()
        .map(|row| {
            (
                row.day.as_str(),
                (row.bytes_received, row.bytes_emitted, row.requests),
            )
        })
        .collect::<std::collections::BTreeMap<_, _>>();
    let today = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
        / 86_400;
    let start = today - 179;
    let grid_start = start - (start + 4).rem_euclid(7);
    let weeks = (today - grid_start) / 7 + 1;
    let step = 16_i64;
    let cell = 13_i64;
    let left = 36_i64;
    let top = 20_i64;
    let width = left + weeks * step + 10;
    let height = top + 7 * step + 10;
    let max_value = (0..180)
        .map(|offset| {
            let (year, month, day) = civil_date_from_days(start + offset);
            let key = format!("{year:04}-{month:02}-{day:02}");
            values
                .get(key.as_str())
                .map_or(0, |(received, emitted, _)| {
                    received.saturating_add(*emitted)
                })
        })
        .max()
        .unwrap_or(0)
        .max(1);
    let colors = theme_heatmap_colors(theme);
    let mut cells = String::new();
    for (day, label) in [(1, "Mon"), (3, "Wed"), (5, "Fri")] {
        cells.push_str(&format!(
            "<text x=\"0\" y=\"{}\" class=\"heatmap-label\" text-anchor=\"start\" dominant-baseline=\"central\">{label}</text>",
            top + day * step + cell / 2
        ));
    }
    let months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let mut last_month = 0;
    for week in 0..weeks {
        let (_, month, _) = civil_date_from_days(grid_start + week * 7);
        if month != last_month {
            last_month = month;
            cells.push_str(&format!(
                "<text x=\"{}\" y=\"10\" class=\"heatmap-label\" text-anchor=\"start\">{}</text>",
                left + week * step,
                months[(month - 1) as usize]
            ));
        }
    }
    let mut hitboxes = String::new();
    for week in 0..weeks {
        for weekday_offset in 0..7 {
            let day_number = grid_start + week * 7 + weekday_offset;
            if day_number < start || day_number > today {
                hitboxes.push_str("<div class=\"heatmap-hitbox\"></div>");
                continue;
            }
            let (year, month, day) = civil_date_from_days(day_number);
            let key = format!("{year:04}-{month:02}-{day:02}");
            let (received, emitted, requests) =
                values.get(key.as_str()).copied().unwrap_or((0, 0, 0));
            let total = received.saturating_add(emitted);
            let ratio = total as f64 / max_value as f64;
            let level = if total == 0 {
                0
            } else if ratio < 0.25 {
                1
            } else if ratio < 0.5 {
                2
            } else if ratio < 0.75 {
                3
            } else {
                4
            };
            let x = left + week * step;
            let y = top + weekday_offset * step;
            let pretty_day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                [(day_number + 4).rem_euclid(7) as usize];
            let tooltip = format!(
                "{pretty_day}, {} {day} {year}\n{} in · {} out · {} request{}",
                months[(month - 1) as usize],
                format_bytes(received),
                format_bytes(emitted),
                requests,
                if requests == 1 { "" } else { "s" }
            );
            let title = format!("{key}: {}", tooltip.replace('\n', " — "));
            cells.push_str(&format!(
                "<rect x=\"{x}\" y=\"{y}\" width=\"{cell}\" height=\"{cell}\" rx=\"2\" fill=\"{}\" class=\"heatmap-cell\" pointer-events=\"none\"><title>{}</title></rect>",
                colors[level],
                html_escape(title)
            ));
            let tooltip = html_escape(tooltip);
            hitboxes.push_str(&format!(
                "<div class=\"heatmap-hitbox\" data-tooltip=\"{tooltip}\" aria-label=\"{tooltip}\"></div>"
            ));
        }
    }
    format!(
        "<div class=\"heatmap\"><svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"Bandwidth activity (last 180 days)\">{cells}</svg><div class=\"heatmap-overlay\" style=\"--heatmap-weeks: {weeks}\" aria-hidden=\"true\">{hitboxes}</div></div>"
    )
}
