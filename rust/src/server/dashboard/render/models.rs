use super::*;

pub(in crate::server::dashboard) fn render_models_page(
    data: &db::DashboardData,
    period: &str,
    theme: &str,
    model_info: &[Value],
    filters: &ModelFilters,
    provider_priorities: &std::collections::BTreeMap<String, u32>,
) -> String {
    let option = |label: &str, value: &str, selected: Option<&str>| {
        format!(
            "<option value=\"{}\"{}>{}</option>",
            html_escape(value),
            if selected.map_or(value.is_empty(), |selected| selected == value) {
                " selected"
            } else {
                ""
            },
            html_escape(label),
        )
    };
    let account_options = std::iter::once(option("(any account)", "", filters.account.as_deref()))
        .chain(
            data.accounts
                .iter()
                .map(|account| option(&account.name, &account.name, filters.account.as_deref())),
        )
        .collect::<String>();
    let used_options = [("All", ""), ("Used", "used"), ("Unused", "unused")]
        .into_iter()
        .map(|(label, value)| option(label, value, filters.used.as_deref()))
        .collect::<String>();
    let info_options = [
        ("All", ""),
        ("Fresh", "fresh"),
        ("Partial", "partial"),
        ("Sparse", "sparse_new"),
        ("Stale", "stale"),
        ("Conflict", "conflicting"),
        ("Unmatched", "unmatched"),
    ]
    .into_iter()
    .map(|(label, value)| option(label, value, filters.info_status.as_deref()))
    .collect::<String>();
    let availability_options = [
        ("All", ""),
        ("Available", "available"),
        ("Unavailable", "unavailable"),
    ]
    .into_iter()
    .map(|(label, value)| option(label, value, filters.availability.as_deref()))
    .collect::<String>();
    let controls = format!(
        "<form method=\"get\" class=\"filter-form\"><label>Account: <select name=\"account\">{account_options}</select></label><label>Used: <select name=\"used\">{used_options}</select></label><label>Info: <select name=\"info_status\">{info_options}</select></label><label>Availability: <select name=\"availability\">{availability_options}</select></label><input type=\"hidden\" name=\"period\" value=\"{}\"><input type=\"hidden\" name=\"theme\" value=\"{}\"><button type=\"submit\">Apply</button></form><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>",
        html_escape(period),
        html_escape(theme),
        period_options(period),
        html_escape(theme),
    );
    let models = data
        .models
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .filter(|row| match filters.used.as_deref() {
            Some("used") => row.requests > 0,
            Some("unused") => row.requests == 0,
            _ => true,
        })
        .filter(|row| match filters.availability.as_deref() {
            Some("available") => matches!(row.resolution_status.as_str(), "available" | "resolved"),
            Some("unavailable") => {
                matches!(row.resolution_status.as_str(), "unavailable" | "withdrawn")
            }
            _ => true,
        })
        .filter(|row| {
            filters.account.as_deref().is_none_or(str::is_empty)
                || data.requests.iter().any(|request| {
                    request.model_id == row.model_id
                        && filters.account.as_deref() == Some(request.account_name.as_str())
                })
        })
        .filter(|row| {
            let Some(status) = filters
                .info_status
                .as_deref()
                .filter(|status| !status.is_empty())
            else {
                return true;
            };
            model_info.iter().any(|info| {
                info["model_id"]
                    .as_str()
                    .is_some_and(|id| id.eq_ignore_ascii_case(&row.model_id))
                    && info["status"].as_str() == Some(status)
            })
        })
        .collect::<Vec<_>>();
    let model_info_warning = if model_info.is_empty() {
        "<p class=\"empty\" role=\"status\">Model catalog metadata is unavailable. Check the server logs for catalog refresh failures.</p>"
    } else {
        ""
    };
    if models.is_empty() {
        let empty_message = if data
            .models
            .iter()
            .any(|row| row.model_id != "__deprecated__")
            && (filters
                .account
                .as_deref()
                .is_some_and(|value| !value.is_empty())
                || filters
                    .used
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
                || filters
                    .info_status
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
                || filters
                    .availability
                    .as_deref()
                    .is_some_and(|value| !value.is_empty()))
        {
            "No models match the selected filters."
        } else {
            "No models discovered from configured providers."
        };
        return format!(
            "<h2>Models</h2>{model_info_warning}{controls}<section class=\"panel\"><p class=\"empty\">{empty_message}</p></section>"
        );
    }
    let rows = models
        .iter()
        .map(|row| {
            let info = model_info
                .iter()
                .find(|info| info["model_id"].as_str().is_some_and(|id| id.eq_ignore_ascii_case(&row.model_id)));
            let info_pill = info.map_or_else(
                // Not a `.pill`: a "no data" dash is a placeholder, not a
                // status. The pill tints its own background, which put the
                // muted em-dash at 1.96:1 in Nord and under 4.5:1 in 17 of the
                // 50 bundled themes, so the cell read as empty. `.muted` is
                // what the adjacent Benchmarks column already emits for the
                // same placeholder.
                || {
                    "<span class=\"muted\" data-tooltip=\"No model info available\" aria-label=\"No model info available\">—</span>"
                        .to_owned()
                },
                |info| {
                    let status = info["status"].as_str().unwrap_or("unknown");
                    let summary = info["summary"].as_str().unwrap_or("");
                    format!("<span class=\"pill pill-{}\" data-tooltip=\"{}\">{}</span>", html_escape(status), html_escape(summary), html_escape(status))
                },
            );
            let (availability, availability_class) = match row.resolution_status.as_str() {
                "available" | "resolved" => ("available", "available"),
                "unavailable" | "withdrawn" => ("unavailable", "unavailable"),
                _ => ("configured", "configured"),
            };
            let availability_tooltip = if availability == "available" {
                " aria-label=\"Catalog entry with resolved protocol; can be routed.\" data-tooltip=\"Catalog entry with resolved protocol; can be routed.\""
            } else if availability == "unavailable" {
                " aria-label=\"Catalog entry is unavailable for routing.\" data-tooltip=\"Catalog entry is unavailable for routing.\""
            } else {
                ""
            };
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
                "—".to_owned()
            };
            let detail_cells = format!(
                "<td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td><td data-priority=\"3\">{}</td>",
                provider_priorities
                    .get(&row.provider_id)
                    .map_or_else(|| "—".to_owned(), |priority| priority.to_string()),
                format_ratio_percent(Some(row.estimated_cost_fraction)),
                format_ratio_percent(row.cache_read_ratio),
                format_ratio_percent(row.cache_write_ratio),
                format_ratio_percent(row.reasoning_output_ratio),
                row.avg_cost_per_request.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
                row.avg_cost_per_1k_tokens.map(format_microdollars).unwrap_or_else(|| "—".to_owned()),
            );
            let model_info_link_tooltip = info
                .and_then(|info| info["summary"].as_str())
                .filter(|summary| !summary.trim().is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("Open model info for {}", row.model_id));
            let model_link = format!(
                "<a class=\"model-link\" href=\"/models/{}?theme={}\" data-model-id=\"{}\" data-provider-id=\"{}\" data-model-info-key=\"{}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</a>",
                query_component(&row.model_id),
                query_component(theme),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                html_escape(&row.model_id),
                html_escape(&model_info_link_tooltip),
                html_escape(&model_info_link_tooltip),
                html_escape(&row.model_id),
            );
            format!(
                "<tr data-model-id=\"{}\" data-model-info-key=\"{}\" data-provider-id=\"{}\"><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\"><span class=\"pill pill-{}\"{}>{}</span></td><td data-priority=\"1\">{}</td><td data-priority=\"2\"><span class=\"muted\">—</span></td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"1\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{}</td><td data-priority=\"2\">{:.1} ms</td><td data-priority=\"2\">{:.1} ms</td><td data-priority=\"2\">{}</td>{}</tr>",
                html_escape(&row.model_id),
                html_escape(&row.model_id),
                html_escape(&row.provider_id),
                model_link,
                html_escape(&row.provider_id),
                availability_class,
                availability_tooltip,
                availability,
                info_pill,
                row.requests,
                format_microdollars(row.cost_microdollars),
                exactness,
                row.errors,
                format_tokens(row.input_tokens),
                format_tokens(row.output_tokens),
                format_tokens(row.input_tokens + row.output_tokens),
                row.avg_latency_ms,
                row.avg_ttft_ms,
                tokens_per_second,
                detail_cells,
            )
        })
        .collect::<String>();
    format!(
        "<h2>Models</h2>{model_info_warning}{controls}<section class=\"panel\"><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th data-priority=\"1\">Model</th><th data-priority=\"1\">Provider</th><th data-priority=\"1\">Avail.</th><th data-priority=\"1\">Info</th><th data-priority=\"2\">Benchmarks</th><th data-priority=\"1\">Requests</th><th data-priority=\"1\">Cost</th><th data-priority=\"1\">Exactness</th><th data-priority=\"2\">Errors</th><th data-priority=\"2\">Input tokens</th><th data-priority=\"2\">Output tokens</th><th data-priority=\"2\">Total tokens</th><th data-priority=\"2\">Avg latency</th><th data-priority=\"2\">Avg TTFT</th><th data-priority=\"2\">TPS</th>{}</tr></thead><tbody>{rows}</tbody></table></div></section>",
        "<th data-priority=\"3\">Priority</th><th data-priority=\"3\">Est. cost</th><th data-priority=\"3\">Cache R</th><th data-priority=\"3\">Cache W</th><th data-priority=\"3\">Reasoning</th><th data-priority=\"3\">Avg cost/req</th><th data-priority=\"3\">Avg cost/1k tok</th>"
    )
}

