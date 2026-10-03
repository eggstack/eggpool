use super::*;

pub(in crate::server::dashboard) fn overview_metric_card(
    label: &str,
    value: impl std::fmt::Display,
    subtext: &str,
) -> String {
    let tooltip = match label {
        "Requests" => Some(
            "Total proxied requests in the selected period. The subtext splits them into successful and error requests.",
        ),
        "Error rate" => Some("Fraction of requests in the selected period that ended in an error."),
        "Total cost" => Some(
            "Total recorded request cost in the selected period. When the upstream provider reports a cost (e.g. OpenCode Go's usage.cost field), that value takes precedence over locally computed rates; otherwise eggpool falls back to per-token rates from the catalog. Reservation-derived estimates are advisory and never inflate the totals when more trustworthy data is available.",
        ),
        "Utilization imbalance" => Some(
            "Coefficient of variation across active accounts. Higher values mean load is concentrated unevenly.",
        ),
        "Total tokens" => Some(
            "Input, output, cache-read, and cache-write tokens recorded for the selected period. This total includes provider cache counters and can exceed fresh input/output volume on cache-heavy workloads.",
        ),
        "Fresh tokens" => Some(
            "Input plus output tokens recorded for the selected period, excluding cache-read and cache-write counters.",
        ),
        "Request shaping" => Some("Request shaping"),
        "Cache reads" => Some(
            "Provider-reported prompt-cache read tokens. The subtext shows the bounded read share cache_read / (input + cache_read + cache_write) and the cache write volume.",
        ),
        "Provider cache hit rate" => Some(
            "Protocol-aware cache hit rate: cache_read_tokens / cache_eligible_input_tokens. For OpenAI-compatible providers the denominator is total billed prompt tokens; for Anthropic it is fresh input + cache read + cache creation. Cache writes/creation are warmup, not hits.",
        ),
        "Cache write/warmup rate" => Some(
            "Cache write (creation) tokens as a share of eligible input. These populate cache entries and are not cache hits.",
        ),
        "Reasoning tokens" => {
            Some("Tokens reported by upstreams as reasoning or extended-thinking output.")
        }
        "Throughput" => Some(
            "Aggregate token throughput across requests, computed from total tokens divided by total latency.",
        ),
        "Streaming" => {
            Some("How many requests used streaming responses versus non-streaming responses.")
        }
        "Exactness" => Some(
            "Count of requests whose cost was exact. The subtext also shows derived, estimated, and unknown-cost rows.",
        ),
        "Bandwidth received" => {
            Some("Total bytes received from clients by EggPool in the selected period.")
        }
        "Bandwidth emitted" => {
            Some("Total bytes emitted by EggPool toward clients in the selected period.")
        }
        "Avg TTFT (streamed)" => {
            Some("Average time to first token for streamed requests, with P50 and P99 shown below.")
        }
        "Pending requests" => {
            Some("Requests still in progress. Subtext shows the oldest pending age.")
        }
        "Active reservations" => Some("Active quota or spend reservations for in-flight work."),
        "Finalizer (24h)" => Some(
            "Reliability cleanup activity over the last 24 hours, including stale request cleanup, timeout cases, and crash recovery runs.",
        ),
        "Retry rate" => Some(
            "Share of upstream attempts that required another try instead of succeeding or failing terminally on the first attempt.",
        ),
        "First-attempt success" => Some("Share of attempts that completed without any retry."),
        _ => None,
    };
    let tooltip_attrs = tooltip.map_or_else(String::new, |text| {
        let text = html_escape(text);
        format!(" aria-label=\"{text}\" data-tooltip=\"{text}\" data-tooltip-pos=\"bottom\"")
    });
    format!(
        "<div class=\"card\"{tooltip_attrs}><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">{}</p></div>",
        html_escape(label),
        html_escape(value),
        html_escape(subtext),
    )
}

