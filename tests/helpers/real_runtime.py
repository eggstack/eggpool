"""Reusable real-runtime test fixture for Eggpool integration tests.

Provides an async context manager that enters the actual Eggpool ASGI
application with a temporary file-backed SQLite database, migrations
applied, upstream interception via respx, and clean shutdown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest_asyncio

from eggpool.accounts.registry import AccountRegistry
from eggpool.app import create_app
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
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest
    from fastapi import FastAPI

UPSTREAM_BASE = "https://real-runtime-upstream.example.com"


def _build_config(tmp_db: str = ":memory:") -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "REAL_RUNTIME_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": tmp_db},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "rt-acct-1", "api_key_env": "REAL_RUNTIME_KEY"},
                {"name": "rt-acct-2", "api_key_env": "REAL_RUNTIME_KEY"},
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def real_runtime_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """Provide an actual Eggpool ASGI application with real components.

    Yields a tuple of (app, httpx.AsyncClient using ASGITransport).
    """
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")

    config = _build_config(tmp_db=str(tmp_path / "test.db"))
    application = create_app(config)

    db = Database(path=str(tmp_path / "test.db"))
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("rt-acct-1", "REAL_RUNTIME_KEY"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("rt-acct-2", "REAL_RUNTIME_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
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

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)

    coordinator = RequestCoordinator(
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
    )
    application.state.coordinator = coordinator

    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "rt-acct-1")
    catalog.cache.add_account_support("gpt-4", "rt-acct-2")

    yield application

    await db.disconnect()
    await httpx_client.aclose()
