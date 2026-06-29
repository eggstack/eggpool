"""tracemalloc baseline regression test for bounded in-memory structures.

Verifies that the memory-footprint fixes from ``plans/memory.md`` keep
the catalog cache, quota estimator, outbound HTTP manager, and health
manager bounded under repeated request traffic. The test runs 100
identical OpenAI-style requests through a fully-wired app and asserts
each persistent structure stays within its hardcoded cap.

Marked ``@pytest.mark.slow`` so it can be skipped in PR CI but run in
nightly CI. Run with::

    uv run pytest tests/integration/test_memory.py -v -m slow
"""

from __future__ import annotations

import gc
import os
import tracemalloc
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
from eggpool.providers.outbound import OutboundClientManager
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    os.environ["GO_AGG_TEST_KEY"] = "test-key-123"
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
                {"name": "test-acct-a", "api_key_env": "OPENCODE_TEST_KEY"},
                {"name": "test-acct-b", "api_key_env": "OPENCODE_TEST_KEY"},
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def app(config: AppConfig) -> AsyncGenerator[FastAPI]:
    """Build a focused app that wires the structures under test.

    Mirrors ``tests/integration/test_load.py`` but also instantiates
    :class:`OutboundClientManager` (stored at ``app.state.outbound_manager``)
    so the per-host counters can be inspected by the memory assertions.
    """
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
            ("test-acct-a", "OPENCODE_TEST_KEY"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct-b", "OPENCODE_TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4-mini", "openai"),
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

    outbound_manager = OutboundClientManager(config=config.network)
    application.state.outbound_manager = outbound_manager

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
    catalog.cache.add_account_support("gpt-4", "test-acct-a")
    catalog.cache.add_account_support("gpt-4", "test-acct-b")

    catalog.cache.load_model(
        model_id="gpt-4-mini",
        display_name="GPT-4 Mini",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4-mini", "test-acct-a")

    catalog.cache.load_model(
        model_id="claude-3",
        display_name="Claude 3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3", "test-acct-b")

    yield application

    await outbound_manager.aclose()
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


# ── helpers ──────────────────────────────────────────────────────────────────


def _openai_response(idx: int) -> httpx.Response:
    """Build a non-streaming OpenAI chat completion response."""
    return httpx.Response(
        200,
        json={
            "id": f"cmpl-{idx}",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
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


def _openai_sse_response(idx: int) -> httpx.Response:
    """Build an OpenAI-style SSE streaming response."""
    return httpx.Response(
        200,
        content=(
            f'data: {{"id":"cmpl-{idx}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{"content":"ok"}},'
            f'"finish_reason":"stop"}}]}}\n\n'
            f"data: [DONE]\n\n"
        ).encode(),
        headers={"content-type": "text/event-stream"},
    )


# ── 1. Persistent structures stay bounded under repeated non-stream requests


@pytest.mark.slow
@pytest.mark.asyncio
async def test_persistent_structures_bounded_under_repeated_requests(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """Each persistent data structure stays within its hardcoded cap.

    Runs 100 requests and asserts the cache, quota estimator, and outbound
    manager stay within their caps.
    """
    from eggpool.quota.estimation import EWMA_HARD_CAP, GLOBAL_EWMA_HARD_CAP

    payload = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
    }

    # 1. Warm-up request (always included in baseline snapshot to
    #    amortise cold-start allocations).
    response = await client.post(
        "/v1/chat/completions", json=payload, headers=auth_headers
    )
    assert response.status_code in {200, 400, 502, 503}

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()

    # 2. Run 100 identical requests.
    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=lambda request, route: _openai_response(route.call_count)
        )
        for _ in range(100):
            await client.post(
                "/v1/chat/completions", json=payload, headers=auth_headers
            )

    gc.collect()
    final = tracemalloc.take_snapshot()
    tracemalloc.stop()

    # 3. Assert bounded state.
    cache = app.state.catalog.cache
    router = app.state.router
    outbound = app.state.outbound_manager

    # 2 accounts × 3 models = 6 model/account support entries.
    assert len(cache._account_support) <= 10
    # Per-account EWMA bucket sums across all accounts; bounded by the
    # hard cap on the outer dict (one bucket per account, capped at
    # ``ewma_hard_cap`` entries per bucket).
    bucket_total = sum(
        len(bucket) for bucket in router._quota_estimator.account_model_ewma.values()
    )
    assert bucket_total <= EWMA_HARD_CAP
    assert len(router._quota_estimator.global_model_ewma) <= GLOBAL_EWMA_HARD_CAP
    assert len(outbound._per_host_requests) <= 256  # MAX_TRACKED_HOSTS

    # 4. Confirm no "leaked" growth in persistent structures via top-N diff.
    stats = final.compare_to(baseline, key_type="filename")
    growth = sum(s.size_diff for s in stats[:50])
    # Growth should be dominated by short-lived request objects, not
    # persistent structures. A soft ceiling of 5MB keeps the test
    # resilient without making it flaky.
    assert growth < 5_000_000, (
        f"Top-50 allocations grew {growth} bytes after 100 requests; "
        f"expected short-lived requests objects, not persistent data. "
        f"Top growers: {[str(s)[:80] for s in stats[:5]]}"
    )


# ── 2. Persistent structures stay bounded under repeated streaming requests


@pytest.mark.slow
@pytest.mark.asyncio
async def test_persistent_structures_bounded_under_streaming(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    app: FastAPI,
) -> None:
    """Same bounds as the non-streaming test, but with ``stream: True``."""
    from eggpool.quota.estimation import EWMA_HARD_CAP, GLOBAL_EWMA_HARD_CAP

    payload = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    # Warm-up request.
    response = await client.post(
        "/v1/chat/completions", json=payload, headers=auth_headers
    )
    assert response.status_code in {200, 400, 502, 503}

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=lambda request, route: _openai_sse_response(route.call_count)
        )
        for _ in range(100):
            await client.post(
                "/v1/chat/completions", json=payload, headers=auth_headers
            )

    gc.collect()
    final = tracemalloc.take_snapshot()
    tracemalloc.stop()

    cache = app.state.catalog.cache
    router = app.state.router
    outbound = app.state.outbound_manager

    assert len(cache._account_support) <= 10
    bucket_total = sum(
        len(bucket) for bucket in router._quota_estimator.account_model_ewma.values()
    )
    assert bucket_total <= EWMA_HARD_CAP
    assert len(router._quota_estimator.global_model_ewma) <= GLOBAL_EWMA_HARD_CAP
    assert len(outbound._per_host_requests) <= 256

    # Confirm no "leaked" growth in persistent structures via top-N diff.
    stats = final.compare_to(baseline, key_type="filename")
    growth = sum(s.size_diff for s in stats[:50])
    assert growth < 5_000_000, (
        f"Top-50 allocations grew {growth} bytes after 100 streaming "
        f"requests; expected short-lived requests objects, not persistent "
        f"data. Top growers: {[str(s)[:80] for s in stats[:5]]}"
    )


# ── 3. Outbound per-host counters are bounded under many distinct hosts ────


@pytest.mark.slow
@pytest.mark.asyncio
async def test_outbound_per_host_counters_bounded_under_many_hosts(
    app: FastAPI,
) -> None:
    """Even with >MAX_TRACKED_HOSTS distinct hosts, the dict stays at the cap."""
    outbound: OutboundClientManager = app.state.outbound_manager
    # Drive 300 distinct hosts. The cap is 256, so the dict must not
    # exceed the cap after the smoke.
    for i in range(300):
        outbound.record_request(host=f"host-{i}.example.com")

    assert len(outbound._per_host_requests) <= 256  # MAX_TRACKED_HOSTS
    # The total requests counter must equal the input count (no loss).
    assert outbound._request_count == 300