pub(in crate::server::dashboard) fn render_model_detail(
    model_id: &str,
    model_info: Option<&Value>,
    observations: &[Value],
) -> String {
    let Some(info) = model_info else {
        return format!(
            "<h2>Model: {}</h2><p class=\"empty\">Model info not available.</p>",
            html_escape(model_id)
        );
    };
    let mut body = render_model_info_detail(model_id, info);
    body.push_str(&render_model_observations(observations));
    body
}

pub(in crate::server::dashboard) fn render_model_info_detail(
    model_id: &str,
    info: &Value,
) -> String {
    let detail = info.get("detail").unwrap_or(&Value::Null);
    let provenance = info.get("provenance").unwrap_or(&Value::Null);
    let conflicts = info.get("conflicts").unwrap_or(&Value::Null);
    let status = info["status"].as_str().unwrap_or("unknown");
    let sparse = info["sparse"].as_bool().unwrap_or(false);
    let summary = info["summary"].as_str().unwrap_or("");
    let status_display = match status {
        "sparse_new" => "sparse",
        "conflicting" => "conflict",
        "source_unavailable" => "source-unavailable",
        "manual_override" => "manual",
        other => other,
    };
    let mut status_pill_class = match status {
        "fresh" => "pill-fresh",
        "partial" | "manual_override" | "manual" => "pill-partial",
        "sparse_new" | "sparse" => "pill-sparse",
        "stale" | "withdrawn" => "pill-stale",
        "conflicting" | "conflict" => "pill-conflict",
        "unmatched" => "pill-unmatched",
        "source_unavailable" | "source-unavailable" => "pill-source-unavailable",
        _ => "pill-unknown",
    };
    // A sparse model with a non-sparse status still renders the "fresh" pill
    // even though the label reads "fresh (sparse)". This used to be an
    // identity `match` that assigned `status_pill_class` to itself, so the
    // sparse styling never applied.
    if sparse && !matches!(status, "sparse" | "sparse_new") {
        status_pill_class = "pill-sparse";
    }
    let benchmarks = detail.get("benchmarks").and_then(Value::as_array);
    let benchmark_brief = benchmarks.map_or_else(String::new, |rows| {
        rows.iter()
            .take(4)
            .filter_map(|row| {
                let name = row["name"]
                    .as_str()
                    .or_else(|| row["benchmark"].as_str())?
                    .trim();
                if name.is_empty() {
                    return None;
                }
                let source = dashboard_benchmark_source(row["source"].as_str().unwrap_or(""));
                Some(format!(
                    "{source}: {} {}",
                    dashboard_benchmark_short_name(name),
                    dashboard_benchmark_result(row)
                ))
            })
            .collect::<Vec<_>>()
            .join(" · ")
    });
    let sources = dashboard_model_sources(provenance.get("sources"));
    let mut tooltip_parts = Vec::new();
    if !summary.trim().is_empty() {
        tooltip_parts.push(summary.to_owned());
    }
    if !sources.is_empty() {
        tooltip_parts.push(format!("Sources: {}", sources.join(", ")));
    }
    if let Some(refreshed) = info["last_refreshed_at"].as_str() {
        tooltip_parts.push(format!(
            "Last checked: {}",
            dashboard_iso_timestamp(refreshed)
        ));
    }
    let tooltip = if tooltip_parts.is_empty() {
        status.to_owned()
    } else {
        tooltip_parts.join(". ")
    };
    let mut status_label = status_display.to_owned();
    if sparse && !matches!(status, "sparse" | "sparse_new") {
        status_label.push_str(" (sparse)");
    }
    let status_pill = format!(
        "<span class=\"pill {status_pill_class}\" data-tooltip=\"{}\" aria-label=\"{}\">{}</span>",
        html_escape(&tooltip),
        html_escape(&tooltip),
        html_escape(&status_label)
    );
    let display_name = detail["display_name"]
        .as_str()
        .filter(|name| !name.trim().is_empty())
        .unwrap_or(model_id);
    let first_seen = model_info_age(info["last_seen_at"].as_str(), false);
    let last_refreshed = model_info_age(info["last_refreshed_at"].as_str(), false);
    let next_refresh = model_info_age(info["next_refresh_at"].as_str(), true);
    let sparse_sub = if sparse { Some("Sparse") } else { None };
    let cards = format!(
        "<section class=\"cards\">{}{}{}{}</section>",
        model_info_metric_card(
            "Status",
            status_label.as_str(),
            sparse_sub,
            "Current freshness status of model-info metadata"
        ),
        model_info_metric_card(
            "Last seen",
            first_seen.as_str(),
            Some("Observation time"),
            "When this model was last observed by any source"
        ),
        model_info_metric_card(
            "Last refreshed",
            last_refreshed.as_str(),
            Some("Refresh time"),
            "When model-info metadata was last refreshed"
        ),
        model_info_metric_card(
            "Next refresh",
            next_refresh.as_str(),
            Some("Scheduled"),
            "When the next scheduled refresh will occur"
        ),
    );
    let summary_html = if summary.trim().is_empty() {
        "<em>No summary available.</em>".to_owned()
    } else {
        html_escape(summary)
    };
    let summary_panel = format!(
        "<section class=\"panel\"><h3>Summary</h3><p>{summary_html}</p>{}</section>",
        if benchmark_brief.is_empty() {
            String::new()
        } else {
            format!(
                "<p class=\"sub\"><strong>Benchmark snapshot:</strong> {}</p>",
                html_escape(&benchmark_brief)
            )
        }
    );
    let limits = detail.get("limits").unwrap_or(&Value::Null);
    let mut limit_parts = Vec::new();
    for (label, nested, legacy) in [
        ("Effective ctx", "effective_context", "context_tokens"),
        (
            "External ctx",
            "external_context",
            "context_window_external",
        ),
        ("Effective out", "effective_output", "max_output_tokens"),
        (
            "External out",
            "external_output",
            "max_output_tokens_external",
        ),
    ] {
        let value = limits.get(nested).or_else(|| detail.get(legacy));
        if let Some(count) = dashboard_positive_integer(value) {
            limit_parts.push(format!("{label}: {}", format_tokens(count)));
        }
    }
    let limits_html = if limit_parts.is_empty() {
        "—".to_owned()
    } else {
        limit_parts.join(" · ")
    };
    let modalities_html = dashboard_string_list(detail.get("modalities"));
    let tools_html = match detail.get("supports_tools").and_then(Value::as_bool) {
        Some(true) => "Yes",
        Some(false) => "No",
        None => "—",
    };
    let callability = format!(
        "<section class=\"panel\"><h3>Provider / Callability</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><tbody><tr><th>Providers</th><td>{}</td></tr><tr><th>Sources</th><td>{}</td></tr><tr><th>Limits</th><td>{limits_html}</td></tr><tr><th>Modalities</th><td>{}</td></tr><tr><th>Tool support</th><td>{tools_html}</td></tr></tbody></table></div></section>",
        dashboard_code_list(detail.get("providers")),
        dashboard_code_list(provenance.get("sources")),
        modalities_html
    );
    let external_ids = detail
        .get("external_ids")
        .and_then(Value::as_object)
        .map(|items| {
            items
                .iter()
                .map(|(source, id)| {
                    format!(
                        "{}: <code>{}</code>",
                        html_escape(source),
                        html_escape(dashboard_json_text(id))
                    )
                })
                .collect::<Vec<_>>()
                .join("<br>")
        })
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "—".to_owned());
    let metadata = format!(
        "<section class=\"panel\"><h3>Metadata</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><tbody><tr><th>Family</th><td>{}</td></tr><tr><th>License</th><td>{}</td></tr><tr><th>Release date</th><td>{}</td></tr><tr><th>External IDs</th><td>{external_ids}</td></tr></tbody></table></div></section>",
        dashboard_optional_text(detail.get("family")),
        dashboard_optional_text(detail.get("license")),
        dashboard_optional_text(detail.get("release_date"))
    );
    let benchmark_panel = dashboard_render_benchmarks(benchmarks);
    let huggingface = dashboard_render_huggingface(detail.get("huggingface_metadata"));
    let conflict_panel = dashboard_render_conflicts(conflicts);
    let reconciled = provenance
        .get("reconciled_at")
        .and_then(Value::as_str)
        .map(dashboard_iso_timestamp)
        .map(html_escape)
        .unwrap_or_else(|| "—".to_owned());
    let provenance_panel = format!(
        "<section class=\"panel\"><h3>Provenance</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><tbody><tr><th>Sources</th><td>{}</td></tr><tr><th>Reconciled at</th><td>{reconciled}</td></tr></tbody></table></div></section>",
        dashboard_code_list(provenance.get("sources"))
    );
    format!(
        "<h2>{} <small>({})</small></h2><p>{status_pill}</p>{cards}{summary_panel}{callability}{metadata}{benchmark_panel}{huggingface}{conflict_panel}{provenance_panel}",
        html_escape(display_name),
        html_escape(model_id)
    )
}

