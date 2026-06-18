"""Security tests for the proxy."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from go_aggregator.api.chat_completions import handle_chat_completions
from go_aggregator.api.errors import anthropic_error_response, openai_error_response
from go_aggregator.api.messages import handle_messages
from go_aggregator.auth import require_auth
from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.dashboard.escape import escape
from go_aggregator.models.config import AppConfig
from go_aggregator.proxy.client import filter_request_headers
from go_aggregator.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    return request


def _make_auth_app(api_key_env: str = "SEC_TEST_KEY") -> FastAPI:
    """Create a minimal FastAPI app that enforces auth."""
    config = AppConfig()
    config.server.api_key_env = api_key_env
    app = FastAPI()
    app.state.config = config

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)
        return {"status": "ok"}

    return app


def _make_chat_app() -> FastAPI:
    """Create a minimal app with /v1/chat/completions that validates JSON and model."""
    app = FastAPI()
    config = AppConfig()
    config.server.api_key_env = ""  # disable auth
    app.state.config = config
    # Mock objects so handler attributes don't crash
    app.state.registry = MagicMock()
    app.state.catalog = MagicMock()
    app.state.router = MagicMock()
    app.state.db = MagicMock()
    app.state.httpx_client = MagicMock()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        model_id = payload.get("model")
        if not model_id:
            return JSONResponse(
                status_code=400, content={"error": "Missing model field"}
            )

        return JSONResponse(status_code=200, content={"ok": True})

    return app


# ===================================================================
# Authentication tests
# ===================================================================


@pytest.mark.asyncio
async def test_missing_api_key_returns_401() -> None:
    os.environ["SEC_TEST_KEY"] = "test-secret"
    app = _make_auth_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/protected")
    assert resp.status_code == 401
    del os.environ["SEC_TEST_KEY"]


@pytest.mark.asyncio
async def test_incorrect_api_key_returns_401() -> None:
    os.environ["SEC_TEST_KEY"] = "test-secret"
    app = _make_auth_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/protected", headers={"Authorization": "Bearer wrong-key"}
        )
    assert resp.status_code == 401
    del os.environ["SEC_TEST_KEY"]


@pytest.mark.asyncio
async def test_api_key_via_x_api_key_header() -> None:
    os.environ["SEC_TEST_KEY"] = "test-secret"
    app = _make_auth_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/protected", headers={"X-API-Key": "test-secret"})
    assert resp.status_code == 200
    del os.environ["SEC_TEST_KEY"]


@pytest.mark.asyncio
async def test_empty_api_key_returns_401() -> None:
    os.environ["SEC_TEST_KEY"] = "test-secret"
    app = _make_auth_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/protected", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401
    del os.environ["SEC_TEST_KEY"]


# ===================================================================
# Header security tests
# ===================================================================


def test_filter_request_headers_removes_local_auth() -> None:
    headers = {
        "Authorization": "Bearer local-key",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "upstream-key")
    assert result["Authorization"] == "Bearer upstream-key"
    assert headers["Authorization"] == "Bearer local-key"


def test_filter_request_headers_removes_hop_by_hop() -> None:
    headers = {
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
        "Te": "deflate",
        "Trailers": "gzip",
        "Upgrade": "websocket",
        "Proxy-Authenticate": "Basic",
        "Proxy-Authorization": "Bearer x",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    for hop_header in (
        "Connection",
        "Transfer-Encoding",
        "Te",
        "Trailers",
        "Upgrade",
        "Proxy-Authenticate",
        "Proxy-Authorization",
    ):
        assert hop_header.lower() not in {k.lower() for k in result}


def test_filter_request_headers_preserves_content_type() -> None:
    headers = {
        "Content-Type": "application/json",
        "X-Custom": "value",
    }
    result = filter_request_headers(headers, "key")
    assert result["Content-Type"] == "application/json"
    assert result["X-Custom"] == "value"


def test_filter_request_headers_removes_host() -> None:
    headers = {"Host": "evil.com", "Content-Type": "application/json"}
    result = filter_request_headers(headers, "key")
    assert "Host" not in result
    assert "host" not in {k.lower() for k in result}


def test_filter_request_headers_removes_content_length() -> None:
    headers = {"Content-Length": "100", "Content-Type": "application/json"}
    result = filter_request_headers(headers, "key")
    assert "Content-Length" not in result
    assert "content-length" not in {k.lower() for k in result}


# ===================================================================
# Input validation tests
# ===================================================================


@pytest.mark.asyncio
async def test_malformed_json_returns_400() -> None:
    app = _make_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            content=b"not valid json {{",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid JSON"


@pytest.mark.asyncio
async def test_empty_body_returns_400() -> None:
    app = _make_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid JSON"


@pytest.mark.asyncio
async def test_missing_model_field_returns_400() -> None:
    app = _make_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": []},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "Missing model field"


# ===================================================================
# Usage extractor safety tests
# ===================================================================


def test_openai_extractor_handles_empty_usage() -> None:
    extractor = OpenAIStreamUsageExtractor()
    result = extractor.extract({"usage": {}})
    assert result is None


def test_openai_extractor_handles_missing_nested_fields() -> None:
    extractor = OpenAIStreamUsageExtractor()
    result = extractor.extract({"usage": {"prompt_tokens": 10}})
    assert result is not None
    assert result.input_tokens == 10
    assert result.output_tokens == 0
    assert result.cache_read_tokens == 0
    assert result.reasoning_tokens == 0


def test_anthropic_extractor_handles_malformed_event() -> None:
    extractor = AnthropicStreamUsageExtractor()
    result = extractor.extract({"type": "unknown_type"})
    assert result is None


def test_openai_extractor_handles_none_usage() -> None:
    extractor = OpenAIStreamUsageExtractor()
    result = extractor.extract({"usage": None})
    assert result is None


# ===================================================================
# Catalog/model safety tests
# ===================================================================


def test_model_catalog_cache_empty_update() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("account-a", [])
    assert cache.model_count == 0


def test_model_catalog_cache_duplicate_model_ids() -> None:
    cache = ModelCatalogCache()
    models = [{"model_id": "gpt-4", "display_name": "GPT-4"}]
    cache.update_from_account("account-a", models)
    cache.update_from_account("account-b", models)
    assert cache.model_count == 1
    accounts = cache.get_supporting_accounts("gpt-4")
    assert "account-a" in accounts
    assert "account-b" in accounts


# ===================================================================
# String escaping tests (HTML injection)
# ===================================================================


def test_model_id_html_escaping() -> None:
    malicious = '<script>alert("xss")</script>'
    escaped = escape(malicious)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_error_message_no_html_leakage() -> None:
    payloads = [
        "<img src=x onerror=alert(1)>",
        '"><script>alert(1)</script>',
        "<svg onload=alert(1)>",
        "<b>bold</b>",
        '<a href="javascript:alert(1)">click</a>',
    ]
    for payload in payloads:
        escaped = escape(payload)
        assert "<img" not in escaped
        assert "<script>" not in escaped
        assert "<svg" not in escaped
        assert "<b>" not in escaped
        assert "<a " not in escaped
        assert "&lt;" in escaped or "&gt;" in escaped or "&amp;" in escaped


# ===================================================================
# Endpoint handler input validation tests (real handlers)
# ===================================================================


def _make_real_chat_app() -> FastAPI:
    """Create a minimal app mounting the real chat completions handler."""
    app = FastAPI()
    config = AppConfig()
    config.server.api_key_env = ""  # disable auth
    app.state.config = config
    app.state.coordinator = MagicMock()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await handle_chat_completions(request)  # type: ignore[return-value]

    return app


def _make_real_messages_app() -> FastAPI:
    """Create a minimal app mounting the real messages handler."""
    app = FastAPI()
    config = AppConfig()
    config.server.api_key_env = ""  # disable auth
    app.state.config = config
    app.state.coordinator = MagicMock()

    @app.post("/v1/messages")
    async def messages(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await handle_messages(request)  # type: ignore[return-value]

    return app


@pytest.mark.asyncio
async def test_chat_completions_invalid_utf8_returns_400() -> None:
    """Invalid UTF-8 bytes in request body must return 400, not 500."""
    app = _make_real_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            content=b"\x80\x81\x82\xff",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_messages_invalid_utf8_returns_400() -> None:
    """Invalid UTF-8 bytes in request body must return 400, not 500."""
    app = _make_real_messages_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages",
            content=b"\x80\x81\x82\xff",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_chat_completions_stream_string_false_returns_400() -> None:
    """'stream: \"false\"' (string) must return 400, not silently coerce to True."""
    app = _make_real_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [], "stream": "false"},
        )
    assert resp.status_code == 400
    assert "Invalid stream" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completions_stream_int_returns_400() -> None:
    """'stream: 1' (int) must return 400, not silently coerce to True."""
    app = _make_real_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [], "stream": 1},
        )
    assert resp.status_code == 400
    assert "Invalid stream" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_messages_stream_string_false_returns_400() -> None:
    """'stream: \"false\"' (string) must return 400 for Anthropic endpoint."""
    app = _make_real_messages_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-3-sonnet", "messages": [], "stream": "false"},
        )
    assert resp.status_code == 400
    assert "Invalid stream" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_messages_stream_int_returns_400() -> None:
    """'stream: 1' (int) must return 400 for Anthropic endpoint."""
    app = _make_real_messages_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-3-sonnet", "messages": [], "stream": 1},
        )
    assert resp.status_code == 400
    assert "Invalid stream" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completions_stream_true_proceeds() -> None:
    """Valid boolean 'stream: true' should proceed to coordinator."""
    app = _make_real_chat_app()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.body = b'{"ok": true}'
    mock_response.headers = []
    mock_response.stream_iterator = None
    mock_response.account_name = "test"
    mock_response.usage = None
    app.state.coordinator.execute = AsyncMock(return_value=mock_response)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [], "stream": True},
        )
    assert resp.status_code == 200
    app.state.coordinator.execute.assert_called_once()


@pytest.mark.asyncio
async def test_chat_completions_missing_model_returns_400() -> None:
    """Missing 'model' field must return 400."""
    app = _make_real_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": []},
        )
    assert resp.status_code == 400
    assert "Missing model" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_messages_missing_model_returns_400() -> None:
    """Missing 'model' field must return 400 for Anthropic endpoint."""
    app = _make_real_messages_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages",
            json={"messages": []},
        )
    assert resp.status_code == 400
    assert "Missing model" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completions_empty_body_returns_400() -> None:
    """Empty request body must return 400."""
    app = _make_real_chat_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_messages_empty_body_returns_400() -> None:
    """Empty request body must return 400 for Anthropic endpoint."""
    app = _make_real_messages_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400


# ===================================================================
# Error response formatting tests
# ===================================================================


def test_openai_error_response_structure() -> None:
    """openai_error_response returns correct JSON structure."""
    resp = openai_error_response(400, "Bad request", "invalid_request_error")
    assert resp.status_code == 400
    body = resp.body.decode()
    data = json.loads(body)
    assert data["error"]["message"] == "Bad request"
    assert data["error"]["type"] == "invalid_request_error"
    assert data["error"]["code"] == "400"


def test_anthropic_error_response_structure() -> None:
    """anthropic_error_response returns correct JSON structure."""
    resp = anthropic_error_response(503, "Unavailable", "api_error")
    assert resp.status_code == 503
    body = resp.body.decode()
    data = json.loads(body)
    assert data["type"] == "error"
    assert data["error"]["type"] == "api_error"
    assert data["error"]["message"] == "Unavailable"


def test_openai_error_response_default_type() -> None:
    """openai_error_response defaults to invalid_request_error."""
    resp = openai_error_response(400, "msg")
    data = json.loads(resp.body.decode())
    assert data["error"]["type"] == "invalid_request_error"


def test_anthropic_error_response_default_type() -> None:
    """anthropic_error_response defaults to invalid_request_error."""
    resp = anthropic_error_response(400, "msg")
    data = json.loads(resp.body.decode())
    assert data["error"]["type"] == "invalid_request_error"
