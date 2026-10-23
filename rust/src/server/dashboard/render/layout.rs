use super::*;

/// Every dashboard table carries this class. The stylesheet scopes its table
/// chrome — the header fill, the row borders, the cell padding — to
/// `table.data`, so a table without the class renders unstyled. The renderers
/// open their tables with `DASHBOARD_TABLE_OPEN` so the next table cannot
/// forget it.
pub(in crate::server::dashboard) const DASHBOARD_TABLE_OPEN: &str = "<table class=\"data\">";
pub(in crate::server::dashboard) const DASHBOARD_TABLE_OPEN_COMPACT: &str =
    "<table class=\"data compact\">";

/// The shared navigation destinations. Every dashboard surface renders them,
/// so an operator always has a route back to the overview.
const DASHBOARD_NAVIGATION: [(&str, &str, &str); 17] = [
    ("overview", "/", "Overview"),
    ("reliability", "/reliability", "Reliability"),
    ("routing", "/routing", "Routing"),
    ("cache", "/cache", "Cache"),
    ("users", "/users", "Users"),
    ("profiles", "/profiles", "Profiles"),
    ("usage", "/usage", "Usage"),
    ("my-account", "/my-account", "My Account"),
    ("accounts", "/accounts", "Accounts"),
    ("models", "/models", "Models"),
    ("latency", "/latency", "Latency"),
    ("pings", "/pings", "Pings"),
    ("bandwidth", "/bandwidth", "Bandwidth"),
    ("traces", "/traces", "Traces"),
    ("events", "/events", "Events"),
    ("timeseries", "/timeseries", "Timeseries"),
    ("runtime", "/runtime", "Runtime"),
];

/// The theme picker's query parameters, rendered as hidden inputs.
///
/// The picker is a GET form, so submitting it sends only the form's own
/// fields: without these, switching the theme silently dropped every filter
/// the operator had set on the page.
pub(in crate::server::dashboard) fn dashboard_query_inputs(pairs: &[(&str, &str)]) -> String {
    pairs
        .iter()
        .filter(|(_, value)| !value.is_empty())
        .map(|(name, value)| {
            format!(
                "<input type=\"hidden\" name=\"{}\" value=\"{}\">",
                html_escape(name),
                html_escape(value)
            )
        })
        .collect::<String>()
}

/// The shared navigation chrome: the destination links plus the theme picker.
pub(in crate::server::dashboard) fn dashboard_navigation_markup(
    active_nav: &str,
    period: &str,
    theme: &str,
    extra_query: &str,
) -> String {
    let query = format!(
        "period={}&amp;theme={}",
        query_component(period),
        query_component(theme)
    );
    let navigation = DASHBOARD_NAVIGATION
        .iter()
        .map(|(key, href, label)| {
            let class = if *key == active_nav { "active" } else { "" };
            format!(
                "<a class=\"{}\" href=\"{}?{}\">{}</a>",
                class,
                href,
                query,
                html_escape(label)
            )
        })
        .collect::<String>();
    let theme_options = THEME_NAMES
        .iter()
        .map(|name| {
            let selected = if *name == theme { " selected" } else { "" };
            format!(
                "<option value=\"{}\"{}>{}</option>",
                html_escape(name),
                selected,
                html_escape(name)
            )
        })
        .collect::<String>();
    format!(
        "<div class=\"topnav-menu\" id=\"topnav-menu\">{}<form method=\"get\" class=\"theme-selector\" data-tooltip=\"Switch dashboard theme\" data-tooltip-pos=\"bottom\" aria-label=\"Switch dashboard theme\"><select name=\"theme\" aria-label=\"Dashboard theme\" onchange=\"this.form.submit()\">{}</select><input type=\"hidden\" name=\"period\" value=\"{}\">{}</form></div>",
        navigation,
        theme_options,
        html_escape(period),
        extra_query
    )
}

/// The shell the on-prem administrative surfaces use when they are not
/// rendered through the shared dashboard layout.
///
/// It carries the same navigation chrome, the same dashboard stylesheet and
/// the same theme stylesheet as the rest of the dashboard, so a member or an
/// administrator who lands on one of these pages sees a complete page rather
/// than a bare fragment, and has a route back to the overview.
pub(in crate::server::dashboard) fn dashboard_standalone_page(
    title: &str,
    active_nav: &str,
    theme: &str,
    body: &str,
) -> String {
    let theme = selected_theme(theme);
    format!(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{}</title><link rel=\"stylesheet\" href=\"/static/dashboard.css\"><link rel=\"stylesheet\" href=\"/static/theme.css?theme={}\"></head><body><header class=\"topbar\"><h1><a href=\"/?period=24h&amp;theme={}\">EggPool</a></h1><nav class=\"topnav\">{}</nav></header><main id=\"dashboard-content\">{}</main></body></html>",
        html_escape(title),
        query_component(theme),
        query_component(theme),
        dashboard_navigation_markup(active_nav, "24h", theme, ""),
        body
    )
}

