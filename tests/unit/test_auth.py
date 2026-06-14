"""Tests for authentication."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from go_aggregator.auth import require_auth, verify_api_key
from go_aggregator.models.config import AppConfig


@pytest.fixture()
def mock_request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    return request


def test_verify_api_key_valid(mock_request: MagicMock) -> None:
    mock_request.headers = {"authorization": "Bearer secret123"}
    assert verify_api_key(mock_request, "secret123") is True


def test_verify_api_key_x_api_key_header(mock_request: MagicMock) -> None:
    mock_request.headers = {"x-api-key": "secret123"}
    assert verify_api_key(mock_request, "secret123") is True


def test_verify_api_key_invalid(mock_request: MagicMock) -> None:
    mock_request.headers = {"authorization": "Bearer wrongkey"}
    assert verify_api_key(mock_request, "secret123") is False


def test_verify_api_key_missing(mock_request: MagicMock) -> None:
    mock_request.headers = {}
    assert verify_api_key(mock_request, "secret123") is False


def test_require_auth_no_env_var() -> None:
    config = AppConfig()
    config.server.api_key_env = "NONEXISTENT_AUTH_ENV"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/protected")
    # Auth is now fail-closed: missing env var at runtime returns 503
    assert response.status_code == 503


def test_require_auth_valid_key() -> None:
    os.environ["TEST_AUTH_VALID"] = "valid-key-abc"
    config = AppConfig()
    config.server.api_key_env = "TEST_AUTH_VALID"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer valid-key-abc"},
    )
    assert response.status_code == 200
    del os.environ["TEST_AUTH_VALID"]


def test_require_auth_invalid_key() -> None:
    os.environ["TEST_AUTH_INVALID"] = "valid-key-def"
    config = AppConfig()
    config.server.api_key_env = "TEST_AUTH_INVALID"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/protected", headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 401
    del os.environ["TEST_AUTH_INVALID"]


@pytest.mark.asyncio()
async def test_auth_fail_closed_at_runtime() -> None:
    """Missing env var at runtime should return 503, not disable auth."""
    config = AppConfig()
    config.server.api_key_env = "RUNTIME_MISSING_KEY"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    # Ensure the env var is NOT set
    os.environ.pop("RUNTIME_MISSING_KEY", None)

    from fastapi.testclient import TestClient as AsyncClient

    client = AsyncClient(app)
    response = client.get("/protected")
    assert response.status_code == 503
    assert "Authentication unavailable" in response.json()["detail"]
