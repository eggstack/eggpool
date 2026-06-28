"""Integration tests for transcoded routing and failover."""

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

UPSTREAM_BASE = "https://api.example.com"


def _build_transcode_config() -> AppConfig:
    """Config with an OpenAI-only provider and an Anthropic-only provider."""
    os.environ["TEST_KEY"] = "test-key-123"
    return AppConfig.from_dict(
        {
            "server": {"api_key_env": "TEST_KEY", "host": "127.0.0.1", "port": 0},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "providers": {
                "openai-provider": {
                    "id": "openai-provider",
                    "base_url": f"{UPSTREAM_BASE}/openai",
                    "protocols": ["openai"],
                    "accounts": [
                        {"name": "openai-acct", "api_key_env": "TEST_KEY"},
                    ],
                },
                "anthropic-provider": {
                    "id": "anthropic-provider",
                    "base_url": f"{UPSTREAM_BASE}/anthropic",
                    "protocols": ["anthropic"],
                    "auth": {"mode": "api_key", "header": "x-api-key"},
                    "accounts": [
                        {"name": "anthropic-acct", "api_key_env": "TEST_KEY"},
                    ],
                },
            },
            "dashboard": {"enabled": False},
            "transcoder": {"enabled": True},
        }
    )


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "test-key-123")


@pytest_asyncio.fixture()
async def two_account_db() -> AsyncGenerator[Database, None]:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("openai-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("anthropic-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )
    yield db
    await db.disconnect()


@pytest_asyncio.fixture()
async def coordinator(
    two_account_db: Database,
) -> AsyncGenerator[RequestCoordinator, None]:
    config = _build_transcode_config()
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, two_account_db, httpx_client)
    # Model "claude-3" resolves to "anthropic" protocol
    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "anthropic-acct")
    catalog.cache.set_account_provider("anthropic-acct", "anthropic-provider")

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    transcoder_policy = TranscoderPolicy(enabled=True)

    request_repo = RequestRepository(two_account_db)
    reservation_repo = ReservationRepository(two_account_db)
    attempt_repo = AttemptRepository(two_account_db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=two_account_db,
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


@pytest.mark.asyncio
async def test_transcoding_widens_candidate_set(
    coordinator: RequestCoordinator,
) -> None:
    """OpenAI request to Anthropic-only model succeeds with transcoding."""
    context = ProxyRequestContext(
        request_id="transcode-widen",
        protocol="openai",
        model_id="claude-3",
        streaming=False,
        original_body=b'{"model":"claude-3","messages":[{"role":"user","content":"Hi"}]}',
        incoming_headers={"content-type": "application/json"},
    )
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/anthropic/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            ),
        )
        response = await coordinator.execute(context)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_transcode_sets_upstream_protocol(
    coordinator: RequestCoordinator,
) -> None:
    """Transcoding sets context.upstream_protocol to model's native protocol."""
    context = ProxyRequestContext(
        request_id="transcode-proto",
        protocol="openai",
        model_id="claude-3",
        streaming=False,
        original_body=b'{"model":"claude-3","messages":[{"role":"user","content":"Hi"}]}',
        incoming_headers={"content-type": "application/json"},
    )
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/anthropic/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            ),
        )
        response = await coordinator.execute(context)
    assert response.status_code == 200
    assert context.upstream_protocol == "anthropic"
    assert context.transcode_required is True


@pytest.mark.asyncio
async def test_native_protocol_no_transcoding() -> None:
    """When client protocol matches model protocol, no transcoding needed."""
    config = _build_transcode_config()
    os.environ["TEST_KEY"] = "test-key-123"
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("openai-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("anthropic-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "anthropic-acct")
    catalog.cache.set_account_provider("anthropic-acct", "anthropic-provider")

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    transcoder_policy = TranscoderPolicy(enabled=True)

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

    context = ProxyRequestContext(
        request_id="native-proto",
        protocol="anthropic",
        model_id="claude-3",
        streaming=False,
        original_body=b'{"model":"claude-3","messages":[{"role":"user","content":"Hi"}]}',
        incoming_headers={"content-type": "application/json"},
    )
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/anthropic/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            ),
        )
        response = await coord.execute(context)
    assert response.status_code == 200
    # No transcoding needed: upstream matches client protocol
    assert context.transcode_required is False
    await httpx_client.aclose()
    await db.disconnect()
