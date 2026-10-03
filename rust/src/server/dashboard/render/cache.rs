use super::*;

pub(in crate::server::dashboard) fn render_cache_page(
    data: &db::DashboardData,
    stats: &Value,
    period: &str,
    theme: &str,
) -> String {
    let num = |path: &[&str]| {
        path.iter()
            .fold(stats, |v, k| &v[*k])
            .as_i64()
            .map(|n| n.to_string())
            .unwrap_or_else(|| "—".to_owned())
    };
    let rate = stats["cache_observability"]["cache_counter_coverage_rate"]
        .as_f64()
        .map(|v| format!("{:.1}%", v * 100.0))
        .unwrap_or_else(|| "—".to_owned());
    let hit_rate = stats["cache_observability"]["cache_hit_ratio_known_only"]
        .as_f64()
        .map(|v| format!("{:.1}%", v * 100.0))
        .unwrap_or_else(|| "—".to_owned());
    let write_rate = stats["cache_observability"]["cache_write_rate"]
        .as_f64()
        .map(|v| format!("{:.1}%", v * 100.0))
        .unwrap_or_else(|| "—".to_owned());
    let cache_sub = format!(
        "{} provider-reported rows · {} classified rows",
        num(&["request_shaping", "cache", "cache_counter_reported_rows"]),
        num(&["request_shaping", "cache", "cache_counter_known_rows"]),
    );
    let stability_notes = stats["cache_stability"]["notes"]
        .as_str()
        .unwrap_or("Boundary detail lives in per-request traces.");
    let segmentation_totals = [
        ("Total finalized requests", &["total_requests"][..]),
        (
            "Stable prefix tokens",
            &["token_totals", "stable_prefix"][..],
        ),
        ("Semi-stable tokens", &["token_totals", "semi_stable"][..]),
        ("Volatile suffix tokens", &["token_totals", "volatile"][..]),
        ("Stable prefix bytes", &["byte_totals", "stable_prefix"][..]),
        ("Semi-stable bytes", &["byte_totals", "semi_stable"][..]),
        ("Volatile suffix bytes", &["byte_totals", "volatile"][..]),
    ]
    .iter()
    .map(|(label, path)| {
        let value = path
            .iter()
            .fold(&stats["canonical_request_segmentation"], |value, key| {
                &value[*key]
            })
            .as_i64()
            .unwrap_or(0);
        format!("<tr><td>{label}</td><td class=\"num\">{value}</td></tr>")
    })
    .collect::<String>();
    let values = [
        // Rust has no configured request-compression path; structural
        // segmentation is observational and never rewrites the request.
        ("Request changes", "no changes".to_owned()),
        ("Provider cache counter coverage", rate.clone()),
        (
            "Rows with cache reads",
            data.cache.rows_with_read.to_string(),
        ),
        (
            "Rows with cache writes",
            data.cache.rows_with_write.to_string(),
        ),
        (
            "Rows without cache counters",
            num(&["cache_observability", "cache_counter_not_reported_requests"]),
        ),
        (
            "Unknown counter format",
            num(&["cache_observability", "cache_counter_unknown_requests"]),
        ),
        (
            "Cache read tokens",
            num(&["cache_observability", "cache_read_tokens_canonical"]),
        ),
        (
            "Cache write tokens",
            num(&["cache_observability", "cache_write_tokens_canonical"]),
        ),
        ("Provider cache hit rate", "not collected".to_owned()),
        (
            "Cache write/warmup rate",
            stats["cache_observability"]["cache_write_rate"]
                .as_f64()
                .map(|v| format!("{:.1}%", v * 100.0))
                .unwrap_or_else(|| "not collected".to_owned()),
        ),
        (
            "Transcoded requests",
            num(&["transcoding", "transcoded_count"]),
        ),
        (
            "Segmented",
            num(&["canonical_request_segmentation", "by_status", "segmented"]),
        ),
        (
            "Not collected",
            num(&[
                "canonical_request_segmentation",
                "by_status",
                "not_collected",
            ]),
        ),
        (
            "Empty request",
            num(&[
                "canonical_request_segmentation",
                "by_status",
                "empty_request",
            ]),
        ),
        (
            "Parse failure",
            num(&[
                "canonical_request_segmentation",
                "by_status",
                "parse_failure",
            ]),
        ),
        (
            "With protected prefix",
            num(&["canonical_request_segmentation", "protected_requests"]),
        ),
        (
            "With volatile suffix",
            num(&[
                "canonical_request_segmentation",
                "compressible_candidate_requests",
            ]),
        ),
        ("Mode", "reporting_only".to_owned()),
        ("Cache metrics", "no".to_owned()),
        ("Compression metrics", "no".to_owned()),
        ("Stable-prefix hash", "no".to_owned()),
        ("Compression policy", "no".to_owned()),
    ];
    let cards = [
        runtime_metric_card("Request changes", "no changes", "disabled by config"),
        runtime_metric_card("Provider cache counters", &rate, &cache_sub),
        // No compressor executes in this runtime, so there can be no
        // compression fallback or compression-policy warning to report.
        runtime_metric_card(
            "Safety guardrail",
            "Clean",
            "0 fallbacks · 0 policy warnings",
        ),
        runtime_metric_card(
            "Routing isolation",
            "Isolated",
            "mode reporting_only · cache/compression stay out of scorer",
        ),
    ]
    .concat();
    let reporting_cards = [
        runtime_metric_card(
            "Rows with cache counters",
            &num(&["cache_observability", "cache_counter_reported_requests"]),
            "upstream returned cache fields",
        ),
        runtime_metric_card(
            "Rows without cache counters",
            &num(&["cache_observability", "cache_counter_not_reported_requests"]),
            "payload clean, no cache keys",
        ),
        runtime_metric_card(
            "Unrecognized payload shape",
            &num(&["cache_observability", "cache_counter_unknown_requests"]),
            "parse failure or unrecognized",
        ),
        runtime_metric_card(
            "Provider cache hit rate",
            &hit_rate,
            &format!(
                "read {} / eligible {} · write/warmup {} · {} reported",
                num(&["cache_observability", "cache_read_tokens_canonical"]),
                num(&["cache_observability", "cache_eligible_input_tokens"]),
                write_rate,
                rate
            ),
        ),
        runtime_metric_card(
            "Cache write/warmup rate",
            &write_rate,
            &format!(
                "warmup, not hits · eligible {}",
                num(&["cache_observability", "cache_eligible_requests"])
            ),
        ),
    ]
    .concat();
    let reporting_table = format!(
        "<div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Metric</th><th data-priority=\"2\">Value</th></tr></thead><tbody><tr><td>Total finalized requests</td><td class=\"num\">{}</td></tr><tr><td>Input tokens (all requests)</td><td class=\"num\">{}</td></tr><tr><td>Output tokens (all requests)</td><td class=\"num\">{}</td></tr><tr><td>Read tokens (canonical)</td><td class=\"num\">{}</td></tr><tr><td>Write tokens (canonical)</td><td class=\"num\">{}</td></tr><tr><td>Eligible input tokens (denominator)</td><td class=\"num\">{}</td></tr><tr><td>Provider cache hit rate</td><td class=\"num\">{}</td></tr><tr><td>Cache write/warmup rate</td><td class=\"num\">{}</td></tr><tr><td>Coverage (cache counters reported)</td><td class=\"num\">{}</td></tr><tr><td>Anthropic cache read</td><td class=\"num\">{}</td></tr><tr><td>Anthropic cache creation</td><td class=\"num\">{}</td></tr></tbody></table></div>",
        num(&["cache_observability", "total_requests"]),
        num(&["cache_observability", "input_tokens_total"]),
        num(&["cache_observability", "output_tokens_total"]),
        num(&["cache_observability", "cache_read_tokens_canonical"]),
        num(&["cache_observability", "cache_write_tokens_canonical"]),
        num(&["cache_observability", "cache_eligible_input_tokens"]),
        hit_rate,
        write_rate,
        rate,
        num(&["cache_observability", "total_cache_read_input_tokens"]),
        num(&["cache_observability", "total_cache_creation_input_tokens"]),
    );
    let card_slice = |items: &[(&str, String)]| {
        items
            .iter()
            .map(|(name, value)| {
                let sub = match *name {
                    "Segmented" => "produced a normal result",
                    "Not collected" => "segmentation intentionally skipped",
                    "Empty request" => "segmentation ran but found no content",
                    "Parse failure" => "non-mapping payload or unknown",
                    "With protected prefix" => "stable_prefix_bytes > 0",
                    "With volatile suffix" => "volatile_bytes > 0",
                    "Mode" => "cache/compression in routing",
                    "Cache metrics"
                    | "Compression metrics"
                    | "Stable-prefix hash"
                    | "Compression policy" => "in scorer inputs",
                    _ => "persisted scalar observation",
                };
                runtime_metric_card(name, value, sub)
            })
            .collect::<String>()
    };
    let advanced = format!(
        "<section class=\"panel\"><h3>Native cache preservation ({})</h3><p class=\"sub\">Native cache annotations are tracked per request during transcoding. The durable summary below confirms the tracker is wired and counts transcoded requests in window; per-boundary detail is in the request trace.</p><section class=\"cards\">{}</section><p class=\"sub\">{}</p></section><section class=\"panel\"><h3>Request segmentation ({})</h3><p class=\"sub\">Structural segmentation shows how much traffic was segmented, intentionally skipped, or had no segmentable content without mutating requests.</p><section class=\"cards\">{}</section><div class=\"table-scroll\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Metric</th><th data-priority=\"2\">Value</th></tr></thead><tbody>{}</tbody></table></div></section><section class=\"panel\"><h3>Routing isolation</h3><p class=\"sub\">Cache and compression metrics are reporting-only. The <code>QuotaFairScorer</code> does NOT consume cache, compression, stable-prefix-hash, or compression-policy fields. Same-provider account scoring stays load-based. These flags are hardcoded; they reflect how the router is built, not the current request stream.</p><section class=\"cards\">{}</section><p class=\"sub\">Scorer inputs (allowed): <code>health, quota, active_requests, model_eligibility</code></p></section>",
        html_escape(period),
        runtime_metric_card(
            "Transcoded requests",
            &num(&["cache_stability", "transcoded_request_count"]),
            "boundary tracker active"
        ),
        html_escape(stability_notes),
        html_escape(period),
        card_slice(&values[11..17]),
        segmentation_totals,
        card_slice(&values[17..]),
    );
    format!(
        "<h2>Cache</h2><p class=\"sub\">Cache reporting, request shaping, and safety guardrails.</p>{}<div id=\"cache-summary\"><section class=\"panel\"><h3>Request shaping ({})</h3><p class=\"sub\">Operator summary for request changes, provider cache counter coverage, safety guardrails, and routing isolation. Routing stays load-based and reporting-only metrics never enter the scorer.</p><section class=\"cards\">{cards}</section></section></div><div id=\"cache-reporting\"><section class=\"panel\"><h3>Provider cache counters ({})</h3><p class=\"sub\">Provider-reported cache counters from upstream payloads. Missing cache fields mean the upstream did not surface them. They are not cache misses and do not prove the upstream is uncached. EggPool never disables provider-side caching.</p><section class=\"cards\">{reporting_cards}</section>{reporting_table}</section></div><details class=\"advanced-details\" id=\"advanced-diagnostics\"><summary>Show advanced diagnostics</summary><div class=\"advanced-body\">{}</div></details>",
        dashboard_period_selector(period, theme),
        html_escape(period),
        html_escape(period),
        advanced,
    )
}