pub(in crate::server::dashboard) fn render_overview(
    summary: &db::DashboardSummary,
    page: OverviewPage<'_>,
) -> String {
    let OverviewPage {
        accounts,
        page_data,
        period,
        theme,
        refresh_interval_s,
        show_disabled,
        health_snapshots,
    } = page;
    let total = summary.total_requests;
    let errors = summary.error_requests;
    let error_rate = if total == 0 {
        0.0
    } else {
        errors as f64 / total as f64 * 100.0
    };
    let fresh_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        fresh_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let cost_subtext = if summary.provider_reported_count > 0 {
        format!(
            "in {} · out {} · total {} · {} provider-billed",
            format_tokens(summary.total_input_tokens),
            format_tokens(summary.total_output_tokens),
            format_tokens(fresh_tokens),
            summary.provider_reported_count
        )
    } else {
        format!(
            "in {} · out {} · total {}",
            format_tokens(summary.total_input_tokens),
            format_tokens(summary.total_output_tokens),
            format_tokens(fresh_tokens),
        )
    };
    let disabled_count = accounts.iter().filter(|account| !account.enabled).count();
    let account_rows = accounts
        .iter()
        .filter(|account| show_disabled || account.enabled)
        .map(|account| {
            let row = page_data.accounts.iter().find(|row| row.name == account.name);
            let live_health = health_snapshots
                .iter()
                .find(|snapshot| snapshot.account_name == account.name);
            let health = live_health
                .map(|snapshot| snapshot.health_state.as_str())
                .or_else(|| {
                    page_data
                        .pings
                        .iter()
                        .find(|ping| ping.account_name == account.name)
                        .map(|ping| {
                            if ping.status_code.is_some_and(|status| (200..300).contains(&status)) {
                                "healthy"
                            } else if ping.status_code.is_some() || ping.error.is_some() {
                                "unhealthy"
                            } else {
                                "unknown"
                            }
                        })
                })
                .unwrap_or("unknown");
            let exactness = row.map_or_else(String::new, |row| {
                exactness_badge(
                    row.exact_count,
                    row.derived_count,
                    row.partial_count,
                    row.estimated_count,
                    row.unknown_count,
                    row.provider_reported_count,
                )
            });
            let requests = row.map_or(0, |row| row.requests);
            let errors = row.map_or(0, |row| row.errors);
            let input_tokens = row.map_or(0, |row| row.input_tokens);
            let output_tokens = row.map_or(0, |row| row.output_tokens);
            let latency = row.map_or(0.0, |row| row.avg_latency_ms);
            let tps = if latency > 0.0 && requests > 0 {
                format!("{:.1} tok/s", output_tokens as f64 * 1_000.0 / (latency * requests as f64))
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
            let detail_cells = row.map_or_else(
                || "<td data-priority=\"3\">—</td>".repeat(19),
                |row| {
                    format!(
                        "<td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\"{}>{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td>",
                        format_microdollars(row.reserved_microdollars),
                        row.active_reservations,
                        format_microdollars(row.utilization_5h),
                        format_microdollars(row.utilization_7d),
                        format_microdollars(row.utilization_30d),
                        format_bytes(row.bytes_received),
                        format_bytes(row.bytes_emitted),
                        live_health.map_or(0, |snapshot| snapshot.consecutive_failures),
                        auth_class, authentication_failed,
                        disabled_class, operator_disabled,
                        format_ratio_percent(Some(row.estimated_cost_fraction)),
                        format_ratio_percent(row.cache_read_ratio),
                        format_ratio_percent(row.cache_write_ratio),
                        format_ratio_percent(row.reasoning_output_ratio),
                        row.avg_cost_per_request.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
                        row.avg_cost_per_1k_tokens.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
                    )
                },
            );
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{}\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\" class=\"{}\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td>{}</tr>",
                html_escape(&account.name), html_escape(&account.provider_id),
                if account.enabled { "yes" } else { "no" }, if account.enabled { "yes" } else { "no" },
                requests, row.map_or_else(|| "$0.00".to_owned(), |row| format_microdollars(row.cost_microdollars)),
                sanitize_class_name(health), html_escape(health), errors, format_tokens(input_tokens),
                format_tokens(output_tokens), format_tokens(input_tokens + output_tokens), format_latency(latency), tps,
                exactness, detail_cells,
            )
        })
        .collect::<String>();
    let toggle_label = if show_disabled {
        "Hide disabled".to_owned()
    } else if disabled_count > 0 {
        format!("Show {disabled_count} disabled")
    } else {
        "Show disabled".to_owned()
    };
    let toggle_value = if show_disabled { "0" } else { "1" };
    let account_href = format!(
        "?period={}&amp;theme={}&amp;show_disabled={toggle_value}",
        query_component(period),
        query_component(theme)
    );
    let account_table = if account_rows.is_empty() {
        "<p class=\"empty\">No accounts configured.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Account</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Enabled</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Health</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Input tokens</th><th data-priority=\"2\">Output tokens</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">TPS</th><th data-priority=\"2\">Exactness</th><th data-priority=\"3\">Reserved</th><th data-priority=\"3\">Resv.</th><th data-priority=\"3\">5h rate</th><th data-priority=\"3\">7d rate</th><th data-priority=\"3\">30d rate</th><th data-priority=\"3\">BW received</th><th data-priority=\"3\">BW emitted</th><th data-priority=\"3\">Over budget</th><th data-priority=\"3\">Upstream backoff</th><th data-priority=\"3\">Backoff until</th><th data-priority=\"3\">Failures</th><th data-priority=\"3\">Auth fail</th><th data-priority=\"3\">Disabled</th><th data-priority=\"3\">Est. cost</th><th data-priority=\"3\">Cache R</th><th data-priority=\"3\">Cache W</th><th data-priority=\"3\">Reasoning</th><th data-priority=\"3\">Avg cost/req</th><th data-priority=\"3\">Avg cost/1k tok</th></tr></thead><tbody>{account_rows}</tbody></table></div>"
        )
    };
    let mut glance_models = page_data
        .models
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .collect::<Vec<_>>();
    glance_models.sort_by(|left, right| {
        right
            .requests
            .cmp(&left.requests)
            .then_with(|| right.cost_microdollars.cmp(&left.cost_microdollars))
            .then_with(|| left.model_id.cmp(&right.model_id))
            .then_with(|| left.provider_id.cmp(&right.provider_id))
    });
    let model_rows = glance_models
        .into_iter()
        .take(10)
        .map(|row| {
            let tooltip = format!("Open model info for {}", row.model_id);
            format!(
                "<tr><td data-priority=\"1\"><a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a></td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td></tr>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&tooltip),
                html_escape(&tooltip),
                html_escape(&row.model_id),
                row.requests,
                format_microdollars(row.cost_microdollars),
                html_escape(&row.provider_id),
                row.errors,
                format_latency(row.avg_latency_ms),
                format_tokens(row.input_tokens + row.output_tokens),
            )
        })
        .collect::<String>();
    let event_rows = page_data
        .events
        .iter()
        .take(10)
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\"><span class=\"event-tag {}\">{}</span></td><td data-priority=\"2\">{}</td></tr>",
                html_escape(&row.created_at),
                html_escape(&row.account_name),
                sanitize_class_name(&row.event_type),
                html_escape(&row.event_type),
                html_escape(row.details.chars().take(120).collect::<String>()),
            )
        })
        .collect::<String>();
    let overview_glance = format!(
        "<section class=\"overview-grid\"><div class=\"panel\"><h3>Top models</h3>{}</div><div class=\"panel\"><h3>Recent events</h3>{}</div></section>",
        if model_rows.is_empty() {
            "<p class=\"empty\">No model activity in this period.</p>".to_owned()
        } else {
            format!(
                "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Model</th><th data-priority=\"1\">Reqs</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Provider</th><th data-priority=\"2\">Errs</th><th data-priority=\"2\">Latency</th><th data-priority=\"3\">Total tokens</th></tr></thead><tbody>{model_rows}</tbody></table></div>"
            )
        },
        if event_rows.is_empty() {
            "<p class=\"empty\">No recent events.</p>".to_owned()
        } else {
            format!(
                "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">When</th><th data-priority=\"1\">Account</th><th data-priority=\"1\">Type</th><th data-priority=\"2\">Details</th></tr></thead><tbody>{event_rows}</tbody></table></div>"
            )
        },
    );
    let ip_rows = page_data
        .ip_stats
        .iter()
        .take(10)
        .map(|row| {
            format!(
                "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
                html_escape(&row.client_ip), row.requests,
                format_microdollars(row.cost_microdollars), format_latency(row.avg_latency_ms),
                row.errors, format_tokens(row.input_tokens), format_tokens(row.output_tokens),
                format_tokens(row.input_tokens + row.output_tokens), row.unique_models,
            )
        })
        .collect::<String>();
    let ip_panel = if ip_rows.is_empty() {
        String::new()
    } else {
        format!(
            "<section class=\"panel\"><h3>Request breakdown by IP</h3><div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">IP Address</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">Errors</th><th data-priority=\"3\">Input tokens</th><th data-priority=\"3\">Output tokens</th><th data-priority=\"3\">Total tokens</th><th data-priority=\"3\">Models</th></tr></thead><tbody>{ip_rows}</tbody></table></div></section>"
        )
    };
    let retry_attempts = page_data
        .retries
        .iter()
        .map(|row| row.attempts)
        .sum::<i64>();
    let retry_outcomes = page_data
        .retries
        .iter()
        .map(|row| row.retry_outcomes)
        .sum::<i64>();
    let success_attempts = page_data
        .retries
        .iter()
        .map(|row| row.successes)
        .sum::<i64>();
    let first_attempt_success = if retry_attempts > 0 {
        format!(
            "{:.1}%",
            success_attempts as f64 / retry_attempts as f64 * 100.0
        )
    } else {
        "0.0%".to_owned()
    };
    let pending_subtext = if page_data.pending_requests == 0 {
        "oldest — · stale 0"
    } else {
        "oldest — · stale count unavailable"
    };
    let active_accounts = accounts
        .iter()
        .filter(|account| account.enabled)
        .filter_map(|account| {
            page_data
                .accounts
                .iter()
                .find(|row| row.name == account.name)
                .filter(|row| row.requests > 0)
                .map(|row| row.cost_microdollars as f64)
        })
        .collect::<Vec<_>>();
    let utilization_imbalance = if active_accounts.len() < 2 {
        "0.00%".to_owned()
    } else {
        let mean = active_accounts.iter().sum::<f64>() / active_accounts.len() as f64;
        if mean == 0.0 {
            "0.00%".to_owned()
        } else {
            let variance = active_accounts
                .iter()
                .map(|cost| (cost - mean).powi(2))
                .sum::<f64>()
                / active_accounts.len() as f64;
            format!("{:.2}%", variance.sqrt() / mean * 100.0)
        }
    };
    let cache_hit_rate = "—";
    let cards_second = format!(
        "<section class=\"cards system-health\">{}{}{}{}{}</section>",
        overview_metric_card(
            "Pending requests",
            page_data.pending_requests,
            pending_subtext
        ),
        overview_metric_card(
            "Active reservations",
            page_data.active_reservations,
            &format!(
                "reserved {}",
                format_microdollars(page_data.active_reserved_microdollars)
            )
        ),
        overview_metric_card(
            "Finalizer (24h)",
            page_data.finalizer_cleaned_24h,
            &format!("cleaned · {} recovery", page_data.crash_recovery_24h)
        ),
        overview_metric_card(
            "Retry rate",
            if retry_attempts > 0 {
                format!(
                    "{:.1}%",
                    retry_outcomes as f64 / retry_attempts as f64 * 100.0
                )
            } else {
                "0.0%".to_owned()
            },
            &format!("of {retry_attempts} attempts"),
        ),
        overview_metric_card(
            "First-attempt success",
            first_attempt_success,
            "no retry needed"
        ),
    );
    let cards_third = format!(
        "<section class=\"cards\">{}{}{}{}{}{}{}{}</section>",
        overview_metric_card(
            "Total tokens",
            format_tokens(accounted_tokens),
            &format!(
                "fresh {} · cache read {} · cache write {}",
                format_tokens(fresh_tokens),
                format_tokens(summary.total_cache_read_tokens),
                format_tokens(summary.total_cache_write_tokens)
            )
        ),
        overview_metric_card("Request shaping", "—", "request shaping state unavailable"),
        overview_metric_card(
            "Fresh tokens",
            format_tokens(fresh_tokens),
            &format!(
                "in {} · out {}",
                format_tokens(summary.total_input_tokens),
                format_tokens(summary.total_output_tokens)
            )
        ),
        overview_metric_card(
            "Provider cache hit rate",
            cache_hit_rate,
            "legacy summary estimate"
        ),
        overview_metric_card(
            "Reasoning tokens",
            format_tokens(summary.total_reasoning_tokens),
            "extended thinking"
        ),
        overview_metric_card(
            "Throughput",
            format!("{:.1} tok/s", summary.tokens_per_second),
            "aggregate Σtokens / Σlatency"
        ),
        overview_metric_card(
            "Streaming",
            summary.streamed_requests,
            &format!("streamed · {} non-streamed", summary.non_streamed_requests)
        ),
        overview_metric_card(
            "Exactness",
            summary.exact_count,
            &format!(
                "exact · {} derived · {} upstream · {} est · {} unk",
                summary.derived_count,
                summary.provider_reported_count,
                summary.estimated_count,
                summary.unknown_count
            )
        ),
    );
    let cards_fourth = format!(
        "<section class=\"cards\">{}{}{}</section>",
        overview_metric_card(
            "Bandwidth received",
            format_bytes(summary.total_bytes_received),
            "client → proxy"
        ),
        overview_metric_card(
            "Bandwidth emitted",
            format_bytes(summary.total_bytes_emitted),
            "upstream → proxy"
        ),
        overview_metric_card(
            "Avg TTFT (streamed)",
            format_latency(summary.avg_ttft_ms),
            &format!(
                "P50 {} · P99 {}",
                format_latency(summary.p50_ttft_ms),
                format_latency(summary.p99_ttft_ms)
            )
        ),
    );
    let token_activity = format!(
        "<section class=\"panel\"><h3>Token activity (last 180 days)</h3>{}</section>",
        render_token_heatmap(&page_data.token_activity, theme)
    );
    let operational_panels = if summary.total_requests > 0 {
        let mut ping_groups = std::collections::BTreeMap::<String, Vec<&db::Ping>>::new();
        for ping in &page_data.pings {
            ping_groups
                .entry(ping.provider_id.clone())
                .or_default()
                .push(ping);
        }
        let ping_rows = ping_groups
            .iter()
            .map(|(provider, observations)| {
                let successes = observations
                    .iter()
                    .filter(|ping| ping.status_code.is_some_and(|status| (200..300).contains(&status)))
                    .count();
                let success_rate = successes as f64 / observations.len() as f64 * 100.0;
                let avg_latency = observations
                    .iter()
                    .filter_map(|ping| ping.latency_ms)
                    .map(|latency| latency as f64)
                    .sum::<f64>()
                    / observations
                        .iter()
                        .filter(|ping| ping.latency_ms.is_some())
                        .count()
                        .max(1) as f64;
                let latest = observations[0];
                let status = if success_rate >= 90.0 { "healthy" } else { "degraded" };
                format!(
                    "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{status}\">{status}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{success_rate:.1}%</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td></tr>",
                    html_escape(provider),
                    format_latency(avg_latency),
                    latest.model_count,
                    html_escape(&latest.probed_at),
                )
            })
            .collect::<String>();
        let provider_health = if ping_rows.is_empty() {
            String::new()
        } else {
            format!(
                "<section class=\"panel\"><h3>Provider health</h3><div class=\"table-scroll\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Status</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">Success rate</th><th data-priority=\"3\">Models</th><th data-priority=\"3\">Last ping</th></tr></thead><tbody>{ping_rows}</tbody></table></div></section>"
            )
        };
        let warning_panel = if summary.reservation_fallback_rows > 0 {
            format!(
                "<section class=\"panel warn reservation-fallback-warning\"><h3>Warnings</h3><p role=\"status\">{} reservation cost fallback rows require review.</p></section>",
                summary.reservation_fallback_rows
            )
        } else {
            String::new()
        };
        format!("{provider_health}{warning_panel}")
    } else {
        String::new()
    };
    let body = format!(
        "{}<section class=\"cards\"><div class=\"card\" aria-label=\"Total proxied requests in the selected period. The subtext splits them into successful and error requests.\" data-tooltip=\"Total proxied requests in the selected period. The subtext splits them into successful and error requests.\" data-tooltip-pos=\"bottom\"><h3>Requests</h3><p class=\"metric\">{}</p><p class=\"sub\">Success {} · Errors {}</p></div><div class=\"card\" aria-label=\"Fraction of requests in the selected period that ended in an error.\" data-tooltip=\"Fraction of requests in the selected period that ended in an error.\" data-tooltip-pos=\"bottom\"><h3>Error rate</h3><p class=\"metric\">{:.2}%</p><p class=\"sub\">avg latency {:.1} ms</p></div><div class=\"card\" aria-label=\"Total recorded request cost in the selected period. When the upstream provider reports a cost (e.g. OpenCode Go's usage.cost field), that value takes precedence over locally computed rates; otherwise eggpool falls back to per-token rates from the catalog. Reservation-derived estimates are advisory and never inflate the totals when more trustworthy data is available.\" data-tooltip=\"Total recorded request cost in the selected period. When the upstream provider reports a cost (e.g. OpenCode Go's usage.cost field), that value takes precedence over locally computed rates; otherwise eggpool falls back to per-token rates from the catalog. Reservation-derived estimates are advisory and never inflate the totals when more trustworthy data is available.\" data-tooltip-pos=\"bottom\"><h3>Total cost</h3><p class=\"metric\">${:.2}</p><p class=\"sub\">{}</p></div><div class=\"card\" aria-label=\"Coefficient of variation across active accounts. Higher values mean load is concentrated unevenly.\" data-tooltip=\"Coefficient of variation across active accounts. Higher values mean load is concentrated unevenly.\" data-tooltip-pos=\"bottom\"><h3>Utilization imbalance</h3><p class=\"metric\">{}</p><p class=\"sub\">CV across active accounts</p></div></section><section class=\"panel\"><div class=\"panel-header\"><h3>Account breakdown<span class=\"panel-header-chip\">{} enabled</span></h3><a class=\"show-disabled-toggle\" href=\"{}\" aria-pressed=\"{}\"><span class=\"disabled-toggle-icon\" aria-hidden=\"true\">&#x25BE;</span>{}</a></div>{}</section><section class=\"panel\"><h3>Request timeseries</h3><div class=\"chart-loading-shell\" data-chart-endpoint=\"/api/timeseries?period={}&amp;bucket=hour\" data-chart-canvas=\"timeseries-chart\" data-chart-state=\"loading\" style=\"height: 300px;\"><span class=\"chart-loading-spinner\" aria-hidden=\"true\"></span><span>Loading chart data…</span></div><script type=\"application/json\" class=\"chart-loading-shell-data\" data-chart-canvas=\"timeseries-chart\">{{}}</script><noscript><div class=\"chart-wrap\" style=\"height: 300px;\"><canvas id=\"timeseries-chart\" data-period=\"{}\"></canvas></div><script type=\"application/json\" id=\"timeseries-initial-data\" data-period=\"{}\">[]</script></noscript></section>",
        dashboard_header("Overview", period, theme),
        total,
        summary.successful_requests,
        errors,
        error_rate,
        summary.avg_latency_ms,
        summary.total_cost_microdollars as f64 / 1_000_000.0,
        cost_subtext,
        utilization_imbalance,
        accounts.iter().filter(|account| account.enabled).count(),
        account_href,
        show_disabled,
        toggle_label,
        account_table,
        html_escape(period),
        html_escape(period),
        html_escape(period)
    );
    let body = body.replacen(
        "<section class=\"cards\">",
        &format!("{cards_second}<section class=\"cards\">"),
        1,
    );
    let body = body.replacen(
        "<section class=\"panel\"><div class=\"panel-header\"><h3>Account breakdown",
        &format!(
            "{cards_third}{cards_fourth}<section class=\"panel\"><div class=\"panel-header\"><h3>Account breakdown"
        ),
        1,
    );
    let body = body.replace(
        "</noscript></section>",
        &format!(
            "</noscript></section>{overview_glance}{ip_panel}{token_activity}{operational_panels}"
        ),
    );
    render_dashboard_layout(
        "Overview",
        "overview",
        period,
        theme,
        refresh_interval_s,
        body.clone(),
        body_requires_chart_runtime(&body),
    )
}

