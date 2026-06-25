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
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/reliability")
    assert response.status_code == 200
    assert '<script defer src="/static/chart.js"></script>' in response.text
    assert "reliability-attempts-by-provider" in response.text


@pytest.mark.asyncio()
async def test_routing_route_loads(migrated_app: FastAPI) -> None:
    """The Routing page returns 200 and pulls in Chart.js."""
    from fastapi.testclient import TestClient

    client = TestClient(migrated_app)
    response = client.get("/routing")
    assert response.status_code == 200
    assert '<script defer src="/static/chart.js"></script>' in response.text
    assert "routing-exclusion-taxonomy" in response.text


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
    # Chart still polls /api/timeseries for updates every 60s.
    assert "/api/timeseries" in body
    # ``initialData`` must be present in the chart script so the chart
    # can render before any background fetch resolves.
    assert "initialData" in body


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
