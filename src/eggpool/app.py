"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from importlib.metadata import version as _get_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response as StarletteResponse

from eggpool.accounts.registry import AccountRegistry, account_config_rows
from eggpool.api.backoff import register_backoff_routes
from eggpool.api.chat_completions import handle_chat_completions
from eggpool.api.messages import handle_messages
from eggpool.api.models import serialize_openai_model
from eggpool.api.stats import register_stats_routes
from eggpool.auth import require_auth, require_auth_at_startup
from eggpool.background import TaskSupervisor
from eggpool.background.cleanup import (
    checkpoint_database,
    cleanup_old_events,
    cleanup_old_requests,
    reconcile_expired_reservations,
)
from eggpool.catalog.pricing import CostCalculator, PriceRepository
from eggpool.catalog.service import CatalogService
from eggpool.constants import API_V1_PREFIX, MAX_REQUEST_BODY_BYTES
from eggpool.dashboard.routes import register_dashboard_routes
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AccountBackoffRepository,
    AccountEventRepository,
    AccountRepository,
    AttemptRepository,
    OperationalEventRepository,
    PingRepository,
    ProviderRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.db.rollup_repository import UsageRollupRepository
from eggpool.errors import (
    AggregatorError,
    CatalogUnavailableError,
    ModelNotFoundError,
    NoEligibleAccountError,
    RequestTooLargeError,
)
from eggpool.health.health_manager import HealthManager
from eggpool.logging import configure_logging
from eggpool.metrics.buffer import MetricsWriteCoalescer
from eggpool.models.api import HealthResponse
from eggpool.models.config import AppConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.dns_cache import DnsNetworkBackend
from eggpool.providers.outbound import OutboundClientManager, default_network_backend
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router
from eggpool.runtime_dispatch import DispatchOverheadRecorder
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import httpcore
    from starlette.requests import Request as StarletteRequest

    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.update_checker import UpdateChecker

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
                if request.url.path.endswith("/messages"):
                    error_body = json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Request body too large",
                            },
                        }
                    )
                else:
                    error_body = (
                        '{"error": {"message": "Request body too large",'
                        ' "type": "invalid_request_error"}}'
                    )
                return StarletteResponse(
                    status_code=413,
                    content=error_body,
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
    """Mark stale pending requests as interrupted, release their reservations.

    A process restart is a hard boundary: any request that was still
    ``pending`` in the previous process is definitively dead.  We do
    NOT time-gate this recovery so that leaked requests from the
    previous run are cleaned up regardless of how recently they were
    created.  The previous 5/10-minute thresholds left long-running
    streams ``pending`` long after the process was killed and
    reintroduced the leak whenever restart coincided with a high
    pending count.
    """
    # Collect affected account_ids before recovery
    affected = await db.fetch_all(
        "SELECT DISTINCT account_id FROM requests WHERE status = 'pending' "
        "UNION "
        "SELECT DISTINCT account_id FROM reservations WHERE status = 'active'"
    )
    affected_account_ids = [int(row["account_id"]) for row in affected]

    async with db.transaction():
        # Recover ALL pending requests (no time threshold)
        stale_requests = await db.execute_write(
            "UPDATE requests SET status = 'interrupted', "
            "completed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'pending'",
            (),
        )
        # Release ALL active reservations (no time threshold)
        stale_reservations = await db.execute_write(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = 'crash_recovery' "
            "WHERE status = 'active'",
            (),
        )
        # Finalize ALL incomplete attempts (no time threshold)
        await db.execute_write(
            "UPDATE request_attempts SET "
            "completed_at = CURRENT_TIMESTAMP, error_class = 'process_interrupted' "
            "WHERE completed_at IS NULL",
            (),
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

        # Phase 3: emit a single operational event summarising the
        # safety-net sweep so the dashboard can chart recovery
        # activity without re-aggregating per-account event rows.
        op_repo = OperationalEventRepository(db)
        await op_repo.record(
            event_type="crash_recovery",
            details={
                "interrupted_requests": int(stale_requests),
                "released_reservations": int(stale_reservations),
                "affected_accounts": len(affected_account_ids),
            },
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


async def _finalize_stale_requests(
    db: Database,
    router: Router,
    quota_estimator: QuotaEstimator,
    max_pending_seconds: float = 300.0,
    cycle_interval_s: float = 60.0,
) -> None:
    """Periodic safety net for leaked streaming requests.

    Streaming request finalization can fail under client-disconnect +
    DB-lock-contention: when the ASGI task is cancelled while the
    finalizer is waiting on the connection lock, the in-flight request
    never reaches terminal state and stays ``pending`` with an active
    reservation.  Accumulated leaks slow down cleanup queries and
    saturate the single SQLite connection lock — producing 503s after
    several minutes of load.

    This background task force-finalizes any request that has been
    ``pending`` longer than ``max_pending_seconds`` (default matches
    the upstream ``read_timeout_s`` so legitimate long-running requests
    are never touched).  It transitions leaked requests to
    ``interrupted`` and releases their reservations in a single
    transaction, then reconciles the in-memory active-count and
    quota-reservation caches so routing decisions observe the cleaned
    state immediately.

    Args:
        db: The primary (write) database connection.
        router: For decrementing active request counts.
        quota_estimator: For removing in-memory reservation tracking.
        max_pending_seconds: How long a request may stay pending
            before it is considered leaked.  Defaults to 300 s, which
            matches the upstream ``read_timeout_s``.
        cycle_interval_s: How long to wait between sweeps.  Defaults
            to 60 s in production; tests pass a smaller value to avoid
            the 60-second wait.
    """
    while True:
        await asyncio.sleep(cycle_interval_s)
        try:
            await _finalize_stale_requests_once(
                db=db,
                router=router,
                quota_estimator=quota_estimator,
                max_pending_seconds=max_pending_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale request finalizer failed")


async def _finalize_stale_requests_once(
    db: Database,
    router: Router,
    quota_estimator: QuotaEstimator | None,
    max_pending_seconds: float,
) -> int:
    """Run a single sweep of the stale-request finalizer.

    Returns the number of leaked requests that were transitioned.
    Split out from :func:`_finalize_stale_requests` so tests and
    one-off operators can invoke the sweep directly without waiting
    for the periodic loop.
    """
    threshold = f"-{int(max_pending_seconds)} seconds"
    async with db.transaction():
        # Find leaked pending requests.  The JOINs keep the
        # accounting logic local to one query so a separate sweep is
        # not needed to map accounts to names.
        rows = await db.execute_returning(
            "SELECT r.id, r.account_id, a.name AS account_name, "
            "       res.id AS reservation_id, "
            "       res.reserved_microdollars "
            "FROM requests r "
            "JOIN accounts a ON a.id = r.account_id "
            "LEFT JOIN reservations res "
            "    ON res.request_id = r.id AND res.status = 'active' "
            "WHERE r.status = 'pending' "
            "  AND r.started_at < datetime('now', ?)",
            (threshold,),
        )
        transitioned = [dict(row) for row in rows]
        if not transitioned:
            return 0

        request_ids = [r["id"] for r in transitioned]
        reservation_ids = [
            r["reservation_id"] for r in transitioned if r["reservation_id"] is not None
        ]

        # Mark requests interrupted.  Re-checking ``status = 'pending'``
        # inside the UPDATE guards against a concurrent legitimate
        # finalizer that finalized one of the rows between the SELECT
        # and the UPDATE.
        req_placeholders = ",".join("?" * len(request_ids))
        await db.execute_write(
            f"UPDATE requests "
            f"SET status = 'interrupted', "
            f"    completed_at = CURRENT_TIMESTAMP, "
            f"    error_class = 'StaleRequestFinalizer' "
            f"WHERE id IN ({req_placeholders}) "
            f"  AND status = 'pending'",
            tuple(request_ids),
        )

        # Release associated reservations.  Same ``status`` guard so
        # a legitimate finalizer is not raced.
        if reservation_ids:
            res_placeholders = ",".join("?" * len(reservation_ids))
            await db.execute_write(
                f"UPDATE reservations "
                f"SET status = 'released', "
                f"    released_at = CURRENT_TIMESTAMP, "
                f"    release_reason = 'stale_request' "
                f"WHERE id IN ({res_placeholders}) "
                f"  AND status = 'active'",
                tuple(reservation_ids),
            )

        # Phase 3: emit an operational event summarising the sweep.
        # Recorded inside the same transaction so a crash between the
        # finalizer and the event cannot leave durable state without
        # its audit row.
        await OperationalEventRepository(db).record(
            event_type="stale_request_finalizer",
            details={
                "leaked_requests": len(transitioned),
                "released_reservations": len(reservation_ids),
            },
        )

    # Post-commit: reconcile runtime state.  Iterate ``transitioned``
    # rather than re-querying so the same
    # ``(account_name, reserved_microdollars)`` rows we just finalized
    # drive the cleanup.  ``decrement_active_request_count`` is
    # deduplicated per account because the count is per-account, but
    # ``remove_reservation`` MUST be called once per leaked row to
    # keep the in-memory reservation total consistent with the
    # released reservations in SQLite.
    seen_accounts: set[str] = set()
    for row in transitioned:
        account_name = row.get("account_name")
        if not account_name:
            continue
        if account_name not in seen_accounts:
            seen_accounts.add(account_name)
            # Decrement active request count (idempotent if already 0)
            await router.decrement_active_request_count(account_name)

        # Remove in-memory reservation tracking.  The exact reserved
        # amount must be removed so a future cost accounting run does
        # not double-count the leaked estimate.
        reserved = row.get("reserved_microdollars") or 0
        if reserved and quota_estimator is not None:
            await quota_estimator.remove_reservation(account_name, int(reserved))

    logger.info(
        "Stale request finalizer: cleaned up %d leaked requests",
        len(transitioned),
    )
    return len(transitioned)


async def _prune_health_disabled_models_loop(
    app_state: Any,
    cycle_interval_s: float = 60.0,
) -> None:
    """Periodic prune: drop stale per-account model state.

    Walks every account in the registry, asks the catalog cache for the
    current advertised set, and prunes ``model_availability`` rows on
    :class:`AccountRuntimeState` and ``disabled_models`` rows on the
    matching :class:`AccountHealth` whose ``model_id`` is no longer
    advertised. The prune is a no-op for accounts whose sets are
    already clean; log lines are emitted at INFO only when rows were
    actually removed.
    """
    while True:
        await asyncio.sleep(cycle_interval_s)
        try:
            pruned = await _prune_health_disabled_models_once(app_state)
            if pruned > 0:
                logger.info(
                    "health_disabled_models_prune: removed %d stale rows",
                    pruned,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("health_disabled_models_prune failed")


async def _prune_health_disabled_models_once(app_state: Any) -> int:
    """Run a single sweep of the disabled-models prune.

    Returns the number of pruned rows. Split out from
    :func:`_prune_health_disabled_models_loop` so tests and one-off
    operators can invoke the sweep directly without waiting for the
    periodic loop. Resolves dependencies lazily so a partially wired
    app_state does not crash the sweep.
    """
    registry: AccountRegistry | None = getattr(app_state, "registry", None)
    health_manager = getattr(app_state, "health_manager", None)
    catalog = getattr(app_state, "catalog", None)
    if registry is None or health_manager is None or catalog is None:
        return 0
    cache = getattr(catalog, "cache", None)
    if cache is None:
        return 0

    total = 0
    for state in registry.get_all_states():
        try:
            advertised = {
                mid
                for mid, accounts in cache._account_support.items()
                if state.name in accounts
            }
            result = registry.prune_account_state(
                state.name,
                advertised,
                health_manager=health_manager,
            )
            total += result["model_availability"] + result["disabled_models"]
        except Exception as exc:  # noqa: BLE001 - per-account isolation
            logger.warning(
                "health_disabled_models_prune: error pruning account=%s: %s",
                state.name,
                exc,
            )
    return total


async def _hydrate_health_from_backoffs(
    repo: AccountBackoffRepository,
    health_manager: HealthManager,
) -> None:
    """Reapply persisted upstream backoffs onto the in-memory health manager.

    Called once at startup after account sync so a 429/402/5xx sequence
    that ended just before the previous shutdown continues to
    suppress the same account (or account/model pair) until the
    recorded deadline expires. ``model_unavailable`` rows with a NULL
    ``backoff_until`` are re-applied as indefinite model disables.

    Errors are surfaced to the caller; the lifespan wraps this call
    in ``try/except`` so a corrupted row cannot block startup.
    """
    account_repo = AccountRepository(repo._db)  # type: ignore[arg-type]  # noqa: SLF001 -- private access by design
    active = await repo.list_active()
    if not active:
        return
    logger.info(
        "Hydrating %d persisted upstream backoffs into HealthManager",
        len(active),
    )
    for row in active:
        account_name = await account_repo.get_name_by_id(int(row["account_id"]))
        if account_name is None:
            continue
        reason = str(row.get("reason") or "")
        model_id = row.get("model_id")
        backoff_until_epoch = row.get("backoff_until_epoch")
        consecutive_failures = int(row.get("consecutive_failures") or 1)
        if reason == "model_unavailable" and backoff_until_epoch is None:
            if model_id:
                health_manager.disable_model(account_name, str(model_id))
            continue
        if backoff_until_epoch is None:
            # Terminal row with unknown handling: skip to avoid
            # creating an infinite-cooldown that the operator did
            # not ask for.
            continue
        remaining = max(0.0, float(backoff_until_epoch) - time.time())
        if remaining <= 0:
            # Already expired; the next periodic ``expire_old`` call
            # will prune it. No need to set a zero-second cooldown.
            continue
        if reason == "quota_exhausted":
            health_manager.record_quota_exhausted(account_name, remaining)
        elif reason == "rate_limited":
            health_manager.record_rate_limit(account_name, remaining)
        elif reason == "authentication_failed":
            health_manager.disable_account(
                account_name, reason="authentication_failed", duration_seconds=remaining
            )
        else:
            # Unknown / transient reason: set a generic cooldown so
            # the account is not selected until the deadline passes.
            health = health_manager.get_account_health(account_name)
            health.cooldown_until = time.time() + remaining
            health.health_state = "cooldown"
            health.is_healthy = False
            health.consecutive_failures = consecutive_failures
            health.last_check = time.time()


def _register_update_checker(
    app: FastAPI,
    supervisor: TaskSupervisor,
    outbound_manager: OutboundClientManager,
) -> UpdateChecker:
    """Register the periodic PyPI update checker as a supervised background task.

    Returns the checker instance so callers can attach it to app.state
    or use it for tests.  The checker runs an initial PyPI probe at
    startup and repeats every 24 hours; it never auto-installs.

    The shared outbound client from *outbound_manager* is used for the
    periodic check so repeated probes reuse the same connection pool
    rather than constructing fresh clients.
    """
    from eggpool.update_checker import UpdateChecker

    update_checker = UpdateChecker()
    app.state.update_checker = update_checker

    async def _run_with_client() -> None:
        update_checker._client = await outbound_manager.get_client()  # pyright: ignore[reportPrivateUsage]
        await update_checker.run_periodic()

    supervisor.register("update_checker", _run_with_client)
    return update_checker


@asynccontextmanager
async def _lifespan_runtime(app: FastAPI) -> AsyncGenerator[None]:
    """Initialize runtime state; cleanup is owned by the outer lifespan."""
    config: AppConfig = app.state.config

    configure_logging(level=config.server.log_level)

    # Record startup timestamps for runtime metrics
    app.state.started_monotonic = time.monotonic()
    app.state.started_epoch = time.time()

    # 1. Validate auth at startup
    require_auth_at_startup(config.server.resolved_api_key)

    # 1b. Validate account credentials
    config.validate_account_credentials()

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

    # 4. Sync providers from config to SQLite
    provider_repo = ProviderRepository(db)
    configured_providers = {
        pid: {"base_url": pcfg.base_url, "protocols": pcfg.protocols}
        for pid, pcfg in config.providers.items()
    }
    await provider_repo.sync_from_config(configured_providers)

    # 5. Sync accounts from config to SQLite
    account_repo = AccountRepository(db)
    config_accounts = account_config_rows(config)
    await account_repo.sync_from_config(config_accounts)

    # 6. Crash recovery
    await _crash_recovery(db)

    # aiosqlite uses one worker thread per connection. The default keeps
    # stats on the primary connection for a single SQLite worker thread;
    # operators can opt into a second read-only stats connection when
    # dashboard analytics should avoid the data-plane connection lock.
    # In-memory SQLite databases cannot be shared by opening a second
    # connection.
    stats_db = db
    if config.database.path != ":memory:" and config.database.worker_threads > 1:
        stats_db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            read_only=True,
        )
        await stats_db.connect()
    app.state.stats_db = stats_db

    # 7. Initialize repositories
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)
    ping_repo = PingRepository(db)

    # 7. HTTPX client pool
    dns_backend: httpcore.AsyncNetworkBackend | None = None
    if config.network.dns_cache.enabled:
        dns_backend = DnsNetworkBackend(
            config.network.dns_cache, default_network_backend()
        )
        app.state.dns_backend = dns_backend
    client_pool = ProviderClientPool.from_app_config(
        config, network_backend=dns_backend
    )
    app.state.client_pool = client_pool
    # Keep backward-compatible alias during transition
    legacy_client = client_pool.get_default_client()
    if legacy_client is not None:
        app.state.httpx_client = legacy_client

    # 7b. Outbound client manager (shared client for background/CLI network paths)
    outbound_manager = OutboundClientManager(
        config=config.network, network_backend=dns_backend
    )
    app.state.outbound_manager = outbound_manager
    # Pre-initialize the shared client so it's ready for catalog resolvers
    outbound_client = await outbound_manager.get_client()

    # 8. Account registry (runtime state)
    registry = AccountRegistry(config)
    app.state.registry = registry

    # 8b. Transcoder policy (protocol transcoding configuration)
    app.state.transcoder_policy = config.transcoder

    # 9. Health manager
    health_manager = HealthManager()
    app.state.health_manager = health_manager

    # 9b. Persistent backoff repository and hydration from SQLite.
    # Phase 4 ensures that real upstream-derived backoffs survive
    # restarts; local-estimate quota overage is never persisted.
    account_backoff_repo = AccountBackoffRepository(db)
    app.state.account_backoff_repo = account_backoff_repo
    try:
        await _hydrate_health_from_backoffs(account_backoff_repo, health_manager)
    except Exception:
        # A corrupted database must not prevent startup; log and
        # continue with the in-memory health manager only.
        logger.exception(
            "Failed to hydrate health manager from persisted backoffs; "
            "continuing without historical suppression state"
        )

    # 10. Catalog service
    catalog = CatalogService(
        config,
        registry,
        db,
        client_pool,
        ping_repo=ping_repo,
        outbound_client=outbound_client,
    )
    app.state.catalog = catalog

    # 11. Attach external pricing resolvers before refresh/persistence
    await catalog.attach_pricing_resolvers()

    # 12. Load cached catalog
    await catalog._load_cached_models()  # pyright: ignore[reportPrivateUsage]

    # 13. Refresh catalog from enabled accounts
    if config.models.startup_refresh:
        try:
            await catalog.refresh()
        except Exception:
            logger.exception("Initial catalog refresh failed")

    # 14. Enforce catalog staleness policy
    if catalog.cache.is_stale(config.models.stale_after_s):
        if not config.models.allow_stale_catalog:
            msg = (
                f"Catalog is stale (older than {config.models.stale_after_s}s) "
                f"and allow_stale_catalog is false"
            )
            logger.error(msg)
            raise CatalogUnavailableError(msg)
        logger.warning(
            "Catalog is stale (older than %ds) but allow_stale_catalog "
            "is true — serving degraded",
            config.models.stale_after_s,
        )

    # 14b. Model info service
    model_info = None
    if config.model_info.enabled:
        from eggpool.model_info.service import ModelInfoService

        try:
            model_info = ModelInfoService(
                config=config.model_info,
                db=db,
                catalog=catalog.cache,
                outbound_client=outbound_client,
            )
            app.state.model_info = model_info
            await model_info.load_cache()
            if config.model_info.startup_refresh:
                try:
                    reconcile_result = await model_info.reconcile_catalog_snapshot(
                        reason="startup"
                    )
                    logger.info(
                        "Model info startup reconciliation: %s",
                        reconcile_result,
                    )
                except Exception:
                    logger.exception("Model info startup reconciliation failed")
        except Exception:
            logger.exception("Failed to initialize model info service")
            model_info = None

    # 15. Price repository and cost calculator
    price_repo = PriceRepository(db)
    cost_calculator = CostCalculator(price_repo)
    catalog.set_price_change_callback(cost_calculator.invalidate_price)
    app.state.cost_calculator = cost_calculator

    # 16. Router (with health manager for circuit breaker integration)
    router = Router(
        registry,
        catalog,
        health_manager=health_manager,
        stale_after_s=float(config.models.stale_after_s),
        local_quota_mode=config.routing.local_quota_mode,
    )
    app.state.router = router

    # 17. Wire routing config into scorer and estimator
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
    router.quota_estimator.default_unknown_reservation_microdollars = (
        config.routing.unknown_request_reservation_microdollars
    )
    router._scorer.prefer_native = config.transcoder.prefer_native  # pyright: ignore[reportPrivateUsage]

    # 18. Load configured model price overrides into estimator
    for model_id, override in config.model_overrides.items():
        input_price = override.input_price_per_1k
        output_price = override.output_price_per_1k
        if input_price is not None and output_price is not None:
            # Convert dollars/1K → dollars/1M (estimator Tier 4 units)
            router.quota_estimator.set_model_override(
                model_id,
                input_price * 1000,
                output_price * 1000,
            )
    for provider in config.providers.values():
        for model_id, override in provider.model_overrides.items():
            global_override = config.model_overrides.get(model_id)
            input_price = (
                override.input_price_per_1k
                if override.input_price_per_1k is not None
                else (
                    global_override.input_price_per_1k
                    if global_override is not None
                    else None
                )
            )
            output_price = (
                override.output_price_per_1k
                if override.output_price_per_1k is not None
                else (
                    global_override.output_price_per_1k
                    if global_override is not None
                    else None
                )
            )
            if input_price is None or output_price is None:
                continue
            for account in provider.accounts:
                router.quota_estimator.set_account_model_override(
                    account.name,
                    model_id,
                    input_price * 1000,
                    output_price * 1000,
                )

    # 19. Load persisted usage windows and set account weights/offsets
    router.quota_estimator.set_usage_window_repo(usage_window_repo)
    config_offsets: dict[str, dict[str, int]] = {}
    for acct_cfg in config.all_accounts():
        config_offsets[acct_cfg.name] = {
            "five_hour": acct_cfg.five_hour_offset_microdollars,
            "weekly": acct_cfg.weekly_offset_microdollars,
            "monthly": acct_cfg.monthly_offset_microdollars,
        }
    await router.quota_estimator.load_persisted_windows(
        offsets=config_offsets,
    )
    # Set account weights from config
    for acct_cfg in config.all_accounts():
        router.set_account_weight(acct_cfg.name, acct_cfg.weight)

    # Configure explicit quota policies from config
    for acct_cfg in config.all_accounts():
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

    # 20. Metrics write coalescer for buffered analytics
    rollup_repo = UsageRollupRepository(db)

    # 21. Statistics service
    app.state.stats = StatsService(
        stats_db,
        health_manager=health_manager,
        ping_repo=PingRepository(stats_db),
        account_backoff_repo=account_backoff_repo,
        rollup_repo=rollup_repo,
    )

    metrics_coalescer = MetricsWriteCoalescer(
        config=config.metrics,
        db=db,
        rollup_repo=rollup_repo,
    )
    app.state.metrics_coalescer = metrics_coalescer

    # 18c. Dispatch-overhead recorder (shared between coordinator and runtime metrics)
    dispatch_overhead_recorder = DispatchOverheadRecorder(window_size=100)
    app.state.dispatch_overhead_recorder = dispatch_overhead_recorder

    # 18d. Request coordinator
    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=client_pool,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
        cost_calculator=cost_calculator,
        quota_estimator=router.quota_estimator,
        max_retry_attempts=1 + config.routing.max_retries_before_stream,
        quota_exhausted_cooldown_seconds=config.routing.quota_exhausted_cooldown_seconds,
        persist_error_detail=config.security.persist_redacted_error_detail,
        config=config,
        account_backoff_repo=account_backoff_repo,
        metrics_coalescer=metrics_coalescer,
        dispatch_overhead_recorder=dispatch_overhead_recorder,
    )
    app.state.coordinator = coordinator

    # 19. Reconcile expired reservations at startup so dashboard counts
    # and in-memory quota state are accurate before readiness reports OK.
    await reconcile_expired_reservations(
        db,
        quota_estimator=router.quota_estimator,
        router=router,
    )

    # 20. Background task supervisor
    supervisor = TaskSupervisor()
    app.state.supervisor = supervisor

    # 20a. Background task monitor for runtime metrics
    from eggpool.background import BackgroundTaskMonitor

    task_monitor = BackgroundTaskMonitor(supervisor)
    app.state.task_monitor = task_monitor

    # 20b. Runtime metrics service (for /api/stats/runtime)
    from eggpool.runtime_metrics import RuntimeMetricsService

    app.state.runtime_metrics = RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=stats_db,
        supervisor=supervisor,
        task_monitor=task_monitor,
        router=router,
        health_manager=health_manager,
        started_monotonic=app.state.started_monotonic,
        started_epoch=app.state.started_epoch,
        metrics_coalescer=metrics_coalescer,
        outbound_manager=outbound_manager,
        dns_backend=dns_backend,
        provider_client_pool=client_pool,
        dispatch_overhead_recorder=dispatch_overhead_recorder,
    )

    # Register catalog refresh task
    if config.models.refresh_interval_s > 0:
        supervisor.register(
            "catalog_refresh",
            lambda: _catalog_refresh_loop(
                catalog,
                config.models.refresh_interval_s,
                model_info if config.model_info.enabled else None,
            ),
        )

    # Register model info periodic refresh task
    if (
        config.model_info.enabled
        and config.model_info.refresh_interval_s > 0
        and model_info is not None
    ):
        supervisor.register(
            "model_info_refresh",
            lambda: model_info.run_periodic_refresh(),
        )

    # Register retention cleanup task (runs every hour)
    async def _retention_cleanup() -> None:
        while True:
            await asyncio.sleep(3600)
            await cleanup_old_requests(db, config.dashboard.retain_request_stats_days)
            await cleanup_old_events(db, config.dashboard.retain_event_days)
            await ping_repo.cleanup_old_pings(config.models.ping_retain_days)
            # Rollup retention cleanup
            await rollup_repo.cleanup_old_rollups(
                config.metrics.rollup_retain_days,
                max_rows=config.metrics.cleanup_max_rows_per_pass,
            )
            # Reconcile expired reservations and sync in-memory state
            await reconcile_expired_reservations(
                db,
                quota_estimator=router.quota_estimator,
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
                await router.quota_estimator.load_persisted_windows()
            except Exception:
                logger.exception("Failed to refresh usage windows")

    supervisor.register("usage_window_refresh", _refresh_usage_windows)

    # Register stale request finalizer (runs every 60s).  Default
    # threshold matches the upstream read_timeout so legitimate
    # long-running requests are never touched; the safety net only
    # catches leaked requests whose finalizer never ran (client
    # disconnect + cancellation timeout killed the generator task
    # before finalize() could acquire the DB lock).
    async def _stale_request_loop() -> None:
        await _finalize_stale_requests(
            db=db,
            router=router,
            quota_estimator=router.quota_estimator,
            max_pending_seconds=config.upstream.read_timeout_s,
        )

    supervisor.register("stale_request_finalizer", _stale_request_loop)

    # Register periodic prune of stale per-account model state
    # (model_availability + disabled_models). Runs every 60s so an
    # upstream model withdrawal is reflected in in-memory state within
    # one cycle without requiring a process restart. Wiring is wrapped
    # so a missing dependency (e.g. a future test app) cannot crash
    # startup.
    try:
        supervisor.register(
            "health_disabled_models_prune",
            lambda: _prune_health_disabled_models_loop(app.state),
        )
    except Exception:  # noqa: BLE001 - best-effort registration
        logger.exception(
            "Failed to register health_disabled_models_prune; skipping",
        )

    # Register metrics flush task for buffered modes
    metrics_stop_event = asyncio.Event()
    app.state.metrics_stop_event = metrics_stop_event
    if config.metrics.write_mode != "immediate":
        supervisor.register(
            "metrics_flush",
            lambda: metrics_coalescer.run(metrics_stop_event),
        )

    # Periodic PyPI update check (default 24h). Drives the dashboard
    # footer indicator and the /api/stats/update endpoint; never
    # auto-installs.  Runs an initial check immediately so the first
    # dashboard render shows the latest state.
    _register_update_checker(app, supervisor, outbound_manager)

    # Register automatic backup task (default: daily, retain 14).
    if config.backup.enabled and config.backup.interval_s > 0:
        from eggpool.background.backup import automatic_backup_loop

        raw_config_path: str | None = getattr(app.state, "config_path", None)
        resolved_config_path = Path(raw_config_path) if raw_config_path else None
        resolved_env_path: Path | None = None
        if resolved_config_path is not None:
            candidate = resolved_config_path.parent / ".env"
            if candidate.exists():
                resolved_env_path = candidate

        supervisor.register(
            "automatic_backup",
            lambda: automatic_backup_loop(
                config=config,
                db=db,
                config_path=resolved_config_path,
                env_path=resolved_env_path,
            ),
        )

    # 21. Start background tasks
    await supervisor.start_all()

    # 22. Transcoding status
    if config.transcoder.enabled is False:
        logger.warning(
            "Protocol transcoding DISABLED via [transcoder] enabled = false. "
            "Cross-protocol requests will fail with HTTP 400 "
            "(ProtocolMismatchError). loss_policy=%s prefer_native=%s",
            config.transcoder.loss_policy,
            config.transcoder.prefer_native,
        )
    else:
        logger.info(
            "Protocol transcoding ENABLED (default) — clients may reach "
            "upstream accounts whose provider.protocols does not match the "
            "client protocol. loss_policy=%s prefer_native=%s",
            config.transcoder.loss_policy,
            config.transcoder.prefer_native,
        )

    # 23. Startup complete
    logger.info(
        "Application started (%d accounts, %d models). "
        "Restart the process to apply configuration changes.",
        len(config.all_accounts()),
        catalog.cache.model_count,
    )

    yield


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage startup and clean up resources even when startup fails."""
    try:
        async with _lifespan_runtime(app):
            yield
    finally:
        logger.info("Application shutting down")

        # Signal metrics coalescer to stop and flush remaining data
        metrics_stop_event: asyncio.Event | None = getattr(
            app.state, "metrics_stop_event", None
        )
        if metrics_stop_event is not None:
            metrics_stop_event.set()
        metrics_coalescer: MetricsWriteCoalescer | None = getattr(
            app.state, "metrics_coalescer", None
        )
        if metrics_coalescer is not None:
            try:
                await asyncio.wait_for(
                    metrics_coalescer.flush(reason="shutdown"), timeout=5.0
                )
            except Exception:
                logger.exception("Error flushing metrics buffer during shutdown")

        supervisor: TaskSupervisor | None = getattr(app.state, "supervisor", None)
        if supervisor is not None:
            try:
                await supervisor.stop_all()
            except Exception:
                logger.exception("Error stopping background tasks during shutdown")

        client_pool: ProviderClientPool | None = getattr(app.state, "client_pool", None)
        if client_pool is not None:
            try:
                await client_pool.close()
            except Exception:
                logger.exception("Error closing client pool during shutdown")

        outbound_manager: OutboundClientManager | None = getattr(
            app.state, "outbound_manager", None
        )
        if outbound_manager is not None:
            try:
                await outbound_manager.aclose()
            except Exception:
                logger.exception(
                    "Error closing outbound client manager during shutdown"
                )

        db: Database | None = getattr(app.state, "db", None)
        stats_db: Database | None = getattr(app.state, "stats_db", None)
        if stats_db is not None and stats_db is not db:
            try:
                await stats_db.disconnect()
            except Exception:
                logger.exception("Error closing statistics database during shutdown")
        if db is not None:
            try:
                await db.disconnect()
            except Exception:
                logger.exception("Error closing database during shutdown")


async def _catalog_refresh_loop(
    catalog: CatalogService,
    interval_s: int,
    model_info: Any = None,
) -> None:
    """Background task for periodic catalog refresh."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            result = await catalog.refresh()
            if model_info is not None:
                try:
                    await model_info.reconcile_catalog_refresh(result)
                except Exception:
                    logger.exception(
                        "Model info reconciliation after catalog refresh failed"
                    )
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
        if config_path is not None:
            config = AppConfig.from_toml(config_path)
        else:
            config = AppConfig()

    app = FastAPI(
        title="EggPool",
        version=_get_version("eggpool"),
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

    # Dashboard and statistics routes (require auth unless dashboard.public = true)
    if config.dashboard.enabled:
        dashboard_require_auth = not config.dashboard.public
        register_dashboard_routes(app, require_auth=dashboard_require_auth)
        register_stats_routes(app, require_auth=dashboard_require_auth)
        register_backoff_routes(app, require_auth=dashboard_require_auth)

        @app.get("/static/dashboard.css")
        async def dashboard_css() -> Response:  # pyright: ignore[reportUnusedFunction]
            css_path: Path = (
                Path(__file__).parent / "dashboard" / "static" / "dashboard.css"
            )
            return FileResponse(
                path=str(css_path),
                media_type="text/css",
                headers={"Cache-Control": "public, max-age=300"},
            )

        @app.get("/static/favicon.svg")
        async def favicon_svg() -> Response:  # pyright: ignore[reportUnusedFunction]
            svg_path: Path = (
                Path(__file__).parent / "dashboard" / "static" / "favicon.svg"
            )
            return FileResponse(
                path=str(svg_path),
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get("/static/chart.js")
        async def chart_js() -> Response:  # pyright: ignore[reportUnusedFunction]
            js_path: Path = (
                Path(__file__).parent / "dashboard" / "static" / "chart.umd.min.js"
            )
            return FileResponse(
                path=str(js_path),
                media_type="application/javascript",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get("/static/dashboard.js")
        async def dashboard_js() -> Response:  # pyright: ignore[reportUnusedFunction]
            js_path: Path = (
                Path(__file__).parent / "dashboard" / "static" / "dashboard.js"
            )
            return FileResponse(
                path=str(js_path),
                media_type="application/javascript",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        # LRU cache for theme CSS: keeps last 3 used themes, TTL 300s for non-active
        class _ThemeCssCache:
            def __init__(self, max_size: int = 3, ttl_s: int = 300) -> None:
                self._max_size = max_size
                self._ttl_s = ttl_s
                self._cache: dict[tuple[str, str | None], tuple[str, float]] = {}
                self._last_used: tuple[str, str | None] | None = None

            def get(self, key: tuple[str, str | None]) -> str | None:
                if key in self._cache:
                    css, ts = self._cache[key]
                    if time.monotonic() - ts < self._ttl_s or key == self._last_used:
                        self._last_used = key
                        return css
                    del self._cache[key]
                return None

            def put(self, key: tuple[str, str | None], css: str) -> None:
                if len(self._cache) >= self._max_size and key not in self._cache:
                    now = time.monotonic()
                    to_evict = [
                        k
                        for k, (_, ts) in self._cache.items()
                        if k != self._last_used and now - ts >= self._ttl_s
                    ]
                    if to_evict:
                        del self._cache[to_evict[0]]
                    elif self._cache:
                        oldest = min(
                            self._cache,
                            key=lambda k: self._cache[k][1],
                        )
                        if oldest != self._last_used:
                            del self._cache[oldest]
                self._cache[key] = (css, time.monotonic())
                self._last_used = key

        _theme_css_cache = _ThemeCssCache()

        @app.get("/static/theme.css")
        async def theme_css(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
            theme_name = request.query_params.get("theme", "default")
            themes_dir = config.dashboard.themes_dir
            cache_key = (theme_name, themes_dir)
            cached = _theme_css_cache.get(cache_key)
            if cached is not None:
                return Response(
                    content=cached,
                    media_type="text/css",
                    headers={"Cache-Control": "public, max-age=300"},
                )
            from eggpool.dashboard.render import get_theme_css

            css = get_theme_css(theme_name, themes_dir)
            _theme_css_cache.put(cache_key, css)
            return Response(
                content=css,
                media_type="text/css",
                headers={"Cache-Control": "public, max-age=300"},
            )

    # Runtime metrics and network diagnostics endpoints — always auth-gated
    from eggpool.api.network import register_network_routes
    from eggpool.api.runtime import register_runtime_routes
    from eggpool.api.update import register_update_routes

    register_runtime_routes(app)
    register_network_routes(app)
    register_update_routes(app)

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
        if not config.all_accounts():
            return Response(
                content='{"status":"degraded","reason":"no accounts configured"}',
                status_code=503,
                media_type="application/json",
            )

        has_enabled = any(acct.enabled for acct in config.all_accounts())
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
            has_credentials = any(
                registry.has_usable_credentials(s.name) for s in enabled_states
            )
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

        config: AppConfig = request.app.state.config
        catalog: CatalogService = request.app.state.catalog
        health_mgr: HealthManager | None = getattr(
            request.app.state, "health_manager", None
        )
        models = catalog.get_models_for_exposure(health_manager=health_mgr)

        data: list[dict[str, Any]] = []
        for m in models:
            provider_id = m.get("provider_id")
            routing_priority: int | None = None
            if provider_id is not None:
                provider_cfg = config.providers.get(provider_id)
                if provider_cfg is not None:
                    routing_priority = provider_cfg.routing_priority
            # Collapsed entries carry no provider_id; surface the
            # contributing providers list and the max routing priority
            # across them.
            providers: list[str] | None = None
            routing_priority_max: int | None = None
            if provider_id is None:
                collapsed_providers: list[str] = list(m.get("providers") or [])
                providers = collapsed_providers
                if providers:
                    priorities = [
                        cfg.routing_priority
                        for pid in providers
                        if (cfg := config.providers.get(pid)) is not None
                    ]
                    if priorities:
                        routing_priority_max = max(priorities)
            data.append(
                serialize_openai_model(
                    m,
                    routing_priority=routing_priority,
                    routing_priority_max=routing_priority_max,
                    providers=providers,
                )
            )

        return {"object": "list", "data": data}

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
        if isinstance(exc, RequestTooLargeError):
            status_code = 413
        elif isinstance(exc, ModelNotFoundError):
            status_code = 404
        elif isinstance(exc, (NoEligibleAccountError, CatalogUnavailableError)):
            status_code = 503
        else:
            status_code = 502
        return JSONResponse(
            status_code=status_code,
            content={"error": str(exc)},
        )

    return app
