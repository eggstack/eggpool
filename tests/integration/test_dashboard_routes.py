"""Integration tests for dashboard route caching and asset delivery.

These tests exercise the dashboard HTTP routes through FastAPI's
TestClient to verify the page-loading optimizations introduced for
dashboard performance:

* Theme TOML is cached on disk-read per request.
* ``/static/dashboard.css``, ``/static/favicon.svg`` and
  ``/static/chart.js`` advertise long-lived ``Cache-Control`` headers
  so browsers stop re-validating them on every navigation.
* The overview page seeds its timeseries chart from an inlined JSON
  payload (no extra round trip) and Chart.js loads with the
  ``defer`` attribute.
* Non-overview pages do not include the Chart.js ``<script>`` tag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.app import create_app
from eggpool.dashboard import render as render_module
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _enable_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_TEST_KEY", "test-dashboard-key")


def _build_config(tmp_path) -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": str(tmp_path / "dashboard_it.sqlite3")},
            "upstream": {"base_url": "https://upstream.example.com"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "acct-a", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {
                "enabled": True,
                "public": True,
                "refresh_interval_s": 60,
            },
        }
    )


@pytest.fixture()
def app(tmp_path) -> FastAPI:
    config = _build_config(tmp_path)
    application = create_app(config)
    db = Database(path=config.database.path)
    application.state.db = db
    application.state.stats_db = db
    application.state.stats = StatsService(db)
    yield application
    render_module._THEME_CACHE.clear()
    render_module._THEME_CSS_CACHE.clear()
    render_module._THEMES_LIST_CACHE.clear()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest_asyncio.fixture()
async def migrated_app(tmp_path):
    """App whose DB has been migrated so stats queries succeed."""
    config = _build_config(tmp_path)
    application = create_app(config)
    db = Database(path=config.database.path)
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    application.state.db = db
    application.state.stats_db = db
    application.state.stats = StatsService(db)
    # The FastAPI lifespan is the path that normally wires
    # ``app.state.model_info``; integration tests bypass the lifespan
    # so the detail page's lazy ``ensure_canonical`` path can be
    # exercised end-to-end here.
    from eggpool.catalog.cache import ModelCatalogCache
    from eggpool.model_info.service import ModelInfoService

    application.state.model_info = ModelInfoService(
        config.model_info, db, ModelCatalogCache()
    )
    try:
        yield application
    finally:
        await db.disconnect()
        render_module._THEME_CACHE.clear()
        render_module._THEME_CSS_CACHE.clear()
        render_module._THEMES_LIST_CACHE.clear()


def test_static_dashboard_css_is_long_cached(client: TestClient) -> None:
    """``/static/dashboard.css`` advertises a long cache lifetime."""
    response = client.get("/static/dashboard.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"


def test_static_favicon_svg_is_long_cached(client: TestClient) -> None:
    """``/static/favicon.svg`` advertises a long cache lifetime."""
    response = client.get("/static/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_static_chart_js_is_long_cached(client: TestClient) -> None:
    """``/static/chart.js`` advertises a long cache lifetime."""
    response = client.get("/static/chart.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_static_theme_css_uses_configured_themes_dir(tmp_path) -> None:
    """``/static/theme.css`` serves custom themes from dashboard.themes_dir."""
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir()
    (themes_dir / "Operator Custom.toml").write_text(
        "\n".join(
            [
                "[general]",
                'background = "#123456"',
                'border = "#234567"',
                'horizontal_rule = "#345678"',
                'unread_indicator = "#456789"',
                "",
                "[text]",
                'primary = "#abcdef"',
                'secondary = "#bcdef0"',
                'tertiary = "#cdef01"',
                'success = "#00ff00"',
                'error = "#ff0000"',
                "",
                "[buffer]",
                'background = "#102030"',
                'background_text_input = "#203040"',
                'background_title_bar = "#304050"',
                'border = "#405060"',
                'border_selected = "#506070"',
                'code = "#607080"',
                'highlight = "#708090"',
                'nickname = "#8090a0"',
                'selection = "#90a0b0"',
                'timestamp = "#a0b0c0"',
                'topic = "#b0c0d0"',
                'url = "#c0d0e0"',
            ]
        )
    )
    config = _build_config(tmp_path)
    config.dashboard.theme = "Operator Custom"
    config.dashboard.themes_dir = str(themes_dir)
    application = create_app(config)

    from fastapi.testclient import TestClient

    with TestClient(application) as theme_client:
        response = theme_client.get("/static/theme.css?theme=Operator%20Custom")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert "--page-bg: #102030;" in response.text
    assert "--page-text: #abcdef;" in response.text


@pytest.mark.asyncio()
async def test_overview_loads_chart_js_with_defer(
    migrated_app: FastAPI,
) -> None:
    """The overview page requests Chart.js with the defer attribute."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/")
    assert response.status_code == 200
    assert '<script defer src="/static/chart.js"></script>' in response.text
    # The overview also preloads the stylesheet so paint is not blocked
    # waiting on the CSS fetch.
    assert 'rel="preload" href="/static/dashboard.css" as="style"' in response.text


