"""Shared fixtures and markers for soak tests."""

from __future__ import annotations

import os
import random
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

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
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

UPSTREAM_BASE = "https://soak-test-upstream.example.com"


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers for soak tests."""
    config.addinivalue_line(
        "markers",
        "soak: long-running soak validation tests",
    )
    config.addinivalue_line(
        "markers",
        "workload_profile: canonical workload profile tests",
    )
    config.addinivalue_line(
        "markers",
        "stability_assertion: early/late stability ratio checks",
    )
    config.addinivalue_line(
        "markers",
        "db_consistency: database lifecycle invariant checks",
    )
    config.addinivalue_line(
        "markers",
        "failure_injection: failure injection and recovery tests",
    )
    config.addinivalue_line(
        "markers",
        "resource_plateau: resource plateau validation",
    )


def _build_config() -> AppConfig:
    os.environ["SOAK_TEST_KEY"] = "soak-test-key-000"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "SOAK_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "soak-acct-1", "api_key_env": "SOAK_TEST_KEY"},
                {"name": "soak-acct-2", "api_key_env": "SOAK_TEST_KEY"},
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def soak_db() -> AsyncGenerator[Database, None]:
    """In-memory database with schema migrations and seed data for soak tests."""
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("soak-acct-1", "SOAK_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 0.5)",
            ("soak-acct-2", "SOAK_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3-sonnet-20240229", "anthropic"),
        )
    yield database
    await database.disconnect()


@pytest.fixture()
def soak_config() -> AppConfig:
    """Minimal application config for soak tests."""
    return _build_config()


@pytest.fixture()
def soak_rng() -> random.Random:
    """Deterministic random number generator for workload generation."""
    return random.Random(42)


@pytest_asyncio.fixture()
async def soak_coordinator(
    soak_db: Database,
    soak_config: AppConfig,
) -> AsyncGenerator[RequestCoordinator, None]:
    """Fully wired RequestCoordinator for soak tests."""
    httpx_client = httpx.AsyncClient(
        base_url=soak_config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(soak_config)
    catalog = CatalogService(soak_config, registry, soak_db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "soak-acct-1")
    catalog.cache.add_account_support("gpt-4", "soak-acct-2")
    catalog.cache.load_model(
        model_id="claude-3-sonnet-20240229",
        display_name="Claude 3 Sonnet",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3-sonnet-20240229", "soak-acct-1")
    catalog.cache.add_account_support("claude-3-sonnet-20240229", "soak-acct-2")

    router = Router(registry, catalog)
    router.set_account_weight("soak-acct-1", 1.0)
    router.set_account_weight("soak-acct-2", 0.5)

    health_manager = HealthManager()
    request_repo = RequestRepository(soak_db)
    reservation_repo = ReservationRepository(soak_db)
    attempt_repo = AttemptRepository(soak_db)
    usage_window_repo = UsageWindowRepository(soak_db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=soak_db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()
