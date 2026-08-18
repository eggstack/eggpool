"""Tests for the dashboard JSON API endpoints.

These tests verify:

- All dashboard JSON endpoints return the expected shape.
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


async def _get(app: FastAPI, path: str, *, authenticate: bool = True) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            path,
            headers=(
                {"Authorization": "Bearer test-key-12345678"} if authenticate else None
            ),
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


@pytest_asyncio.fixture()
async def app_public(db: Database) -> FastAPI:
    """Public dashboard (no auth)."""
    config = _build_config(api_key=None, public_dashboard=True)
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.stats = StatsService(db)
    app.state.runtime_metrics = _make_runtime_metrics(db, config)
    register_dashboard_routes(app, require_auth=False)
    return app


class _FakeCanonicalSegmentationDB:
    async def fetch_all(
        self, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        del params
        if "provider_id, upstream_protocol, segmentation_status" in sql:
            return [
                {
                    "provider_id": "prov-a",
                    "upstream_protocol": "openai",
                    "segmentation_status": "segmented",
                    "request_count": 4,
                },
                {
                    "provider_id": "prov-a",
                    "upstream_protocol": "openai",
                    "segmentation_status": "not_collected",
                    "request_count": 2,
                },
                {
                    "provider_id": "prov-b",
                    "upstream_protocol": "anthropic",
                    "segmentation_status": "empty_request",
                    "request_count": 1,
                },
                {
                    "provider_id": "prov-b",
                    "upstream_protocol": "anthropic",
                    "segmentation_status": "parse_failure",
                    "request_count": 3,
                },
            ]
        if "model_id, segmentation_status" in sql:
            return [
                {
                    "model_id": "gpt-4o",
                    "segmentation_status": "segmented",
                    "request_count": 4,
                    "total_stable_prefix_estimated_tokens": 12,
                    "total_volatile_estimated_tokens": 8,
                },
                {
                    "model_id": "gpt-4o",
                    "segmentation_status": "not_collected",
                    "request_count": 2,
                    "total_stable_prefix_estimated_tokens": 0,
                    "total_volatile_estimated_tokens": 0,
                },
                {
                    "model_id": "claude-3",
                    "segmentation_status": "empty_request",
                    "request_count": 1,
                    "total_stable_prefix_estimated_tokens": 0,
                    "total_volatile_estimated_tokens": 0,
                },
                {
                    "model_id": "unknown",
                    "segmentation_status": "parse_failure",
                    "request_count": 3,
                    "total_stable_prefix_estimated_tokens": 0,
                    "total_volatile_estimated_tokens": 0,
                },
            ]
        if "GROUP BY segmentation_status" in sql:
            return [
                {"segmentation_status": "segmented", "request_count": 4},
                {"segmentation_status": "not_collected", "request_count": 2},
                {"segmentation_status": "empty_request", "request_count": 1},
                {"segmentation_status": "parse_failure", "request_count": 3},
            ]
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetch_one(
        self, sql: str, params: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        del params
        if "total_stable_prefix_estimated_tokens" not in sql:
            raise AssertionError(f"unexpected SQL: {sql}")
        return {
            "total_stable_prefix_estimated_tokens": 12,
            "total_semi_stable_estimated_tokens": 4,
            "total_volatile_estimated_tokens": 8,
            "total_stable_prefix_bytes": 120,
            "total_semi_stable_bytes": 40,
            "total_volatile_bytes": 80,
            "compressible_candidate_requests": 2,
            "protected_requests": 3,
        }


class _FakeRequestShapingRuntimeMetrics:
    async def snapshot(self) -> dict[str, Any]:
        return {"routing_runtime": {"guardrails": {}}}


class _FakeRequestShapingStatsService:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def get_cache_observability(
        self, period: str | None = None
    ) -> dict[str, Any]:
        del period
        return {
            "by_status": {"reported": 0, "not_reported": 0, "unknown_format": 0},
            "total_cached_input_tokens": 0,
            "total_cache_read_input_tokens": 0,
            "total_cache_creation_input_tokens": 0,
        }

    async def get_canonical_request_segmentation(
        self, period: str | None = None
    ) -> dict[str, Any]:
        del period
        return {
            "total_requests": 6,
            "by_status": {
                "segmented": 3,
                "not_collected": 2,
                "empty_request": 1,
                "parse_failure": 0,
            },
            "per_provider_status": {
                ("prov-a", "openai"): {
                    "segmented": 3,
                    "not_collected": 2,
                    "empty_request": 1,
                    "parse_failure": 0,
                }
            },
            "per_model_status": {
                "gpt-4o": {
                    "segmented": 3,
                    "not_collected": 2,
                    "empty_request": 1,
                    "parse_failure": 0,
                    "total_requests": 6,
                    "stable_prefix_estimated_tokens": 0,
                    "volatile_estimated_tokens": 0,
                }
            },
            "token_totals": {
                "stable_prefix": 0,
                "semi_stable": 0,
                "volatile": 0,
                "all": 0,
            },
            "byte_totals": {
                "stable_prefix": 0,
                "semi_stable": 0,
                "volatile": 0,
                "all": 0,
            },
            "compressible_candidate_requests": 0,
            "protected_requests": 0,
        }

    async def get_compression_observability(
        self, period: str | None = None
    ) -> dict[str, Any]:
        del period
        return {"totals": {"observed_requests": 0}}

    async def get_compression_runtime(
        self, period: str | None = None
    ) -> dict[str, Any]:
        del period
        return {
            "mode_counts": {},
            "applied_count": 0,
            "failed_fallback_count": 0,
            "estimated_savings_tokens": 0,
            "actual_savings_tokens": 0,
            "latency_ms": {"p95": None},
            "warnings": {},
            "cache_safety": {},
        }

    async def get_compression_policy_stats(
        self, period: str | None = None
    ) -> dict[str, Any]:
        del period
        return {"policy_counts": []}

    async def get_cache_stability(self, period: str | None = None) -> dict[str, Any]:
        del period
        return {"transcoded_request_count": 0}


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------


class TestCacheObservabilityEndpoint:
    """GET /api/stats/cache-observability returns JSON."""

    @pytest.mark.asyncio()
    async def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/cache-observability")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        data = response.json()
        assert "by_status" in data
        assert "total_requests" in data

    @pytest.mark.asyncio()
    async def test_empty_db_has_stable_shape(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/cache-observability")
        data = response.json()
        assert data["total_requests"] == 0
        assert "by_status" in data


class TestCanonicalRequestSegmentationEndpoint:
    """GET /api/stats/canonical-request-segmentation returns JSON."""

    @pytest.mark.asyncio()
    async def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/canonical-request-segmentation")
        assert response.status_code == 200
        data = response.json()
        assert "by_status" in data
        assert "token_totals" in data

    @pytest.mark.asyncio()
    async def test_empty_db_has_stable_shape(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/canonical-request-segmentation")
        data = response.json()
        assert data["by_status"] == {
            "segmented": 0,
            "not_collected": 0,
            "empty_request": 0,
            "parse_failure": 0,
        }
        assert data["per_provider_status"] == {}
        assert data["per_model_status"] == {}

    @pytest.mark.asyncio()
    async def test_service_distinguishes_not_collected(
        self,
    ) -> None:
        from eggpool.stats import StatsService

        service = StatsService(_FakeCanonicalSegmentationDB())
        data = await service.get_canonical_request_segmentation("24h")
        assert data["total_requests"] == 10
        assert data["by_status"] == {
            "segmented": 4,
            "not_collected": 2,
            "empty_request": 1,
            "parse_failure": 3,
        }
        assert data["per_provider_status"][("prov-a", "openai")]["not_collected"] == 2
        assert data["per_model_status"]["gpt-4o"]["not_collected"] == 2

    @pytest.mark.asyncio()
    async def test_json_serializes_provider_status_keys(
        self, app_with_key: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replace the stats service on the app state with a mock
        app_with_key.state.stats = _FakeRequestShapingStatsService(
            app_with_key.state.db
        )
        app_with_key.state.runtime_metrics = _FakeRequestShapingRuntimeMetrics()
        response = await _get(app_with_key, "/api/stats/canonical-request-segmentation")
        data = response.json()
        assert data["by_status"]["not_collected"] == 2
        assert data["per_provider_status"]["prov-a->openai"]["not_collected"] == 2


class TestCacheStabilityEndpoint:
    """GET /api/stats/cache-stability returns JSON."""

    @pytest.mark.asyncio()
    async def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/cache-stability")
        assert response.status_code == 200
        data = response.json()
        assert "transcoded_request_count" in data
        assert "notes" in data

    @pytest.mark.asyncio()
    async def test_empty_db_returns_zero(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/cache-stability")
        data = response.json()
        assert data["transcoded_request_count"] == 0


class TestRequestShapingEndpoint:
    """GET /api/stats/request-shaping returns the aggregated summary JSON."""

    @pytest.mark.asyncio()
    async def test_returns_200_with_auth(self, app_with_key: FastAPI) -> None:
        response = await _get(app_with_key, "/api/stats/request-shaping")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == "24h"
        assert "mode" in data
        assert "cache" in data
        assert "guardrails" in data

    @pytest.mark.asyncio()
    async def test_summary_exposes_not_collected(
        self, app_with_key: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replace the stats service on the app state with a mock
        app_with_key.state.stats = _FakeRequestShapingStatsService(
            app_with_key.state.db
        )
        app_with_key.state.runtime_metrics = _FakeRequestShapingRuntimeMetrics()
        response = await _get(app_with_key, "/api/stats/request-shaping")
        data = response.json()
        assert data["segmentation"] == {
            "requests_segmented": 3,
            "requests_not_collected": 2,
            "requests_empty_request": 1,
            "requests_parse_failure": 0,
            "protected_requests": 0,
            "compressible_candidate_requests": 0,
        }


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
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    @pytest.mark.asyncio()
    async def test_endpoint_requires_auth(
        self, app_with_key: FastAPI, endpoint: str
    ) -> None:
        response = await _get(app_with_key, endpoint, authenticate=False)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/stats/cache-observability",
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    @pytest.mark.asyncio()
    async def test_endpoint_accepts_auth(
        self, app_with_key: FastAPI, endpoint: str
    ) -> None:
        response = await _get(app_with_key, endpoint)
        assert response.status_code == 200


class TestPublicDashboard:
    """Endpoints are accessible without auth when dashboard is public."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/stats/cache-observability",
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    @pytest.mark.asyncio()
    async def test_endpoint_accepts_no_auth_when_public(
        self, app_public: FastAPI, endpoint: str
    ) -> None:
        response = await _get(app_public, endpoint, authenticate=False)
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
            "/api/stats/cache-stability",
            "/api/stats/request-shaping",
        ],
    )
    @pytest.mark.asyncio()
    async def test_no_raw_prompt_leakage(
        self, app_with_key: FastAPI, endpoint: str
    ) -> None:
        response = await _get(app_with_key, endpoint)
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
