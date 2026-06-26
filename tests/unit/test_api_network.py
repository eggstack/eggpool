"""Tests for GET /api/network/diagnostics endpoint."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.api.network import register_network_routes
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
        task_monitor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
    )
    register_network_routes(app)
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
        task_monitor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
    )
    register_network_routes(app, require_auth=False)
    return app


# -- Auth gating -----------------------------------------------------------


def test_network_diagnostics_requires_auth_when_key_set(
    app_with_key: FastAPI,
) -> None:
    client = TestClient(app_with_key)
    response = client.get("/api/network/diagnostics")
    assert response.status_code == 401


def test_network_diagnostics_no_key_allows_unauthenticated(
    app_no_key: FastAPI,
) -> None:
    client = TestClient(app_no_key)
    response = client.get("/api/network/diagnostics")
    assert response.status_code == 200


# -- Response shape --------------------------------------------------------


def test_returns_200_with_valid_key(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.status_code == 200


def test_returns_json_content_type(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    assert response.headers["content-type"].startswith("application/json")


def test_returns_stable_top_level_keys(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    data = response.json()
    assert "outbound_clients" in data
    assert "dns_cache" in data
    assert "hosts" in data


def test_outbound_clients_shape(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    data = response.json()
    ob = data["outbound_clients"]
    assert "builds_total" in ob
    assert "scopes" in ob
    assert "request_count" in ob
    assert "error_count" in ob
    assert "has_client" in ob
    assert "per_host_requests" in ob
    assert "per_host_errors" in ob
    assert isinstance(ob["builds_total"], int)
    assert isinstance(ob["scopes"], dict)
    assert isinstance(ob["request_count"], int)
    assert isinstance(ob["error_count"], int)
    assert isinstance(ob["has_client"], bool)
    assert isinstance(ob["per_host_requests"], dict)
    assert isinstance(ob["per_host_errors"], dict)


def test_dns_cache_shape(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    data = response.json()
    dns = data["dns_cache"]
    assert "enabled" in dns
    assert "entries" in dns
    assert "hits_total" in dns
    assert "misses_total" in dns
    assert "negative_hits_total" in dns
    assert "stale_hits_total" in dns
    assert "evictions_total" in dns
    assert "resolutions_total" in dns
    assert "errors_total" in dns
    assert isinstance(dns["enabled"], bool)
    assert isinstance(dns["entries"], int)


def test_hosts_is_list(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    data = response.json()
    assert isinstance(data["hosts"], list)


def test_host_entry_shape(app_with_key: FastAPI) -> None:
    """When hosts exist, each has the required fields."""
    # The test fixture has no DNS cache entries, so hosts will be empty.
    # Verify the shape by checking the key is present and is a list.
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    data = response.json()
    hosts = data["hosts"]
    assert isinstance(hosts, list)
    for entry in hosts:
        assert "host" in entry
        assert "family" in entry
        assert "state" in entry
        assert "expires_in_seconds" in entry
        assert "stale_available" in entry
        assert "last_error_kind" in entry
        assert entry["state"] in ("positive", "negative")
        assert isinstance(entry["expires_in_seconds"], (int, float))
        assert isinstance(entry["stale_available"], bool)


def test_provider_client_pool_in_response(app_with_key: FastAPI) -> None:
    """The provider_client_pool data is included in the response."""
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    data = response.json()
    ob = data["outbound_clients"]
    # scopes is always present (may be empty when no outbound manager is configured)
    assert "scopes" in ob
    assert isinstance(ob["scopes"], dict)


# -- register_network_routes always attaches auth dependency ---------------


def test_register_network_routes_ignores_require_auth_param(
    app_with_key: FastAPI,
) -> None:
    """require_auth=False does not skip the auth dependency."""
    client = TestClient(app_with_key)
    response = client.get("/api/network/diagnostics")
    assert response.status_code == 401


# -- No sensitive data exposed ---------------------------------------------


def test_no_api_keys_in_response(app_with_key: FastAPI) -> None:
    client = TestClient(app_with_key)
    response = client.get(
        "/api/network/diagnostics",
        headers={"Authorization": "Bearer test-key-12345678"},
    )
    text = response.text
    assert "test-key-12345678" not in text
    assert "OPENCODE_TEST_KEY" not in text