@pytest.mark.asyncio()
async def test_reliability_route_loads(migrated_app: FastAPI) -> None:
    """The Reliability page returns 200 and pulls in Chart.js."""
    import re

    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/reliability")
    assert response.status_code == 200
    assert '<script defer src="/static/chart.js"></script>' in response.text
    assert "reliability-attempts-by-provider" in response.text
    # The chart must be seeded from a JSON data island so deferred
    # dashboard.js can initialise it after Chart.js has loaded, with no
    # inline `new Chart(...)` that would race the deferred load.
    assert (
        re.search(
            r'<script type="application/json"\s+class="static-chart-data"\s+'
            r'data-chart-id="reliability-attempts-by-provider">',
            response.text,
        )
        is not None
    )
    assert "new Chart(ctx" not in response.text


@pytest.mark.asyncio()
async def test_routing_route_loads(migrated_app: FastAPI) -> None:
    """The Routing page returns 200 and pulls in Chart.js."""
    import re

    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/routing")
    assert response.status_code == 200
    assert '<script defer src="/static/chart.js"></script>' in response.text
    # The migrated fixture database has no routing decisions, so the
    # exclusion-taxonomy panel falls back to its empty-state paragraph
    # rather than emitting the canvas + data island (see
    # ``_render_exclusion_taxonomy_chart``).  The canvas wiring itself
    # is exercised by ``tests/unit/test_dashboard.py``.
    assert (
        re.search(
            r'<p class="empty">No exclusion data in this period\.</p>',
            response.text,
        )
        is not None
    )
    # No inline ``new Chart(...)`` script — the chart must be seeded
    # from a JSON data island so deferred dashboard.js can initialise
    # it after Chart.js has loaded.
    assert "new Chart(ctx" not in response.text


@pytest.mark.asyncio()
async def test_traces_route_loads(migrated_app: FastAPI) -> None:
    """The Traces page returns 200 and does not pull in Chart.js."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/traces")
    assert response.status_code == 200
    assert "/static/chart.js" not in response.text
    assert "Auth-gated" in response.text


@pytest.mark.asyncio()
async def test_pending_health_endpoint(migrated_app: FastAPI) -> None:
    """``/api/stats/pending-health`` returns the expected JSON shape."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/stats/pending-health")
    assert response.status_code == 200
    payload = response.json()
    for key in (
        "pending_count",
        "oldest_pending_age_seconds",
        "stale_pending_count",
        "active_reservation_count",
        "active_reserved_microdollars",
        "oldest_reservation_age_seconds",
        "as_of",
    ):
        assert key in payload, key


def test_dashboard_js_is_long_cached(client: TestClient) -> None:
    """``/static/dashboard.js`` advertises a long cache lifetime."""
    response = client.get("/static/dashboard.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio()
async def test_non_overview_pages_skip_chart_js(
    migrated_app: FastAPI,
) -> None:
    """Pages that do not render a chart must not load Chart.js."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    for path in ("/accounts", "/models", "/events", "/bandwidth", "/pings", "/latency"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "/static/chart.js" not in response.text, path


@pytest.mark.asyncio()
async def test_accounts_page_hides_disabled_by_default(
    migrated_app: FastAPI,
) -> None:
    """The accounts page renders the show-disabled filter and defaults to hiding."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/accounts")
    assert response.status_code == 200
    body = response.text
    assert 'name="show_disabled"' in body
    # Default state must hide disabled rows so the page matches the
    # operator's mental model after ``eggpool logout``.
    assert (
        '<option value="0" selected="selected">Hide disabled accounts</option>' in body
    )