pub(in crate::server::dashboard) fn model_info_metric_card(
    title: &str,
    metric: &str,
    sub: Option<&str>,
    tooltip: &str,
) -> String {
    let sub = sub
        .map(|value| format!("<p class=\"sub\">{}</p>", html_escape(value)))
        .unwrap_or_default();
    let metric = if metric.is_empty() {
        String::new()
    } else {
        format!("<p class=\"metric\">{}</p>", html_escape(metric))
    };
    format!(
        "<div class=\"card\" data-tooltip=\"{}\" data-tooltip-pos=\"bottom\" aria-label=\"{}\"><h3>{}</h3>{metric}{sub}</div>",
        html_escape(tooltip),
        html_escape(tooltip),
        html_escape(title)
    )
}

pub(in crate::server::dashboard) fn dashboard_json_text(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}

pub(in crate::server::dashboard) fn dashboard_optional_text(value: Option<&Value>) -> String {
    value
        .filter(|value| !value.is_null())
        .map(dashboard_json_text)
        .map(html_escape)
        .unwrap_or_else(|| "—".to_owned())
}

pub(in crate::server::dashboard) fn dashboard_iso_timestamp(value: &str) -> String {
    if value.len() == 19
        && value.as_bytes().get(10) == Some(&b' ')
        && let (Some(prefix), Some(suffix)) = (value.get(..10), value.get(11..))
    {
        return format!("{prefix}T{suffix}+00:00");
    }
    value.to_owned()
}