pub(in crate::server::dashboard) fn dashboard_header(
    title: &str,
    period: &str,
    theme: &str,
) -> String {
    format!(
        "<h2>{}</h2><form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>",
        html_escape(title),
        period_options(period),
        html_escape(theme),
    )
}

pub(in crate::server::dashboard) fn dashboard_period_selector(period: &str, theme: &str) -> String {
    format!(
        "<form method=\"get\" class=\"period-selector\" data-period-selector aria-label=\"Period selector\"><label for=\"period\">Period: <select id=\"period\" name=\"period\">{}</select></label><input type=\"hidden\" name=\"theme\" value=\"{}\"></form>",
        period_options(period),
        html_escape(theme),
    )
}

pub(in crate::server::dashboard) fn dashboard_empty(title: &str, message: &str) -> String {
    format!(
        "<section class=\"panel\"><div class=\"panel-header\"><h2>{}</h2></div><p class=\"empty\" role=\"status\">{}</p></section>",
        html_escape(title),
        html_escape(message),
    )
}

#[allow(clippy::too_many_arguments)]
pub(in crate::server::dashboard) fn dashboard_page_with_body(
    configured_theme: &str,
    title: &str,
    active_nav: &str,
    period: Option<String>,
    theme: Option<String>,
    refresh_interval_s: u64,
    body: String,
    extra_query: &str,
) -> Response {
    let period = period.as_deref().unwrap_or("24h");
    let period = match normalize_period(Some(period)) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let theme = selected_theme(theme.as_deref().unwrap_or(configured_theme));
    let include_chart_js =
        body_requires_chart_runtime(&body) || matches!(active_nav, "reliability" | "routing");
    html_response(render_dashboard_layout(
        title,
        active_nav,
        period,
        theme,
        refresh_interval_s,
        body,
        include_chart_js,
        extra_query,
    ))
}

pub(in crate::server::dashboard) fn body_requires_chart_runtime(body: &str) -> bool {
    [
        "data-chart-endpoint",
        "grouped-timeseries-chart",
        "static-chart-data",
        "id=\"timeseries-chart\"",
    ]
    .iter()
    .any(|hook| body.contains(hook))
}

#[allow(clippy::too_many_arguments)]
pub(in crate::server::dashboard) fn render_dashboard_layout(
    title: &str,
    active_nav: &str,
    period: &str,
    theme: &str,
    refresh_interval_s: u64,
    body: String,
    include_chart_js: bool,
    extra_query: &str,
) -> String {
    let query = format!(
        "period={}&amp;theme={}",
        query_component(period),
        query_component(theme)
    );
    let chart_preload = if include_chart_js {
        "<link rel=\"preload\" href=\"/static/chart.js\" as=\"script\">"
    } else {
        ""
    };
    let chart_script = if include_chart_js {
        "<script defer src=\"/static/chart.js\"></script>"
    } else {
        ""
    };
    let refresh_script = if matches!(active_nav, "overview" | "runtime" | "cache") {
        auto_refresh_script(refresh_interval_s)
    } else {
        String::new()
    };
    let navigation_markup = dashboard_navigation_markup(active_nav, period, theme, extra_query);
    // Only advertise auto-refresh on the pages that actually get the polling
    // script. Claiming it everywhere told operators to expect live data on
    // pages that render once and never update.
    let refresh_note = if refresh_script.is_empty() {
        String::new()
    } else {
        format!(" &middot; auto-refresh {}s", refresh_interval_s.max(1))
    };
    // The refresh timestamp is a live region: the auto-refresh tick replaces
    // its text, and an operator who is not looking at the footer still has to
    // learn that the page moved on. It is tied to the refresh tick itself
    // rather than to a timer of its own, so it never announces on a cadence
    // no operator could use.
    let footer = format!(
        "<footer><small>Period: <span class=\"period-label\">{}</span>{} &middot; <span id=\"dashboard-updated\" role=\"status\" aria-live=\"polite\">ready</span></small></footer>",
        html_escape(period),
        refresh_note
    );
    format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>{}</title>\n<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\">\n<link rel=\"preload\" href=\"/static/dashboard.css\" as=\"style\">\n<link rel=\"stylesheet\" href=\"/static/dashboard.css\">\n<link rel=\"stylesheet\" href=\"/static/theme.css?theme={}\">\n{}\n</head>\n<body>\n<a class=\"skip-link\" href=\"#dashboard-content\">Skip to main content</a>\n<svg class=\"egg-background\" viewBox=\"0 0 256 256\" preserveAspectRatio=\"xMidYMid meet\" aria-hidden=\"true\" focusable=\"false\"><path class=\"shape\" d=\"M128 30\n           C82 30 55 88 57 145\n           C59 202 89 231 128 231\n           C167 231 197 202 199 145\n           C201 88 174 30 128 30 Z\" /><path class=\"thin\" d=\"M86 132 H112 L126 111 L144 158 L159 132 H174\" /><circle class=\"shape\" cx=\"85\" cy=\"132\" r=\"5\" /><circle class=\"shape\" cx=\"174\" cy=\"132\" r=\"5\" /></svg>\n<header class=\"topbar\"><button class=\"topnav-burger\" type=\"button\" aria-label=\"Open page menu\" aria-expanded=\"false\" aria-controls=\"topnav-menu\"><svg class=\"topnav-burger-icon\" viewBox=\"0 0 24 24\" width=\"24\" height=\"24\" aria-hidden=\"true\" focusable=\"false\"><rect class=\"bar bar-1\" x=\"0\" y=\"0\" width=\"24\" height=\"2\" rx=\"1\"/><rect class=\"bar bar-2\" x=\"0\" y=\"11\" width=\"24\" height=\"2\" rx=\"1\"/><rect class=\"bar bar-3\" x=\"0\" y=\"22\" width=\"24\" height=\"2\" rx=\"1\"/></svg></button><h1><a href=\"/?{}\">EggPool</a></h1><nav class=\"topnav\">{}<button type=\"button\" class=\"topnav-refresh\" data-tooltip=\"Reload this page\" aria-label=\"Reload this page\" onclick=\"window.location.reload()\">↻</button></nav></header>\n<main id=\"dashboard-content\">\n{}\n</main>\n{footer}\n{}<script defer src=\"/static/dashboard.js\"></script>{}\n</body>\n</html>",
        html_escape(title),
        query_component(theme),
        chart_preload,
        query,
        navigation_markup,
        body,
        refresh_script,
        chart_script
    )
}