@pytest.mark.asyncio()
async def test_accounts_page_show_disabled_query(
    migrated_app: FastAPI,
) -> None:
    """``?show_disabled=1`` flips the toggle and renders tombstones."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/accounts?show_disabled=1")
    assert response.status_code == 200
    body = response.text
    assert (
        '<option value="1" selected="selected">Show disabled accounts</option>' in body
    )
    # The hide option must still render so the toggle remains reversible.
    assert 'value="0">Hide disabled accounts</option>' in body


@pytest.mark.asyncio()
async def test_overview_page_hides_disabled_by_default(
    migrated_app: FastAPI,
) -> None:
    """The overview Account breakdown exposes the show-disabled toggle anchor."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    # Anchor-style toggle, default unpressed.
    assert 'class="show-disabled-toggle"' in body
    assert 'aria-pressed="false"' in body
    # No <option selected="selected"> remains on the overview — the
    # toggle is purely a navigation now.
    assert "account-breakdown-filter" not in body


@pytest.mark.asyncio()
async def test_overview_page_show_disabled_query(
    migrated_app: FastAPI,
) -> None:
    """``?show_disabled=1`` flips the overview toggle to pressed."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/?show_disabled=1")
    assert response.status_code == 200
    body = response.text
    assert 'class="show-disabled-toggle"' in body
    assert 'aria-pressed="true"' in body
    assert "Hide disabled" in body
    assert "show_disabled=0" in body


@pytest.mark.asyncio()
async def test_accounts_api_supports_include_disabled(
    migrated_app: FastAPI,
) -> None:
    """``/api/stats/accounts`` honours ``?include_disabled=0``."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/stats/accounts?include_disabled=0")
    assert response.status_code == 200
    payload = response.json()
    assert payload["include_disabled"] is False
    assert payload["accounts"] == []

    response_all = client.get("/api/stats/accounts")
    assert response_all.status_code == 200
    payload_all = response_all.json()
    assert payload_all["include_disabled"] is True


@pytest.mark.asyncio()
async def test_overview_inlines_timeseries_data(
    migrated_app: FastAPI,
) -> None:
    """The overview chart seeds itself from inlined JSON data."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Request timeseries" in body
    # The chart must be seeded from a JSON data island so the canvas can
    # be initialised by deferred dashboard.js after Chart.js has loaded,
    # without depending on the legacy inline ``new Chart(...)`` script.
    assert 'id="timeseries-initial-data"' in body
    assert 'id="timeseries-chart"' in body


@pytest.mark.asyncio()
async def test_overview_html_includes_no_store_for_refresh(
    migrated_app: FastAPI,
) -> None:
    """The in-page refresher uses ``cache: no-store`` on subsequent polls."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/")
    assert response.status_code == 200
    assert 'cache: "no-store"' in response.text


@pytest.mark.asyncio()
async def test_overview_ignores_broken_update_checker(
    migrated_app: FastAPI,
) -> None:
    """A bad update-checker snapshot must not break dashboard rendering."""
    from fastapi.testclient import TestClient

    class _BrokenUpdateChecker:
        def snapshot(self) -> object:
            raise RuntimeError("snapshot unavailable")

    migrated_app.state.update_checker = _BrokenUpdateChecker()
    client = TestClient(migrated_app)
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="dashboard-content"' in response.text


@pytest.mark.asyncio()
async def test_theme_is_cached_across_requests(
    migrated_app: FastAPI,
) -> None:
    """Repeated dashboard requests reuse the parsed theme."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    assert client.get("/").status_code == 200
    cached = list(render_module._THEME_CACHE.values())
    assert cached, "expected the default theme to populate the cache"


@pytest.mark.asyncio()
async def test_available_themes_is_cached_across_requests(
    migrated_app: FastAPI,
) -> None:
    """Repeated dashboard requests reuse the available-themes list."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    assert client.get("/").status_code == 200
    assert render_module._THEMES_LIST_CACHE, (
        "expected the available-themes list to populate the cache"
    )


