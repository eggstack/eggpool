"""Contract tests verifying protocol transcoding through the full proxy lifecycle.

Boots a real FastAPI app with transcoding enabled, mocks upstreams via
respx, and asserts that the full HTTP request -> coordinator -> transcoder
-> upstream -> transcoder -> coordinator -> HTTP response pipeline
preserves protocol semantics in both directions.
"""

from __future__ import annotations

import json
import logging
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
from eggpool.transcoder.policy import TranscoderFeatures, TranscoderPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://contract-upstream.example.com"
OPENAI_PATH = "/chat/completions"
ANTHROPIC_PATH = "/messages"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_upstream_sse_events() -> list[str]:
    """Build Anthropic SSE frames for streaming tests."""
    return [
        _sse_line(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        ),
        "",
        _sse_line(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        "",
        _sse_line(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        ),
        "",
        _sse_line(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": " world"},
            },
        ),
        "",
        _sse_line(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        "",
        _sse_line(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        "",
        _sse_line("message_stop", {"type": "message_stop"}),
    ]


def _sse_line(event: str, data: dict[str, Any]) -> str:
    """Format one Anthropic SSE event line."""
    return f"event: {event}\ndata: {json.dumps(data)}"


def _build_openai_stream_events() -> list[str]:
    """Build OpenAI SSE chunks for streaming tests."""
    base = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "gpt-4",
    }
    return [
        _openai_sse_line(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        _openai_sse_line(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hi"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        "",
        _openai_sse_line(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            }
        ),
        "",
        "data: [DONE]",
    ]


def _openai_sse_line(data: dict[str, Any]) -> str:
    """Format one OpenAI SSE data line."""
    return f"data: {json.dumps(data)}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_config() -> AppConfig:
    os.environ.setdefault("OPENCODE_TEST_KEY", "test-key-123")
    os.environ.setdefault("GO_AGG_TEST_KEY", "test-key-123")
    os.environ.setdefault("ANTHROPIC_TEST_KEY", "test-key-123")
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
            "accounts": [
                {"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"},
                {
                    "name": "anthropic-acct",
                    "api_key_env": "ANTHROPIC_TEST_KEY",
                },
            ],
            "providers": {
                "test-provider": {
                    "id": "test-provider",
                    "base_url": UPSTREAM_BASE,
                    "protocols": ["openai"],
                    "auth": {"mode": "api_key", "header": "authorization"},
                    "accounts": [
                        {
                            "name": "test-acct",
                            "api_key_env": "OPENCODE_TEST_KEY",
                        },
                    ],
                },
                "anthropic-provider": {
                    "id": "anthropic-provider",
                    "base_url": UPSTREAM_BASE,
                    "protocols": ["anthropic"],
                    "auth": {"mode": "api_key", "header": "x-api-key"},
                    "accounts": [
                        {
                            "name": "anthropic-acct",
                            "api_key_env": "ANTHROPIC_TEST_KEY",
                        },
                    ],
                },
            },
            "transcoder": {"enabled": True, "prefer_native": True},
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
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("anthropic-acct", "ANTHROPIC_TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
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
    application.state.transcoder_policy = config.transcoder

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
        config=config,
        transcoder_policy=TranscoderPolicy(enabled=True, prefer_native=True),
    )
    application.state.coordinator = coordinator

    # Register claude-3 (anthropic) with both accounts
    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "test-acct")
    catalog.cache.set_account_provider("test-acct", "test-provider")

    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "anthropic-acct")
    catalog.cache.set_account_provider("anthropic-acct", "anthropic-provider")

    # Register gpt-4 (openai) with test-acct
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")
    catalog.cache.set_account_provider("gpt-4", "test-provider")

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


