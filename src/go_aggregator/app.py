"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response as StarletteResponse

from go_aggregator.accounts.registry import AccountRegistry, account_config_rows
from go_aggregator.api.chat_completions import handle_chat_completions
from go_aggregator.api.messages import handle_messages
from go_aggregator.api.stats import register_stats_routes
from go_aggregator.auth import require_auth, require_auth_at_startup
from go_aggregator.background import TaskSupervisor
from go_aggregator.background.cleanup import (
    checkpoint_database,
    cleanup_old_events,
    cleanup_old_requests,
    reconcile_expired_reservations,
)
from go_aggregator.catalog.pricing import CostCalculator, PriceRepository
from go_aggregator.catalog.service import CatalogService
from go_aggregator.constants import API_V1_PREFIX, MAX_REQUEST_BODY_BYTES
from go_aggregator.dashboard.routes import register_dashboard_routes
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AccountEventRepository,
    AccountRepository,
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from go_aggregator.errors import AggregatorError
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.logging import configure_logging
from go_aggregator.models.api import HealthResponse
from go_aggregator.models.config import AppConfig
from go_aggregator.request.coordinator import RequestCoordinator
from go_aggregator.routing.router import Router
from go_aggregator.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.requests import Request as StarletteRequest

logger = logging.getLogger(__name__)