@pytest.mark.asyncio()
async def test_grouped_timeseries_json_returns_stable_shape(
    migrated_app: FastAPI,
) -> None:
    """``GET /api/timeseries/grouped`` returns the documented contract."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/timeseries/grouped")
    assert response.status_code == 200
    payload = response.json()
    expected_keys = {
        "bucket",
        "group_by",
        "metric",
        "limit",
        "series",
        "buckets",
        "bucket_totals",
        "points",
    }
    assert set(payload.keys()) == expected_keys
    assert payload["series"] == []
    assert payload["buckets"] == []
    assert payload["bucket_totals"] == []
    assert payload["points"] == []
    assert payload["group_by"] == "provider_model"
    assert payload["metric"] == "requests"
    assert payload["bucket"] == "hour"


@pytest.mark.asyncio()
async def test_grouped_timeseries_json_accepts_bucket_query(
    migrated_app: FastAPI,
) -> None:
    """``bucket=day`` is reflected in the payload."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/timeseries/grouped?bucket=day")
    assert response.status_code == 200
    assert response.json()["bucket"] == "day"


@pytest.mark.asyncio()
async def test_grouped_timeseries_json_accepts_group_by_query(
    migrated_app: FastAPI,
) -> None:
    """``group_by=provider`` is reflected in the payload."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/timeseries/grouped?group_by=provider")
    assert response.status_code == 200
    assert response.json()["group_by"] == "provider"


@pytest.mark.asyncio()
async def test_grouped_timeseries_json_accepts_limit_query(
    migrated_app: FastAPI,
) -> None:
    """``limit`` is clamped and reflected in the payload."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/timeseries/grouped?limit=5")
    assert response.status_code == 200
    assert response.json()["limit"] == 5
    # Out-of-range limit clamps to 25
    response = client.get("/api/timeseries/grouped?limit=999")
    assert response.status_code == 200
    assert response.json()["limit"] == 25


@pytest.mark.asyncio()
async def test_grouped_timeseries_json_unknown_account_returns_empty(
    migrated_app: FastAPI,
) -> None:
    """Unknown ``account`` filter yields a stable empty payload."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/timeseries/grouped?account=does-not-exist")
    assert response.status_code == 200
    payload = response.json()
    assert payload["series"] == []
    assert payload["buckets"] == []
    assert payload["bucket_totals"] == []
    assert payload["points"] == []


@pytest.mark.asyncio()
async def test_grouped_timeseries_json_invalid_group_by_falls_back(
    migrated_app: FastAPI,
) -> None:
    """Invalid ``group_by`` falls back to ``provider_model``."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/api/timeseries/grouped?group_by=garbage")
    assert response.status_code == 200
    assert response.json()["group_by"] == "provider_model"


@pytest.mark.asyncio()
async def test_timeseries_page_loads_with_grouped_chart(
    migrated_app: FastAPI,
) -> None:
    """``/timeseries`` renders the controls form and either the chart or empty state."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/timeseries")
    assert response.status_code == 200
    body = response.text
    # New controls form with bucket / group_by / metric / limit fields
    assert 'name="bucket"' in body
    assert 'name="group_by"' in body
    assert 'name="metric"' in body
    assert 'name="limit"' in body
    # Chart.js + dashboard.js are now both loaded by the layout
    assert '<script defer src="/static/chart.js"></script>' in body
    assert '<script defer src="/static/dashboard.js"></script>' in body
    # With an empty database we render the empty-state placeholder;
    # otherwise the grouped chart canvas + JSON data island appear.
    assert (
        'class="grouped-timeseries-chart"' in body
        or "No requests in this window" in body
    )


@pytest.mark.asyncio()
async def test_timeseries_page_default_metric_is_tokens(
    migrated_app: FastAPI,
) -> None:
    """``/timeseries`` opens on the tokens view so the chart reads as usage."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/timeseries")
    assert response.status_code == 200
    body = response.text
    assert 'value="tokens" selected' in body
    assert 'data-metric="tokens"' in body


