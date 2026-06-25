"""Phase 9 reproduction tests for the upstream-authoritative-suppression plan.

These tests verify the four scenarios from the plan's Phase 9 section:

* Scenario A: Local overage must not suppress accounts in ``score_only`` mode.
* Scenario B: Upstream 429 suppresses and fails over to a healthy account.
* Scenario C: Single-account pass-through returns the upstream status verbatim.
* Scenario D: Restart preserves authoritative backoff but never local overage.

All scenarios run with respx-mocked upstream providers so no real
network is involved.
"""

from __future__ import annotations

import json
import os
import time
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
    AccountBackoffRepository,
    AccountRepository,
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

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


UPSTREAM_BASE = "https://test-upstream.example.com"


def _make_config(account_names: list[str]) -> AppConfig:
    """Build a configuration with one fake provider and N fake accounts."""
    os.environ["OPENCODE_TEST_KEY"] = "test-key-default"
    for name in account_names:
        os.environ.setdefault(f"OPENCODE_TEST_KEY_{name.upper()}", f"key-{name}")
    accounts = [
        {"name": name, "api_key_env": "OPENCODE_TEST_KEY"}
        if name == account_names[0]
        else {"name": name, "api_key_env": f"OPENCODE_TEST_KEY_{name.upper()}"}
        for name in account_names
    ]
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
            "accounts": accounts,
            "dashboard": {"enabled": False},
        }
    )


async def _seed_accounts(
    db: Database,
    names: list[str],
    api_key_envs: list[str],
) -> None:
    """Insert accounts and the gpt-4 model row."""
    async with db.transaction():
        for name, env in zip(names, api_key_envs, strict=True):
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                (name, env),
            )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


def _build_coordinator(
    db: Database,
    config: AppConfig,
    quota_estimator_only: bool = False,
) -> tuple[
    RequestCoordinator,
    httpx.AsyncClient,
    AccountRegistry,
    CatalogService,
    Router,
    HealthManager,
    AccountBackoffRepository,
]:
    """Construct a coordinator wired with the shared test scaffold."""
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
    for state in registry.get_enabled_states():
        catalog.cache.add_account_support("gpt-4", state.name)

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    for state in registry.get_enabled_states():
        router.set_account_weight(state.name, 1.0)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)
    backoff_repo = AccountBackoffRepository(db)

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
        account_backoff_repo=backoff_repo,
        quota_estimator=router.quota_estimator,
        max_retry_attempts=3,
    )
    return (
        coord,
        httpx_client,
        registry,
        catalog,
        router,
        health_manager,
        backoff_repo,
    )


def _success_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()


def _ok_response() -> httpx.Response:
    return httpx.Response(
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


@pytest_asyncio.fixture()
async def four_account_db() -> AsyncGenerator[Database, None]:
    """In-memory DB with four accounts and the gpt-4 model row."""
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    await _seed_accounts(
        database,
        ["acct-a", "acct-b", "acct-c", "acct-d"],
        [
            "OPENCODE_TEST_KEY",
            "OPENCODE_TEST_KEY_ACCT-B",
            "OPENCODE_TEST_KEY_ACCT-C",
            "OPENCODE_TEST_KEY_ACCT-D",
        ],
    )
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def single_account_db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    await _seed_accounts(database, ["solo-acct"], ["OPENCODE_TEST_KEY"])
    yield database
    await database.disconnect()


def _configure_tiny_capacity(router: Router) -> None:
    """Set a tiny 5h capacity on each known account via the quota estimator."""
    for name in ("acct-a", "acct-b", "acct-c", "acct-d"):
        router.quota_estimator.configure_account_policy(
            name,
            weight=1.0,
            capacity_5h_microdollars=100,
            capacity_7d_microdollars=100,
            capacity_30d_microdollars=100,
            offset_5h_microdollars=0,
            offset_7d_microdollars=0,
            offset_30d_microdollars=0,
        )


def _seed_heavy_usage(router: Router) -> None:
    """Record usage that exceeds the tiny capacity for every account."""
    for name in ("acct-a", "acct-b", "acct-c", "acct-d"):
        router.quota_estimator.record_usage(name, tokens=100, cost_microdollars=500)


# ---------------------------------------------------------------------------
# Scenario A: Local overage must not suppress accounts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_a_local_overage_does_not_suppress(
    four_account_db: Database,
) -> None:
    """Seeded local overage must not block requests in score_only mode."""
    config = _make_config(["acct-a", "acct-b", "acct-c", "acct-d"])
    coord, httpx_client, registry, _catalog, router, _hm, _repo = _build_coordinator(
        four_account_db, config
    )
    try:
        _configure_tiny_capacity(router)
        _seed_heavy_usage(router)
        _configure_tiny_capacity(router)
        eligible = router.get_eligible_account_names(
            "gpt-4", provider_id=None, protocol="openai"
        )
        assert set(eligible) == {"acct-a", "acct-b", "acct-c", "acct-d"}, (
            "score_only mode must keep all four accounts eligible"
        )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_ok_response()
            )
            response = await coord.execute(
                ProxyRequestContext(
                    request_id="scenario-a-1",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_success_body(),
                    incoming_headers={"content-type": "application/json"},
                )
            )

        assert response.status_code == 200
        assert response.account_name in {"acct-a", "acct-b", "acct-c", "acct-d"}
    finally:
        await httpx_client.aclose()


