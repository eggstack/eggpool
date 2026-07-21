"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from importlib.metadata import version as _get_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from eggpool.accounts.registry import AccountRegistry, account_config_rows
from eggpool.api.backoff import register_backoff_routes
from eggpool.api.chat_completions import handle_chat_completions
from eggpool.api.messages import handle_messages
from eggpool.api.models import serialize_openai_model
from eggpool.api.stats import register_stats_routes
from eggpool.auth import require_auth, require_auth_at_startup
from eggpool.background import TaskSupervisor
from eggpool.background.cleanup import (
    reconcile_expired_reservations,
)
from eggpool.catalog.pricing import CostCalculator, PriceRepository
from eggpool.catalog.service import CatalogService
from eggpool.cli_exit_codes import STAGE_RELOAD_IN_PROGRESS
from eggpool.constants import API_V1_PREFIX, MAX_REQUEST_BODY_BYTES
from eggpool.control.reload_manager import ReloadInProgressError, ReloadManager
from eggpool.control.server import (
    PROTOCOL_VERSION,
    ControlRequest,
    ControlResponse,
    ControlServer,
)
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
from eggpool.event_loop_lag import EventLoopLagMonitor
from eggpool.health.health_manager import HealthManager
from eggpool.jsonx import dumps_bytes as jsonx_dumps_bytes
from eggpool.logging import configure_logging
from eggpool.metrics.buffer import MetricsWriteCoalescer
from eggpool.model_info.presentation import compact_model_info_summary
from eggpool.models.api import HealthResponse
from eggpool.models.config import AppConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.dns_cache import DnsNetworkBackend
from eggpool.providers.outbound import OutboundClientManager, default_network_backend
from eggpool.request.coordinator import RequestCoordinator
from eggpool.request.stream_diagnostics import get_stream_diagnostics
from eggpool.routing.config import routing_stale_after_s
from eggpool.routing.router import Router
from eggpool.runtime_dispatch import (
    DispatchOverheadRecorder,
    DispatchSpanRecorder,
    LocalPreUpstreamRecorder,
)
from eggpool.runtime_manager import (
    ProcessRuntime,
    RuntimeGeneration,
    RuntimeManager,
    attach_runtime_manager,
)
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import httpcore

    from eggpool.quota.estimation import QuotaEstimator

logger = logging.getLogger(__name__)