class _BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the configured limit."""

    def __init__(self, app: Any, max_bytes: int) -> None:  # noqa: ANN401
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(
        self,
        request: StarletteRequest,
        call_next: Any,  # noqa: ANN401
    ) -> StarletteResponse:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = None
            if declared_size is not None and declared_size > self._max_bytes:
                return StarletteResponse(
                    status_code=413,
                    content='{"error": {"message": "Request body too large",'
                    ' "type": "invalid_request_error"}}',
                    media_type="application/json",
                )
        return await call_next(request)


class _HeaderRedactionMiddleware(BaseHTTPMiddleware):
    """Redact configured headers from upstream responses."""

    def __init__(self, app: Any, headers_to_redact: list[str]) -> None:  # noqa: ANN401
        super().__init__(app)
        self._redact = {h.lower() for h in headers_to_redact}

    async def dispatch(
        self,
        request: StarletteRequest,
        call_next: Any,  # noqa: ANN401
    ) -> StarletteResponse:
        response = await call_next(request)
        for header in self._redact:
            if header in response.headers:
                del response.headers[header]
        return response


async def _crash_recovery(db: Database) -> None:
    """Mark stale pending requests as interrupted, release their reservations."""
    # Collect affected account_ids before recovery
    affected = await db.fetch_all(
        "SELECT DISTINCT account_id FROM requests "
        "WHERE status = 'pending' "
        "AND started_at < datetime('now', '-10 minutes') "
        "UNION "
        "SELECT DISTINCT account_id FROM reservations "
        "WHERE status = 'active' "
        "AND created_at < datetime('now', '-10 minutes')"
    )
    affected_account_ids = [int(row["account_id"]) for row in affected]

    async with db.transaction():
        stale_requests = await db.execute_write(
            "UPDATE requests SET status = 'interrupted', "
            "completed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'pending' "
            "AND started_at < datetime('now', '-10 minutes')"
        )
        stale_reservations = await db.execute_write(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = 'crash_recovery' "
            "WHERE status = 'active' "
            "AND created_at < datetime('now', '-10 minutes')"
        )
        await db.execute_write(
            "UPDATE request_attempts SET "
            "completed_at = CURRENT_TIMESTAMP, error_class = 'process_interrupted' "
            "WHERE completed_at IS NULL "
            "AND started_at < datetime('now', '-10 minutes')"
        )

        # Record recovery events in the same transaction so a crash
        # between the recovery updates and event recording cannot
        # leave accounts without their recovery audit trail.
        if affected_account_ids:
            event_repo = AccountEventRepository(db)
            for account_id in affected_account_ids:
                await event_repo.record(
                    account_id=account_id,
                    event_type="crash_recovery",
                    details='{"action": "marked_interrupted", '
                    '"reason": "startup_recovery"}',
                )

    if affected_account_ids:
        logger.info(
            "Crash recovery: marked %d stale requests, released %d reservations, "
            "recorded events for %d accounts",
            stale_requests,
            stale_reservations,
            len(affected_account_ids),
        )
    else:
        logger.info("Crash recovery: no stale requests found")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown."""
    config: AppConfig = app.state.config

    configure_logging(level=config.server.log_level)

    # 1. Validate auth at startup
    require_auth_at_startup(config.server.api_key_env)

    # 2. Database
    db = Database(
        path=config.database.path,
        busy_timeout_ms=config.database.busy_timeout_ms,
        wal=config.database.wal,
        synchronous=config.database.synchronous,
    )
    await db.connect()
    app.state.db = db

    # 3. Migrations
    runner = MigrationRunner(db)
    await runner.run()

    # 4. Sync accounts from config to SQLite
    account_repo = AccountRepository(db)
    config_accounts = account_config_rows(config)
    await account_repo.sync_from_config(config_accounts, db)

    # 5. Crash recovery
    await _crash_recovery(db)

    # 6. Initialize repositories
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)

    # 7. HTTPX client
    app.state.httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(
            connect=config.upstream.connect_timeout_s,
            read=config.upstream.read_timeout_s,
            write=config.upstream.write_timeout_s,
            pool=config.upstream.connect_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=config.upstream.max_connections,
            max_keepalive_connections=config.upstream.max_keepalive,
            keepalive_expiry=config.upstream.keepalive_timeout_s,
        ),
    )

    # 8. Account registry (runtime state)
    registry = AccountRegistry(config)
    app.state.registry = registry

    # 9. Health manager
    health_manager = HealthManager()
    app.state.health_manager = health_manager

    # 10. Catalog service
    catalog = CatalogService(config, registry, db, app.state.httpx_client)
    app.state.catalog = catalog

    # 11. Load cached catalog
    await catalog._load_cached_models()  # pyright: ignore[reportPrivateUsage]

    # 12. Refresh catalog from enabled accounts
    if config.models.startup_refresh:
        try:
            await catalog.refresh()
        except Exception:
            logger.exception("Initial catalog refresh failed")

    # 14. Price repository and cost calculator
    price_repo = PriceRepository(db)
    cost_calculator = CostCalculator(price_repo)
    app.state.cost_calculator = cost_calculator

    # 15. Router (with health manager for circuit breaker integration)
    router = Router(registry, catalog, health_manager=health_manager)
    app.state.router = router

    # 16. Wire routing config into scorer and estimator
    five_hour_capacity = float(config.limits.five_hour_microdollars)
    router._scorer.tiebreaker_range = config.routing.near_tie_epsilon  # pyright: ignore[reportPrivateUsage]
    if not config.routing.randomize_near_ties:
        router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]
    if five_hour_capacity > 0:
        router._scorer.inflight_penalty_per_request = (  # pyright: ignore[reportPrivateUsage]
            config.routing.inflight_penalty / five_hour_capacity
        )
        router._scorer.health_penalty_value = (  # pyright: ignore[reportPrivateUsage]
            config.routing.health_penalty / five_hour_capacity
        )
    router._quota_estimator.default_unknown_reservation_microdollars = (  # pyright: ignore[reportPrivateUsage]
        config.routing.unknown_request_reservation_microdollars
    )

    # 17b. Load configured model price overrides into estimator
    for model_id, override in config.model_overrides.items():
        input_price = override.input_price_per_1k
        output_price = override.output_price_per_1k
        if input_price is not None and output_price is not None:
            # Convert dollars/1K → dollars/1M (estimator Tier 4 units)
            router._quota_estimator.set_model_override(  # pyright: ignore[reportPrivateUsage]
                model_id,
                input_price * 1000,
                output_price * 1000,
            )

    # 18. Load persisted usage windows and set account weights/offsets
    router._quota_estimator.set_usage_window_repo(  # pyright: ignore[reportPrivateUsage]
        usage_window_repo
    )
    config_offsets: dict[str, dict[str, int]] = {}
    for acct_cfg in config.accounts:
        config_offsets[acct_cfg.name] = {
            "five_hour": acct_cfg.five_hour_offset_microdollars,
            "weekly": acct_cfg.weekly_offset_microdollars,
            "monthly": acct_cfg.monthly_offset_microdollars,
        }
    await router._quota_estimator.load_persisted_windows(  # pyright: ignore[reportPrivateUsage]
        offsets=config_offsets,
    )
    # Set account weights from config
    for acct_cfg in config.accounts:
        router.set_account_weight(acct_cfg.name, acct_cfg.weight)

    # Configure explicit quota policies from config
    for acct_cfg in config.accounts:
        router.configure_account_policy(
            account_name=acct_cfg.name,
            weight=acct_cfg.weight,
            capacity_5h_microdollars=int(
                config.limits.five_hour_microdollars * acct_cfg.weight
            ),
            capacity_7d_microdollars=int(
                config.limits.weekly_microdollars * acct_cfg.weight
            ),
            capacity_30d_microdollars=int(
                config.limits.monthly_microdollars * acct_cfg.weight
            ),
            offset_5h_microdollars=acct_cfg.five_hour_offset_microdollars,
            offset_7d_microdollars=acct_cfg.weekly_offset_microdollars,
            offset_30d_microdollars=acct_cfg.monthly_offset_microdollars,
        )

    # 17. Statistics service
    app.state.stats = StatsService(db, health_manager=health_manager)

    # 18. Request coordinator
    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        httpx_client=app.state.httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
        cost_calculator=cost_calculator,
        quota_estimator=router._quota_estimator,  # pyright: ignore[reportPrivateUsage]
        max_retry_attempts=1 + config.routing.max_retries_before_stream,
        quota_exhausted_cooldown_seconds=config.routing.quota_exhausted_cooldown_seconds,
        persist_error_detail=config.security.persist_redacted_error_detail,
    )
    app.state.coordinator = coordinator

    # 19. Reconcile expired reservations at startup so dashboard counts
    # and in-memory quota state are accurate before readiness reports OK.
    await reconcile_expired_reservations(
        db,
        quota_estimator=router._quota_estimator,  # pyright: ignore[reportPrivateUsage]
        router=router,
    )

    # 20. Background task supervisor
    supervisor = TaskSupervisor()
    app.state.supervisor = supervisor

    # Register catalog refresh task
    if config.models.refresh_interval_s > 0:
        supervisor.register(
            "catalog_refresh",
            lambda: _catalog_refresh_loop(catalog, config.models.refresh_interval_s),
        )

    # Register retention cleanup task (runs every hour)
    async def _retention_cleanup() -> None:
        while True:
            await asyncio.sleep(3600)
            await cleanup_old_requests(db, config.dashboard.retain_request_stats_days)
            await cleanup_old_events(db, config.dashboard.retain_event_days)
            # Reconcile expired reservations and sync in-memory state
            await reconcile_expired_reservations(
                db,
                quota_estimator=router._quota_estimator,  # pyright: ignore[reportPrivateUsage]
                router=router,
            )

    supervisor.register("retention_cleanup", _retention_cleanup)

    # Register periodic checkpoint task (runs every 4 hours)
    async def _periodic_checkpoint() -> None:
        while True:
            await asyncio.sleep(14400)
            await checkpoint_database(db)

    supervisor.register("checkpoint", _periodic_checkpoint)

    # Register periodic usage window refresh (every 60 seconds)
    async def _refresh_usage_windows() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await router._quota_estimator.load_persisted_windows()  # pyright: ignore[reportPrivateUsage]
            except Exception:
                logger.exception("Failed to refresh usage windows")

    supervisor.register("usage_window_refresh", _refresh_usage_windows)

    # 21. Start background tasks
    await supervisor.start_all()

    # 22. Startup complete
    logger.info(
        "Application started (%d accounts, %d models). "
        "Note: configuration changes require service restart.",
        len(config.accounts),
        catalog.cache.model_count,
    )
    yield

    # Shutdown
    logger.info("Application shutting down")
    await supervisor.stop_all()
    httpx_client = getattr(app.state, "httpx_client", None)
    if httpx_client is not None:
        try:
            await httpx_client.aclose()
        except Exception:
            logger.exception("Error closing httpx client during shutdown")
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


