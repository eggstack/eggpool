use super::*;

const DASHBOARD_CSS: &[u8] = include_bytes!("../../../assets/dashboard/static/dashboard.css");
const DASHBOARD_JS: &[u8] = include_bytes!("../../../assets/dashboard/static/dashboard.js");
const CHART_JS: &[u8] = include_bytes!("../../../assets/dashboard/static/chart.umd.min.js");
const FAVICON_SVG: &[u8] = include_bytes!("../../../assets/dashboard/static/favicon.svg");

/// Thin Axum handlers: admission/auth/body limits are existing boundaries;
/// each handler invokes exactly one coordinator entry point and translates
/// its typed result to the established client surface. No routing, retry,
pub(in crate::server) async fn static_css() -> Response {
    static_response(DASHBOARD_CSS, "text/css", "public, max-age=300")
}

pub(in crate::server) async fn static_js() -> Response {
    static_response(
        DASHBOARD_JS,
        "application/javascript",
        "public, max-age=86400",
    )
}

pub(in crate::server) async fn static_chart_js() -> Response {
    static_response(CHART_JS, "application/javascript", "public, max-age=86400")
}

pub(in crate::server) async fn static_favicon() -> Response {
    static_response(FAVICON_SVG, "image/svg+xml", "public, max-age=86400")
}

/// Always serves a complete variable set. `dashboard.css` declares no custom
/// properties of its own, so an empty body here renders the whole page
/// unstyled — every `var()` goes invalid, panels lose their backgrounds and
/// the `.egg-background` watermark falls back to the SVG initial fill. Unknown
/// names resolve through `selected_theme` to `DEFAULT_THEME` for the same
/// reason; `theme=default` is a real alias for it (see `theme_bytes`).
pub(in crate::server) async fn theme_css(Query(query): Query<ThemeQuery>) -> Response {
    let requested = query.theme.as_deref().unwrap_or("default");
    let css = theme_variables(selected_theme(requested));
    static_response(css.as_bytes(), "text/css", "public, max-age=300")
}
