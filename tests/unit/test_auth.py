"""Tests for authentication."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from eggpool.auth import require_auth, require_auth_at_startup, verify_api_key
from eggpool.models.config import AppConfig


@pytest.fixture()
def mock_request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    return request


def test_verify_api_key_valid(mock_request: MagicMock) -> None:
    mock_request.headers = {"authorization": "Bearer secret123"}
    assert verify_api_key(mock_request, "secret123") is True


def test_verify_api_key_valid_with_tab_after_bearer(mock_request: MagicMock) -> None:
    mock_request.headers = {"authorization": "Bearer\tsecret123"}
    assert verify_api_key(mock_request, "secret123") is True


def test_verify_api_key_x_api_key_header(mock_request: MagicMock) -> None:
    mock_request.headers = {"x-api-key": "secret123"}
    assert verify_api_key(mock_request, "secret123") is True


def test_verify_api_key_invalid(mock_request: MagicMock) -> None:
    mock_request.headers = {"authorization": "Bearer wrongkey"}
    assert verify_api_key(mock_request, "secret123") is False


def test_verify_api_key_rejects_bearer_without_separator(
    mock_request: MagicMock,
) -> None:
    mock_request.headers = {"authorization": "Bearersecret123"}
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
    # Auth is now fail-closed: missing env var at runtime returns 401
    assert response.status_code == 401


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
    """Missing env var at runtime should return 401, not disable auth."""
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
    assert response.status_code == 401
    assert "Authentication unavailable" in response.json()["detail"]


@pytest.mark.asyncio()
async def test_auth_whitespace_key_rejected_at_runtime() -> None:
    """Whitespace-only env var at runtime should return 401."""
    os.environ["TEST_WHITESPACE_RUNTIME"] = "   "
    config = AppConfig()
    config.server.api_key_env = "TEST_WHITESPACE_RUNTIME"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 401
    del os.environ["TEST_WHITESPACE_RUNTIME"]


class TestRequireAuthAtStartup:
    """Tests for require_auth_at_startup."""

    def test_returns_none_when_env_is_empty(self) -> None:
        """Empty api_key_env disables auth and returns None."""
        assert require_auth_at_startup("") is None

    def test_returns_none_when_env_is_none(self) -> None:
        """None api_key_env disables auth and returns None."""
        assert require_auth_at_startup(None) is None

    def test_returns_key_when_env_var_is_set(self) -> None:
        """Returns the key value when the env var is set."""
        os.environ["TEST_STARTUP_KEY"] = "secret-value"
        try:
            result = require_auth_at_startup("TEST_STARTUP_KEY")
            assert result == "secret-value"
        finally:
            del os.environ["TEST_STARTUP_KEY"]

    def test_raises_when_env_var_is_missing(self) -> None:
        """Raises RuntimeError when env var is not set."""
        os.environ.pop("MISSING_STARTUP_KEY", None)
        with pytest.raises(RuntimeError, match="not set"):
            require_auth_at_startup("MISSING_STARTUP_KEY")

    def test_error_message_recommends_empty_string(self) -> None:
        """Error message tells user to set api_key_env = \"\" to disable."""
        os.environ.pop("MISSING_KEY_MSG", None)
        with pytest.raises(RuntimeError, match=r'api_key_env = ""'):
            require_auth_at_startup("MISSING_KEY_MSG")

    def test_whitespace_only_key_rejected(self) -> None:
        """Whitespace-only env var should be rejected at startup."""
        os.environ["TEST_WHITESPACE_KEY"] = "   "
        try:
            with pytest.raises(RuntimeError, match="not set"):
                require_auth_at_startup("TEST_WHITESPACE_KEY")
        finally:
            del os.environ["TEST_WHITESPACE_KEY"]

    def test_newlines_only_key_rejected(self) -> None:
        """Newline-only env var should be rejected at startup."""
        os.environ["TEST_NEWLINE_KEY"] = "\n\n"
        try:
            with pytest.raises(RuntimeError, match="not set"):
                require_auth_at_startup("TEST_NEWLINE_KEY")
        finally:
            del os.environ["TEST_NEWLINE_KEY"]
