"""Contract tests verifying the proxy preserves upstream protocol semantics."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.app import create_app
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://contract-upstream.example.com"
OPENAI_PATH = "/chat/completions"
ANTHROPIC_PATH = "/messages"


def _build_config() -> AppConfig:
    os.environ.setdefault("OPENCODE_TEST_KEY", "test-key-123")
    os.environ.setdefault("GO_AGG_TEST_KEY", "test-key-123")
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "GO_AGG_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def app(config: AppConfig) -> AsyncGenerator[FastAPI]:
    application = create_app(config)

    db = Database(path=config.database.path)
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )

    application.state.httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(
            config.upstream.read_timeout_s,
            connect=config.upstream.connect_timeout_s,
            read=config.upstream.read_timeout_s,
            write=config.upstream.write_timeout_s,
            pool=config.upstream.keepalive_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=config.upstream.max_connections,
            max_keepalive_connections=config.upstream.max_keepalive,
            keepalive_expiry=config.upstream.keepalive_timeout_s,
        ),
    )

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, application.state.httpx_client)
    application.state.catalog = catalog

    router = Router(registry, catalog)
    application.state.router = router

    application.state.stats = StatsService(db)

    health_manager = HealthManager()
    application.state.health_manager = health_manager

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)

    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=application.state.httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
    )
    application.state.coordinator = coordinator

    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")

    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )

    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "test-acct")

    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )

    yield application

    await application.state.httpx_client.aclose()
    await db.disconnect()


@pytest.fixture
def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key-123"}


# ── 1. Unknown fields preserved in OpenAI request ────────────────────────────


@pytest.mark.asyncio
async def test_unknown_fields_preserved_in_openai_request(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    request_body = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hi"}],
        "custom_field": "custom_value",
        "another_unknown": 123,
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 0,
                            "total_tokens": 1,
                        },
                    },
                ),
            )[-1]
        )

        response = await client.post(
            "/v1/chat/completions",
            json=request_body,
            headers=auth_headers,
        )

    assert response.status_code == 200
    upstream_body = json.loads(captured["body"])
    assert upstream_body["custom_field"] == "custom_value"
    assert upstream_body["another_unknown"] == 123
    assert upstream_body["model"] == "gpt-4"
    assert upstream_body["messages"] == [{"role": "user", "content": "Hi"}]


# ── 2. Unknown fields preserved in Anthropic request ─────────────────────────


@pytest.mark.asyncio
async def test_unknown_fields_preserved_in_anthropic_request(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    request_body = {
        "model": "claude-3",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hi"}],
        "metadata": {"user_id": "test"},
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg_123",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 1},
                    },
                ),
            )[-1]
        )

        response = await client.post(
            "/v1/messages",
            json=request_body,
            headers=auth_headers,
        )

    assert response.status_code == 200
    upstream_body = json.loads(captured["body"])
    assert upstream_body["metadata"] == {"user_id": "test"}
    assert upstream_body["model"] == "claude-3"
    assert upstream_body["max_tokens"] == 100
    assert upstream_body["messages"] == [{"role": "user", "content": "Hi"}]


# ── 3. OpenAI streaming events preserved ─────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_streaming_events_preserved(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    sse_events = [
        'data: {"id":"chatcmpl-1","choices":[{"delta":{"role":"assistant"}}]}',
        "",
        'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}',
        "",
        'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
    ]
    sse_content = "\n".join(sse_events) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    collected = [line for line in response.text.splitlines() if line]
    assert collected[0] == sse_events[0]
    assert collected[1] == sse_events[2]
    assert collected[2] == sse_events[4]
    assert collected[3] == sse_events[6]


# ── 4. Status codes preserved (OpenAI) ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500, 503])
async def test_status_code_preserved_openai(
    status_code: int,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                status_code,
                json={
                    "error": {
                        "message": f"Upstream error {status_code}",
                        "type": "upstream_error",
                    }
                },
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == status_code


# ── 5. Status codes preserved (Anthropic) ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500, 503])
async def test_status_code_preserved_anthropic(
    status_code: int,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(
                status_code,
                json={
                    "type": "error",
                    "error": {
                        "type": "upstream_error",
                        "message": f"Upstream error {status_code}",
                    },
                },
            )
        )

        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == status_code


# ── 6. Content-Type preserved ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_content_type_preserved(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                },
                headers={"content-type": "application/json"},
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]


# ── 7. Usage adapter tolerates extra fields (OpenAI) ─────────────────────────


@pytest.mark.asyncio
async def test_usage_adapter_tolerates_extra_fields_openai(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response_body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {
                "cached_tokens": 3,
                "unknown_field": "value",
            },
            "completion_tokens_details": {
                "reasoning_tokens": 2,
                "extra": "data",
            },
            "additional_usage": "info",
        },
    }

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(200, json=response_body)
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["usage"]["prompt_tokens"] == 10
    assert body["usage"]["completion_tokens"] == 5
    assert body["usage"]["total_tokens"] == 15


# ── 8. Usage adapter tolerates extra fields (Anthropic) ──────────────────────


@pytest.mark.asyncio
async def test_usage_adapter_tolerates_extra_fields_anthropic(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response_body = {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "claude-3",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 1,
            "extra_usage_field": "should_not_break",
        },
    }

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(200, json=response_body)
        )

        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 5
    assert body["usage"]["cache_read_input_tokens"] == 3


# ── 9. Error response preserves upstream format ──────────────────────────────


@pytest.mark.asyncio
async def test_error_response_preserves_upstream_error_format(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    error_body = {
        "error": {
            "message": "Invalid request",
            "type": "invalid_request_error",
            "code": "invalid_model",
        }
    }

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(400, json=error_body)
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["message"] == "Invalid request"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "invalid_model"


# ── 10. Proxy request ID header added ────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_request_id_header_added(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    proxy_id = response.headers.get("x-proxy-request-id")
    assert proxy_id is not None
    import uuid

    uuid_obj = uuid.UUID(proxy_id)
    assert str(uuid_obj) == proxy_id


# ── 11. Hop-by-hop headers stripped from response ────────────────────────────


@pytest.mark.asyncio
async def test_hop_by_hop_headers_stripped_from_response(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                },
                headers={
                    "connection": "keep-alive",
                    "transfer-encoding": "chunked",
                    "upgrade": "websocket",
                    "content-type": "application/json",
                },
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    for header in ("connection", "transfer-encoding", "upgrade"):
        assert header.lower() not in {k.lower() for k in response.headers}, (
            f"Hop-by-hop header '{header}' should be stripped"
        )


# ---------------------------------------------------------------------------
# Bug 5: Oversized Content-Length returns protocol-appropriate error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_content_length_openai_envelope(
    app: FastAPI,
) -> None:
    """Oversized Content-Length on /v1/chat/completions returns OpenAI envelope."""
    from eggpool.constants import MAX_REQUEST_BODY_BYTES

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b"x" * 100,
            headers={
                "content-length": str(MAX_REQUEST_BODY_BYTES + 1),
                "content-type": "application/json",
                "authorization": "Bearer test-key-123",
            },
        )

    assert response.status_code == 413
    body = response.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_oversized_content_length_anthropic_envelope(
    app: FastAPI,
) -> None:
    """Oversized Content-Length on /v1/messages returns Anthropic envelope."""
    from eggpool.constants import MAX_REQUEST_BODY_BYTES

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/messages",
            content=b"x" * 100,
            headers={
                "content-length": str(MAX_REQUEST_BODY_BYTES + 1),
                "content-type": "application/json",
                "x-api-key": "test-key-123",
                "anthropic-version": "2023-06-01",
            },
        )

    assert response.status_code == 413
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