# ---------------------------------------------------------------------------
# Scenario B: Upstream 429 suppresses and fails over.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_b_upstream_429_suppresses_and_fails_over(
    four_account_db: Database,
) -> None:
    """First account returns 429; second account returns 200."""
    config = _make_config(["acct-a", "acct-b", "acct-c", "acct-d"])
    coord, httpx_client, registry, _catalog, router, _hm, backoff_repo = (
        _build_coordinator(four_account_db, config)
    )
    try:
        account_repo = AccountRepository(four_account_db)
        accounts_list = await account_repo.list_enabled()
        accounts = {row["name"]: int(row["id"]) for row in accounts_list}
        accounts_by_id = {int(v): k for k, v in accounts.items()}
        first_call = {"done": False}

        def _handler(request: httpx.Request) -> httpx.Response:
            if not first_call["done"]:
                first_call["done"] = True
                return httpx.Response(
                    429,
                    json={"error": {"message": "rate limited"}},
                    headers={"retry-after": "5"},
                )
            return _ok_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)
            response = await coord.execute(
                ProxyRequestContext(
                    request_id="scenario-b-1",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_success_body(),
                    incoming_headers={"content-type": "application/json"},
                )
            )

        assert response.status_code == 200
        first_attempts = await four_account_db.fetch_all(
            "SELECT account_id FROM request_attempts ORDER BY attempt_number"
        )
        attempted_ids = {int(row["account_id"]) for row in first_attempts}
        assert len(attempted_ids) >= 2

        active = await backoff_repo.list_active()
        rate_limited = [
            r
            for r in active
            if str(r.get("reason") or "") == "rate_limited"
            and r.get("status_code") == 429
        ]
        assert rate_limited, "expected at least one persisted rate_limited backoff"

        suppressed = rate_limited[0]
        assert suppressed["backoff_until_epoch"] is not None
        assert float(suppressed["backoff_until_epoch"]) > time.time()
        suppressed_name = accounts_by_id.get(int(suppressed["account_id"]))
        assert suppressed_name in {"acct-a", "acct-b", "acct-c", "acct-d"}

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_ok_response()
            )
            second = await coord.execute(
                ProxyRequestContext(
                    request_id="scenario-b-2",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_success_body(),
                    incoming_headers={"content-type": "application/json"},
                )
            )
        assert second.status_code == 200
        assert second.account_name != suppressed_name, (
            "subsequent request must avoid the suppressed account"
        )
    finally:
        await httpx_client.aclose()


