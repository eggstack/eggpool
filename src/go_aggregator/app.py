"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from go_aggregator.constants import API_V1_PREFIX
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.errors import AggregatorError
from go_aggregator.logging import configure_logging
from go_aggregator.models.api import HealthResponse, ReadyResponse
from go_aggregator.models.config import AppConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown."""
    config: AppConfig = app.state.config

    configure_logging(level=config.server.log_level)

    db = Database(
        path=config.database.path,
        busy_timeout_ms=config.database.busy_timeout_ms,
        wal=config.database.wal,
        synchronous=config.database.synchronous,
    )
    await db.connect()
    app.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    app.state.httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(
            connect=config.upstream.connect_timeout_s,
            read=config.upstream.read_timeout_s,
            write=config.upstream.write_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=config.upstream.max_connections,
            max_keepalive_connections=config.upstream.max_keepalive,
            keepalive_expiry=config.upstream.keepalive_timeout_s,
        ),
    )

    logger.info("Application started")
    yield

    logger.info("Application shutting down")
    await app.state.httpx_client.aclose()
    await db.disconnect()


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = AppConfig()

    app = FastAPI(
        title="OpenCode Go Aggregator",
        version="0.1.0",
        docs_url=f"{API_V1_PREFIX}/docs",
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        lifespan=lifespan,
    )
    app.state.config = config

    @app.get(f"{API_V1_PREFIX}/healthz")
    async def healthz() -> HealthResponse:  # pyright: ignore[reportUnusedFunction]
        return HealthResponse(status="ok")

    @app.get(f"{API_V1_PREFIX}/readyz")
    async def readyz(request: Request) -> ReadyResponse:  # pyright: ignore[reportUnusedFunction]
        db: Database | None = getattr(request.app.state, "db", None)
        if db is None or db._conn is None:  # pyright: ignore[reportPrivateUsage]
            return ReadyResponse(status="degraded", reason="database not connected")

        config: AppConfig = request.app.state.config
        if not config.accounts:
            return ReadyResponse(status="degraded", reason="no accounts configured")

        has_enabled = any(acct.enabled for acct in config.accounts)
        if not has_enabled:
            return ReadyResponse(status="degraded", reason="no enabled accounts")

        return ReadyResponse(status="ok")

    @app.exception_handler(AggregatorError)
    async def handle_aggregator_error(  # pyright: ignore[reportUnusedFunction]
        exc: AggregatorError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": str(exc)},
        )

    return app
