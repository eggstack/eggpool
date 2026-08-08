"""Tests for the synthetic cache observability API endpoint."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from eggpool.dashboard.routes import register_dashboard_routes
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.runtime_metrics import RuntimeMetricsService
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _build_config(
    *,
    api_key: str | None = "test-key-12345678",
    public_dashboard: bool = False,
) -> AppConfig:
    config = AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"},
            ],
            "dashboard": {"enabled": True, "public": public_dashboard},
        }
    )
    if api_key:
        config.server.api_key = api_key
    return config


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "sc_routes_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


def _make_runtime_metrics(db: Database, config: AppConfig) -> RuntimeMetricsService:
    return RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=db,
        supervisor=None,
        task_monitor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
    )


@pytest_asyncio.fixture()
async def app_with_key(db: Database, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("OPENCODE_TEST_KEY", "test-key-12345678")
    config = _build_config(api_key="test-key-12345678")
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.stats = StatsService(db)
    app.state.runtime_metrics = _make_runtime_metrics(db, config)
    register_dashboard_routes(app, require_auth=True)
    return app


@pytest.mark.asyncio
class TestSyntheticCacheObservabilityEndpoint:
    """GET /api/stats/synthetic-cache-observability returns expected shape."""

    async def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        """The endpoint returns 200 with valid JSON shape."""
        transport = httpx.ASGITransport(app=app_with_key)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/stats/synthetic-cache-observability",
                headers={"Authorization": "Bearer test-key-12345678"},
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        data = response.json()
        assert "total_requests" in data
        assert "status_counts" in data
        assert "by_policy" in data
        assert "routing_separation_notice" in data
        assert isinstance(data["status_counts"], dict)
        assert isinstance(data["by_policy"], list)

    async def test_empty_db_has_stable_shape(self, app_with_key: FastAPI) -> None:
        """An empty DB returns the full response shape with zeros."""
        transport = httpx.ASGITransport(app=app_with_key)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/stats/synthetic-cache-observability",
                headers={"Authorization": "Bearer test-key-12345678"},
            )
        data = response.json()
        assert data["total_requests"] == 0
        assert "status_counts" in data
        assert "by_policy" in data

    async def test_routing_separation_notice_content(
        self, app_with_key: FastAPI
    ) -> None:
        """The routing-separation notice is always present."""
        transport = httpx.ASGITransport(app=app_with_key)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/stats/synthetic-cache-observability",
                headers={"Authorization": "Bearer test-key-12345678"},
            )
        data = response.json()
        notice = data["routing_separation_notice"]
        assert "QuotaFairScorer" in notice
        assert "reporting only" in notice.lower() or "Reporting only" in notice

    async def test_no_raw_content_in_response(self, app_with_key: FastAPI) -> None:
        """No raw prompts or auth headers leak into the JSON response."""
        transport = httpx.ASGITransport(app=app_with_key)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/stats/synthetic-cache-observability",
                headers={"Authorization": "Bearer test-key-12345678"},
            )
        import json

        body = json.dumps(response.json())
        forbidden = ["sk-", "Bearer ", "system prompt", "<tool_use", "<tool_result>"]
        for needle in forbidden:
            assert needle not in body, (
                f"Forbidden substring {needle!r} leaked into JSON response"
            )