# ---------------------------------------------------------------------------
# Scenario C: Single account pass-through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_c_single_account_passthrough(
    single_account_db: Database,
) -> None:
    """Single account returning 429 must surface 429 to the client, not 503."""
    config = _make_config(["solo-acct"])
    coord, httpx_client, registry, _catalog, router, _hm, backoff_repo = (
        _build_coordinator(single_account_db, config)
    )
    try:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(
                    429,
                    json={"error": {"message": "rate limited"}},
                    headers={"retry-after": "10"},
                )
            )
            response = await coord.execute(
                ProxyRequestContext(
                    request_id="scenario-c-1",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_success_body(),
                    incoming_headers={"content-type": "application/json"},
                )
            )

        assert response.status_code == 429
        active = await backoff_repo.list_active()
        assert any(str(r.get("reason") or "") == "rate_limited" for r in active), (
            "single-account 429 must persist a rate_limited backoff"
        )
    finally:
        await httpx_client.aclose()


# ---------------------------------------------------------------------------
# Scenario D: Restart preserves authoritative backoff but never local overage.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_d_restart_preserves_upstream_backoff_only(
    tmp_path,
) -> None:
    """Authoritative upstream backoff survives restart; local overage does not.

    Uses a real on-disk SQLite file because :memory: connections are
    per-connection and cannot be reopened to simulate a process restart.
    """
    db_path = tmp_path / "scenario_d.sqlite3"
    db = Database(path=str(db_path))
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_accounts(
        db,
        ["acct-1", "acct-2"],
        ["OPENCODE_TEST_KEY", "OPENCODE_TEST_KEY_ACCT-2"],
    )

    config = _make_config(["acct-1", "acct-2"])
    coord, httpx_client, _registry, _catalog, _router, _hm, backoff_repo = (
        _build_coordinator(db, config)
    )
    try:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(
                    429,
                    json={"error": {"message": "rate limited"}},
                    headers={"retry-after": "60"},
                )
            )
            first_response = await coord.execute(
                ProxyRequestContext(
                    request_id="scenario-d-1",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_success_body(),
                    incoming_headers={"content-type": "application/json"},
                )
            )
        assert first_response.status_code == 429
    finally:
        await httpx_client.aclose()
        await db.disconnect()

    reopened_db = Database(path=str(db_path))
    await reopened_db.connect()
    runner2 = MigrationRunner(reopened_db)
    await runner2.run()
    try:
        reopened_backoff_repo = AccountBackoffRepository(reopened_db)
        active = await reopened_backoff_repo.list_active()
        assert any(str(r.get("reason") or "") == "rate_limited" for r in active), (
            "rate_limited backoff must persist across restart"
        )

        account_repo = AccountRepository(reopened_db)
        acct_2_id = await account_repo.get_id_by_name("acct-2")
        assert acct_2_id is not None
        async with reopened_db.transaction():
            await reopened_db.execute_write(
                "INSERT INTO requests (account_id, model_id, status, "
                "started_at, cost_microdollars, exactness, "
                "input_tokens, output_tokens, reserved_microdollars) "
                "VALUES (?, ?, 'completed', datetime('now', '-1 hours'), "
                "?, 'exact', 100, 200, ?)",
                (acct_2_id, "gpt-4", 9_999_999_999, 9_999_999_999),
            )

        second_config = _make_config(["acct-1", "acct-2"])
        (
            second_coord,
            second_client,
            _registry2,
            second_catalog,
            second_router,
            second_hm,
            second_repo,
        ) = _build_coordinator(reopened_db, second_config)

        try:
            assert second_router.quota_estimator is not None
            second_router.quota_estimator.configure_account_policy(
                "acct-2",
                weight=1.0,
                capacity_5h_microdollars=100,
                capacity_7d_microdollars=100,
                capacity_30d_microdollars=100,
                offset_5h_microdollars=0,
                offset_7d_microdollars=0,
                offset_30d_microdollars=0,
            )
            eligible = second_router.get_eligible_account_names(
                "gpt-4", provider_id=None, protocol="openai"
            )
            assert "acct-2" in eligible, (
                "score_only mode must keep acct-2 eligible despite local overage"
            )

            with respx.mock:
                respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                    return_value=_ok_response()
                )
                second_response = await second_coord.execute(
                    ProxyRequestContext(
                        request_id="scenario-d-2",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=False,
                        original_body=_success_body(),
                        incoming_headers={"content-type": "application/json"},
                    )
                )
            assert second_response.status_code == 200
        finally:
            await second_client.aclose()
    finally:
        await reopened_db.disconnect()
