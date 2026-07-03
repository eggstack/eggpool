"""Tests for the Phase 7 dashboard JSON API endpoints.

These tests verify:

- All six Phase 7 endpoints return JSON 200 with the correct shape.
- Auth gating works (rejects unauthenticated requests when API key set).
- Empty DB returns the stable zero shape (no crashes).
- Invalid period strings return 400, not 500.
- Bad window parameters return 400 (if implemented) or 200 with
  empty data — never server errors.
- JSON responses do not leak raw upstream content.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.dashboard.routes import register_dashboard_routes
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.runtime_metrics import RuntimeMetricsService

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
    database = Database(path=str(tmp_path / "phase7_api.sqlite3"))
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
    app.state.runtime_metrics = _make_runtime_metrics(db, config)
    register_dashboard_routes(app, require_auth=True)
    return app


@pytest_asyncio.fixture()
async def app_public(db: Database) -> FastAPI:
    """Public dashboard (no auth)."""
    config = _build_config(api_key=None, public_dashboard=True)
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.runtime_metrics = _make_runtime_metrics(db, config)
    register_dashboard_routes(app, require_auth=False)
    return app


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------


class TestCacheObservabilityEndpoint:
    """GET /api/stats/cache-observability returns JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/cache-observability",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        data = response.json()
        assert "by_status" in data
        assert "total_requests" in data

    def test_empty_db_has_stable_shape(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/cache-observability",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        data = response.json()
        assert data["total_requests"] == 0
        assert "by_status" in data


class TestCanonicalRequestSegmentationEndpoint:
    """GET /api/stats/canonical-request-segmentation returns JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/canonical-request-segmentation",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "by_status" in data
        assert "token_totals" in data


class TestCompressionObservabilityEndpoint:
    """GET /api/stats/compression-observability returns JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/compression-observability",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "by_status" in data
        assert "totals" in data
        assert "by_policy" in data


class TestCompressionRuntimeEndpoint:
    """GET /api/stats/compression-runtime returns JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/compression-runtime",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "window" in data
        assert "mode_counts" in data
        assert "applied_count" in data
        assert "latency_ms" in data
        assert "transforms" in data
        assert "warnings" in data
        assert "cache_safety" in data

    def test_empty_db_returns_stable_shape(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/compression-runtime",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        data = response.json()
        assert data["window"]["request_count"] == 0
        assert data["applied_count"] == 0
        assert data["mode_counts"] == {
            "disabled": 0,
            "observe": 0,
            "safe": 0,
        }
        assert data["latency_ms"] == {
            "avg": None,
            "p50": None,
            "p95": None,
            "max": None,
        }


class TestCompressionPolicyStatsEndpoint:
    """GET /api/stats/compression-policies returns JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/compression-policies",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "policy_counts" in data
        assert "total_requests" in data
        assert "total_policies" in data


class TestCacheStabilityEndpoint:
    """GET /api/stats/cache-stability returns JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/cache-stability",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "transcoded_request_count" in data
        assert "notes" in data

    def test_empty_db_returns_zero(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/cache-stability",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        data = response.json()
        assert data["transcoded_request_count"] == 0


class TestRequestShapingEndpoint:
    """GET /api/stats/request-shaping returns the aggregated summary JSON."""

    def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/request-shaping",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == "24h"
        assert "mode" in data
        assert "compression" in data
        assert "cache" in data
        assert "synthetic_cache" in data
        assert "guardrails" in data


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


class TestAuthGating:
    """Endpoints reject unauthenticated requests when API key is set."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/stats/cache-observability",
            "/api/stats/canonical-request-segmentation",
            "/api/stats/compression-observability",
            "/api/stats/compression-runtime",
            "/api/stats/compression-policies",
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    def test_endpoint_requires_auth(self, app_with_key: FastAPI, endpoint: str) -> None:
        client = TestClient(app_with_key)
        response = client.get(endpoint)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/stats/cache-observability",
            "/api/stats/compression-runtime",
            "/api/stats/compression-policies",
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    def test_endpoint_accepts_auth(self, app_with_key: FastAPI, endpoint: str) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            endpoint,
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200


class TestPublicDashboard:
    """Endpoints are accessible without auth when dashboard is public."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/stats/cache-observability",
            "/api/stats/compression-runtime",
            "/api/stats/compression-policies",
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    def test_endpoint_accepts_no_auth_when_public(
        self, app_public: FastAPI, endpoint: str
    ) -> None:
        client = TestClient(app_public)
        response = client.get(endpoint)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# JSON safety
# ---------------------------------------------------------------------------


class TestJsonSafety:
    """JSON responses never leak raw upstream content."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/stats/cache-observability",
            "/api/stats/canonical-request-segmentation",
            "/api/stats/compression-observability",
            "/api/stats/compression-runtime",
            "/api/stats/compression-policies",
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    def test_no_raw_prompt_leakage(self, app_with_key: FastAPI, endpoint: str) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            endpoint,
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        body = response.text
        forbidden = ["sk-", "Bearer ", "<tool_use", "<tool_result"]
        for needle in forbidden:
            assert needle not in body, (
                f"Forbidden substring {needle!r} leaked into {endpoint}"
            )


# ---------------------------------------------------------------------------
# Period parameter
# ---------------------------------------------------------------------------


class TestPeriodParameter:
    """Period parameter is accepted; bad values do not crash."""

    @pytest.mark.parametrize(
        "period",
        ["1h", "24h", "7d", "30d"],
    )
    def test_compression_runtime_accepts_preset(
        self, app_with_key: FastAPI, period: str
    ) -> None:
        client = TestClient(app_with_key)
        response = client.get(
            f"/api/stats/compression-runtime?period={period}",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200

    def test_compression_runtime_handles_unknown_period(
        self, app_with_key: FastAPI
    ) -> None:
        """Unknown period strings return 200 with empty data, not 500."""
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/compression-runtime?period=garbage",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        # 200 with empty/stable data; we don't 500 on bad input.
        assert response.status_code == 200
