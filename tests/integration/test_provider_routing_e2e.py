"""End-to-end integration tests for multi-provider request routing.

Verifies that provider-suffixed model IDs are correctly parsed, routed
to the right provider's upstream, and that provider-specific paths and
clients are used.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

OPENCODE_BASE = "https://opencode.example.com/zen/go/v1"
MINIMAX_BASE = "https://api.minimaxi.com"


def _build_multi_provider_config() -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "GO_AGG_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": OPENCODE_BASE,
                    "protocols": ["openai", "anthropic"],
                    "openai_path": "/chat/completions",
                    "anthropic_path": "/messages",
                    "accounts": [
                        {"name": "oc-personal", "api_key_env": "OC_KEY_1"},
                    ],
                },
                "minimax": {
                    "id": "minimax",
                    "base_url": MINIMAX_BASE,
                    "protocols": ["openai", "anthropic"],
                    "openai_path": "/v1/chat/completions",
                    "anthropic_path": "/anthropic/v1/messages",
                    "accounts": [
                        {"name": "mm-prod", "api_key_env": "MM_KEY_1"},
                    ],
                },
            },
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GO_AGG_TEST_KEY", "test-key-123")
    monkeypatch.setenv("OC_KEY_1", "oc-test-key")
    monkeypatch.setenv("MM_KEY_1", "mm-test-key")


@pytest_asyncio.fixture()
async def app() -> AsyncGenerator[FastAPI]:
    config = _build_multi_provider_config()
    application = create_app(config)

    db = Database(path=":memory:")
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    # Insert accounts with provider_id
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("oc-personal", "OC_KEY_1", "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("mm-prod", "MM_KEY_1", "minimax"),
        )
        # Insert models
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )

    # Create per-provider HTTP clients
    oc_client = httpx.AsyncClient(
        base_url=OPENCODE_BASE,
        timeout=httpx.Timeout(connect=5, read=300, write=30, pool=5),
    )
    mm_client = httpx.AsyncClient(
        base_url=MINIMAX_BASE,
        timeout=httpx.Timeout(connect=5, read=300, write=30, pool=5),
    )
    client_pool = ProviderClientPool()
    client_pool.register("opencode-go", oc_client)
    client_pool.register("minimax", mm_client)

    application.state.client_pool = client_pool
    application.state.httpx_client = oc_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, client_pool)
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
        client_pool=client_pool,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
        config=config,
    )
    application.state.coordinator = coordinator

    # Load models into catalog with provider tracking
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.set_account_provider("oc-personal", "opencode-go")
    catalog.cache.set_account_provider("mm-prod", "minimax")
    catalog.cache.add_account_support("gpt-4", "oc-personal")
    catalog.cache.add_account_support("gpt-4", "mm-prod")

    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "oc-personal")
    catalog.cache.add_account_support("claude-3", "mm-prod")

    yield application

    await client_pool.close()
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


MOCK_OPENAI_RESPONSE = {
    "id": "cmpl-1",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from provider"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    },
}


MOCK_ANTHROPIC_RESPONSE = {
    "id": "msg-1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello from provider"}],
    "model": "claude-3",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


# ── OpenCode Go provider: OpenAI path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_opencode_go_provider_openai_path(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Request to opencode-go provider uses /chat/completions path."""
    with respx.mock:
        route = respx.post(f"{OPENCODE_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json=MOCK_OPENAI_RESPONSE)
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4/opencode-go",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Hello from provider"
    assert json.loads(route.calls.last.request.content)["model"] == "gpt-4"


# ── MiniMax provider: OpenAI path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_minimax_provider_openai_path(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Request to minimax provider uses /v1/chat/completions path."""
    with respx.mock:
        route = respx.post(f"{MINIMAX_BASE}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=MOCK_OPENAI_RESPONSE)
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4/minimax",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Hello from provider"
    assert json.loads(route.calls.last.request.content)["model"] == "gpt-4"


@pytest.mark.asyncio
async def test_provider_suffix_is_removed_from_streaming_payload(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Streaming rewrites the model while retaining usage-option injection."""
    stream_body = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    with respx.mock:
        route = respx.post(f"{MINIMAX_BASE}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=stream_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4/minimax",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    sent_payload = json.loads(route.calls.last.request.content)
    assert sent_payload["model"] == "gpt-4"
    assert sent_payload["stream_options"] == {"include_usage": True}


# ── MiniMax provider: Anthropic path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_minimax_provider_anthropic_path(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Request to minimax provider uses /anthropic/v1/messages path."""
    with respx.mock:
        route = respx.post(f"{MINIMAX_BASE}/anthropic/v1/messages").mock(
            return_value=httpx.Response(200, json=MOCK_ANTHROPIC_RESPONSE)
        )

        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3/minimax",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 100,
            },
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["content"][0]["text"] == "Hello from provider"
    assert json.loads(route.calls.last.request.content)["model"] == "claude-3"


# ── No provider suffix: defaults to opencode-go ────────────────────────────


@pytest.mark.asyncio
async def test_no_provider_suffix_defaults_to_opencode_go(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Request without provider suffix uses default opencode-go provider."""
    with respx.mock:
        respx.post(f"{OPENCODE_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json=MOCK_OPENAI_RESPONSE)
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


# ── Provider-suffixed model appears in /v1/models ─────────────────────────


@pytest.mark.asyncio
async def test_models_endpoint_returns_suffixed_ids(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The /v1/models endpoint returns provider-suffixed model IDs."""
    response = await client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    model_ids = {m["id"] for m in body["data"]}
    # Both providers support gpt-4 and claude-3, so we get suffixed IDs
    assert "gpt-4/opencode-go" in model_ids
    assert "gpt-4/minimax" in model_ids
    assert "claude-3/opencode-go" in model_ids
    assert "claude-3/minimax" in model_ids


# ── Database records provider_id ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_records_provider_id(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Completed requests record the provider_id."""
    with respx.mock:
        respx.post(f"{MINIMAX_BASE}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=MOCK_OPENAI_RESPONSE)
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4/minimax",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=auth_headers,
        )

    assert response.status_code == 200

    db: Database = app.state.db
    row = await db.fetch_one(
        "SELECT provider_id FROM requests ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["provider_id"] == "minimax"


# ── Collapsed /v1/models shape ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collapsed_models_endpoint_emits_providers_and_max_priority(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """With ``collapse_models=true`` the endpoint emits ``providers`` and
    ``routing_priority_max`` for each collapsed entry.
    """
    config = app.state.config
    new_config = config.model_copy(
        update={
            "models": config.models.model_copy(update={"collapse_models": True}),
            "providers": {
                **config.providers,
                "opencode-go": config.providers["opencode-go"].model_copy(
                    update={"routing_priority": 0}
                ),
                "minimax": config.providers["minimax"].model_copy(
                    update={"routing_priority": 2}
                ),
            },
        }
    )
    app.state.config = new_config
    # Catalog service holds its own config reference; sync it so the
    # new collapse_models flag takes effect.
    app.state.catalog._config = new_config  # type: ignore[attr-defined]

    response = await client.get("/v1/models", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    # Find the collapsed gpt-4 entry
    gpt_entries = [m for m in body["data"] if m["id"] == "gpt-4"]
    assert len(gpt_entries) == 1
    gpt = gpt_entries[0]
    assert gpt["eggpool"]["providers"] == ["minimax", "opencode-go"]
    assert gpt["eggpool"]["routing_priority_max"] == 2
    # No per-provider fields on a collapsed entry
    assert "provider_id" not in gpt["eggpool"]
    assert "routing_priority" not in gpt["eggpool"]
