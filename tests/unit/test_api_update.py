"""Tests for GET /api/stats/update endpoint."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.api.update import register_update_routes
from eggpool.models.config import AppConfig
from eggpool.update_checker import UpdateChecker, UpdateInfo

if TYPE_CHECKING:
    import pytest


def _build_config(*, api_key: str | None = "test-key-12345678") -> AppConfig:
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
            "dashboard": {"enabled": False, "public": False},
        }
    )
    if api_key:
        config.server.api_key = api_key
    return config


@pytest_asyncio.fixture()
async def app_with_key() -> FastAPI:
    config = _build_config()
    app = FastAPI()
    app.state.config = config
    checker = UpdateChecker()
    checker._info = UpdateInfo(  # type: ignore[attr-defined]
        current_version="0.1.0",
        latest_version="0.2.0",
        update_available=True,
        install_method="pip",
        update_command="eggpool update",
        last_check_at=time.time(),
    )
    app.state.update_checker = checker
    register_update_routes(app)
    return app


@pytest_asyncio.fixture()
async def app_public_dashboard() -> FastAPI:
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
            "dashboard": {"enabled": False, "public": True},
        }
    )
    config.server.api_key = "test-key-12345678"
    app = FastAPI()
    app.state.config = config
    app.state.update_checker = UpdateChecker()
    register_update_routes(app, require_auth=False)
    return app


@pytest_asyncio.fixture()
async def app_no_checker() -> FastAPI:
    config = _build_config()
    app = FastAPI()
    app.state.config = config
    # No update_checker on state
    register_update_routes(app)
    return app


def test_endpoint_requires_auth_when_key_set(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get("/api/stats/update")
    assert response.status_code == 401


def test_endpoint_returns_snapshot_with_auth(
    app_with_key: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_TEST_KEY", "test-key-12345678")
    client = TestClient(app_with_key)
    response = client.get(
        "/api/stats/update",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["current_version"] == "0.1.0"
    assert data["latest_version"] == "0.2.0"
    assert data["update_available"] is True
    assert data["update_command"] == "eggpool update"


def test_endpoint_returns_empty_payload_without_checker(
    app_no_checker: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_TEST_KEY", "test-key-12345678")
    client = TestClient(app_no_checker)
    response = client.get(
        "/api/stats/update",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["update_available"] is False
    assert data["current_version"] == ""
    assert data["latest_version"] == ""


def test_endpoint_is_auth_gated_even_when_dashboard_public(
    app_public_dashboard: FastAPI,
) -> None:
    client = TestClient(app_public_dashboard)
    response = client.get("/api/stats/update")
    assert response.status_code == 401