# ---------------------------------------------------------------------------
# 1. OpenAI client -> Anthropic upstream: full round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_client_anthropic_upstream_roundtrip(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """OpenAI /v1/chat/completions with claude-3 transcodes end-to-end."""
    request_body = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "Hello!"}],
        "temperature": 0.7,
        "max_tokens": 100,
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg-abc",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hi there!"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
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

    # Verify upstream received Anthropic-format request
    upstream_body = json.loads(captured["body"])
    assert upstream_body["model"] == "claude-3"
    assert upstream_body["messages"] == [{"role": "user", "content": "Hello!"}]
    assert "max_tokens" in upstream_body
    assert "stream" not in upstream_body

    # Verify client received OpenAI-format response
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "claude-3"
    assert len(body["choices"]) == 1
    msg = body["choices"][0]["message"]
    assert msg["content"] == "Hi there!"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 10
    assert body["usage"]["completion_tokens"] == 5
    assert body["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_loss_policy_reject_blocks_lossy_request_before_dispatch(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Reject mode returns 400 before upstream dispatch when fields are dropped."""
    app.state.transcoder_policy = TranscoderPolicy(
        enabled=True,
        loss_policy="reject",
        prefer_native=True,
    )
    request_body = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 100,
        "top_p": 0.9,
    }

    with respx.mock:
        upstream = respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(200, json={})
        )

        response = await client.post(
            "/v1/chat/completions",
            json=request_body,
            headers=auth_headers,
        )

    assert response.status_code == 400
    assert upstream.called is False
    body = response.json()
    assert "top_p" in body["error"]["message"]
    assert "dropped_field" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 2. Anthropic client -> OpenAI upstream: mirror
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_client_openai_upstream_roundtrip(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Anthropic /v1/messages with an OpenAI-native model transcodes."""
    request_body = {
        "model": "gpt-4",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hello!"}],
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl-123",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "gpt-4",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "Hi there!",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
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

    # Verify upstream received OpenAI-format request
    upstream_body = json.loads(captured["body"])
    assert upstream_body["model"] == "gpt-4"
    assert upstream_body["messages"] == [{"role": "user", "content": "Hello!"}]

    # Verify client received Anthropic-format response
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "gpt-4"
    assert body["stop_reason"] == "end_turn"
    assert len(body["content"]) == 1
    assert body["content"][0]["type"] == "text"
    assert body["content"][0]["text"] == "Hi there!"
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 5


# ---------------------------------------------------------------------------
# 3. Streaming: OpenAI client -> Anthropic upstream -> translated SSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_openai_client_anthropic_upstream(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Streaming OpenAI client to Anthropic upstream produces OpenAI SSE."""
    sse_content = "\n".join(_build_upstream_sse_events()) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200

    text = response.text
    # Must contain OpenAI SSE chunks, not Anthropic event types
    assert "chat.completion.chunk" in text
    assert "data: [DONE]" in text
    # Content deltas are translated
    assert "Hello" in text
    assert " world" in text
    # Usage is present
    assert "prompt_tokens" in text
    assert "completion_tokens" in text


# ---------------------------------------------------------------------------
# 4. Streaming: Anthropic client -> OpenAI upstream -> translated SSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_anthropic_client_openai_upstream(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Streaming Anthropic client to OpenAI upstream produces Anthropic SSE."""
    sse_content = "\n".join(_build_openai_stream_events()) + "\n"

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        response = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200

    text = response.text
    # Must contain Anthropic SSE event types, not OpenAI chunks
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text
    # Content is translated
    assert "Hi" in text
    # Usage is present in Anthropic format
    assert "input_tokens" in text
    assert "output_tokens" in text


# ---------------------------------------------------------------------------
# 5. Error pass-through: upstream 400 -> client-protocol error envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_openai_client_anthropic_upstream_400(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Upstream Anthropic 400 error re-rendered as OpenAI error envelope."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Invalid model parameter",
                    },
                },
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 400
    body = response.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"
    assert "Invalid model parameter" in body["error"]["message"]


@pytest.mark.asyncio
async def test_error_anthropic_client_openai_upstream_400(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Upstream OpenAI 400 error re-rendered as Anthropic error envelope."""
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{OPENAI_PATH}").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Invalid request",
                        "type": "invalid_request_error",
                        "code": "invalid_model",
                    }
                },
            )
        )

        response = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "invalid_request_error"
    assert "Invalid request" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 6. Usage is recorded in the database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_recorded_after_transcode(
    app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    """After transcoded request completes, usage recorded in DB."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "msg-abc",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello!"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 15,
                            "output_tokens": 8,
                        },
                    },
                )
            )

            response = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )

        assert response.status_code == 200
        proxy_id = response.headers.get("x-proxy-request-id")
        assert proxy_id is not None

        db = app.state.db
        row = await db.fetch_one(
            "SELECT input_tokens, output_tokens "
            "FROM requests WHERE proxy_request_id = ?",
            (proxy_id,),
        )
        assert row is not None
        assert row["input_tokens"] == 15
        assert row["output_tokens"] == 8


