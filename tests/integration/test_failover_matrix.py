"""Integration tests for failover and health behavior (Section 6).

Tests that:
- Retryable errors (401, 402, 429, 500, connect fail) failover to another account
- Same account is never attempted twice
- Client 400 does not retry
- Exhausted retries return final upstream status/body
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
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
from go_aggregator.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from go_aggregator.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_two_account_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    os.environ["OPENCODE_TEST_KEY_2"] = "test-key-456"
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
            "accounts": [
                {"name": "acct-a", "api_key_env": "OPENCODE_TEST_KEY"},
                {"name": "acct-b", "api_key_env": "OPENCODE_TEST_KEY_2"},
            ],
            "dashboard": {"enabled": False},
        }
    )


_success_response = httpx.Response(
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


def _error_response(status: int, body: str = '{"error": "fail"}') -> httpx.Response:
    return httpx.Response(status, content=body.encode())


_success_body = json.dumps(
    {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hi"}],
    }
).encode()


@pytest_asyncio.fixture()
async def two_account_db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    await database.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("acct-a", "OPENCODE_TEST_KEY"),
    )
    await database.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("acct-b", "OPENCODE_TEST_KEY_2"),
    )
    await database.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
    )
    await database.connection.commit()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def coordinator(
    two_account_db: Database,
) -> AsyncGenerator[RequestCoordinator, None]:
    config = _build_two_account_config()
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, two_account_db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "acct-a")
    catalog.cache.add_account_support("gpt-4", "acct-b")

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    router.set_account_weight("acct-a", 1.0)
    router.set_account_weight("acct-b", 1.0)

    request_repo = RequestRepository(two_account_db)
    reservation_repo = ReservationRepository(two_account_db)
    attempt_repo = AttemptRepository(two_account_db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=two_account_db,
        httpx_client=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
        max_retry_attempts=2,
    )
    yield coord
    await httpx_client.aclose()


@pytest.mark.asyncio
async def test_failover_401_to_success(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """Account A returns 401, B returns 200."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return _error_response(401, '{"error": "unauthorized"}')
        return _success_response

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="failover-401",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.account_name in ("acct-a", "acct-b")
    assert call_count[0] == 2

    # Both attempts should be recorded with different accounts
    attempts = await two_account_db.fetch_all(
        "SELECT a.account_id FROM request_attempts a "
        "JOIN accounts ac ON a.account_id = ac.id "
        "ORDER BY a.attempt_number"
    )
    assert len(attempts) == 2
    account_names = set()
    for a in attempts:
        name = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE id = ?", (a["account_id"],)
        )
        if name:
            account_names.add(name["name"])
    assert len(account_names) == 2, "Failover did not use different accounts"


@pytest.mark.asyncio
async def test_failover_402_to_success(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """Account A returns 402, B returns 200."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return _error_response(402, '{"error": "quota exceeded"}')
        return _success_response

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="failover-402",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.account_name in ("acct-a", "acct-b")
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_failover_429_to_success(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """Account A returns 429, B returns 200."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return _error_response(429, '{"error": "rate limited"}')
        return _success_response

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="failover-429",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.account_name in ("acct-a", "acct-b")
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_failover_connect_error_to_success(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """Account A connection fails, B returns 200."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            raise httpx.ConnectError("Connection refused")
        return _success_response

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="failover-connect",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.account_name in ("acct-a", "acct-b")


@pytest.mark.asyncio
async def test_failover_500_to_success(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """Account A returns 500, B returns 200."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return _error_response(500, '{"error": "internal error"}')
        return _success_response

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="failover-500",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.account_name in ("acct-a", "acct-b")
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_same_account_never_attempted_twice(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """When first account fails, second attempt must use different account."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        # First call fails (account-a), second succeeds (account-b)
        if call_count[0] == 1:
            return _error_response(401, '{"error": "unauthorized"}')
        return _success_response

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="no-double-attempt",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200

    # Verify different accounts were used
    attempts = await two_account_db.fetch_all(
        "SELECT a.account_id FROM request_attempts a "
        "JOIN accounts ac ON a.account_id = ac.id "
        "ORDER BY a.attempt_number"
    )
    assert len(attempts) == 2
    account_ids = [a["account_id"] for a in attempts]
    assert len(set(account_ids)) == 2, "Same account attempted twice"


@pytest.mark.asyncio
async def test_client_400_does_not_retry(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """Client 400 errors should not trigger failover."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return _error_response(400, '{"error": "invalid request"}')

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="no-retry-400",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 400
    assert response.body is not None
    assert b"invalid request" in response.body
    # Only one attempt - no retry
    attempts = await two_account_db.fetch_all("SELECT * FROM request_attempts")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_exhausted_returns_final_upstream_status(
    coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """When all retries exhausted, return the final upstream status/body."""
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return _error_response(500, '{"error": "persistent failure"}')

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="exhausted",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=_success_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 500
    assert response.body is not None
    assert b"persistent failure" in response.body
    assert response.account_name in ("acct-a", "acct-b")