pub(in crate::server::dashboard) fn dashboard_positive_integer(
    value: Option<&Value>,
) -> Option<i64> {
    let value = value?;
    if value.is_boolean() {
        return None;
    }
    let integer = value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|number| i64::try_from(number).ok()))
        .or_else(|| {
            value
                .as_f64()
                .filter(|number| number.is_finite() && number.fract() == 0.0)
                .map(|number| number as i64)
        })?;
    (integer > 0).then_some(integer)
}

pub(in crate::server::dashboard) fn dashboard_string_list(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| !item.is_null())
                .map(|item| html_escape(dashboard_json_text(item)))
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "—".to_owned())
}

pub(in crate::server::dashboard) fn dashboard_model_sources(value: Option<&Value>) -> Vec<String> {
    if let Some(items) = value.and_then(Value::as_array) {
        return items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
    }
    value
        .and_then(Value::as_object)
        .map(|items| {
            items
                .iter()
                .map(|(key, item)| {
                    item.get("source")
                        .and_then(Value::as_str)
                        .unwrap_or(key)
                        .to_owned()
                })
                .collect()
        })
        .unwrap_or_default()
}

pub(in crate::server::dashboard) fn dashboard_code_list(value: Option<&Value>) -> String {
    let sources = dashboard_model_sources(value);
    if sources.is_empty() {
        return "—".to_owned();
    }
    sources
        .iter()
        .map(|item| format!("<code>{}</code>", html_escape(item)))
        .collect::<Vec<_>>()
        .join(", ")
}

