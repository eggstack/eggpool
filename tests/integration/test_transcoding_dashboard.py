"""Integration tests for the transcoding card on the runtime dashboard page.

Covers:
- ``render_runtime`` with/without ``transcoding_stats``
- ``/api/stats/transcoding`` JSON endpoint
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from starlette.testclient import TestClient

from eggpool.app import create_app
from eggpool.dashboard import render as render_module
from eggpool.dashboard.render import render_runtime
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.runtime_metrics import RuntimeMetricsService
from eggpool.stats import StatsService


def _build_config(tmp_path) -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": str(tmp_path / "transcode_it.sqlite3")},
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


@pytest.fixture(autouse=True)
def _enable_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_TEST_KEY", "test-dashboard-key")


@pytest_asyncio.fixture()
async def migrated_client(tmp_path):
    """A TestClient wired to a migrated DB for the /api/stats/transcoding endpoint."""
    config = _build_config(tmp_path)
    application = create_app(config)
    db = Database(path=config.database.path)
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    application.state.db = db
    application.state.stats_db = db
    application.state.stats = StatsService(db)
    application.state.runtime_metrics = RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=db,
        supervisor=None,
        task_monitor=None,
        router=None,
        health_manager=None,
        started_monotonic=0.0,
        started_epoch=0.0,
    )
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            yield client
    finally:
        await db.disconnect()
        render_module._THEME_CACHE.clear()
        render_module._THEME_CSS_CACHE.clear()
        render_module._THEMES_LIST_CACHE.clear()


def _minimal_snapshot() -> dict[str, Any]:
    return {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
    }


class TestTranscodingCardRendering:
    def test_card_present_when_stats_provided(self) -> None:
        stats = {
            "total": 50,
            "native_count": 40,
            "transcoded_count": 10,
            "per_direction": {("openai", "anthropic"): 7, ("anthropic", "openai"): 3},
        }
        html = render_runtime(_minimal_snapshot(), transcoding_stats=stats)
        assert "Transcoding (24h)" in html
        assert "Total requests" in html
        assert "Native" in html
        assert "Transcoded" in html
        assert "openai → anthropic" in html
        assert "anthropic → openai" in html

    def test_card_absent_when_no_stats(self) -> None:
        html = render_runtime(_minimal_snapshot(), transcoding_stats=None)
        assert "Transcoding (24h)" not in html

    def test_card_with_zero_transcoded(self) -> None:
        stats = {
            "total": 100,
            "native_count": 100,
            "transcoded_count": 0,
            "per_direction": {},
        }
        html = render_runtime(_minimal_snapshot(), transcoding_stats=stats)
        assert "Transcoding (24h)" in html
        assert "Direction" not in html

    def test_card_with_single_direction(self) -> None:
        stats = {
            "total": 25,
            "native_count": 20,
            "transcoded_count": 5,
            "per_direction": {("openai", "anthropic"): 5},
        }
        html = render_runtime(_minimal_snapshot(), transcoding_stats=stats)
        assert "openai → anthropic" in html
        assert "5" in html

    def test_direction_table_sorted_by_count(self) -> None:
        stats = {
            "total": 100,
            "native_count": 50,
            "transcoded_count": 50,
            "per_direction": {
                ("anthropic", "openai"): 40,
                ("openai", "anthropic"): 10,
            },
        }
        html = render_runtime(_minimal_snapshot(), transcoding_stats=stats)
        openai_idx = html.index("openai → anthropic")
        anthropic_idx = html.index("anthropic → openai")
        assert anthropic_idx < openai_idx


class TestTranscodingJsonEndpoint:
    def test_returns_empty_stats_on_empty_db(self, migrated_client: TestClient) -> None:
        response = migrated_client.get("/api/stats/transcoding")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["native_count"] == 0
        assert data["transcoded_count"] == 0
        assert data["per_direction"] == {}

    def test_respects_period_query_param(self, migrated_client: TestClient) -> None:
        response = migrated_client.get("/api/stats/transcoding?period=7d")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data

    def test_default_period_is_24h(self, migrated_client: TestClient) -> None:
        response = migrated_client.get("/api/stats/transcoding")
        assert response.status_code == 200
        assert response.json()["total"] == 0