pub(in crate::server::dashboard) fn auto_refresh_script(refresh_interval_s: u64) -> String {
    let interval_ms = refresh_interval_s.max(1).saturating_mul(1000);
    format!(
        r#"<script>
(() => {{
  const intervalMs = {interval_ms};
  const content = document.getElementById("dashboard-content");
  const updated = document.getElementById("dashboard-updated");
  if (!content || !updated || !window.DOMParser) {{
    return;
  }}
  // The region is replaced wholesale on every tick, and the period/theme
  // selectors are rendered *inside* it (`dashboard_header`), so a tick that
  // lands while the operator is reading a control destroyed the focused node:
  // the native `<select>` popup closed under them and focus fell to `<body>`,
  // costing keyboard position once per `intervalMs`. Measured at a 3s
  // interval: the select was a different node after two ticks and
  // `document.activeElement` was `BODY`. Rather than try to restore focus
  // around a node swap — which cannot re-open a native popup anyway — hold the
  // tick while focus sits on an interactive control inside the region. The
  // data goes one tick stale for as long as the operator is mid-interaction,
  // which is the cheaper trade, and the footer says so.
  const INTERACTIVE = "a[href], button, input, select, textarea, summary, [tabindex]";
  const refresh = async () => {{
    const active = document.activeElement;
    if (active && content.contains(active) && active.matches(INTERACTIVE)) {{
      updated.textContent = "paused";
      return;
    }}
    try {{
      const response = await fetch(window.location.href, {{
        cache: "no-store",
        headers: {{"x-dashboard-refresh": "1"}},
      }});
      if (!response.ok) {{
        return;
      }}
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const next = doc.getElementById("dashboard-content");
      if (next) {{
        if (window.Chart && typeof window.Chart.getChart === "function") {{
          content.querySelectorAll("canvas").forEach((canvas) => {{
            const chart = window.Chart.getChart(canvas);
            if (chart) {{
              chart.destroy();
            }}
          }});
        }}
        const replacement = document.importNode(next, true);
        content.replaceChildren(...replacement.childNodes);
        updated.textContent = new Date().toLocaleTimeString();
        if (window.EggPoolDashboard) {{
          const dash = window.EggPoolDashboard;
          if (typeof dash.bootstrap === "function") {{
            dash.bootstrap();
          }} else {{
            if (typeof dash.initGroupedTimeseriesCharts === "function") {{
              dash.initGroupedTimeseriesCharts();
            }}
            if (typeof dash.reinitTimeseriesChart === "function") {{
              dash.reinitTimeseriesChart();
            }}
            if (typeof dash.initChartLoadingShells === "function") {{
              dash.initChartLoadingShells();
            }}
          }}
        }}
      }}
    }} catch (_err) {{
      updated.textContent = "stale";
    }}
  }};
  window.setInterval(refresh, intervalMs);
}})();
</script>"#
    )
}
