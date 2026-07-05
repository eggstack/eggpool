"""Shared fixtures and markers for performance benchmark tests."""

from __future__ import annotations

import os
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

UPSTREAM_BASE = "https://perf-test-upstream.example.com"


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers for performance tests."""
    config.addinivalue_line(
        "markers",
        "perf_baseline: performance benchmark baseline snapshot",
    )
    config.addinivalue_line(
        "markers",
        "perf_regression: performance regression guard",
    )


def _build_config() -> AppConfig:
    os.environ["PERF_TEST_KEY"] = "perf-test-key-000"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "PERF_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "perf-acct", "api_key_env": "PERF_TEST_KEY"},
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def perf_db() -> AsyncGenerator[Database, None]:
    """In-memory database with schema migrations and seed data."""
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("perf-acct", "PERF_TEST_KEY"),
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
def perf_config() -> AppConfig:
    """Minimal application config for perf tests."""
    return _build_config()


@pytest_asyncio.fixture()
async def perf_coordinator(
    perf_db: Database,
    perf_config: AppConfig,
) -> AsyncGenerator[RequestCoordinator, None]:
    """Fully wired RequestCoordinator with in-memory DB and mock upstream."""
    httpx_client = httpx.AsyncClient(
        base_url=perf_config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(perf_config)
    catalog = CatalogService(perf_config, registry, perf_db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "perf-acct")
    catalog.cache.load_model(
        model_id="claude-3-sonnet-20240229",
        display_name="Claude 3 Sonnet",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("claude-3-sonnet-20240229", "perf-acct")

    router = Router(registry, catalog)
    router.set_account_weight("perf-acct", 1.0)

    health_manager = HealthManager()
    request_repo = RequestRepository(perf_db)
    reservation_repo = ReservationRepository(perf_db)
    attempt_repo = AttemptRepository(perf_db)
    usage_window_repo = UsageWindowRepository(perf_db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=perf_db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()