# ---------------------------------------------------------------------------
# 7. Native-protocol requests skip transcoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_protocol_skips_transcoding(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """When client protocol matches model native, no transcoding occurs."""
    request_body = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 100,
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg-native",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OK"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": 2,
                        },
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

    # Upstream body should be original Anthropic format (no translation)
    upstream_body = json.loads(captured["body"])
    assert upstream_body["model"] == "claude-3"
    assert upstream_body["messages"] == [{"role": "user", "content": "Hello!"}]

    # Response should be Anthropic format (no translation)
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "OK"


# ---------------------------------------------------------------------------
# 8. Phase 1 regression: coordinator receives transcoder policy from startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_effort_translated_when_thinking_enabled(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """OpenAI reasoning_effort is translated to Anthropic thinking when enabled."""
    app.state.coordinator._transcoder_policy = TranscoderPolicy(
        enabled=True,
        prefer_native=True,
        features=TranscoderFeatures(thinking=True),
    )

    request_body = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "test"}],
        "reasoning_effort": "medium",
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OK"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 2},
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
    assert upstream_body["thinking"] == {"type": "enabled", "budget_tokens": 4096}


@pytest.mark.asyncio
async def test_reasoning_effort_dropped_when_thinking_disabled(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OpenAI reasoning_effort is dropped when thinking transcoding is disabled."""
    app.state.coordinator._transcoder_policy = TranscoderPolicy(
        enabled=True,
        prefer_native=True,
        features=TranscoderFeatures(thinking=False),
    )

    request_body = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "test"}],
        "reasoning_effort": "medium",
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OK"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    },
                ),
            )[-1]
        )

        with caplog.at_level(logging.INFO, logger="eggpool.request.coordinator"):
            response = await client.post(
                "/v1/chat/completions",
                json=request_body,
                headers=auth_headers,
            )

    assert response.status_code == 200
    upstream_body = json.loads(captured["body"])
    assert "thinking" not in upstream_body
    assert upstream_body.get("reasoning_effort") is None

    warning_texts = [r.message for r in caplog.records]
    assert any(
        "reasoning_effort" in t and "thinking_disabled" in t for t in warning_texts
    ), (
        "Expected a dropped_field loss warning for reasoning_effort with "
        "reason=thinking_disabled in coordinator logs"
    )


@pytest.mark.asyncio
async def test_assistant_reasoning_content_survives_when_enabled(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """Assistant reasoning_content becomes Anthropic thinking block when enabled."""
    app.state.coordinator._transcoder_policy = TranscoderPolicy(
        enabled=True,
        prefer_native=True,
        features=TranscoderFeatures(thinking=True),
    )

    request_body = {
        "model": "claude-3",
        "messages": [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "reasoning_content": "Let me think about this...",
                "content": "The answer is 42.",
            },
            {"role": "user", "content": "continue"},
        ],
        "max_tokens": 100,
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OK"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 2},
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
    assistant_msg = upstream_body["messages"][1]
    assert assistant_msg["content"][0] == {
        "type": "thinking",
        "thinking": "Let me think about this...",
    }
    assert assistant_msg["content"][1] == {
        "type": "text",
        "text": "The answer is 42.",
    }


@pytest.mark.asyncio
async def test_coordinator_policy_not_default(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """Coordinator uses the policy it was given, not a stale default."""
    policy = TranscoderPolicy(
        enabled=True,
        prefer_native=True,
        features=TranscoderFeatures(thinking=True),
    )
    app.state.coordinator._transcoder_policy = policy

    assert app.state.coordinator._transcoder_policy is policy
    assert app.state.coordinator._transcoder_policy.features.thinking is True

    request_body = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "test"}],
        "reasoning_effort": "high",
    }

    with respx.mock:
        captured: dict[str, Any] = {}

        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=lambda request: (
                captured.update({"body": request.content}),
                httpx.Response(
                    200,
                    json={
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OK"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 2},
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
    assert upstream_body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
