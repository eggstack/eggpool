use super::*;

pub(in crate::server::dashboard) fn render_runtime_page(
    data: &db::DashboardData,
    summary: &db::DashboardSummary,
    observability: &Value,
    diagnostics: Option<&crate::runtime_lifecycle::RuntimeDiagnosticsSnapshot>,
    runtime_uptime: std::time::Duration,
) -> String {
    let diag = diagnostics
        .map(serde_json::to_value)
        .and_then(Result::ok)
        .unwrap_or(Value::Null);
    let metric = |path: &[&str]| -> String {
        path.iter()
            .fold(&diag, |value, key| &value[*key])
            .as_i64()
            .map(|n| n.to_string())
            .unwrap_or_else(|| "—".to_owned())
    };
    let state = |path: &[&str]| -> String {
        path.iter()
            .fold(&diag, |value, key| &value[*key])
            .as_str()
            .map(html_escape)
            .unwrap_or_else(|| "not collected".to_owned())
    };
    let tasks = diagnostics
        .map(|snapshot| {
            snapshot
                .tasks
                .iter()
                .take(64)
                .map(|task| {
                    format!(
                        "<tr><td data-priority=\"1\">{}</td><td data-priority=\"1\" class=\"{}\">{}</td><td data-priority=\"2\">—</td><td data-priority=\"2\">—</td><td data-priority=\"2\">—</td><td data-priority=\"3\">—</td><td data-priority=\"3\">—</td><td data-priority=\"4\">—/—</td><td data-priority=\"4\">—</td></tr>",
                        html_escape(&task.name),
                        if task.running {
                            "yes"
                        } else if task.enabled {
                            ""
                        } else {
                            "no"
                        },
                        if task.running { "running" } else { "stopped" },
                    )
                })
                .collect::<String>()
        })
        .unwrap_or_default();
    let transcoded = observability["transcoding"]["transcoded_count"]
        .as_i64()
        .map(|n| n.to_string())
        .unwrap_or_else(|| "—".to_owned());
    let parent_process_id = parent_process_id()
        .map(|pid| pid.to_string())
        .unwrap_or_else(|| "—".to_owned());
    let daemon_hint = if std::io::stdin().is_terminal() {
        "no"
    } else {
        "yes"
    };
    let process_sub = format!("PPID {parent_process_id} · daemon {daemon_hint}");
    let host_platform = host_platform_label();
    let server_cards = format!(
        "<section class=\"cards\">{}{}{}</section>",
        runtime_metric_card("Server PID", &std::process::id().to_string(), &process_sub),
        runtime_metric_card(
            "Uptime",
            &format_runtime_age(runtime_uptime),
            "uptime since start"
        ),
        runtime_metric_card("Python", "not applicable", &host_platform),
    );
    let load_summary = load_average_summary();
    let memory_cards = format!(
        "<section class=\"cards\">{}{}{}{}{}</section>",
        runtime_metric_card("RSS memory", "not collected", "resident set size"),
        runtime_metric_card("Open FDs", "not collected", "file descriptors"),
        runtime_metric_card(
            "Active threads",
            "not collected",
            "threading.active_count()",
        ),
        runtime_metric_card("Load average", "not collected", &load_summary),
        runtime_metric_card(
            "Dispatch overhead",
            "not collected",
            "last 0 / 100 attempts"
        ),
    );
    let task_rows = if tasks.is_empty() {
        "<p class=\"empty\">No background tasks registered.</p>".to_owned()
    } else {
        format!(
            "<div class=\"table-scroll\" tabindex=\"0\" role=\"region\" aria-label=\"Scrollable data table\"><table class=\"data compact\"><thead><tr><th data-priority=\"1\">Task</th><th data-priority=\"1\">Status</th><th data-priority=\"2\">Restarts</th><th data-priority=\"2\">Max restarts</th><th data-priority=\"2\">Interval</th><th data-priority=\"3\">Next run</th><th data-priority=\"3\">Done</th><th data-priority=\"4\">Success/Fail</th><th data-priority=\"4\">Last error</th></tr></thead><tbody>{}</tbody></table></div>",
            tasks
        )
    };
    let database_cards = format!(
        "<section class=\"cards\">{}{}{}{}</section>",
        runtime_metric_card(
            "Database",
            "not collected",
            "file path and size unavailable"
        ),
        runtime_metric_card("WAL", "not collected", "WAL mode unavailable"),
        runtime_metric_card("Sync", "not collected", "synchronous mode unavailable"),
        runtime_metric_card("Stats DB", "shared", "single serialized SQLite owner"),
    );
    let routing_cards = format!(
        "<section class=\"cards\">{}{}{}{}</section>",
        runtime_metric_card(
            "Pending requests",
            &data.pending_requests.to_string(),
            "oldest age not collected"
        ),
        runtime_metric_card(
            "Active reservations",
            &data.active_reservations.to_string(),
            "reserved amount not collected"
        ),
        runtime_metric_card("In-flight requests", "not collected", "active upstream"),
        runtime_metric_card("Active backoffs", "not collected", "account backoff rows"),
    );
    let network_cards = format!(
        "<section class=\"cards\">{}{}{}</section>",
        runtime_metric_card("Outbound builds", "not collected", "client lifecycle"),
        runtime_metric_card(
            "Outbound requests",
            "not collected",
            "request count unavailable"
        ),
        runtime_metric_card(
            "Provider clients",
            "not collected",
            "pool snapshot unavailable"
        ),
    );
    let reload_cards = format!(
        "<section class=\"cards\">{}{}{}</section>",
        runtime_metric_card(
            "Reload outcomes",
            &metric(&["counters", "reload_attempts"]),
            "attempts observed"
        ),
        runtime_metric_card(
            "Reload failures",
            &metric(&["counters", "reload_failures"]),
            "failures observed"
        ),
        runtime_metric_card(
            "Reload phase",
            &state(&["reload", "phase"]),
            "runtime diagnostic snapshot"
        ),
    );
    let transcoding_panel = format!(
        "<section class=\"panel\"><h3>Transcoding (24h)</h3><section class=\"cards\">{}{}{}</section><p class=\"empty-state\">Loss warnings are not collected for this period.</p></section>",
        runtime_metric_card(
            "Total requests",
            &summary.total_requests.to_string(),
            "in period"
        ),
        runtime_metric_card(
            "Native",
            &summary
                .total_requests
                .saturating_sub(
                    observability["transcoding"]["transcoded_count"]
                        .as_i64()
                        .unwrap_or(0)
                )
                .to_string(),
            "no transcoding"
        ),
        runtime_metric_card("Transcoded", &transcoded, "cross-protocol"),
    );
    let runtime_snapshot = format!(
        "<section class=\"panel\"><h3>Dispatch spans</h3><p class=\"empty\">Dispatch span details are not collected.</p><p class=\"status\">Metrics received/flushed/dropped: {}/{}/{}</p></section>",
        metric(&["metrics", "total_received"]),
        metric(&["metrics", "total_flushed"]),
        metric(&["metrics", "total_dropped"]),
    );
    format!(
        "<h2>Runtime</h2><p class=\"sub\">Process-level diagnostics for the running EggPool instance.</p>{server_cards}{memory_cards}<section class=\"panel\"><h3>Background tasks</h3>{task_rows}</section>{database_cards}{routing_cards}{network_cards}{transcoding_panel}{runtime_snapshot}{reload_cards}<section class=\"panel\"><h3>Health states</h3><p class=\"empty\">No health state data.</p></section>",
    )
}

