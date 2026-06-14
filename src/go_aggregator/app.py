"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.api.chat_completions import handle_chat_completions
from go_aggregator.api.messages import handle_messages
from go_aggregator.auth import require_auth
from go_aggregator.catalog.service import CatalogService
from go_aggregator.constants import API_V1_PREFIX
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.errors import AggregatorError
from go_aggregator.logging import configure_logging
from go_aggregator.models.api import HealthResponse, ReadyResponse
from go_aggregator.models.config import AppConfig
from go_aggregator.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown."""
    config: AppConfig = app.state.config

    configure_logging(level=config.server.log_level)

    # Database
    db = Database(
        path=config.database.path,
        busy_timeout_ms=config.database.busy_timeout_ms,
        wal=config.database.wal,
        synchronous=config.database.synchronous,
    )
    await db.connect()
    app.state.db = db

    # Migrations
    runner = MigrationRunner(db)
    await runner.run()

    # HTTPX client
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

    # Account registry
    registry = AccountRegistry(config)
    app.state.registry = registry

    # Catalog service
    catalog = CatalogService(config, registry, db, app.state.httpx_client)
    app.state.catalog = catalog

    # Router
    router = Router(registry, catalog)
    app.state.router = router

    # Initial catalog refresh
    if config.models.startup_refresh:
        with contextlib.suppress(Exception):
            logger.exception("Initial catalog refresh failed")

    # Background refresh task
    refresh_task: asyncio.Task[None] | None = None
    if config.models.refresh_interval_s > 0:
        refresh_task = asyncio.create_task(
            _catalog_refresh_loop(catalog, config.models.refresh_interval_s)
        )
        app.state.refresh_task = refresh_task

    logger.info("Application started")
    yield

    # Shutdown
    logger.info("Application shutting down")
    if refresh_task is not None:
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
    await app.state.httpx_client.aclose()
    await db.disconnect()


async def _catalog_refresh_loop(
    catalog: CatalogService,
    interval_s: int,
) -> None:
    """Background task for periodic catalog refresh."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await catalog.refresh()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Catalog refresh failed")


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
            return ReadyResponse(
                status="degraded",
                reason="database not connected",
            )

        config: AppConfig = request.app.state.config
        if not config.accounts:
            return ReadyResponse(
                status="degraded",
                reason="no accounts configured",
            )

        has_enabled = any(acct.enabled for acct in config.accounts)
        if not has_enabled:
            return ReadyResponse(
                status="degraded",
                reason="no enabled accounts",
            )

        return ReadyResponse(status="ok")

    @app.get(f"{API_V1_PREFIX}/models")
    async def list_models(
        request: Request,
    ) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)

        catalog: CatalogService = request.app.state.catalog
        models = catalog.get_models_for_exposure()

        return {
            "object": "list",
            "data": [
                {
                    "id": m["model_id"],
                    "object": "model",
                    "created": int(m.get("first_seen_at", 0)),
                    "owned_by": "opencode",
                    "name": m.get("display_name") or m["model_id"],
                }
                for m in models
            ],
        }

    @app.post(f"{API_V1_PREFIX}/chat/completions")
    async def chat_completions(
        request: Request,
    ) -> Any:  # pyright: ignore[reportUnusedFunction]
        return await handle_chat_completions(request)

    @app.post(f"{API_V1_PREFIX}/messages")
    async def messages(
        request: Request,
    ) -> Any:  # pyright: ignore[reportUnusedFunction]
        return await handle_messages(request)

    @app.exception_handler(AggregatorError)
    async def handle_aggregator_error(  # pyright: ignore[reportUnusedFunction]
        exc: AggregatorError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": str(exc)},
        )

    return app