class _BodyLimitMiddleware:
    """Reject requests whose Content-Length exceeds the configured limit.

    Implemented as a raw ASGI middleware to avoid the body-buffering
    overhead of ``BaseHTTPMiddleware``.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:  # noqa: ANN401
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        content_length: str | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                content_length = value.decode("latin-1")
                break

        if content_length is not None:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = None
            if declared_size is not None and declared_size > self._max_bytes:
                path = scope.get("path", "")
                if path.endswith("/messages"):
                    error_body = jsonx_dumps_bytes(
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
                        b'{"error":{"message":"Request body too large",'
                        b'"type":"invalid_request_error"}}'
                    )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [
                                b"content-length",
                                str(len(error_body)).encode("ascii"),
                            ],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": error_body})
                return

        await self._app(scope, receive, send)


class _HeaderRedactionMiddleware:
    """Redact configured headers from upstream responses.

    Implemented as a raw ASGI middleware to avoid the body-buffering
    overhead of ``BaseHTTPMiddleware`` and to work transparently with
    streaming responses.
    """

    def __init__(self, app: Any, headers_to_redact: list[str]) -> None:  # noqa: ANN401
        self._app = app
        self._redact = frozenset(h.lower().encode("ascii") for h in headers_to_redact)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        async def _filtered_send(message: dict[str, Any]) -> None:  # noqa: ANN401
            if message.get("type") == "http.response.start":
                headers = message.get("headers", [])
                message["headers"] = [
                    [name, value]
                    for name, value in headers
                    if name.lower() not in self._redact
                ]
            await send(message)

        await self._app(scope, receive, _filtered_send)


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


async def _finalize_stale_requests(  # pyright: ignore[reportUnusedFunction]
    db: Database,
    router: Router,
    quota_estimator: QuotaEstimator,
    max_pending_seconds: float = 300.0,
    cycle_interval_s: float = 60.0,
) -> None:
    """Legacy ``while True`` wrapper kept for backward compatibility.

    The supervisor now drives the periodic cadence via
    :func:`finalize_stale_requests_once` directly (see the
    registration in :func:`_lifespan_runtime`).  This wrapper remains
    so existing tests that rely on ``_finalize_stale_requests`` for
    direct invocation can still drive a single-pass finalizer with a
    custom ``cycle_interval_s`` without going through the supervisor.
    """
    while True:
        await asyncio.sleep(cycle_interval_s)
        try:
            await finalize_stale_requests_once(
                db=db,
                router=router,
                quota_estimator=quota_estimator,
                max_pending_seconds=max_pending_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale request finalizer failed")


async def finalize_stale_requests_once(
    db: Database,
    router: Router,
    quota_estimator: QuotaEstimator | None,
    max_pending_seconds: float,
    batch_size: int = 500,
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
            "       res.reserved_microdollars, "
            "       res.estimated_tokens "
            "FROM requests r "
            "JOIN accounts a ON a.id = r.account_id "
            "LEFT JOIN reservations res "
            "    ON res.request_id = r.id AND res.status = 'active' "
            "WHERE r.status = 'pending' "
            "  AND r.started_at < datetime('now', ?)"
            " LIMIT ?",
            (threshold, batch_size),
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
    per_account_reconciled: dict[str, dict[str, int]] = {}
    for row in transitioned:
        account_name = row.get("account_name")
        if not account_name:
            continue
        if account_name not in seen_accounts:
            seen_accounts.add(account_name)
            # Decrement active request count (idempotent if already 0)
            await router.decrement_active_request_count(account_name)
            per_account_reconciled[account_name] = {
                "requests": 0,
                "tokens": 0,
                "microdollars": 0,
            }

        # Remove in-memory reservation tracking.  The exact reserved
        # amount must be removed so a future cost accounting run does
        # not double-count the leaked estimate. The in-flight request
        # and projected token components are also decremented so the
        # new load-aware scoring signal stays consistent.
        reserved = row.get("reserved_microdollars") or 0
        leaked_tokens = row.get("estimated_tokens") or 0
        if reserved and quota_estimator is not None:
            await quota_estimator.remove_reservation(
                account_name,
                int(reserved),
                requests=1,
                tokens=int(leaked_tokens),
            )
        bucket = per_account_reconciled.setdefault(
            account_name,
            {"requests": 0, "tokens": 0, "microdollars": 0},
        )
        bucket["requests"] += 1
        bucket["tokens"] += int(leaked_tokens)
        bucket["microdollars"] += int(reserved)

    logger.info(
        "Stale request finalizer: cleaned up %d leaked requests across %d accounts",
        len(transitioned),
        len(seen_accounts),
    )
    if per_account_reconciled:
        for acct, bucket in sorted(per_account_reconciled.items()):
            logger.info(
                "stale_finalizer_reconcile account=%s requests=%d "
                "tokens=%d microdollars=%d",
                acct,
                bucket["requests"],
                bucket["tokens"],
                bucket["microdollars"],
            )
    return len(transitioned)


async def _prune_health_disabled_models_loop(  # pyright: ignore[reportUnusedFunction]
    app_state: Any,
    cycle_interval_s: float = 60.0,
) -> None:
    """Legacy ``while True`` wrapper kept for backward compatibility.

    The supervisor now drives the periodic cadence via
    :func:`prune_health_disabled_models_once` directly.  This
    wrapper is retained so external callers and tests that expect a
    loop entry point can still drive the prune with a custom cadence.
    """
    while True:
        await asyncio.sleep(cycle_interval_s)
        try:
            pruned = await prune_health_disabled_models_once(app_state)
            if pruned > 0:
                logger.info(
                    "health_disabled_models_prune: removed %d stale rows",
                    pruned,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("health_disabled_models_prune failed")


async def prune_health_disabled_models_once(app_state: Any) -> int:
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


def _default_client(generation: RuntimeGeneration) -> Any:
    """Return the provider client pool's default client for ``app.state``.

    Mirrors the pre-milestone-B behaviour of stashing the default
    client on ``app.state.httpx_client`` so legacy consumers that
    bypass the pool still find a usable client.
    """
    pool = generation.client_pool
    if pool is None:  # pyright: ignore[reportUnnecessaryComparison]
        return None
    getter = getattr(pool, "get_default_client", None)
    if getter is None:
        return None
    return getter()


def _mirror_generation_on_app_state(
    app: FastAPI,
    generation: RuntimeGeneration,
) -> None:
    """Mirror the active generation's services onto ``app.state``.

    The mirrors exist so existing dashboard routes, readyz probes,
    and request handlers can keep reading ``app.state.router``,
    ``app.state.catalog``, etc. without immediate refactors.  Milestone
    C's transactional reload will replace these mirrors whenever a new
    generation is published so the pointers always reflect the
    currently active slot.

    The mirror only writes attributes the manager actually owns;
    process-owned attributes (``app.state.db``, ``app.state.config``,
    ``app.state.started_*``) are intentionally untouched.
    """
    # Process-owned attributes that we never overwrite on a reload.
    process_owned = frozenset(
        {
            "db",
            "stats_db",
            "config",
            "config_path",
            "config_digest",
            "started_monotonic",
            "started_epoch",
            "runtime_manager",
        }
    )
    mirrors: dict[str, object] = {
        "registry": generation.registry,
        "catalog": generation.catalog,
        "router": generation.router,
        "coordinator": generation.coordinator,
        "client_pool": generation.client_pool,
        "outbound_manager": generation.outbound_manager,
        "dns_backend": generation.dns_backend,
        "httpx_client": _default_client(generation),
        "health_manager": generation.health_manager,
        "account_backoff_repo": generation.account_backoff_repo,
        "cost_calculator": generation.cost_calculator,
        "transcoder_policy": generation.transcoder_policy,
        "compression_policy": generation.compression_policy,
        "cache_config": generation.cache_config,
        "compression_tuning_registry": generation.compression_tuning_registry,
        "dispatch_overhead_recorder": generation.dispatch_overhead_recorder,
        "dispatch_span_recorder": generation.dispatch_span_recorder,
        "stats": generation.stats_service,
        "supervisor": generation.supervisor,
        "finalization_retry_queue": generation.finalization_retry_queue,
        "routing_trace_guard": generation.routing_trace_guard,
    }
    for name, value in mirrors.items():
        if name in process_owned:
            continue
        if value is None:
            continue
        setattr(app.state, name, value)


async def cleanup_partial_generation(process: ProcessRuntime) -> None:
    """Best-effort cleanup for a partially constructed generation.

    .. deprecated::
        Superseded by :class:`RuntimeGenerationCandidate.abort` (Phase 4).
        The candidate container now tracks and closes resources in reverse
        registration order.  This function is retained for backward
        compatibility with code that still calls
        :meth:`RuntimeGenerationBuilder.cleanup_partial` directly.

    Called by :class:`RuntimeGenerationBuilder.cleanup_partial` when a
    build raises before completion.  Mirrors the deterministic
    retirement order documented for :meth:`RuntimeManager.shutdown`
    (supervisor, then network clients) and never touches
    process-owned resources (database connections, metrics coalescer,
    runtime metrics service) because those live across generations.

    The helper is idempotent; calling it twice is a no-op.  Module-level
    (not underscore-prefixed) because
    :class:`eggpool.runtime_manager.RuntimeGenerationBuilder` calls it
    from a sibling module.
    """
    logger.warning(
        "Partial runtime generation cleanup: "
        "cleanup_partial_generation is a no-op; "
        "use RuntimeGenerationCandidate.abort for explicit cleanup"
    )
    return None


def _log_operational_profile(
    *,
    config: AppConfig,
    db: Database,
    stats_db: Database | None,
    process: ProcessRuntime,
    supervisor: TaskSupervisor,
    process_supervisor: TaskSupervisor | None,
    model_info_enabled: bool,
) -> None:
    """Emit a single structured startup profile log (Milestone A6).

    Captures the runtime knobs that influence timing / database /
    observability measurements so the operator can interpret any
    baseline run captured from this process.  The log deliberately
    excludes secrets, request content, and provider keys.

    Fields:

    - ``workers`` / ``runtime_threads``: Granian process model.
    - ``database_worker_threads``: stats-connection isolation knob.
    - ``stats_db_separate``: ``True`` when stats_db is a separate
      read-only connection.
    - ``wal`` / ``wal_mode`` / ``synchronous``: SQLite durability knobs.
    - ``busy_timeout_ms``: contention timeout.
    - ``routing_trace_mode`` / ``routing_trace_sample_rate``: trace
      write-pressure controls.
    - ``metrics_write_mode`` / ``metrics_flush_interval_s``: metrics
      coalescer knobs.
    - ``transcoder_enabled`` / ``compression_enabled`` /
      ``compression_mode`` / ``cache_enabled``: pre-upstream
      observability / transformation modes.
    - ``task_total`` / ``task_process_owned`` /
      ``task_generation_leased``: background-task ownership counts.
    - ``model_info_enabled``: whether the model-info service is
      active.
    """
    stats_db_separate = stats_db is not None and stats_db is not db
    gen_tasks = list(supervisor._tasks.keys())  # pyright: ignore[reportPrivateUsage]
    proc_tasks: list[str] = []
    if process_supervisor is not None:
        proc_tasks = list(
            process_supervisor._tasks.keys()  # pyright: ignore[reportPrivateUsage]
        )
    process_owned = {
        "checkpoint",
        "metrics_flush",
        "update_checker",
        "automatic_backup",
    }
    proc_owned_count = sum(1 for n in proc_tasks if n in process_owned)
    gen_leased_count = sum(1 for n in gen_tasks if n not in process_owned)

    trace_mode = getattr(getattr(config.routing, "trace", None), "mode", "sampled")
    trace_sample_rate = getattr(
        getattr(config.routing, "trace", None), "sample_rate", 0.05
    )
    trace_queue_capacity = getattr(
        getattr(config.routing, "trace", None), "queue_capacity", 1000
    )
    transcoder_enabled = bool(getattr(config.transcoder, "enabled", True))
    compression_enabled = bool(getattr(config.compression, "enabled", False))
    compression_mode = str(getattr(config.compression, "mode", "off"))
    cache_enabled = bool(getattr(config.cache, "enabled", False))

    # Runtime topology (Milestone F): process identity and asyncio
    # loop identity so operators can verify the single-loop model.
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = None

    profile = {
        "workers": 1,
        "runtime_threads": config.server.threads,
        "database_worker_threads": config.database.worker_threads,
        "stats_db_separate": stats_db_separate,
        "wal": config.database.wal,
        "synchronous": config.database.synchronous,
        "busy_timeout_ms": config.database.busy_timeout_ms,
        "routing_trace_mode": trace_mode,
        "routing_trace_sample_rate": trace_sample_rate,
        "routing_trace_queue_capacity": trace_queue_capacity,
        "metrics_write_mode": config.metrics.write_mode,
        "metrics_flush_interval_s": config.metrics.flush_interval_s,
        "transcoder_enabled": transcoder_enabled,
        "compression_enabled": compression_enabled,
        "compression_mode": compression_mode,
        "cache_enabled": cache_enabled,
        "model_info_enabled": model_info_enabled,
        "task_total": len(gen_tasks) + len(proc_tasks),
        "task_process_owned": proc_owned_count,
        "task_generation_leased": gen_leased_count,
        "process_task_spec_version": getattr(process, "task_spec_version", 0),
        "pid": os.getpid(),
        "asyncio_loop_id": loop_id,
    }
    # ``extra={"profile": ...}`` lets structured-log consumers parse
    # the dict directly without scraping the rendered message.  The
    # human-readable summary is still rendered into ``msg`` for the
    # plain-text log path.
    logger.info(
        "Operational profile: %s",
        profile,
        extra={"profile": profile},
    )

    # Runtime topology (Milestone F): explicit topology summary so
    # operators can verify the single-loop model at a glance.
    logger.info(
        "Runtime topology: pid=%d threads=%d asyncio_loop_id=%s",
        os.getpid(),
        config.server.threads,
        loop_id,
    )


def register_candidate_tasks(
    supervisor: TaskSupervisor,
    config: AppConfig,
    process: ProcessRuntime,
    runtime_manager: RuntimeManager,
    *,
    effective_model_info: Any = None,  # noqa: ARG001 -- legacy arg, ignored
    process_supervisor: TaskSupervisor | None = None,
) -> None:
    """Backward-compatible wrapper for the unified task registration.

    The closure-pass plan unified startup and candidate task
    registration behind :func:`eggpool.runtime_tasks.register_runtime_tasks`.
    This thin wrapper preserves the old positional signature so
    external callers (and the reload manager) keep working, while
    delegating to the shared function.  ``effective_model_info`` is
    accepted for backward compatibility but ignored -- the unified
    function acquires the current generation per tick and reads
    ``gen.model_info`` directly.

    When ``process_supervisor`` is provided, process-owned tasks
    (checkpoint, metrics_flush, update_checker, automatic_backup)
    register there; otherwise they register on the gen supervisor
    for backward compatibility.
    """
    from eggpool.runtime_tasks import (  # noqa: PLC0415
        TaskRegistrationContext,
        register_runtime_tasks,
    )

    register_runtime_tasks(
        supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=runtime_manager,
            config=config,
            update_checker_outbound=None,
            process_supervisor=process_supervisor,
        ),
    )


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

    # aiosqlite uses one worker thread per connection. The default of 2 opens
    # a separate read-only stats connection for file-backed databases so
    # dashboard analytics do not share the data-plane connection lock.
    # Set worker_threads = 1 for minimum-footprint mode on extremely
    # constrained devices or in-memory test databases. In-memory SQLite
    # databases cannot be shared by opening a second connection.
    stats_db = db
    if config.database.path != ":memory:" and config.database.worker_threads > 1:
        stats_db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            read_only=True,
        )
        await stats_db.connect()
    app.state.stats_db = stats_db

    # 6b. ProcessRuntime — process-owned dependency container for
    # resources that outlive any single generation (milestone B/C).
    raw_config_path: str | None = getattr(app.state, "config_path", None)
    process = ProcessRuntime(
        db=db,
        stats_db=stats_db,
        config_path=raw_config_path,
        metrics_coalescer=None,  # populated later after coalescer init
    )
    app.state.process = process

    # Operator warning: when dashboard reads must share the primary
    # connection, they queue behind the request path and amplify lock
    # contention under high concurrency.  This is informational, not
    # fatal — minimum-footprint installs may accept the trade.
    if (
        config.dashboard.enabled
        and stats_db is db
        and config.database.path != ":memory:"
    ):
        logger.warning(
            "dashboard_shares_data_plane: database.worker_threads=%d and "
            "dashboard.enabled=true on a file-backed database will route "
            "dashboard reads through the primary connection. "
            "Consider worker_threads=2 for production installs.",
            config.database.worker_threads,
        )

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
    # 8c. Compression policy (Phase 4 observe-mode compression
    # accounting).  Disabled by default; the proxy_request
    # handler reads this from app.state and short-circuits the
    # analyzer when ``enabled = false``.
    app.state.compression_policy = config.compression
    # 8c.b Phase 10 closed-loop threshold tuning.  The registry holds
    # at most one runtime override per policy.  The proxy_request
    # resolver looks up the registry by resolved policy name and
    # overlays the tunable thresholds (min_candidate_tokens,
    # min_savings_tokens, max_compression_latency_ms) onto the
    # request's effective config.  All other knobs (enabled, mode,
    # placement, transforms, static-prefix, synthetic cache knobs)
    # are immutable from the registry.  Operators populate entries
    # by setting ``[compression.tuning] mode = "apply"`` (advisory
    # dashboards only fire in ``recommend`` mode).
    from eggpool.transcoder.compression.tuning import (
        RuntimeCompressionPolicyOverrideRegistry,
    )

    app.state.compression_tuning_registry = RuntimeCompressionPolicyOverrideRegistry()
    # 8d. Phase 9 synthetic cache-controls config.  Disabled by
    # default; the proxy_request handler reads this from app.state
    # and short-circuits the selector when ``enabled = false``.
    app.state.cache_config = config.cache

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
                # Backfill canonical rows for any models table rows
                # that reconcile_catalog_snapshot missed (e.g. models
                # withdrawn and later reappeared).
                try:
                    backfill = await model_info.backfill_missing_canonical()
                    if backfill["backfilled"] > 0:
                        logger.info("Model info startup backfill: %s", backfill)
                except Exception:
                    logger.exception("Model info startup backfill failed")
                # Phase F: upgrade pre-Phase-B canonical rows whose
                # ``detail`` lacks the nested ``limits`` block.  This
                # is a one-shot repair that runs alongside the
                # regular startup backfill so legacy rows participate
                # in the normalized detail schema on the next reload.
                try:
                    legacy_repair = await model_info.backfill_legacy_detail_blocks()
                    if legacy_repair["upgraded"] > 0:
                        logger.info(
                            "Model info legacy detail backfill: %s",
                            legacy_repair,
                        )
                except Exception:
                    logger.exception("Model info legacy detail backfill failed")
        except Exception:
            logger.exception("Failed to initialize model info service")
            model_info = None

    # 15. Price repository and cost calculator
    price_repo = PriceRepository(db)
    cost_calculator = CostCalculator(price_repo)
    catalog.set_price_change_callback(cost_calculator.invalidate_price)
    app.state.cost_calculator = cost_calculator

    # 16. Router (with health manager for circuit breaker integration)
    def _schedule_missing_account_recovery(account_name: str) -> None:
        """Fire-and-forget one-shot catalog refresh for an account that
        is configured+healthy but missing from ``_account_support``.

        A transient per-account refresh failure can age a sibling's
        ``_account_last_refresh`` past ``stale_after_s`` and silently
        de-pool the account from routing. The router detects this
        condition and asks the catalog service to refresh a single
        account so traffic can re-spread across configured siblings.
        """

        async def _run() -> None:
            try:
                await catalog.refresh_one_account(account_name)
            except Exception:
                logger.exception(
                    "One-shot catalog recovery failed for %r", account_name
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_run())

    router = Router(
        registry,
        catalog,
        health_manager=health_manager,
        stale_after_s=routing_stale_after_s(config),
        local_quota_mode=config.routing.local_quota_mode,
        fairness_mode=config.routing.fairness_mode,
        fairness_epsilon=config.routing.fairness_epsilon,
        fairness_scope=config.routing.fairness_scope,
        missing_account_recovery_callback=_schedule_missing_account_recovery,
        missing_account_recovery_min_interval_s=float(config.models.refresh_interval_s)
        / 2.0,
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

    # 20. Metrics write coalescer for buffered analytics.  Writes stay on the
    # primary connection; dashboard rollup reads use the same dedicated
    # read-only stats connection as the rest of StatsService when available.
    rollup_repo = UsageRollupRepository(db)
    stats_rollup_repo = UsageRollupRepository(stats_db)
    stats_account_backoff_repo = (
        account_backoff_repo if stats_db is db else AccountBackoffRepository(stats_db)
    )

    # 21. Statistics service
    app.state.stats = StatsService(
        stats_db,
        health_manager=health_manager,
        ping_repo=PingRepository(stats_db),
        account_backoff_repo=stats_account_backoff_repo,
        rollup_repo=stats_rollup_repo,
    )
    if config.dashboard.enabled and stats_db is db:
        logger.warning(
            "stats_db_fallback_to_primary: stats_db is the same connection as "
            "the primary db; dashboard reads will queue behind the request "
            "path. Set database.worker_threads=2 for a dedicated read-only "
            "stats connection."
        )

    metrics_coalescer = MetricsWriteCoalescer(
        config=config.metrics,
        db=db,
        rollup_repo=rollup_repo,
    )
    app.state.metrics_coalescer = metrics_coalescer
    process.metrics_coalescer = metrics_coalescer

    # 18c. Dispatch-overhead recorder (shared between coordinator and runtime metrics)
    dispatch_overhead_recorder = DispatchOverheadRecorder(window_size=100)
    app.state.dispatch_overhead_recorder = dispatch_overhead_recorder

    # 18c.1. Local pre-upstream recorder (Milestone A4): total
    # EggPool-side latency from ASGI handler entry to dispatch.  Distinct
    # from the coarse ``dispatch_overhead`` above which only covers the
    # coordinator-internal slice.
    local_pre_upstream_recorder = LocalPreUpstreamRecorder(window_size=100)
    app.state.local_pre_upstream_recorder = local_pre_upstream_recorder

    # 18c.1. Dispatch-span recorder for fine-grained per-region latency
    # (Phase 1 hot-path dispatch optimization).  Tracks named spans
    # such as ``body_read``, ``json_parse``, ``routing_plan``,
    # ``selection_lock_wait`` and ``compression_apply`` so operators
    # can identify dispatch hotspots without re-reading source code.
    dispatch_span_recorder = DispatchSpanRecorder(
        window_size=200,
        detailed_span_sample_rate=config.metrics.detailed_span_sample_rate,
    )
    app.state.dispatch_span_recorder = dispatch_span_recorder

    # 18d.1. Dispatch persistence writer (Milestone C): process-owned
    # microbatching writer that persists dispatch bundles in bounded
    # batches.  Only constructed when the feature is enabled in config.
    dispatch_writer = None
    if config.dispatch_writer.enabled:
        from eggpool.request.dispatch_writer import (  # noqa: PLC0415
            DispatchPersistenceWriter,
        )

        dispatch_writer = DispatchPersistenceWriter(
            db=db,
            max_queue_depth=config.dispatch_writer.max_queue_depth,
            max_batch_size=config.dispatch_writer.max_batch_size,
            max_batch_wait_ms=config.dispatch_writer.max_batch_wait_ms,
            enqueue_timeout_ms=config.dispatch_writer.enqueue_timeout_ms,
            shutdown_drain_timeout_s=config.dispatch_writer.shutdown_drain_timeout_s,
        )
        dispatch_writer.start()
    process.dispatch_writer = dispatch_writer
    app.state.dispatch_writer = dispatch_writer

    # 18d.2. Routing trace writer (Milestone D): process-owned async
    # writer that persists routing-decision traces in background
    # batches, removing trace writes from the synchronous request path.
    from eggpool.db.repositories import RoutingDecisionRepository  # noqa: PLC0415
    from eggpool.observability.routing_trace_writer import (  # noqa: PLC0415
        RoutingTraceWriter,
    )

    routing_trace_writer = RoutingTraceWriter(
        db=db,
        routing_decision_repo=RoutingDecisionRepository(db),
        queue_capacity=config.routing.trace.queue_capacity,
        flush_interval_s=config.routing.trace.flush_interval_s,
        max_batch_size=config.routing.trace.max_batch_size,
        shutdown_flush_timeout_s=config.routing.trace.shutdown_flush_timeout_s,
    )
    routing_trace_writer.configure(
        mode=config.routing.trace.mode,
        sample_rate=config.routing.trace.sample_rate,
    )
    routing_trace_writer.start()
    process.routing_trace_writer = routing_trace_writer
    app.state.routing_trace_writer = routing_trace_writer

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
        local_pre_upstream_recorder=local_pre_upstream_recorder,
        dispatch_span_recorder=dispatch_span_recorder,
        transcoder_policy=config.transcoder,
        cache_config=config.cache,
        compression_tuning_registry=app.state.compression_tuning_registry,
        compression_policy=config.compression,
        stream_diagnostics=get_stream_diagnostics(),
        dispatch_writer=dispatch_writer,
        routing_trace_writer=routing_trace_writer,
    )
    app.state.coordinator = coordinator

    # Finalization retry queue (Phase 3): short-cadence targeted drain
    # of cancellation finalizations that escaped the immediate shielded
    # path (10s timeout hit under heavy SQLite lock contention).  Must
    # be constructed before RuntimeMetricsService so the runtime snapshot
    # can expose queue state.
    from eggpool.request.finalization_queue import FinalizationRetryQueue

    finalization_retry_queue = FinalizationRetryQueue(
        db=db,
        # coordinator._finalizer is intentionally wired here so the
        # retry queue can re-run finalization idempotently when the
        # immediate shielded path timed out.
        finalizer=coordinator._finalizer,  # pyright: ignore[reportPrivateUsage]
        router=router,
        quota_estimator=router.quota_estimator,
    )
    app.state.finalization_retry_queue = finalization_retry_queue
    # Re-wire the coordinator so it routes new enqueues to the same
    # queue instance the periodic task drains.
    coordinator._finalization_retry_queue = (  # pyright: ignore[reportPrivateUsage]
        finalization_retry_queue
    )

    # Routing trace write guardrail (Phase 4): when the SQLite
    # lock-wait p95 exceeds the configured threshold, skip the
    # best-effort routing trace writes to avoid amplifying contention.
    from eggpool.request.routing_trace_guard import get_routing_trace_guard

    routing_trace_guard = get_routing_trace_guard()
    routing_trace_guard.configure(
        threshold_ms=config.routing.trace.skip_above_lock_wait_p95_ms,
        queue_occupancy_threshold=config.routing.trace.guard_queue_occupancy_threshold,
        oldest_event_age_s=config.routing.trace.guard_oldest_event_age_s,
        cooldown_s=config.routing.trace.guard_cooldown_s,
    )
    app.state.routing_trace_guard = routing_trace_guard
    coordinator._routing_trace_guard = (  # pyright: ignore[reportPrivateUsage]
        routing_trace_guard
    )

    # 18e. Event-loop lag monitor (process-owned, Milestone F6).
    # Measures event-loop starvation via periodic callback drift.
    event_loop_lag_monitor = EventLoopLagMonitor(
        cadence_s=1.0,
        window_size=200,
    )
    app.state.event_loop_lag_monitor = event_loop_lag_monitor

    # 19. Reconcile expired reservations at startup so dashboard counts
    # and in-memory quota state are accurate before readiness reports OK.
    await reconcile_expired_reservations(
        db,
        quota_estimator=router.quota_estimator,
        router=router,
    )

    # 19b. Publish the initial runtime generation.  This used to be
    # step 24 (after task registration) but the closure pass requires
    # the unified register_runtime_tasks() helper to receive a real
    # RuntimeManager reference rather than a mutable dict ref, so the
    # manager is created first.  The supervisor reference is patched
    # in step 20 below via RuntimeManager.attach_supervisor_to_active.
    runtime_manager = RuntimeManager()
    initial_generation = RuntimeGeneration(
        generation_id=runtime_manager.reserve_next_generation_id(),
        config=config,
        config_digest=getattr(app.state, "config_digest", ""),
        registry=app.state.registry,
        catalog=app.state.catalog,
        router=app.state.router,
        coordinator=app.state.coordinator,
        client_pool=app.state.client_pool,
        outbound_manager=app.state.outbound_manager,
        dns_backend=getattr(app.state, "dns_backend", None),
        health_manager=app.state.health_manager,
        cost_calculator=app.state.cost_calculator,
        transcoder_policy=app.state.transcoder_policy,
        compression_policy=app.state.compression_policy,
        cache_config=app.state.cache_config,
        compression_tuning_registry=app.state.compression_tuning_registry,
        dispatch_overhead_recorder=app.state.dispatch_overhead_recorder,
        dispatch_span_recorder=app.state.dispatch_span_recorder,
        account_backoff_repo=app.state.account_backoff_repo,
        stats_service=app.state.stats,
        supervisor=None,  # patched in step 20 via attach_supervisor_to_active
        finalization_retry_queue=app.state.finalization_retry_queue,
        routing_trace_guard=app.state.routing_trace_guard,
        routing_trace_writer=routing_trace_writer,
        created_at_monotonic=app.state.started_monotonic,
        created_at_epoch=app.state.started_epoch,
    )
    await runtime_manager.install_initial(initial_generation)
    attach_runtime_manager(app, runtime_manager)
    _mirror_generation_on_app_state(app, initial_generation)
    logger.info(
        "RuntimeManager: generation %d published (%d owned services)",
        initial_generation.generation_id,
        len([name for name in vars(initial_generation) if name != "config"]),
    )

    # 20. Background task supervisor.
    supervisor = TaskSupervisor()
    app.state.supervisor = supervisor
    # Patch the active generation's supervisor reference now that it
    # exists so retirement closes it.
    patched = runtime_manager.attach_supervisor_to_active(supervisor)
    if patched is not None:
        _mirror_generation_on_app_state(app, patched)

    # 20a. Process-owned task supervisor (survives generation swaps).
    # Holds process-owned tasks (checkpoint, metrics_flush, update_checker,
    # automatic_backup) so reloads do not lose them.
    process_supervisor = TaskSupervisor()
    process.process_supervisor = process_supervisor

    # 20a. Background task monitor for runtime metrics
    from eggpool.background import BackgroundTaskMonitor

    task_monitor = BackgroundTaskMonitor(supervisor)
    app.state.task_monitor = task_monitor

    # 20b. Runtime metrics service (for /api/stats/runtime)
    # 20c. Dashboard performance telemetry (low-overhead render timing)
    from eggpool.background.maintenance import MaintenanceState
    from eggpool.dashboard.telemetry import DashboardTelemetry
    from eggpool.runtime_metrics import RuntimeMetricsService

    app.state.dashboard_telemetry = DashboardTelemetry()
    app.state.dashboard_telemetry.cache_stats = app.state.stats.cache_snapshot

    # Stream outcome diagnostics service (process-wide singleton so the
    # coordinator and runtime-metrics paths see the same counters).
    app.state.stream_diagnostics = get_stream_diagnostics()

    # Maintenance state aggregator for /api/stats/runtime diagnostics.
    app.state.maintenance_state = MaintenanceState()
    process.maintenance_state = app.state.maintenance_state

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
        local_pre_upstream_recorder=local_pre_upstream_recorder,
        dispatch_span_recorder=dispatch_span_recorder,
        model_info=model_info,
        dashboard_telemetry=app.state.dashboard_telemetry,
        stream_diagnostics=app.state.stream_diagnostics,
        finalization_retry_queue=getattr(app.state, "finalization_retry_queue", None),
        routing_trace_guard=getattr(app.state, "routing_trace_guard", None),
        runtime_manager=None,  # wired in step 24 below
        process=process,
        dispatch_writer=dispatch_writer,
        routing_trace_writer=routing_trace_writer,
        maintenance_state=app.state.maintenance_state,
        event_loop_lag_monitor=event_loop_lag_monitor,
    )

    # Use the unified register_runtime_tasks helper so the startup and
    # reload paths share one registration table.  Pass the
    # update_checker_outbound manager so the process-owned PyPI
    # probe is registered at startup; the reload path leaves it
    # None so a candidate generation does not duplicate the checker.
    from eggpool.runtime_tasks import (  # noqa: PLC0415
        TaskRegistrationContext,
        register_runtime_tasks,
    )

    register_runtime_tasks(
        supervisor,
        TaskRegistrationContext(
            process=process,
            runtime_manager=runtime_manager,
            config=config,
            update_checker_outbound=outbound_manager,
            app_state=app.state,
            process_supervisor=process_supervisor,
        ),
    )
    # 21. Start background tasks
    await supervisor.start_all()
    # Start process-owned tasks on the process supervisor.
    await process_supervisor.start_all()

    # 21b. Start the event-loop lag monitor (process-owned, F6).
    event_loop_lag_monitor.start()

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

    # 23a. Operational profile (Milestone A6).  Single structured log
    # line summarizing the runtime knobs that influence timing /
    # database / observability measurements so operators can interpret
    # any captured baseline (see tests/perf/test_dispatch_baseline.py).
    # The log is intentionally free of secrets, request content, and
    # provider keys.  Counts come from the live registries so the
    # numbers always reflect the post-startup state.
    _log_operational_profile(
        config=config,
        db=db,
        stats_db=stats_db,
        process=process,
        supervisor=supervisor,
        process_supervisor=process_supervisor,
        model_info_enabled=model_info is not None,
    )

    # 23b. Wire the runtime metrics service so its snapshot includes
    # the manager's diagnostics.  The service was constructed before
    # the manager existed; ``runtime_manager=None`` is replaced here.
    runtime_metrics_service = app.state.runtime_metrics
    runtime_metrics_service._runtime_manager = runtime_manager  # pyright: ignore[reportPrivateUsage]
    runtime_metrics_service._process = process  # pyright: ignore[reportPrivateUsage]

    # 24. (moved earlier to step 19b).  The active generation is
    # already installed via install_initial above; the supervisor
    # reference is patched via attach_supervisor_to_active once the
    # supervisor is constructed (step 20).

    # 25. Start the control server for live config rehash (milestone C)
    reload_manager = ReloadManager(
        runtime_manager=runtime_manager,
        process=process,
    )
    app.state.reload_manager = reload_manager
    runtime_metrics_service._reload_manager = reload_manager  # pyright: ignore[reportPrivateUsage]

    async def _control_reload_handler(request: ControlRequest) -> ControlResponse:
        """Handle a reload_config command from the control socket."""
        from eggpool.config_validation import validate_config_file  # noqa: PLC0415

        if process.config_path is None:
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=False,
                stage="validation",
                generation=None,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message="No config file path available for validation",
            )

        try:
            validation = validate_config_file(process.config_path)
        except Exception as exc:
            try:
                from eggpool.db.repositories import (
                    OperationalEventRepository,
                )

                repo = OperationalEventRepository(process.db)
                await repo.record(
                    "reload_validation_rejection",
                    {"error_class": type(exc).__name__},
                )
            except Exception:
                logger.debug(
                    "Failed to record validation rejection event",
                    exc_info=True,
                )
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=False,
                stage="validation",
                generation=None,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message=f"Server-side validation failed: {exc}",
            )

        try:
            result = await reload_manager.reload(
                validation,
                expected_digest=request.validated_digest,
            )
        except ReloadInProgressError as exc:
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=False,
                stage=STAGE_RELOAD_IN_PROGRESS,
                generation=None,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message=str(exc),
            )
        except Exception as exc:
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=False,
                stage="error",
                generation=None,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message=f"Reload failed: {exc}",
            )

        return ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            ok=result.ok,
            stage=(
                result.stage.value
                if hasattr(result.stage, "value")
                else str(result.stage)
            ),
            generation=result.generation,
            changed_sections=result.changed_sections,
            warnings=tuple(w.to_display() for w in result.warnings),
            restart_required=tuple(
                f"{c.path} ({c.old_display} → {c.new_display})"
                for c in result.restart_required
            ),
            retirement_pending=result.ok,
            message=result.message,
        )

    control_server = ControlServer(_control_reload_handler)
    try:
        await control_server.start()
        app.state.control_server = control_server
    except Exception:
        logger.exception("Failed to start control server; live reload unavailable")
        app.state.control_server = None

    yield


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage startup and clean up resources even when startup fails."""
    try:
        async with _lifespan_runtime(app):
            yield
    finally:
        logger.info("Application shutting down")

        # Drive generation retirement through the runtime manager
        # first.  This stops the supervisor and closes generation-owned
        # network clients in the documented order; the metrics coalescer
        # and database remain process-owned and are flushed/closed
        # below so buffered analytics still hit disk on exit.

        # Stop the control server first so no new reload requests arrive
        # while we're tearing down the runtime.
        control_server: ControlServer | None = getattr(
            app.state, "control_server", None
        )
        if control_server is not None:
            try:
                await control_server.stop()
            except Exception:
                logger.exception("Error stopping control server during shutdown")

        runtime_manager: RuntimeManager | None = getattr(
            app.state, "runtime_manager", None
        )
        if runtime_manager is not None:
            try:
                await runtime_manager.shutdown()
            except Exception:
                logger.exception("Error shutting down runtime manager")

        # Stop the event-loop lag monitor before flushing metrics.
        event_loop_lag_monitor: EventLoopLagMonitor | None = getattr(
            app.state, "event_loop_lag_monitor", None
        )
        if event_loop_lag_monitor is not None:
            try:
                await event_loop_lag_monitor.stop()
            except Exception:
                logger.exception(
                    "Error stopping event-loop lag monitor during shutdown"
                )

        # Flush buffered metrics — process-owned; the manager's
        # shutdown only handles generation-owned resources.
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

        # Drain the dispatch writer before closing the database so
        # committed intents are not lost.
        dispatch_writer: Any = getattr(app.state, "dispatch_writer", None)
        if dispatch_writer is not None:
            try:
                await dispatch_writer.stop()
            except Exception:
                logger.exception("Error stopping dispatch writer during shutdown")

        routing_trace_writer: Any = getattr(app.state, "routing_trace_writer", None)
        if routing_trace_writer is not None:
            try:
                await routing_trace_writer.stop()
            except Exception:
                logger.exception("Error stopping routing trace writer during shutdown")

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