pub(in crate::server::dashboard) fn runtime_metric_card(
    title: &str,
    metric: &str,
    sub: &str,
) -> String {
    let tooltip = match title {
        "Server PID" => "Process identity of the running supervisor (PPID and daemon mode).",
        "Uptime" => "Elapsed time since the current EggPool process started.",
        "Python" => "Python runtime version and platform for the running process.",
        "RSS memory" => "Resident memory currently held by the EggPool process.",
        "Open FDs" => "Open file descriptors currently held by the process.",
        "Active threads" => "Current number of active Python threads in the process.",
        "Load average" => {
            "Host load average (1m primary, subtext shows normalized load or 5m/15m)."
        }
        "Dispatch overhead" => {
            "EggPool-local time spent before each upstream dispatch attempt begins."
        }
        "Database" => "Primary SQLite database path and on-disk size.",
        "WAL" => "SQLite write-ahead log size and whether WAL mode is active.",
        "Sync" => "SQLite synchronous mode and whether the primary DB connection is live.",
        "Stats DB" => "Whether stats use a separate SQLite connection.",
        "Pending requests" => "Requests still in progress. Subtext shows the oldest pending age.",
        "Active reservations" => "Active quota or spend reservations for in-flight work.",
        "In-flight requests" => "Requests currently active against upstream providers.",
        "Active backoffs" => {
            "Persisted account backoff rows currently suppressing or delaying eligible accounts."
        }
        "Outbound builds" => {
            "How many times the shared outbound client manager has built a client."
        }
        "Outbound requests" => "Requests via the shared outbound client. Subtext shows errors.",
        "Provider clients" => {
            "How many per-provider HTTP clients were built in the provider client pool."
        }
        "Provider cache hit rate" => {
            "Protocol-aware cache hit rate: cache_read_tokens / cache_eligible_input_tokens. For OpenAI-compatible providers the denominator is total billed prompt tokens; for Anthropic it is fresh input + cache read + cache creation. Cache writes/creation are warmup, not hits."
        }
        "Cache write/warmup rate" => {
            "Cache write (creation) tokens as a share of eligible input. These populate cache entries and are not cache hits."
        }
        _ => title,
    };
    let tooltip = html_escape(tooltip);
    format!(
        "<div class=\"card\" data-tooltip=\"{tooltip}\" data-tooltip-pos=\"bottom\" aria-label=\"{tooltip}\"><h3>{}</h3><p class=\"metric\">{}</p><p class=\"sub\">{}</p></div>",
        html_escape(title),
        html_escape(metric),
        html_escape(sub),
    )
}

