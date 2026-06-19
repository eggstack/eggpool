"""Integration tests for startup, catalog lifecycle, and readiness scenarios."""

from __future__ import annotations

import os
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
from eggpool.db.repositories import AccountRepository
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.routing.router import Router
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI


UPSTREAM_BASE = "https://test-upstream.example.com"

EXPECTED_TABLES = frozenset(
    {
        "requests",
        "reservations",
        "request_attempts",
        "accounts",
        "models",
        "model_price_snapshots",
    }
)


# ---------------------------------------------------------------------------
# 1. Fresh database migration creates all required tables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_database_migration_and_account_sync() -> None:
    """Fresh in-memory database has all tables after migration; accounts sync."""
    db = Database(path=":memory:")
    await db.connect()

    runner = MigrationRunner(db)
    await runner.run()

    # Verify all expected tables exist
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' "
        r"AND name NOT LIKE '\_%' ESCAPE '\'"
    )
    table_names = {row["name"] for row in rows}
    for table in EXPECTED_TABLES:
        assert table in table_names, f"Missing table: {table}"

    # Sync accounts from config
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    config = AppConfig.from_dict(
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
                {"name": "acct-alpha", "api_key_env": "OPENCODE_TEST_KEY"},
                {"name": "acct-beta", "api_key_env": "OPENCODE_TEST_KEY"},
            ],
            "dashboard": {"enabled": False},
        }
    )

    account_repo = AccountRepository(db)
    config_accounts = [
        {
            "name": acct.name,
            "api_key_env": acct.api_key_env,
            "enabled": acct.enabled,
            "weight": acct.weight,
        }
        for acct in config.all_accounts()
    ]
    name_to_id = await account_repo.sync_from_config(config_accounts)

    assert "acct-alpha" in name_to_id
    assert "acct-beta" in name_to_id
    assert name_to_id["acct-alpha"] != name_to_id["acct-beta"]

    acct_rows = await db.fetch_all("SELECT name FROM accounts ORDER BY name")
    names = [row["name"] for row in acct_rows]
    assert names == ["acct-alpha", "acct-beta"]

    await db.disconnect()


# ---------------------------------------------------------------------------
# 2. Catalog refresh populates cache from mocked upstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_immediate_startup_catalog_refresh() -> None:
    """Catalog.refresh() populates the in-memory cache from upstream models."""
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    config = AppConfig.from_dict(
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

    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)

    with respx.mock:
        respx.get(f"{UPSTREAM_BASE}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "gpt-4", "object": "model", "owned_by": "openai"},
                        {"id": "claude-3", "object": "model", "owned_by": "anthropic"},
                    ],
                },
            )
        )

        await catalog.refresh()

    assert catalog.cache.model_count == 2
    assert catalog.cache.has_model("gpt-4")
    assert catalog.cache.has_model("claude-3")

    await httpx_client.aclose()
    await db.disconnect()


# ---------------------------------------------------------------------------
# 3. Failed refresh preserves valid cached catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_refresh_failure_with_valid_cached_catalog() -> None:
    """A failed upstream refresh does not wipe an existing valid cache."""
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    config = AppConfig.from_dict(
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

    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)

    # Pre-populate cache
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")
    assert catalog.cache.model_count == 1

    # Now mock upstream to fail
    with respx.mock:
        respx.get(f"{UPSTREAM_BASE}/models").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        await catalog.refresh()

    # Cache should still have the pre-populated model
    assert catalog.cache.model_count == 1
    assert catalog.cache.has_model("gpt-4")

    await httpx_client.aclose()
    await db.disconnect()


# ---------------------------------------------------------------------------
# 4. Empty catalog causes degraded readiness
# ---------------------------------------------------------------------------


def _build_config(
    accounts: list[dict[str, str]] | None = None,
) -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    if accounts is None:
        accounts = [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}]
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


@pytest_asyncio.fixture()
async def readyz_app_empty_catalog() -> AsyncGenerator[FastAPI]:
    """App fixture with a valid DB and accounts but an empty model catalog."""
    config = _build_config()
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

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    application.state.httpx_client = httpx_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, httpx_client)
    # Leave catalog cache empty (model_count == 0)
    application.state.catalog = catalog

    router = Router(registry, catalog)
    application.state.router = router

    application.state.stats = StatsService(db)
    application.state.health_manager = HealthManager()

    yield application

    await httpx_client.aclose()
    await db.disconnect()


@pytest.mark.asyncio
async def test_empty_catalog_causes_degraded_readiness(
    readyz_app_empty_catalog: FastAPI,
) -> None:
    """Readyz returns 503 when catalog cache is empty."""
    transport = httpx.ASGITransport(app=readyz_app_empty_catalog)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/v1/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "no usable model catalog"


# ---------------------------------------------------------------------------
# 5. Missing accounts prevent readiness
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def readyz_app_no_accounts() -> AsyncGenerator[FastAPI]:
    """App fixture with a valid DB but zero configured accounts."""
    config = _build_config(accounts=[])
    application = create_app(config)

    db = Database(path=":memory:")
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    yield application

    await db.disconnect()


@pytest.mark.asyncio
async def test_missing_accounts_prevent_readiness(
    readyz_app_no_accounts: FastAPI,
) -> None:
    """Readyz returns 503 when no accounts are configured."""
    transport = httpx.ASGITransport(app=readyz_app_no_accounts)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/v1/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "no accounts configured"
