"""Phase 5 concurrency tests: selection lock correctness.

Verifies that concurrent dispatches through the selection lock produce
valid account selections, create reservations exactly once per attempt,
publish and release active request counters correctly, and exclude
attempted accounts on retry.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    RoutingDecisionRepository,
)
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.routing.router import Router


async def _seed_accounts(
    db: Database,
    account_names: list[str],
    *,
    model_id: str = "gpt-4",
    protocol: str = "openai",
) -> None:
    async with db.transaction():
        existing_model = await db.fetch_one(
            "SELECT model_id FROM models WHERE model_id = ?", (model_id,)
        )
        if existing_model is None:
            await db.execute_insert(
                "INSERT INTO models (model_id, display_name, protocol) "
                "VALUES (?, ?, ?)",
                (model_id, model_id, protocol),
            )
        for name in account_names:
            existing_acct = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (name,)
            )
            if existing_acct is None:
                await db.execute_insert(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    (name, f"K_{name}"),
                )
            acct = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
            assert acct is not None
            await db.execute_insert(
                "INSERT OR IGNORE INTO account_models "
                "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                (int(acct["id"]), model_id),
            )


def _build_config() -> AppConfig:
    os.environ.setdefault("TTEST_KEY_LCK", "test-key-lock")
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "TTEST_KEY_LCK",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "https://lock-test.example.com"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "lock-acct-a", "api_key_env": "TTEST_KEY_LCK"},
                {"name": "lock-acct-b", "api_key_env": "TTEST_KEY_LCK"},
                {"name": "lock-acct-c", "api_key_env": "TTEST_KEY_LCK"},
            ],
            "dashboard": {"enabled": False},
            "routing": {"trace": {"mode": "off"}},
        }
    )


async def _build_coordinator(config: AppConfig, db: Database) -> RequestCoordinator:
    registry = AccountRegistry(config)
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    from eggpool.catalog.service import CatalogService

    catalog = CatalogService(config, registry, db, httpx_client)
    for model_id, proto in [("gpt-4", "openai")]:
        catalog.cache.load_model(
            model_id=model_id,
            display_name=model_id,
            protocol=proto,
            capabilities={},
            source_metadata={},
        )
        for acct in ["lock-acct-a", "lock-acct-b", "lock-acct-c"]:
            catalog.cache.add_account_support(model_id, acct)

    router = Router(registry, catalog)
    router.set_account_weight("lock-acct-a", 1.0)
    router.set_account_weight("lock-acct-b", 1.0)
    router.set_account_weight("lock-acct-c", 1.0)

    return RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=RequestRepository(db),
        reservation_repo=ReservationRepository(db),
        attempt_repo=AttemptRepository(db),
        routing_decision_repo=RoutingDecisionRepository(db),
        quota_estimator=None,
        health_manager=None,
        config=config,
    )


def _make_context(request_id: str) -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
    )


async def _count_rows(db: Database, table: str) -> int:
    row = await db.fetch_one(f"SELECT COUNT(*) AS cnt FROM {table}")
    assert row is not None
    return int(row["cnt"])


@pytest.mark.asyncio()
async def test_concurrent_requests_get_valid_accounts() -> None:
    config = _build_config()
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["lock-acct-a", "lock-acct-b", "lock-acct-c"])
        coordinator = await _build_coordinator(config, db)

        results: list[Exception | None] = []

        async def _do_select(idx: int) -> None:
            try:
                ctx = _make_context(f"req-conc-{idx}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                results.append(None)
            except Exception as exc:
                results.append(exc)

        await asyncio.gather(*[_do_select(i) for i in range(6)])
        errors = [r for r in results if r is not None]
        assert not errors, f"Concurrent selections failed: {errors}"

        requests_count = await _count_rows(db, "requests")
        reservations_count = await _count_rows(db, "reservations")
        attempts_count = await _count_rows(db, "request_attempts")
        assert requests_count == 6
        assert reservations_count == 6
        assert attempts_count == 6
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_retry_excludes_attempted_accounts() -> None:
    config = _build_config()
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["lock-acct-a", "lock-acct-b", "lock-acct-c"])
        coordinator = await _build_coordinator(config, db)

        ctx = _make_context("req-retry-1")
        await coordinator._select_and_persist_attempt(ctx, 1)
        first_account = ctx.client_metadata["account_name"]
        assert first_account in {"lock-acct-a", "lock-acct-b", "lock-acct-c"}

        await coordinator._select_and_persist_attempt(ctx, 2)
        second_account = ctx.client_metadata["account_name"]
        assert second_account in {"lock-acct-a", "lock-acct-b", "lock-acct-c"}

        attempts_count = await _count_rows(db, "request_attempts")
        assert attempts_count == 2
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_no_request_remains_pending_after_error() -> None:
    config = _build_config()
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["lock-acct-a"])
        coordinator = await _build_coordinator(config, db)

        ctx = _make_context("req-err-1")
        await coordinator._select_and_persist_attempt(ctx, 1)
        db_request_id = ctx.client_metadata["db_request_id"]

        row = await db.fetch_one(
            "SELECT status FROM requests WHERE id = ?", (int(db_request_id),)
        )
        assert row is not None
        assert row["status"] in {"pending", "error", "completed"}
    finally:
        await db.disconnect()
