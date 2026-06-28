"""End-to-end test for the canonical MiniMax transcoding scenario.

An OpenAI client posts to /v1/chat/completions with
model: "MiniMax-M2.7/minimax". The model resolves to "anthropic" in the
catalogue, the transcoder translates OpenAI <-> Anthropic, and the
response is rendered as OpenAI.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

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
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.routing.router import Router
from eggpool.transcoder.policy import TranscoderPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

UPSTREAM_BASE = "https://api.minimax.io"


def _build_minimax_config() -> AppConfig:
    """Config mimicking the MiniMax International scenario."""
    os.environ["MINIMAX_KEY"] = "test-minimax-key"
    return AppConfig.from_dict(
        {
            "server": {"api_key_env": "TEST_KEY", "host": "127.0.0.1", "port": 0},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "providers": {
                "minimax": {
                    "id": "minimax",
                    "base_url": f"{UPSTREAM_BASE}/anthropic",
                    "protocols": ["anthropic"],
                    "auth": {"mode": "api_key", "header": "x-api-key"},
                    "accounts": [
                        {"name": "minimax-acct", "api_key_env": "MINIMAX_KEY"},
                    ],
                },
            },
            "dashboard": {"enabled": False},
            "transcoder": {"enabled": True, "prefer_native": True},
        }
    )


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_KEY", "test-minimax-key")
    monkeypatch.setenv("TEST_KEY", "test-key-123")


@pytest_asyncio.fixture()
async def minimax_coordinator() -> AsyncGenerator[RequestCoordinator, None]:
    config = _build_minimax_config()
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("minimax-acct", "MINIMAX_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("MiniMax-M2.7", "anthropic"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="MiniMax-M2.7",
        display_name="MiniMax M2.7",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("MiniMax-M2.7", "minimax-acct")
    catalog.cache.set_account_provider("minimax-acct", "minimax")

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    transcoder_policy = TranscoderPolicy(enabled=True, prefer_native=True)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
        transcoder_policy=transcoder_policy,
        config=config,
    )
    yield coord
    await httpx_client.aclose()
    await db.disconnect()


@pytest.mark.asyncio
async def test_minimax_openai_to_anthropic_transcode(
    minimax_coordinator: RequestCoordinator,
) -> None:
    """OpenAI client -> MiniMax-M2.7/minimax -> Anthropic upstream -> 200."""
    context = ProxyRequestContext(
        request_id="minimax-e2e",
        protocol="openai",
        model_id="MiniMax-M2.7",
        streaming=False,
        original_body=b'{"model":"MiniMax-M2.7/minimax","messages":[{"role":"user","content":"Hello"}]}',
        incoming_headers={"content-type": "application/json"},
        provider_id="minimax",
    )
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/anthropic/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg-minimax-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                    "model": "MiniMax-M2.7",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 8, "output_tokens": 3},
                },
            ),
        )
        response = await minimax_coordinator.execute(context)

    assert response.status_code == 200
    assert context.upstream_protocol == "anthropic"
    assert context.transcode_required is True


@pytest.mark.asyncio
async def test_minimax_context_protocol_is_set(
    minimax_coordinator: RequestCoordinator,
) -> None:
    """After _validate_endpoint_or_transcode, upstream_protocol is 'anthropic'."""
    context = ProxyRequestContext(
        request_id="minimax-proto",
        protocol="openai",
        model_id="MiniMax-M2.7",
        streaming=False,
        original_body=b'{"model":"MiniMax-M2.7/minimax","messages":[{"role":"user","content":"Hi"}]}',
        incoming_headers={"content-type": "application/json"},
        provider_id="minimax",
    )
    minimax_coordinator._validate_endpoint_or_transcode(context)
    assert context.upstream_protocol == "anthropic"
    assert context.transcode_required is True