pub(in crate::server::dashboard) fn format_runtime_age(elapsed: std::time::Duration) -> String {
    let seconds = elapsed.as_secs();
    if seconds < 1 {
        "<1s".to_owned()
    } else if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3_600 {
        format!("{}m{}s", seconds / 60, seconds % 60)
    } else if seconds < 86_400 {
        format!("{}h{}m", seconds / 3_600, (seconds % 3_600) / 60)
    } else {
        format!("{}d{}h", seconds / 86_400, (seconds % 86_400) / 3_600)
    }
}

#[cfg(unix)]
pub(in crate::server::dashboard) fn parent_process_id() -> Option<u32> {
    Some(std::os::unix::process::parent_id())
}

#[cfg(not(unix))]
pub(in crate::server::dashboard) fn parent_process_id() -> Option<u32> {
    None
}

pub(in crate::server::dashboard) fn host_platform_label() -> String {
    let platform = match std::env::consts::OS {
        "macos" => "macOS",
        "linux" => "Linux",
        "windows" => "Windows",
        other => other,
    };
    format!("{platform}-{}", std::env::consts::ARCH)
}

pub(in crate::server::dashboard) fn load_average_summary() -> String {
    #[cfg(target_os = "linux")]
    if let Ok(loadavg) = std::fs::read_to_string("/proc/loadavg")
        && let Some(load) = loadavg
            .split_whitespace()
            .next()
            .and_then(|value| value.parse::<f64>().ok())
        && let Ok(cpu_count) = std::thread::available_parallelism()
    {
        return format!(
            "{:.2}/core · {} CPUs",
            load / cpu_count.get() as f64,
            cpu_count.get()
        );
    }

    "load average unavailable".to_owned()
}
