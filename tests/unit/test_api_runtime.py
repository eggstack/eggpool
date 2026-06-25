"""Tests for GET /api/stats/runtime endpoint."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.api.runtime import register_runtime_routes
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.runtime_metrics import RuntimeMetricsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest


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
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False, "public": public_dashboard},
        }
    )
    if api_key:
        config.server.api_key = api_key
    return config


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def app_with_key(db: Database) -> FastAPI:
    config = _build_config(api_key="test-key-12345678")
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.runtime_metrics = RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=db,
        supervisor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
    )
    register_runtime_routes(app)
    return app


@pytest_asyncio.fixture()
async def app_no_key(db: Database, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("OPENCODE_TEST_KEY", raising=False)
    config = _build_config(api_key=None)
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.runtime_metrics = RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=db,
        supervisor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
    )
    register_runtime_routes(app, require_auth=False)
    return app


@pytest_asyncio.fixture()
async def app_public_dashboard(db: Database) -> FastAPI:
    config = _build_config(api_key="test-key-12345678", public_dashboard=True)
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.runtime_metrics = RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=db,
        supervisor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
    )
    register_runtime_routes(app, require_auth=False)
    return app


# -- Auth gating -----------------------------------------------------------


def test_runtime_endpoint_requires_auth_when_key_set(
    app_with_key: FastAPI,
) -> None:
    """Runtime endpoint always requires auth, even with public dashboard."""
    client = TestClient(app_with_key)
    response = client.get("/api/stats/runtime")
    assert response.status_code == 401


def test_runtime_endpoint_requires_auth_even_when_dashboard_public(
    app_public_dashboard: FastAPI,
) -> None:
    """Runtime endpoint is always auth-gated regardless of dashboard.public."""
    client = TestClient(app_public_dashboard)
    response = client.get("/api/stats/runtime")
    assert response.status_code == 401


def test_runtime_endpoint_no_key_configured_allows_unauthenticated(
    app_no_key: FastAPI,
) -> None:
    """When no API key is configured, auth is disabled and endpoint returns 200."""
    client = TestClient(app_no_key)
    response = client.get("/api/stats/runtime")
    assert response.status_code == 200


# -- Authenticated call returns stable top-level keys ----------------------


def test_authenticated_call_returns_200_with_valid_key(
    app_with_key: FastAPI,
) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/stats/runtime",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.status_code == 200


def test_authenticated_call_returns_stable_top_level_keys(
    app_with_key: FastAPI,
) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/stats/runtime",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "server" in data
    assert "memory" in data
    assert "processes" in data
    assert "background_tasks" in data
    assert "db" in data
    assert "routing_runtime" in data
    assert "probe_errors" in data


def test_endpoint_returns_json_response(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/stats/runtime",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


# -- Partial data on probe failure -----------------------------------------


def test_endpoint_returns_200_with_partial_data_on_probe_failure(
    app_with_key: FastAPI,
) -> None:
    """Even if a probe returns partial data, the endpoint returns 200."""
    # Mock _snapshot_memory to return partial data (some fields None)
    with patch.object(
        app_with_key.state.runtime_metrics,
        "_snapshot_memory",
        return_value={
            "rss_bytes": None,
            "vms_bytes": None,
            "open_fd_count": None,
            "thread_count": None,
        },
    ):
        client = TestClient(app_with_key)
        response = client.get(
            "/api/stats/runtime",
            headers={"Authorization": "Bearer test-key-12345678"},
        )
        assert response.status_code == 200
        data = response.json()
        # Memory fields should be present but None
        assert data["memory"]["rss_bytes"] is None
        assert data["memory"]["thread_count"] is None


# -- register_runtime_routes always attaches auth dependency ----------------


def test_register_runtime_routes_ignores_require_auth_param(
    app_with_key: FastAPI,
) -> None:
    """require_auth=False does not skip the auth dependency."""
    client = TestClient(app_with_key)
    response = client.get("/api/stats/runtime")
    assert response.status_code == 401