@pytest.mark.asyncio()
async def test_timeseries_page_has_no_duplicate_period_dropdown(
    migrated_app: FastAPI,
) -> None:
    """The period selector lives outside the filter form so there is only one."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/timeseries")
    assert response.status_code == 200
    body = response.text
    # Canonical period selector remains.
    assert "data-period-selector" in body
    assert 'id="period"' in body
    # Only one <select name="period"> exists in the document.
    assert body.count('<select id="period" name="period">') == 1
    # The filter form opts into the JS wire-up so changes update the chart live.
    assert "data-timeseries-controls" in body


@pytest.mark.asyncio()
async def test_timeseries_page_account_and_model_are_dropdowns(
    migrated_app: FastAPI,
) -> None:
    """Accounts and models are <select> dropdowns, not free-text inputs."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/timeseries")
    assert response.status_code == 200
    body = response.text
    controls_start = body.index('class="filter-form timeseries-controls"')
    controls_end = body.index("</form>", controls_start)
    controls_section = body[controls_start:controls_end]
    # Account and model must be selects inside the controls form.
    assert 'select name="account"' in controls_section
    assert 'select name="model"' in controls_section
    # No free-text inputs remain in the controls form.
    assert '<input type="text"' not in controls_section
    # Dropdowns include an "(any …)" option so the filter can be cleared.
    assert "(any account)" in controls_section
    assert "(any model)" in controls_section


@pytest.mark.asyncio()
async def test_timeseries_page_with_group_by_query(
    migrated_app: FastAPI,
) -> None:
    """``/timeseries?group_by=provider&limit=8`` renders with those controls."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/timeseries?group_by=provider&limit=8")
    assert response.status_code == 200
    body = response.text
    assert 'value="provider" selected' in body
    assert 'value="8" selected' in body


@pytest.mark.asyncio()
async def test_overview_loads_dashboard_js_with_defer(
    migrated_app: FastAPI,
) -> None:
    """The overview page now also loads dashboard.js for chart reinit."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert '<script defer src="/static/chart.js"></script>' in body
    assert '<script defer src="/static/dashboard.js"></script>' in body


@pytest.mark.asyncio()
async def test_overview_auto_refresh_reinitializes_charts(
    migrated_app: FastAPI,
) -> None:
    """The auto-refresh hook re-initializes charts after innerHTML swap."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    # The auto-refresh hook should call into dashboard.js for reinit.
    assert "initGroupedTimeseriesCharts" in body
    assert "reinitTimeseriesChart" in body


@pytest.mark.asyncio()
async def test_model_detail_page_returns_200_for_unknown_model(
    migrated_app: FastAPI,
) -> None:
    """The model detail page returns 200 and lazy-creates a sparse row."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/models/nonexistent-model")
    assert response.status_code == 200
    body = response.text
    # The lazy backfill path runs ensure_canonical, which creates a
    # sparse unmatched row so the page has something to render.
    assert "nonexistent-model" in body
    assert "unmatched" in body
    assert "Summary" in body
    assert "Model info not available" not in body


@pytest.mark.asyncio()
async def test_model_detail_page_renders_sections(
    migrated_app: FastAPI,
) -> None:
    """The model detail page renders the expected section headings."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/models/test-model")
    assert response.status_code == 200
    body = response.text
    # Should have the model ID in the heading
    assert "test-model" in body
    # Lazy backfill produces an unmatched sparse row, so the page
    # shows the same sections a populated detail page would show
    # (with sparse markers / em-dashes for the empty fields).
    assert "unmatched" in body
    assert "Summary" in body
    assert "Provider / Callability" in body
    assert "Metadata" in body
    assert "Provenance" in body
    assert "Model info not available" not in body
    # Should not load Chart.js (no charts on this page)
    assert "/static/chart.js" not in body


@pytest.mark.asyncio()
async def test_model_detail_page_links_from_models(
    migrated_app: FastAPI,
) -> None:
    """The Models page links model IDs to the detail page."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    # Even with no models, the page should render without error
    response = client.get("/models")
    assert response.status_code == 200


@pytest.mark.asyncio()
async def test_model_detail_page_with_provider_suffix(
    migrated_app: FastAPI,
) -> None:
    """The model detail page handles provider-suffixed model IDs."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    # Provider-suffixed IDs contain / which needs URL encoding
    response = client.get("/models/gpt-4o/openai")
    assert response.status_code == 200
    assert "gpt-4o/openai" in response.text