async def _catalog_refresh_loop(  # pyright: ignore[reportUnusedFunction]
    catalog: CatalogService,
    interval_s: int,
    model_info: Any = None,
) -> None:
    """Inner-loop catalog refresh body, retained for test compatibility.

    Historical lifecycle: a ``while True`` coroutine that slept for
    ``interval_s`` between refreshes and reconciled model-info state
    after every refresh.

    Production registration now uses
    :meth:`TaskSupervisor.register_periodic` via a one-shot tick
    wrapper registered in :func:`_lifespan_runtime` so the supervisor
    owns cadence + heartbeat.  This legacy loop is kept around only so
    existing tests (which directly invoke it with a small interval
    and cancel after one cycle) continue to compile and exercise the
    catalog.refresh + model_info.reconcile_catalog_refresh seam.
    """
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
    # Best-effort content digest for diagnostics.  ``None`` until
    # milestone C's reload handler runs validation against the file;
    # the milestone-B lifespan records an empty digest so the active
    # generation always carries a (possibly empty) digest string.
    app.state.config_digest = ""

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

        # Model-info JSON endpoints (same auth policy as dashboard)
        if config.model_info.enabled:
            from eggpool.api.model_info import register_model_info_routes

            register_model_info_routes(app, require_auth=dashboard_require_auth)

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

        dispatch_writer: Any = getattr(request.app.state, "dispatch_writer", None)
        if dispatch_writer is not None:
            writer_state = getattr(dispatch_writer, "state", None)
            if writer_state in ("closed", "draining"):
                return Response(
                    content=(
                        '{"status":"degraded",'
                        f'"reason":"dispatch writer {writer_state}"'
                        "}"
                    ),
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

        # Build model-info summary map when enabled and available.
        # A single DB read avoids per-model queries inside the loop.
        model_info_map: dict[str, Any] = {}
        mi_config = getattr(config, "model_info", None)
        mi_service = getattr(request.app.state, "model_info", None)
        if (
            mi_config is not None
            and getattr(mi_config, "include_in_models_endpoint", False)
            and mi_service is not None
        ):
            try:
                raw_map = await mi_service.get_summary_map()
                model_info_map = {
                    mid: compact_model_info_summary(info)
                    for mid, info in raw_map.items()
                }
            except Exception:
                logger.debug("Model info enrichment skipped", exc_info=True)

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

            # Resolve model-info by base_model_id first (for provider-suffixed
            # entries), then by model_id.
            mi_summary: dict[str, Any] | None = None
            if model_info_map:
                base_id = m.get("base_model_id")
                if base_id and base_id in model_info_map:
                    mi_summary = model_info_map[base_id]
                elif m.get("model_id") in model_info_map:
                    mi_summary = model_info_map[m["model_id"]]

            data.append(
                serialize_openai_model(
                    m,
                    routing_priority=routing_priority,
                    routing_priority_max=routing_priority_max,
                    providers=providers,
                    model_info=mi_summary,
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