pub(in crate::server::dashboard) fn dashboard_benchmark_source(value: &str) -> String {
    match value {
        "artificial_analysis" => "AA".to_owned(),
        "openrouter" => "OpenRouter".to_owned(),
        "huggingface" => "Hugging Face".to_owned(),
        "" => "Unknown".to_owned(),
        other => other.to_owned(),
    }
}

pub(in crate::server::dashboard) fn dashboard_benchmark_short_name(name: &str) -> &str {
    name.strip_prefix("Artificial Analysis ")
        .or_else(|| name.strip_prefix("Design Arena: "))
        .unwrap_or(name)
}

pub(in crate::server::dashboard) fn dashboard_number(value: &Value) -> Option<String> {
    if value.is_boolean() {
        return None;
    }
    let number = value.as_f64()?;
    if !number.is_finite() {
        return None;
    }
    let text = format!("{number:.1}");
    Some(text.strip_suffix(".0").unwrap_or(&text).to_owned())
}

pub(in crate::server::dashboard) fn dashboard_benchmark_result(row: &Value) -> String {
    let mut parts = Vec::new();
    if let Some(score) = dashboard_number(&row["score"]) {
        parts.push(score);
    }
    if let Some(rank) = dashboard_positive_integer(Some(&row["rank"])) {
        parts.push(format!("#{rank}"));
    }
    if let Some(mut percentile) = dashboard_number(&row["percentile"]) {
        if let Some(value) = row["percentile"]
            .as_f64()
            .filter(|value| (0.0..=1.0).contains(value))
        {
            percentile = dashboard_number(&Value::from(value * 100.0)).unwrap_or(percentile);
        }
        parts.push(format!("{percentile}%ile"));
    }
    if let Some(version) = row["version"]
        .as_str()
        .filter(|value| !value.trim().is_empty())
    {
        parts.push(format!("v{version}"));
    }
    if parts.is_empty() {
        "—".to_owned()
    } else {
        parts.join(" · ")
    }
}