def create_app(
    config: AppConfig | None = None,
    config_path: str | None = None,
) -> FastAPI:
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
    app.state.config_path = config_path

    # Security middleware
    if config.security.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.security.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    if config.security.allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=config.security.allowed_hosts,
        )
    if config.security.redact_headers:
        app.add_middleware(
            _HeaderRedactionMiddleware,
            headers_to_redact=config.security.redact_headers,
        )
    app.add_middleware(
        _BodyLimitMiddleware,
        max_bytes=MAX_REQUEST_BODY_BYTES,
    )

    # Dashboard and statistics routes (read-only, no auth by default)
    if config.dashboard.enabled:
        dashboard_require_auth = not config.dashboard.public
        register_dashboard_routes(app, require_auth=dashboard_require_auth)
        register_stats_routes(app, require_auth=dashboard_require_auth)

        @app.get("/static/dashboard.css")
        async def dashboard_css() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
            css_path: Path = (
                Path(__file__).parent / "dashboard" / "static" / "dashboard.css"
            )
            return FileResponse(
                path=str(css_path),
                media_type="text/css",
            )

    @app.get(f"{API_V1_PREFIX}/healthz")
    async def healthz() -> HealthResponse:  # pyright: ignore[reportUnusedFunction]
        return HealthResponse(status="ok")

    @app.get(f"{API_V1_PREFIX}/readyz")
    async def readyz(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        db: Database | None = getattr(request.app.state, "db", None)
        if db is None or db._conn is None:  # pyright: ignore[reportPrivateUsage]
            return Response(
                content='{"status":"degraded","reason":"database not connected"}',
                status_code=503,
                media_type="application/json",
            )

        # Real writeability probe using probe_writable()
        if not await db.probe_writable():
            return Response(
                content='{"status":"degraded","reason":"database not writable"}',
                status_code=503,
                media_type="application/json",
            )

        config: AppConfig = request.app.state.config
        if not config.accounts:
            return Response(
                content='{"status":"degraded","reason":"no accounts configured"}',
                status_code=503,
                media_type="application/json",
            )

        has_enabled = any(acct.enabled for acct in config.accounts)
        if not has_enabled:
            return Response(
                content='{"status":"degraded","reason":"no enabled accounts"}',
                status_code=503,
                media_type="application/json",
            )

        # Check loaded credentials
        registry: AccountRegistry | None = getattr(request.app.state, "registry", None)
        if registry is not None:
            enabled_states = registry.get_enabled_states()
            has_credentials = any(registry.get_api_key(s.name) for s in enabled_states)
            if not has_credentials:
                return Response(
                    content='{"status":"degraded","reason":"no loaded credentials"}',
                    status_code=503,
                    media_type="application/json",
                )

        # Check usable model catalog
        catalog: CatalogService | None = getattr(request.app.state, "catalog", None)
        if catalog is not None and catalog.cache.model_count == 0:
            return Response(
                content='{"status":"degraded","reason":"no usable model catalog"}',
                status_code=503,
                media_type="application/json",
            )

        # Real eligible-pairing readiness (Section 12.2)
        router: Router | None = getattr(request.app.state, "router", None)
        if router is not None and not router.has_eligible_pairing():
            return Response(
                content=(
                    '{"status":"degraded","reason":"no eligible account pairings"}'
                ),
                status_code=503,
                media_type="application/json",
            )

        supervisor: TaskSupervisor | None = getattr(
            request.app.state, "supervisor", None
        )
        if supervisor is not None and not supervisor.all_healthy:
            return Response(
                content='{"status":"degraded","reason":"background tasks degraded"}',
                status_code=503,
                media_type="application/json",
            )

        return Response(
            content='{"status":"ok"}',
            status_code=200,
            media_type="application/json",
        )

    @app.get(f"{API_V1_PREFIX}/models")
    async def list_models(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> dict[str, Any]:
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
    async def chat_completions(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Any:
        return await handle_chat_completions(request)

    @app.post(f"{API_V1_PREFIX}/messages")
    async def messages(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Any:
        return await handle_messages(request)

    @app.exception_handler(AggregatorError)
    async def handle_aggregator_error(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        exc: AggregatorError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": str(exc)},
        )

    return app