pub(in crate::server::dashboard) fn summary_json(
    summary: &db::DashboardSummary,
    period: &str,
) -> Value {
    let total_tokens = summary.total_input_tokens + summary.total_output_tokens;
    let accounted_tokens =
        total_tokens + summary.total_cache_read_tokens + summary.total_cache_write_tokens;
    let cache_denominator = summary.total_input_tokens
        + summary.total_cache_read_tokens
        + summary.total_cache_write_tokens;
    json!({
        "period": period,
        "total_requests": summary.total_requests,
        "successful_requests": summary.successful_requests,
        "error_requests": summary.error_requests,
        "error_rate": if summary.total_requests > 0 { summary.error_requests as f64 / summary.total_requests as f64 } else { 0.0 },
        "total_input_tokens": summary.total_input_tokens,
        "total_output_tokens": summary.total_output_tokens,
        "total_tokens": total_tokens,
        "fresh_tokens": total_tokens,
        "accounted_tokens": accounted_tokens,
        "total_cost_microdollars": summary.total_cost_microdollars,
        "avg_latency_ms": summary.avg_latency_ms,
        "total_cache_read_tokens": summary.total_cache_read_tokens,
        "total_cache_write_tokens": summary.total_cache_write_tokens,
        "total_reasoning_tokens": summary.total_reasoning_tokens,
        "cache_read_ratio": if cache_denominator > 0 { Some(summary.total_cache_read_tokens as f64 / cache_denominator as f64) } else { None },
        "streamed_requests": summary.streamed_requests,
        "non_streamed_requests": summary.non_streamed_requests,
        "exact_count": summary.exact_count,
        "derived_count": summary.derived_count,
        "partial_count": summary.partial_count,
        "estimated_count": summary.estimated_count,
        "unknown_count": summary.unknown_count,
        "provider_reported_count": summary.provider_reported_count,
        "provider_reported_cost_microdollars": summary.provider_reported_cost_microdollars,
        "estimated_cost_sum_microdollars": summary.estimated_cost_sum_microdollars,
        "reservation_fallback_rows": summary.reservation_fallback_rows,
        "reservation_fallback_excess_microdollars": summary.reservation_fallback_excess_microdollars,
        "total_bytes_received": summary.total_bytes_received,
        "total_bytes_emitted": summary.total_bytes_emitted,
        "total_providers": summary.total_providers,
        "avg_ttft_ms": summary.avg_ttft_ms,
        "tokens_per_second": summary.tokens_per_second,
        "p50_ttft_ms": summary.p50_ttft_ms,
        "p99_ttft_ms": summary.p99_ttft_ms,
    })
}
