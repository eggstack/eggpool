"""Integration tests for the proxy endpoints with mocked upstreams."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.app import create_app
from go_aggregator.catalog.service import CatalogService
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.models.config import AppConfig
from go_aggregator.request.coordinator import RequestCoordinator
from go_aggregator.routing.router import Router
from go_aggregator.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://test-upstream.example.com"


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set test API key env vars for every test; monkeypatch restores them."""
    monkeypatch.setenv("OPENCODE_TEST_KEY", "test-key-123")
    monkeypatch.setenv("GO_AGG_TEST_KEY", "test-key-123")


def _build_config() -> AppConfig:
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

    db = Database(path=":memory:")
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
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )

    httpx_client = httpx.AsyncClient(
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
    application.state.httpx_client = httpx_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, httpx_client)
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
        client_pool=httpx_client,
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

    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "test-acct")

    yield application

    await httpx_client.aclose()
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


# ── 1. Successful OpenAI non-streaming response ──────────────────────────────


@pytest.mark.asyncio
async def test_openai_non_streaming_success(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
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
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Hello"
    assert response.headers.get("x-proxy-request-id") is not None

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 1


# ── 2. Successful Anthropic non-streaming response ───────────────────────────


@pytest.mark.asyncio
async def test_anthropic_non_streaming_success(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there"}],
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
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

    assert response.status_code == 200
    body = response.json()
    assert body["content"][0]["text"] == "Hi there"
    assert response.headers.get("x-proxy-request-id") is not None


# ── 3. OpenAI stream with terminal usage ─────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stream_with_usage(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    sse_lines = [
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        ),
        "",
        "data: [DONE]",
    ]
    sse_content = "\n".join(sse_lines) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
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
    text = response.text
    assert "Hello" in text
    assert "[DONE]" in text


# ── 4. Anthropic stream with terminal usage ──────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_stream_with_usage(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    sse_lines = [
        "event: message_start",
        "data: "
        + json.dumps(
            {
                "type": "message_start",
                "message": {
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "usage": {"input_tokens": 20},
                },
            }
        ),
        "",
        "event: content_block_delta",
        "data: "
        + json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            }
        ),
        "",
        "event: message_delta",
        "data: "
        + json.dumps(
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 5},
            }
        ),
        "",
        "event: message_stop",
        "data: " + json.dumps({"type": "message_stop"}),
    ]
    sse_content = "\n".join(sse_lines) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/messages").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    text = response.text
    assert "Hi" in text
    assert "message_delta" in text


# ── 5. Unknown SSE event passthrough ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_sse_event_passthrough(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    sse_lines = [
        "event: ping",
        'data: {"type":"ping"}',
        "",
        "data: "
        + json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        "data: [DONE]",
    ]
    sse_content = "\n".join(sse_lines) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
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
    text = response.text
    assert "ping" in text
    assert "Hello" in text


# ── 6. Upstream connection error ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_connection_error(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 502
    body = response.json()
    assert "error" in body


# ── 7. Upstream timeout ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_timeout(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=httpx.TimeoutException("Read timed out")
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 504
    body = response.json()
    assert "error" in body


# ── 8. Upstream 401 passed through ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_401_passed_through(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": {
                        "message": "Invalid API key",
                        "type": "authentication_error",
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

    assert response.status_code == 401


# ── 9. Upstream 429 with Retry-After ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_429_with_retry_after(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "Rate limited", "type": "rate_limit_error"}},
                headers={"retry-after": "30"},
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

    assert response.status_code == 429
    health = app.state.health_manager.get_account_health("test-acct")
    assert health.health_state == "rate_limited"
    assert health.cooldown_until > time.time() + 20


# ── 9b. Upstream 402 quota failure passed through ────────────────────────────


@pytest.mark.asyncio
async def test_upstream_402_quota_failure(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                402,
                json={
                    "error": {
                        "message": "Quota exceeded",
                        "type": "billing_error",
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

    assert response.status_code == 402
    body = response.json()
    assert "error" in body


# ── 10. Upstream 404 passed through ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_404_passed_through(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"message": "Model not found", "type": "not_found"}},
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

    assert response.status_code == 404


# ── 11. Upstream 500 returns 500 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_500_returns_500(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                500,
                json={"error": {"message": "Internal error", "type": "server_error"}},
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

    assert response.status_code == 500


# ── 12. All accounts unavailable ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_accounts_unavailable(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    registry: AccountRegistry = app.state.registry
    for state in registry.get_enabled_states():
        state.enabled = False

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        },
        headers=auth_headers,
    )

    # Model check runs before account selection; no eligible accounts means
    # the service cannot fulfill the request, so the proxy reports 503.
    assert response.status_code == 503


# ── 13. Invalid JSON body ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_json_body(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        content=b"not valid json {{",
        headers={**auth_headers, "content-type": "application/json"},
    )

    assert response.status_code == 400


# ── 13b. JSON body must be an object ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/chat/completions",
        "/v1/messages",
    ],
)
async def test_json_body_must_be_object(
    path: str,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        path,
        content=b"[]",
        headers={**auth_headers, "content-type": "application/json"},
    )

    assert response.status_code == 400


# ── 14. Missing model field ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_model_field(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hi"}]},
        headers=auth_headers,
    )

    assert response.status_code == 400
    body = response.json()
    err = body["error"]
    error_msg = err["message"] if isinstance(err, dict) else err
    assert "model" in error_msg.lower() or "missing" in error_msg.lower()


# ── 15. Model not available ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_not_available(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nonexistent-model",
            "messages": [{"role": "user", "content": "Hi"}],
        },
        headers=auth_headers,
    )

    # Unknown model is not in the catalog, so the proxy returns 404
    # (ModelNotFoundError) to distinguish from temporary unavailability.
    assert response.status_code == 404


# ── 15b. Unresolved protocol is a controlled server error ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "expected_error_type"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "mystery-model",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            "server_error",
        ),
        (
            "/v1/messages",
            {
                "model": "mystery-model",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            "api_error",
        ),
    ],
)
async def test_unresolved_protocol_returns_503(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    path: str,
    payload: dict[str, object],
    expected_error_type: str,
) -> None:
    app.state.catalog.cache.load_model(
        model_id="mystery-model",
        display_name="Mystery Model",
        protocol="",
        capabilities={},
        source_metadata={},
    )

    response = await client.post(path, json=payload, headers=auth_headers)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["type"] == expected_error_type
    assert "unresolved protocol" in body["error"]["message"].lower()


# ── 16. Request recorded in database ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_recorded_in_database(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers=auth_headers,
        )

    db: Database = app.state.db
    rows = await db.fetch_all("SELECT * FROM requests")
    assert len(rows) == 1
    assert rows[0]["model_id"] == "gpt-4"
    assert rows[0]["status"] == "completed"


# ── 17. Multiple requests accumulate in database ─────────────────────────────


@pytest.mark.asyncio
async def test_multiple_requests_accumulate(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )
        )

        for _ in range(3):
            await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )

    db: Database = app.state.db
    row = await db.fetch_one("SELECT COUNT(*) as cnt FROM requests")
    assert row is not None
    assert row["cnt"] == 3


# ── 18. Proxy request ID consistent ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_request_id_is_uuid(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    import uuid

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
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

    proxy_id = response.headers.get("x-proxy-request-id")
    assert proxy_id is not None
    uuid_obj = uuid.UUID(proxy_id)
    assert str(uuid_obj) == proxy_id


# ── 19. Health endpoint without auth ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_no_auth_required(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


# ── 20. Readyz reports degraded when no accounts ─────────────────────────────


@pytest.mark.asyncio
async def test_readyz_reports_status(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