pub(in crate::server::dashboard) fn dashboard_render_benchmarks(
    benchmarks: Option<&Vec<Value>>,
) -> String {
    let Some(benchmarks) = benchmarks else {
        return String::new();
    };
    let rows = benchmarks.iter().filter_map(|row| {
        let name = row["name"].as_str().or_else(|| row["benchmark"].as_str())?.trim();
        if name.is_empty() { return None; }
        let result = dashboard_benchmark_result(row);
        let source = row["source"].as_str().unwrap_or("");
        let source_label = dashboard_benchmark_source(source);
        let observed = row["observed_at"].as_str().unwrap_or("");
        let observed_short = observed.chars().take(19).collect::<String>();
        let percentile = dashboard_number(&row["percentile"]).map(|mut value| {
            if let Some(raw) = row["percentile"]
                .as_f64()
                .filter(|number| (0.0..=1.0).contains(number))
            {
                value = dashboard_number(&Value::from(raw * 100.0)).unwrap_or(value);
            }
            format!("Percentile: {value}%")
        });
        let detail = [
            Some(name.to_owned()),
            dashboard_number(&row["score"]).map(|value| format!("Score: {value}")),
            dashboard_positive_integer(Some(&row["rank"]))
                .map(|value| format!("Rank: #{value}")),
            percentile,
            row["version"]
                .as_str()
                .filter(|value| !value.trim().is_empty())
                .map(|value| format!("Version: {value}")),
            row["notes"]
                .as_str()
                .or_else(|| row["caveat"].as_str())
                .filter(|value| !value.trim().is_empty())
                .map(str::to_owned),
            (!source.is_empty()).then(|| format!("Source: {source}")),
            (!observed.is_empty()).then(|| format!("Observed: {observed}")),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join("\n");
        let source_tooltip = if source.is_empty() { "Unknown source" } else { source };
        let observed_tooltip = if observed.is_empty() { "No observation timestamp" } else { observed };
        Some(format!("<tr><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td><td><span data-tooltip=\"{}\" aria-label=\"{}\">{}</span></td></tr>", html_escape(name), html_escape(name), html_escape(dashboard_benchmark_short_name(name)), html_escape(&detail), html_escape(&detail), html_escape(result), html_escape(source_tooltip), html_escape(source_tooltip), html_escape(source_label), html_escape(observed_tooltip), html_escape(observed_tooltip), html_escape(observed_short)))
    }).collect::<String>();
    if rows.is_empty() {
        String::new()
    } else {
        format!(
            "<section class=\"panel\"><h3>Benchmarks</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th>Benchmark</th><th>Result</th><th>Source</th><th>Observed</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
        )
    }
}

pub(in crate::server::dashboard) fn dashboard_render_huggingface(value: Option<&Value>) -> String {
    let Some(metadata) = value.and_then(Value::as_object) else {
        return String::new();
    };
    let mut rows = Vec::new();
    for key in [
        "downloads",
        "likes",
        "pipeline_tag",
        "library_name",
        "license",
    ] {
        if let Some(value) = metadata.get(key).filter(|value| !value.is_null()) {
            rows.push(format!(
                "<tr><th>{}</th><td>{}</td></tr>",
                html_escape(key),
                html_escape(dashboard_json_text(value))
            ));
        }
    }
    if let Some(tags) = metadata.get("tags").and_then(Value::as_array) {
        let values = tags
            .iter()
            .take(10)
            .filter(|value| !value.is_null())
            .map(|value| html_escape(dashboard_json_text(value)))
            .collect::<Vec<_>>();
        if !values.is_empty() {
            rows.push(format!(
                "<tr><th>Tags</th><td>{}</td></tr>",
                values.join(", ")
            ));
        }
    }
    if rows.is_empty() {
        String::new()
    } else {
        format!(
            "<section class=\"panel\"><h3>Hugging Face</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><tbody>{}</tbody></table></div></section>",
            rows.join("")
        )
    }
}

pub(in crate::server::dashboard) fn dashboard_render_conflicts(value: &Value) -> String {
    let Some(conflicts) = value.as_object().filter(|value| !value.is_empty()) else {
        return String::new();
    };
    let rows = conflicts
        .iter()
        .map(|(field, value)| {
            if let Some(mapping) = value.as_object().filter(|mapping| !mapping.is_empty()) {
                let sources = mapping
                    .get("sources")
                    .and_then(Value::as_object)
                    .map(|items| {
                        items
                            .iter()
                            .map(|(key, value)| {
                                format!(
                                    "{}: {}",
                                    html_escape(key),
                                    html_escape(dashboard_json_text(value))
                                )
                            })
                            .collect::<Vec<_>>()
                            .join(", ")
                    })
                    .unwrap_or_default();
                format!(
                    "<tr><td>{}</td><td>{sources}</td><td>{}</td><td>{}</td></tr>",
                    html_escape(field),
                    mapping
                        .get("selected")
                        .map(dashboard_json_text)
                        .map(html_escape)
                        .unwrap_or_default(),
                    mapping
                        .get("reason")
                        .map(dashboard_json_text)
                        .map(html_escape)
                        .unwrap_or_default()
                )
            } else {
                format!(
                    "<tr><td>{}</td><td colspan='3'>{}</td></tr>",
                    html_escape(field),
                    html_escape(dashboard_json_text(value))
                )
            }
        })
        .collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Conflicts</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th>Field</th><th>Source values</th><th>Selected</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(in crate::server::dashboard) fn render_model_observations(observations: &[Value]) -> String {
    if observations.is_empty() {
        return String::new();
    }
    let rows = observations.iter().map(|row| format!(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td><code>{}</code></td><td>{}</td></tr>",
        html_escape(row["source"].as_str().unwrap_or("")),
        html_escape(row["source_model_id"].as_str().unwrap_or("—")),
        row["provider_id"].as_str().map(|value| format!("<code>{}</code>", html_escape(value))).unwrap_or_else(|| "—".to_owned()),
        row["observed_at"]
            .as_str()
            .map(dashboard_iso_timestamp)
            .map(|value| html_escape(&value))
            .unwrap_or_else(|| "—".to_owned()),
        row["confidence"]
            .as_f64()
            .map(|_| row["confidence"].to_string())
            .unwrap_or_else(|| "—".to_owned()),
    )).collect::<String>();
    format!(
        "<section class=\"panel\"><h3>Observations</h3><div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data\"><thead><tr><th>Source</th><th>Source model id</th><th>Provider</th><th>Observed</th><th>Confidence</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )
}

pub(in crate::server::dashboard) fn model_info_age(
    timestamp: Option<&str>,
    reverse: bool,
) -> String {
    let Some(timestamp) = timestamp.and_then(parse_dashboard_timestamp) else {
        return "—".to_owned();
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64);
    let delta = if reverse {
        timestamp - now
    } else {
        now - timestamp
    };
    if delta < 0 {
        return "—".to_owned();
    }
    if delta == 0 {
        return "<1s".to_owned();
    }
    if delta < 60 {
        return format!("{delta}s");
    }
    let minutes = delta / 60;
    let seconds = delta % 60;
    if minutes < 60 {
        return format!("{minutes}m{seconds}s");
    }
    let hours = minutes / 60;
    let remaining_minutes = minutes % 60;
    if hours < 24 {
        return format!("{hours}h{remaining_minutes}m");
    }
    format!("{}d{}h", hours / 24, hours % 24)
}

pub(in crate::server::dashboard) fn parse_dashboard_timestamp(value: &str) -> Option<i64> {
    let value = value.trim();
    let date_time = value.get(..19)?;
    let year = date_time.get(0..4)?.parse::<i64>().ok()?;
    let month = date_time.get(5..7)?.parse::<i64>().ok()?;
    let day = date_time.get(8..10)?.parse::<i64>().ok()?;
    let hour = date_time.get(11..13)?.parse::<i64>().ok()?;
    let minute = date_time.get(14..16)?.parse::<i64>().ok()?;
    let second = date_time.get(17..19)?.parse::<i64>().ok()?;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    let y = year - i64::from(month <= 2);
    let era = if y >= 0 { y / 400 } else { (y - 399) / 400 };
    let year_of_era = y - era * 400;
    let shifted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let mut result =
        (era * 146097 + day_of_era - 719468) * 86400 + hour * 3600 + minute * 60 + second;
    let zone = value.get(19..).unwrap_or("");
    if let Some(offset) = zone.find(['+', '-']) {
        let sign = if zone.as_bytes()[offset] == b'+' {
            1
        } else {
            -1
        };
        let digits = zone.get(offset + 1..).unwrap_or("").replace(':', "");
        if digits.len() >= 4 {
            let hours = digits.get(..2)?.parse::<i64>().ok()?;
            let minutes = digits.get(2..4)?.parse::<i64>().ok()?;
            result -= sign * (hours * 3600 + minutes * 60);
        }
    }
    Some(result)
}
