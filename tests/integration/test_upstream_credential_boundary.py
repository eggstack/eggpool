"""Integration tests for the local-to-upstream credential boundary.

A locally supplied ``Authorization``, ``X-Api-Key``, or
``Proxy-Authorization`` must never reach the upstream service. The
upstream must receive exactly the credential of the selected account.

Both protocol endpoint families (``/v1/chat/completions`` and
``/v1/messages``) are covered.
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
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://test-upstream.example.com"

LOCAL_BEARER_SECRET = "LOCAL_BEARER_SECRET_MARKER"
LOCAL_X_API_SECRET = "LOCAL_X_API_SECRET_MARKER"
LOCAL_PROXY_SECRET = "LOCAL_PROXY_SECRET_MARKER"
UPSTREAM_ACCOUNT_SECRET = "UPSTREAM_ACCOUNT_SECRET_MARKER"


def _build_config() -> AppConfig:
    os.environ["GO_AGG_LOCAL_KEY"] = LOCAL_BEARER_SECRET
    os.environ["OPENCODE_TEST_KEY"] = UPSTREAM_ACCOUNT_SECRET
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "GO_AGG_LOCAL_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {
                "startup_refresh": False,
                "refresh_interval_s": 0,
            },
            "accounts": [
                {
                    "name": "test-acct",
                    "api_key_env": "OPENCODE_TEST_KEY",
                },
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def app(config: AppConfig) -> AsyncGenerator[FastAPI]:
    from eggpool.app import create_app

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


def _assert_no_local_secrets(headers: httpx.Headers) -> None:
    for name, value in headers.items():
        for marker in (
            LOCAL_BEARER_SECRET,
            LOCAL_X_API_SECRET,
            LOCAL_PROXY_SECRET,
        ):
            assert marker not in value, (
                f"Local secret {marker!r} survived header {name!r}={value!r}"
            )


def _assert_exactly_one_upstream_authorization(headers: httpx.Headers) -> None:
    auths = [(n, v) for n, v in headers.items() if n.lower() == "authorization"]
    assert len(auths) == 1, f"Expected exactly one Authorization header, got {auths}"
    name, value = auths[0]
    assert UPSTREAM_ACCOUNT_SECRET in value, (
        f"Authorization header {name!r}={value!r} does not contain the "
        "selected account credential."
    )


# ─── OpenAI-compatible endpoint ──────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_local_credentials_do_not_reach_upstream(
    app: FastAPI,
) -> None:
    """Local bearer/x-api/proxy-authorization are stripped before upstream."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        captured_request: list[httpx.Request] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured_request.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "ok",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_capture)
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "Authorization": f"Bearer {LOCAL_BEARER_SECRET}",
                    "X-Api-Key": LOCAL_X_API_SECRET,
                    "Proxy-Authorization": f"Basic {LOCAL_PROXY_SECRET}",
                },
            )

        assert response.status_code == 200
        assert len(captured_request) == 1
        upstream_headers = captured_request[0].headers
        _assert_no_local_secrets(upstream_headers)
        _assert_exactly_one_upstream_authorization(upstream_headers)
    finally:
        await client.aclose()


# ─── Anthropic-compatible endpoint ───────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_local_credentials_do_not_reach_upstream(
    app: FastAPI,
) -> None:
    """Anthropic endpoint must not forward local credentials either."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        captured_request: list[httpx.Request] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured_request.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/messages").mock(side_effect=_capture)
            response = await client.post(
                "/v1/messages",
                json={
                    "model": "claude-3",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "Authorization": f"Bearer {LOCAL_BEARER_SECRET}",
                    "X-Api-Key": LOCAL_X_API_SECRET,
                    "Proxy-Authorization": f"Basic {LOCAL_PROXY_SECRET}",
                    "anthropic-version": "2023-06-01",
                },
            )

        assert response.status_code == 200
        assert len(captured_request) == 1
        upstream_headers = captured_request[0].headers
        _assert_no_local_secrets(upstream_headers)
        _assert_exactly_one_upstream_authorization(upstream_headers)
    finally:
        await client.aclose()


# ─── Local x-api-key alone ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_x_api_key_does_not_replace_upstream_authorization(
    app: FastAPI,
) -> None:
    """A local x-api-key must not become the upstream Authorization."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        captured_request: list[httpx.Request] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured_request.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-2",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "ok",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_capture)
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "Authorization": f"Bearer {LOCAL_BEARER_SECRET}",
                },
            )

        assert response.status_code == 200
        assert len(captured_request) == 1
        upstream_headers = captured_request[0].headers
        _assert_no_local_secrets(upstream_headers)
        _assert_exactly_one_upstream_authorization(upstream_headers)
        # x-api-key must not be injected as the upstream credential.
        x_api_keys = [
            v for n, v in upstream_headers.items() if n.lower() == "x-api-key"
        ]
        for value in x_api_keys:
            assert value != LOCAL_BEARER_SECRET
            assert value != LOCAL_X_API_SECRET
    finally:
        await client.aclose()
