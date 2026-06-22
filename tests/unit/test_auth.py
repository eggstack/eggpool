"""Tests for authentication."""

from __future__ import annotations

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


def test_verify_api_key_rejects_malformed_format(mock_request: MagicMock) -> None:
    """Regression test (M11): provided keys that fail format
    validation must not be compared against the configured key.
    """
    mock_request.headers = {"x-api-key": "short"}
    assert verify_api_key(mock_request, "short") is False

    mock_request.headers = {"x-api-key": "contains space char"}
    assert verify_api_key(mock_request, "contains space char") is False

    mock_request.headers = {"x-api-key": "weird\x00chars"}
    assert verify_api_key(mock_request, "weird\x00chars") is False


def test_require_auth_no_key() -> None:
    """When no api_key is set and no env var exists, auth is disabled."""
    config = AppConfig()
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/protected")
    # No key configured at all → auth disabled → 200
    assert response.status_code == 200


def test_require_auth_wrong_key() -> None:
    """When api_key is set but request has wrong key, returns 401."""
    config = AppConfig()
    config.server.api_key = "correct-secret"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_require_auth_valid_key() -> None:
    config = AppConfig()
    config.server.api_key = "valid-key-abc"
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


def test_require_auth_invalid_key() -> None:
    config = AppConfig()
    config.server.api_key = "valid-key-def"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/protected", headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 401


@pytest.mark.asyncio()
async def test_auth_fail_closed_at_runtime() -> None:
    """Missing api_key at runtime should return 401, not disable auth."""
    config = AppConfig()
    config.server.api_key = "expected-key"
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        await require_auth(request)
        return {"status": "ok"}

    from fastapi.testclient import TestClient as AsyncClient

    client = AsyncClient(app)
    response = client.get("/protected")
    assert response.status_code == 401
    assert "Invalid or missing API key" in response.json()["detail"]


@pytest.mark.asyncio()
async def test_auth_whitespace_key_rejected_at_runtime() -> None:
    """Whitespace-only api_key at runtime should return 401."""
    config = AppConfig()
    config.server.api_key = "   "
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


class TestRequireAuthAtStartup:
    """Tests for require_auth_at_startup."""

    def test_returns_none_when_key_is_empty(self) -> None:
        """Empty api_key disables auth and returns None."""
        assert require_auth_at_startup("") is None

    def test_returns_none_when_key_is_none(self) -> None:
        """None api_key disables auth and returns None."""
        assert require_auth_at_startup(None) is None

    def test_returns_key_when_set(self) -> None:
        """Returns the key value when provided."""
        result = require_auth_at_startup("secret-value")
        assert result == "secret-value"

    @pytest.mark.parametrize(
        "value",
        ["short", "contains spaces", "contains.dot", "x" * 513],
    )
    def test_rejects_keys_runtime_validation_cannot_accept(self, value: str) -> None:
        with pytest.raises(RuntimeError, match="8-512 characters"):
            require_auth_at_startup(value)

    def test_raises_when_key_is_whitespace(self) -> None:
        """Raises RuntimeError when key is whitespace-only."""
        with pytest.raises(RuntimeError, match="not set"):
            require_auth_at_startup("   ")

    def test_error_message_mentions_api_key(self) -> None:
        """Error message tells user to set api_key."""
        with pytest.raises(RuntimeError, match="API key"):
            require_auth_at_startup("your-api-key-here")

    def test_whitespace_only_key_rejected(self) -> None:
        """Whitespace-only key should be rejected at startup."""
        with pytest.raises(RuntimeError, match="not set"):
            require_auth_at_startup("   ")

    def test_newlines_only_key_rejected(self) -> None:
        """Newline-only key should be rejected at startup."""
        with pytest.raises(RuntimeError, match="not set"):
            require_auth_at_startup("\n\n")
