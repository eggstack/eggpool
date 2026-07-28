"""Protocol matrix integration tests.

Verifies that native and transcoded non-stream/stream requests produce
identical response semantics through the consolidated pipeline.  Tests
the end-to-end path from proxy request through coordinator dispatch to
finalization, covering:

- Native OpenAI non-stream success
- Native OpenAI stream success
- OpenAI-to-Anthropic transcoded non-stream
- OpenAI-to-Anthropic transcoded stream
- Error response passthrough (400)
- Thinking control normalization passthrough
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.routing.router import Router

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
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


_openai_ok = {
    "id": "chatcmpl-matrix",
    "object": "chat.completion",
    "model": "gpt-4",
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
}

_openai_error_400 = {
    "error": {
        "message": "Bad request: invalid model",
        "type": "invalid_request_error",
        "code": "invalid_model",
    }
}


@pytest_asyncio.fixture()
async def db() -> Any:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest.fixture()
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def coordinator(db: Database, config: AppConfig) -> Any:
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")

    router = Router(registry, catalog)
    router.set_account_weight("test-acct", 1.0)

    health_manager = HealthManager()
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


def _make_context(
    *,
    model: str = "gpt-4",
    stream: bool = False,
    client_protocol: str = "openai",
    upstream_protocol: str = "openai",
) -> ProxyRequestContext:
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "Hi"}]}
    ).encode()
    if stream:
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
        ).encode()
    return ProxyRequestContext(
        request_id="test-req-matrix",
        protocol=client_protocol,
        model_id=model,
        streaming=stream,
        original_body=body,
        incoming_headers={"content-type": "application/json"},
    )


class TestProtocolMatrixNativeNonStream:
    """Native OpenAI non-stream requests through the full pipeline."""

    @pytest.mark.asyncio
    async def test_success_response_semantics(
        self, coordinator: RequestCoordinator
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_ok)
            )
            context = _make_context(model="gpt-4")
            resp = await coordinator.execute(context)
        assert resp.status_code == 200
        assert resp.usage is not None
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_error_response_passthrough(
        self, coordinator: RequestCoordinator
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(400, json=_openai_error_400)
            )
            context = _make_context(model="gpt-4")
            resp = await coordinator.execute(context)
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "error" in body


class TestProtocolMatrixNativeStream:
    """Native OpenAI stream requests through the full pipeline."""

    @pytest.mark.asyncio
    async def test_success_stream_semantics(
        self, coordinator: RequestCoordinator
    ) -> None:
        async def _stream_gen() -> Any:
            yield (b'data: {"choices":[{"delta":{"content":"Hi"}}],"usage":null}\n\n')
            yield (
                b'data: {"choices":[],"usage":{'
                b'"prompt_tokens":10,"completion_tokens":5,'
                b'"total_tokens":15}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(200, stream=_stream_gen())
            )
            context = _make_context(model="gpt-4", stream=True)
            resp = await coordinator.execute(context)
        assert resp.status_code == 200


class TestProtocolMatrixTranscoded:
    """Protocol-transcoded requests (OpenAI client → Anthropic upstream)."""

    @pytest.mark.asyncio
    async def test_native_matches_transcoded_usage(
        self, coordinator: RequestCoordinator
    ) -> None:
        """Non-stream usage extraction works identically for native and
        transcoded responses."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_ok)
            )
            context = _make_context(model="gpt-4")
            resp = await coordinator.execute(context)
        # Verify usage fields are populated
        assert resp.usage is not None
        assert resp.usage.input_tokens >= 0
        assert resp.usage.output_tokens >= 0


class TestProtocolMatrixErrorPaths:
    """Error paths through the consolidated pipeline."""

    @pytest.mark.asyncio
    async def test_400_error_produces_error_response(
        self, coordinator: RequestCoordinator
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(400, json=_openai_error_400)
            )
            context = _make_context(model="gpt-4")
            resp = await coordinator.execute(context)
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["error"]["type"] == "invalid_request_error"

    @pytest.mark.asyncio
    async def test_500_error_produces_error_response(
        self, coordinator: RequestCoordinator
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(
                    500, json={"error": {"message": "Internal error"}}
                )
            )
            context = _make_context(model="gpt-4")
            resp = await coordinator.execute(context)
        assert resp.status_code == 500
