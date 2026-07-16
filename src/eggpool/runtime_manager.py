"""Runtime generation ownership and lease infrastructure.

This module implements the runtime-manager primitive introduced in
milestone B of the live-configuration-rehash plan.  It owns:

- :class:`RuntimeGeneration` -- an immutable snapshot of every
  configuration-derived service that can be swapped coherently.
- :class:`GenerationLease` -- an async context manager that hands a
  caller one active generation for the lifetime of a request or
  stream.
- :class:`RuntimeManager` -- the process-wide owner of the active
  generation slot, retiring generations, and lease accounting.
- :class:`RuntimeGenerationBuilder` -- the single construction site
  milestone C will reuse for candidate generation preparation.

Design principles
-----------------

- The active generation is immutable after publication.
- New requests acquire exactly one generation and retain it for their
  entire lifetime.
- Streaming responses retain their generation lease until stream
  completion or disconnect cleanup.
- Configuration-derived clients, routing, quota, health, and task
  scheduling are generation-owned unless documented otherwise.
- Generation teardown is idempotent and closes each owned resource
  exactly once.
- No request path reads a mixture of old and new generation services.

The runtime manager never assumes that milestone C has landed; it
ships a fully working ``install_initial()``/``acquire()``/``shutdown()``
contract that the existing ``_lifespan_runtime`` flow uses, and
milestone C will add a narrow ``install_candidate()`` transactional
publication API on top of this primitive without bypassing it.

Ownership inventory
-------------------

Every object initialized in ``app._lifespan_runtime`` and
``create_app`` is classified below.  This table is the single
reviewable inventory; changes to ownership must update both this
table and ``_RUNTIME_OWNED_APP_STATE_ATTRS``.

**Process-owned** (never recreated for a generation):

- ``AppConfig`` -- immutable after startup; shared across generations.
- ``Database`` (primary + stats) -- single-connection serialization.
- All repositories (``AccountRepository``, ``RequestRepository``,
  ``AttemptRepository``, ``ReservationRepository``,
  ``UsageWindowRepository``, ``OperationalEventRepository``,
  ``AccountEventRepository``, ``PingRepository``,
  ``ProviderRepository``, ``UsageRollupRepository``,
  ``RoutingDecisionRepository``) -- thin repos over process-owned DB.
- ``MetricsWriteCoalescer`` -- flushed at shutdown; survives gen.
- ``RuntimeMetricsService`` -- reads manager diagnostics.
- ``DashboardTelemetry`` -- 30s in-memory cache.
- ``UpdateChecker`` -- 24h PyPI probe.

**Generation-owned** (rebuilt/closed on config change):

- ``AccountRegistry`` -- rebuilt on config change.
- ``Router`` -- rebuilt on config change.
- ``RequestCoordinator`` -- rebuilt; carries generation finalizer.
- ``ProviderClientPool`` -- closed on retirement.
- ``OutboundClientManager`` -- closed on retirement.
- ``DnsNetworkBackend`` -- closed on retirement.
- ``HealthManager`` -- rebuilt on config change.
- ``CostCalculator`` -- rebuilt on config change.
- ``CatalogService`` -- rebuilt on config change.
- ``ModelInfoService`` -- rebuilt on config change.
- ``StatsService`` -- DB-backed; lifecycle matches gen.
- ``AccountBackoffRepository`` -- DB-backed; lifecycle matches gen.
- ``TaskSupervisor`` -- closed on retirement.
- ``TranscoderPolicy`` / ``CompressionPolicy`` / ``CacheConfig``
  / ``CompressionTuningRegistry`` -- frozen config snapshots.
- ``DispatchOverheadRecorder`` / ``DispatchSpanRecorder``
  / ``StreamDiagnostics`` / ``FinalizationRetryQueue``
  / ``RoutingTraceGuard`` -- per-generation telemetry/guardrails.

**Closures that capture startup services** (reload hazard):

- ``_catalog_refresh_once`` captures ``catalog``, ``effective_model_info``.
- ``_retention_cleanup_once`` captures ``db``, ``config``, ``router``.
- ``_refresh_usage_windows_once`` captures ``router``.
- ``_stale_request_finalizer_once`` captures ``db``, ``router``, ``config``.
- ``_health_disabled_models_prune_once`` captures ``app`` (reads ``app.state``).
- ``_metrics_flush_once`` captures ``metrics_coalescer``.
- ``_automatic_backup_once`` captures ``config``, ``db``, paths.

All other callbacks capture only process-owned resources (``db``,
``config``, ``metrics_coalescer``).

Forward references
------------------

The typed reload-result types live in :mod:`eggpool.config_reload_policy`
(milestone A) and are imported lazily here to keep this module
lightweight.  Diagnostics never include secret material.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import (  # noqa: TC003 - used at runtime
    AsyncGenerator,
    AsyncIterator,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.catalog.pricing import CostCalculator
    from eggpool.catalog.service import CatalogService
    from eggpool.db.connection import Database
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig
    from eggpool.providers.client_pool import ProviderClientPool
    from eggpool.providers.dns_cache import DnsNetworkBackend
    from eggpool.providers.outbound import OutboundClientManager
    from eggpool.request.coordinator import RequestCoordinator
    from eggpool.routing.router import Router
    from eggpool.runtime_dispatch import (
        DispatchOverheadRecorder,
        DispatchSpanRecorder,
    )
    from eggpool.stats import StatsService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-owned runtime container
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProcessRuntime:
    """Process-owned dependency container for stable resources.

    Items here are constructed exactly once per process and are never
    replaced for a generation.  The runtime manager borrows these
    references when constructing a new generation but never owns their
    lifecycle.

    Fields:

    - ``db``: primary write/read SQLite connection.
    - ``stats_db``: read-only stats connection (may be ``db`` itself
      when ``database.worker_threads == 1`` or the DB is in-memory).
    - ``config_path``: resolved path the server is using for this
      process (used by milestone C's reload control-plane handler).
    - ``metrics_store``: placeholder for the buffered metrics coalescer;
      populated by milestone B wiring rather than by this module.
    """

    db: Database
    stats_db: Database
    config_path: str | None = None
    metrics_coalescer: Any = None  # noqa: ANN401
    process_supervisor: Any = None  # noqa: ANN401 — TaskSupervisor, avoids circular import
    task_spec_version: int = 0
    last_task_transition: dict[str, Any] | None = None
    dispatch_writer: Any = None  # noqa: ANN401 — DispatchPersistenceWriter
    routing_trace_writer: Any = None  # noqa: ANN401 — RoutingTraceWriter
    maintenance_state: Any = None  # noqa: ANN401 — MaintenanceState


# ---------------------------------------------------------------------------
# Generation-owned runtime snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeGeneration:
    """Immutable snapshot of one generation's configuration-derived services.

    Frozen so the active generation cannot mutate after publication.
    Mutable lifecycle accounting (lease count, retirement state) lives
    on the companion :class:`_GenerationSlot` rather than here.

    Fields
    ------

    - ``generation_id``: monotonic per-process integer.
    - ``config``: validated :class:`AppConfig` for this generation.
    - ``config_digest``: SHA-256 of the validated bytes that produced
      ``config`` (mirrors ``ConfigValidationResult.content_digest``).
    - ``registry``/``catalog``/``router``/``coordinator``: the request-path
      services that must be retired together.
    - ``client_pool``/``outbound_manager``/``dns_backend``: network
      transport owned by this generation.
    - ``health_manager``/``cost_calculator``: generation-owned state
      containers.
    - ``transcoder_policy``/``compression_policy``/
      ``cache_config``/``compression_tuning_registry``: frozen
      configuration snapshots consumed during dispatch.
    - ``dispatch_overhead_recorder``/``dispatch_span_recorder``:
      per-generation telemetry recorders (the supervisor and runtime
      metrics service read these in process-owned wrappers).
    - ``account_backoff_repo``/``stats_service``: DB-backed services
      whose lifecycle matches the generation.
    - ``supervisor``: generation-owned task supervisor driving periodic
      ticks (catalog refresh, model info refresh, retention cleanup,
      metrics flush, etc.).
    - ``created_at_monotonic`` / ``created_at_epoch``: timestamps
      exposed via diagnostics.
    """

    generation_id: int
    config: AppConfig
    config_digest: str
    registry: AccountRegistry
    catalog: CatalogService
    router: Router
    coordinator: RequestCoordinator
    client_pool: ProviderClientPool
    outbound_manager: OutboundClientManager
    dns_backend: DnsNetworkBackend | None
    health_manager: HealthManager
    cost_calculator: CostCalculator
    transcoder_policy: Any
    compression_policy: Any
    cache_config: Any
    compression_tuning_registry: Any
    dispatch_overhead_recorder: DispatchOverheadRecorder
    dispatch_span_recorder: DispatchSpanRecorder
    account_backoff_repo: Any
    stats_service: StatsService
    supervisor: Any
    finalization_retry_queue: Any
    routing_trace_guard: Any
    routing_trace_writer: Any
    created_at_monotonic: float
    created_at_epoch: float


# ---------------------------------------------------------------------------
# Generation slot (mutable lifecycle accounting)
# ---------------------------------------------------------------------------


@dataclass
class _GenerationSlot:
    """Mutable companion to a published :class:`RuntimeGeneration`.

    Holds the per-generation accounting state that the manager needs
    to track leases, retirement, and shutdown.  Kept private to the
    manager so external callers cannot mutate the slot directly.

    Lifecycle:

    1. Built by :meth:`RuntimeManager.install_initial` once a generation
       is ready to publish.
    2. Serves incoming :meth:`RuntimeManager.acquire` calls until
       :attr:`accepting_leases` flips to ``False`` (typically because
       a new generation was published).
    3. Waits for active leases to drain (or a hard deadline elapses
       on process shutdown) and then performs the ordered teardown.
    """

    generation: RuntimeGeneration
    active_leases: int = 0
    accepting_leases: bool = True
    retirement_started: bool = False
    retirement_complete: asyncio.Event = field(default_factory=asyncio.Event)
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_close_error: str | None = None


# ---------------------------------------------------------------------------
# Generation lease
# ---------------------------------------------------------------------------


@dataclass
class GenerationLease:
    """One acquired generation handle.

    Returned by :meth:`RuntimeManager.acquire` and consumed by request
    handlers, streaming responses, and post-finalization tasks.  Each
    release decrements the slot's ``active_leases`` counter exactly
    once; subsequent calls are no-ops so a streaming wrapper that
    also runs a finalization task cannot double-release by accident.

    The lease carries the immutable ``generation_id`` for diagnostics;
    a request handler that wants to attach diagnostics can log the id
    without exposing the underlying services.
    """

    generation_id: int
    slot: _GenerationSlot
    release_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    released: bool = False

    @property
    def runtime(self) -> RuntimeGeneration:
        """Return the immutable generation snapshot this lease holds."""
        return self.slot.generation

    async def release(self) -> None:
        """Decrement the slot's active lease count exactly once.

        Idempotent: a second call from a streaming wrapper or a
        post-finalization task is silently ignored.
        """
        async with self.release_lock:
            if self.released:
                return
            self.released = True
            self.slot.active_leases -= 1
            if self.slot.active_leases <= 0:
                self.slot.active_leases = 0
            logger.debug(
                "Generation %d lease released (active=%d)",
                self.generation_id,
                self.slot.active_leases,
            )

    async def __aenter__(self) -> RuntimeGeneration:
        return self.runtime

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()


# ---------------------------------------------------------------------------
# Generation diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationDiagnostics:
    """Read-only diagnostic view of one generation slot.

    Returned by :meth:`RuntimeManager.active_diagnostics` and
    :meth:`RuntimeManager.diagnostics`.  Never exposes raw config
    objects or secrets; only counts, IDs, digests, and timestamps.
    """

    generation_id: int
    config_digest_prefix: str
    created_at_monotonic: float
    created_at_epoch: float
    age_seconds: float
    active_leases: int
    accepting_leases: bool
    retirement_started: bool
    retirement_complete: bool
    last_close_error: str | None


@dataclass(frozen=True)
class RuntimeDiagnostics:
    """Process-wide snapshot of the runtime manager.

    Returned by :meth:`RuntimeManager.diagnostics`.  Never includes
    the underlying ``AppConfig`` or any service references -- only the
    public counts and IDs operators need to confirm live reload
    progress.

    Fields
    ------

    - ``active``: diagnostics for the currently active generation slot
      (``None`` before ``install_initial`` or after ``shutdown``).
    - ``retiring``: diagnostics for every generation currently
      retiring (zero or more).
    - ``shutdown_in_progress``: ``True`` once :meth:`shutdown` has been
      invoked.
    - ``next_generation_id``: next ID that will be assigned when a new
      generation is published.
    """

    active: GenerationDiagnostics | None
    retiring: tuple[GenerationDiagnostics, ...]
    shutdown_in_progress: bool
    next_generation_id: int


# ---------------------------------------------------------------------------
# Runtime manager
# ---------------------------------------------------------------------------


GENERATION_LEASE_TIMEOUT_S: Final[float] = 30.0
"""Maximum time a request can wait for a generation lease before failing.

Used by :meth:`RuntimeManager.acquire` when no active slot is currently
accepting leases (race between publication and the first acquire).
Generous by default because the publication path is short; milestone C
will revisit once transactional reloads need to coordinate with client
disconnects.
"""


class RuntimeManagerShutdownError(RuntimeError):
    """Raised when callers attempt to acquire or install after shutdown."""


class RuntimeManagerLeaseExhaustedError(RuntimeError):
    """Raised when no generation slot can accept new leases.

    Typically raised when :meth:`acquire` is invoked after
    :meth:`shutdown` or when a publication race causes a request to
    miss the brief window where a new slot is being prepared.
    """


class RuntimeManager:
    """Process-wide owner of generation slots, leases, and retirement.

    The manager owns a single :class:`_GenerationSlot` at a time (the
    "active" slot) plus zero or more "retiring" slots that still hold
    active leases.  New requests acquire the active slot; once a new
    generation is published, the previous slot transitions to retiring
    and drains its leases before closing its owned resources.

    The manager's lock protects slot publication and the active-slot
    pointer.  Lease acquisition is lock-free once a slot is accepting;
    this keeps the request hot path off the manager's lock so a
    slow background task cannot serialize unrelated request paths.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: _GenerationSlot | None = None
        self._retiring: list[_GenerationSlot] = []
        self._next_generation_id = 0
        self._shutdown_in_progress = False
        self._acquire_id = 0  # monotonic tie-breaker for lease diagnostics
        self._synthetic_generation_digest: str = ""

    # -- publication --------------------------------------------------------

    async def install_initial(self, generation: RuntimeGeneration) -> None:
        """Publish the first generation and mark it accepting.

        Called once from the lifespan during normal startup.  Idempotent
        on repeat invocations: a second call raises
        :class:`RuntimeManagerShutdownError` because the manager does
        not allow replacing the active slot outside the publication
        path milestone C will introduce.
        """
        async with self._lock:
            if self._shutdown_in_progress:
                raise RuntimeManagerShutdownError(
                    "Cannot install initial generation after shutdown"
                )
            if self._active is not None:
                raise RuntimeError(
                    "RuntimeManager.install_initial called twice; "
                    "use the publication API for swaps"
                )
            slot = _GenerationSlot(generation=generation)
            self._active = slot
            self._next_generation_id = max(
                self._next_generation_id,
                generation.generation_id + 1,
            )
            logger.info(
                "Runtime generation %d published (initial install; digest=%s)",
                generation.generation_id,
                _digest_prefix(generation.config_digest),
            )

    async def install_candidate(
        self,
        generation: RuntimeGeneration,
        *,
        drain_timeout_s: float = 300.0,
        expected_active_generation_id: int | None = None,
    ) -> None:
        """Publish a candidate generation, retiring the current active slot.

        Called by the reload manager after candidate preparation and
        persistence reconciliation.  The publication is atomic: the new
        slot begins accepting leases immediately and the old slot is
        marked for retirement under the same lock acquisition.

        Idempotent: if the generation has the same ``generation_id``
        already active, this is a no-op.  After shutdown begins the
        call raises :class:`RuntimeManagerShutdownError`.
        """
        async with self._lock:
            if self._shutdown_in_progress:
                raise RuntimeManagerShutdownError(
                    "Cannot install candidate generation after shutdown"
                )
            old_slot = self._active
            if (
                expected_active_generation_id is not None
                and old_slot is not None
                and old_slot.generation.generation_id != expected_active_generation_id
            ):
                raise RuntimeError(
                    "Active generation changed during candidate preparation; "
                    f"expected {expected_active_generation_id}, "
                    f"found {old_slot.generation.generation_id}"
                )
            new_slot = _GenerationSlot(generation=generation)
            self._active = new_slot
            self._next_generation_id = max(
                self._next_generation_id,
                generation.generation_id + 1,
            )
            logger.info(
                "Runtime generation %d published (candidate swap; digest=%s)",
                generation.generation_id,
                _digest_prefix(generation.config_digest),
            )
        if old_slot is not None:
            await self.begin_retirement(old_slot, drain_timeout_s=drain_timeout_s)

    # -- lease acquisition -------------------------------------------------

    async def acquire(self) -> GenerationLease:
        """Acquire one generation lease from the currently active slot.

        Race-safe with publication: if the active slot has just stopped
        accepting leases, the manager briefly waits for a new slot
        (bounded by :data:`GENERATION_LEASE_TIMEOUT_S`) and then retries
        against the new active slot.  After the timeout elapses or
        shutdown begins, the call raises
        :class:`RuntimeManagerLeaseExhaustedError` so the request handler
        can render an explicit 503 rather than blocking the worker.
        """
        deadline = time.monotonic() + GENERATION_LEASE_TIMEOUT_S
        while True:
            slot = self._active
            if slot is not None and slot.accepting_leases:
                # Hot path: no lock acquisition.  We briefly snapshot
                # the active count under the slot's own lock so a
                # concurrent retirement cannot drop the count to
                # negative while we increment it.
                async with slot.close_lock:
                    if not slot.accepting_leases:
                        # Lost the race; restart the wait loop.
                        slot = None
                    else:
                        slot.active_leases += 1
                        self._acquire_id += 1
                if slot is not None:
                    return GenerationLease(
                        generation_id=slot.generation.generation_id,
                        slot=slot,
                    )
            if self._shutdown_in_progress:
                raise RuntimeManagerLeaseExhaustedError(
                    "RuntimeManager is shutting down; no generation slot "
                    "is accepting leases"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeManagerLeaseExhaustedError(
                    "No accepting generation slot within "
                    f"{GENERATION_LEASE_TIMEOUT_S}s of acquire()"
                )
            await asyncio.sleep(min(0.01, remaining))

    def active_snapshot(self) -> RuntimeGeneration:
        """Return the current active generation without acquiring a lease.

        Intended for diagnostics endpoints, readyz probes, and tests
        that only need a read-only reference.  Raises
        :class:`RuntimeManagerShutdownError` if no active generation is
        installed yet (or after shutdown).
        """
        slot = self._active
        if slot is None:
            raise RuntimeManagerShutdownError("No active runtime generation installed")
        return slot.generation

    def has_active_generation(self) -> bool:
        """Return ``True`` once :meth:`install_initial` has succeeded."""
        return self._active is not None

    # -- retirement ---------------------------------------------------------

    async def begin_retirement(
        self, slot: _GenerationSlot, *, drain_timeout_s: float = 5.0
    ) -> None:
        """Drive the deterministic teardown for one slot.

        Marks the slot as not accepting new leases, waits for active
        leases to drain (with a bounded wait so a runaway stream cannot
        keep resources alive forever), and then closes each owned
        resource exactly once.  Process shutdown uses a tight bound;
        future live reload (milestone C) will pass a generous bound
        because it cannot afford to interrupt in-flight streams.

        Idempotent: a slot whose retirement has already started is left
        alone.  Errors during resource close are captured in
        ``slot.last_close_error`` and logged but do not abort the
        teardown of remaining resources.
        """
        async with slot.close_lock:
            if slot.retirement_started:
                return
            slot.retirement_started = True
            slot.accepting_leases = False
        logger.info(
            "Runtime generation %d retirement starting (active_leases=%d)",
            slot.generation.generation_id,
            slot.active_leases,
        )
        # Move the slot into the retiring list under the manager lock
        # so a subsequent diagnostics snapshot sees consistent state.
        async with self._lock:
            if self._active is slot:
                self._active = None
            if slot not in self._retiring:
                self._retiring.append(slot)
        await self._drain_and_close(slot, drain_timeout_s=drain_timeout_s)

    async def _drain_and_close(
        self, slot: _GenerationSlot, *, drain_timeout_s: float = 5.0
    ) -> None:
        """Wait for active leases to reach zero, then close owned resources."""
        deadline = time.monotonic() + drain_timeout_s
        while slot.active_leases > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Runtime generation %d retirement drain timed out with "
                    "%d active leases; forcing close",
                    slot.generation.generation_id,
                    slot.active_leases,
                )
                break
            await asyncio.sleep(min(0.05, remaining))
        # Close generation-owned resources in the documented order.
        await self._close_slot_resources(slot)
        slot.retirement_complete.set()
        async with self._lock:
            with contextlib.suppress(ValueError):
                self._retiring.remove(slot)
        logger.info(
            "Runtime generation %d retirement complete",
            slot.generation.generation_id,
        )

    async def _close_slot_resources(self, slot: _GenerationSlot) -> None:
        """Close generation-owned resources in the documented order.

        The order matches workstream B8 of the milestone plan:

        1. stop scheduling new periodic ticks (supervisor.stop_all);
        2. close the provider client pool;
        3. close the outbound client manager;
        4. close any additional generation-owned services.

        Each step is idempotent and tolerates partially constructed
        state, so a builder that raised halfway through construction
        can still call :meth:`begin_retirement` on its slot without
        crashing.
        """
        generation = slot.generation
        # 1. Stop background-task scheduling first so no new ticks fire
        #    while we drain in-flight tasks.
        supervisor = generation.supervisor
        if supervisor is not None:
            try:
                await supervisor.stop_all()
            except Exception as exc:  # noqa: BLE001 -- close path must not raise
                slot.last_close_error = f"supervisor.stop_all: {exc!r}"
                logger.exception(
                    "Runtime generation %d supervisor.stop_all failed",
                    generation.generation_id,
                )
        # 2. Close the provider client pool.  This must happen before
        #    closing the outbound manager because outbound may share
        #    DNS or backend state with the pool.
        client_pool = cast("ProviderClientPool | None", generation.client_pool)
        if client_pool is not None:
            try:
                await _safe_aclose(client_pool)
            except Exception as exc:  # noqa: BLE001
                slot.last_close_error = f"client_pool.close: {exc!r}"
                logger.exception(
                    "Runtime generation %d client_pool.close failed",
                    generation.generation_id,
                )
        # 3. Close the outbound client manager / DNS backend.
        outbound_manager = cast(
            "OutboundClientManager | None", generation.outbound_manager
        )
        if outbound_manager is not None:
            try:
                await _safe_aclose(outbound_manager)
            except Exception as exc:  # noqa: BLE001
                slot.last_close_error = f"outbound_manager.aclose: {exc!r}"
                logger.exception(
                    "Runtime generation %d outbound_manager.aclose failed",
                    generation.generation_id,
                )
        dns_backend = cast("Any | None", generation.dns_backend)
        if dns_backend is not None:
            try:
                await _safe_aclose(dns_backend)
            except Exception as exc:  # noqa: BLE001
                slot.last_close_error = f"dns_backend.aclose: {exc!r}"
                logger.exception(
                    "Runtime generation %d dns_backend.aclose failed",
                    generation.generation_id,
                )

    # -- shutdown -----------------------------------------------------------

    async def shutdown(self) -> None:
        """Drain and retire the active generation on process shutdown.

        Idempotent.  After this returns, :meth:`acquire` raises
        :class:`RuntimeManagerLeaseExhaustedError` so any late
        request handlers fail closed rather than hang.
        """
        async with self._lock:
            if self._shutdown_in_progress:
                return
            self._shutdown_in_progress = True
            active = self._active
        if active is not None:
            await self.begin_retirement(active, drain_timeout_s=5.0)
            return
        # No active generation (e.g. startup never completed): still
        # wait for any already-retiring slots to finish.
        async with self._lock:
            retiring = list(self._retiring)
        for slot in retiring:
            await slot.retirement_complete.wait()
            async with slot.close_lock:
                await self._close_slot_resources(slot)

    # -- diagnostics --------------------------------------------------------

    def diagnostics(self) -> RuntimeDiagnostics:
        """Return a process-wide read-only diagnostic snapshot.

        Safe to call from any thread or task; no locks are taken
        because the only mutable state we read (active_leases) is a
        plain ``int`` and an out-of-date value at most one-off.
        """
        now = time.monotonic()
        active = self._active
        active_diag: GenerationDiagnostics | None = None
        if active is not None:
            active_diag = _slot_diagnostics(active, now)
        retiring = tuple(_slot_diagnostics(slot, now) for slot in self._retiring)
        return RuntimeDiagnostics(
            active=active_diag,
            retiring=retiring,
            shutdown_in_progress=self._shutdown_in_progress,
            next_generation_id=self._next_generation_id,
        )

    # -- internals ----------------------------------------------------------

    @property
    def next_generation_id(self) -> int:
        """Return the next generation ID the manager will assign."""
        return self._next_generation_id

    def reserve_next_generation_id(self) -> int:
        """Return and bump the next generation ID.

        Used by the builder when preparing a candidate generation so
        the reservation is visible to diagnostics before publication.
        """
        current = self._next_generation_id
        self._next_generation_id = current + 1
        return current

    def attach_supervisor_to_active(
        self,
        supervisor: Any,
    ) -> RuntimeGeneration | None:
        """Replace the supervisor field on the active generation.

        Used by initial startup to wire the :class:`TaskSupervisor` into
        the already-installed generation: the supervisor is constructed
        after ``install_initial`` so the lifespan can publish the
        generation first, then register tasks through the unified
        helper.  Returns the new generation, or ``None`` when no
        active generation is installed.
        """
        slot = self._active
        if slot is None:
            return None
        new_generation = replace(slot.generation, supervisor=supervisor)
        slot.generation = new_generation
        return new_generation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _digest_prefix(digest: str) -> str:
    """Return a short, non-secret prefix of a digest for diagnostics."""
    return digest[:12] if digest else "<empty>"


def _slot_diagnostics(
    slot: _GenerationSlot, now_monotonic: float
) -> GenerationDiagnostics:
    """Build a :class:`GenerationDiagnostics` view for one slot."""
    generation = slot.generation
    created_at_monotonic = generation.created_at_monotonic
    return GenerationDiagnostics(
        generation_id=generation.generation_id,
        config_digest_prefix=_digest_prefix(generation.config_digest),
        created_at_monotonic=created_at_monotonic,
        created_at_epoch=generation.created_at_epoch,
        age_seconds=max(0.0, now_monotonic - created_at_monotonic),
        active_leases=slot.active_leases,
        accepting_leases=slot.accepting_leases,
        retirement_started=slot.retirement_started,
        retirement_complete=slot.retirement_complete.is_set(),
        last_close_error=slot.last_close_error,
    )


async def _safe_aclose(obj: object) -> None:
    """Call ``aclose`` on objects that may or may not implement it.

    Tolerates partially constructed objects (e.g. a builder that raised
    before the close hook was wired in).
    """
    aclose = getattr(obj, "aclose", None)
    if aclose is None:
        return
    result = aclose()
    if asyncio.iscoroutine(result):
        await result
    return


# ---------------------------------------------------------------------------
# Generation builder
# ---------------------------------------------------------------------------


@dataclass
class GenerationBuildResult:
    """Outcome of one :meth:`RuntimeGenerationBuilder.build` invocation.

    Carries the constructed :class:`RuntimeGeneration` plus the
    process-owned resources it depended on, so the manager can pin
    diagnostics without re-deriving the build environment.  Tests that
    exercise partial builder failures should assert the
    ``cleanup_complete`` invariant alongside the result.
    """

    generation: RuntimeGeneration
    process: ProcessRuntime
    cleanup_complete: bool = True


# ---------------------------------------------------------------------------
# Generation builder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Default generation builder
# ---------------------------------------------------------------------------


class RuntimeGenerationBuilder:
    """Constructs a :class:`RuntimeGeneration` from process-owned resources.

    The builder owns the single construction site that both startup
    and future reload (milestone C) will use.  It wraps the services
    already constructed by ``_lifespan_runtime`` into an immutable
    generation snapshot; milestone C will extend this to construct
    services from a candidate config.

    Tests inject a thinner subclass to avoid pulling in the full
    service graph; production code uses the default ``build_initial``
    path.
    """

    async def build_initial(
        self,
        config: AppConfig,
        process: ProcessRuntime,
        *,
        generation_id: int = 0,
        config_digest: str = "",
        **services: Any,
    ) -> GenerationBuildResult:
        """Wrap pre-built services into a :class:`RuntimeGeneration`.

        The ``**services`` keyword arguments are the generation-owned
        services already constructed by the lifespan.  The builder
        validates that all required keys are present, wraps them in an
        immutable ``RuntimeGeneration``, and returns the result.

        When a required service is missing the builder raises
        ``RuntimeError`` so the caller can invoke
        :meth:`cleanup_partial`.
        """
        required = (
            "registry",
            "catalog",
            "router",
            "coordinator",
            "client_pool",
            "outbound_manager",
            "health_manager",
            "cost_calculator",
            "transcoder_policy",
            "compression_policy",
            "cache_config",
            "compression_tuning_registry",
            "dispatch_overhead_recorder",
            "dispatch_span_recorder",
            "account_backoff_repo",
            "stats_service",
            "supervisor",
        )
        missing = [k for k in required if k not in services]
        if missing:
            raise RuntimeError(
                f"build_initial missing required services: {', '.join(missing)}"
            )

        now_mono = time.monotonic()
        now_epoch = time.time()
        generation = RuntimeGeneration(
            generation_id=generation_id,
            config=config,
            config_digest=config_digest,
            registry=services["registry"],
            catalog=services["catalog"],
            router=services["router"],
            coordinator=services["coordinator"],
            client_pool=services["client_pool"],
            outbound_manager=services["outbound_manager"],
            dns_backend=services.get("dns_backend"),
            health_manager=services["health_manager"],
            cost_calculator=services["cost_calculator"],
            transcoder_policy=services["transcoder_policy"],
            compression_policy=services["compression_policy"],
            cache_config=services["cache_config"],
            compression_tuning_registry=services["compression_tuning_registry"],
            dispatch_overhead_recorder=services["dispatch_overhead_recorder"],
            dispatch_span_recorder=services["dispatch_span_recorder"],
            account_backoff_repo=services["account_backoff_repo"],
            stats_service=services["stats_service"],
            supervisor=services["supervisor"],
            finalization_retry_queue=services.get("finalization_retry_queue"),
            routing_trace_guard=services.get("routing_trace_guard"),
            routing_trace_writer=services.get("routing_trace_writer"),
            created_at_monotonic=services.get("created_at_monotonic", now_mono),
            created_at_epoch=services.get("created_at_epoch", now_epoch),
        )
        return GenerationBuildResult(
            generation=generation,
            process=process,
        )

    async def cleanup_partial(self, process: ProcessRuntime) -> None:
        """Best-effort cleanup after a partial build failure.

        Delegates to :func:`eggpool.app.cleanup_partial_generation`
        which closes any generation-owned resources that were
        constructed before the failure.  Tolerates partially
        constructed state and repeated calls.
        """
        from eggpool.app import cleanup_partial_generation  # noqa: PLC0415

        await cleanup_partial_generation(process)


# ---------------------------------------------------------------------------
# Generation build helper (used by app.py)
# ---------------------------------------------------------------------------


async def build_generation_from_config(
    config: AppConfig,
    process: ProcessRuntime,
    *,
    generation_id: int,
    config_digest: str,
    builder: RuntimeGenerationBuilder,
) -> GenerationBuildResult:
    """Convenience wrapper used by the lifespan and milestone C reload path.

    Milestone B only calls this once per process (initial install);
    milestone C will reuse it from the reload transaction path so
    startup and reload exercise the same construction site.
    """
    return await builder.build_initial(
        config,
        process,
        generation_id=generation_id,
        config_digest=config_digest,
    )


@asynccontextmanager
async def leased_runtime(
    manager: RuntimeManager,
) -> AsyncGenerator[RuntimeGeneration, None]:
    """Acquire a generation lease and release it on context exit.

    Helper for call sites that do not need to hold the lease object
    themselves; the streaming response wrapper uses the explicit
    :class:`GenerationLease` because the lease lifetime extends past
    the request handler's ``return``.
    """
    lease = await manager.acquire()
    try:
        yield lease.runtime
    finally:
        await lease.release()


def attach_runtime_manager(app: Any, manager: RuntimeManager) -> None:
    """Store the manager on ``app.state`` so request handlers can find it.

    The manager is the single source of truth for active and retiring
    generations.  Direct reads of generation-owned fields on
    ``app.state`` are retained during the milestone-B transition but
    will be removed in milestone C; see the milestone plan for the
    removal roadmap.
    """
    app.state.runtime_manager = manager


def is_runtime_owned_attr(name: str) -> bool:
    """Return ``True`` when ``name`` identifies a generation-owned attribute.

    Centralised so audit tests, static greps, and future removal work
    all share the same allow-list.  The names returned here MUST also
    be retired together; tests in ``tests/unit/test_runtime_manager.py``
    pin the allow-list to keep dashboard and request-path consumers
    honest.
    """
    return name in _RUNTIME_OWNED_APP_STATE_ATTRS


_RUNTIME_OWNED_APP_STATE_ATTRS: frozenset[str] = frozenset(
    {
        "registry",
        "catalog",
        "health_manager",
        "account_backoff_repo",
        "model_info",
        "cost_calculator",
        "router",
        "stats",
        "metrics_coalescer",
        "dispatch_overhead_recorder",
        "dispatch_span_recorder",
        "coordinator",
        "finalization_retry_queue",
        "routing_trace_guard",
        "supervisor",
        "task_monitor",
        "dashboard_telemetry",
        "stream_diagnostics",
        "runtime_metrics",
        "transcoder_policy",
        "compression_policy",
        "cache_config",
        "compression_tuning_registry",
        "client_pool",
        "outbound_manager",
        "dns_backend",
        "httpx_client",
        "dispatch_writer",
    }
)


def mark_generation_owned(*names: str) -> tuple[str, ...]:
    """Register additional generation-owned ``app.state`` names.

    Used by tests that introduce new generation-owned state during the
    milestone-B window so the audit allow-list stays in sync.  Returns
    the registered names for convenience.

    The allow-list is mutable because tests extend it; this function
    is the only place that performs the mutation so the audit trail
    is reviewable.
    """
    global _RUNTIME_OWNED_APP_STATE_ATTRS  # noqa: PLW0603
    updated = _RUNTIME_OWNED_APP_STATE_ATTRS | frozenset(names)
    _RUNTIME_OWNED_APP_STATE_ATTRS = updated  # pyright: ignore[reportConstantRedefinition]
    return names


# ---------------------------------------------------------------------------
# Streaming lease wrapper
# ---------------------------------------------------------------------------


async def wrap_stream_with_lease(
    iterator: AsyncIterator[Any],
    lease: GenerationLease,
) -> AsyncGenerator[Any, None]:
    """Wrap a streaming iterator so the lease outlives the handler.

    A request handler returns a :class:`StreamingResponse` whose body
    iterator runs after the handler returns and the FastAPI dependency
    graph has unwound.  The lease cannot be released by the handler's
    ``async with`` block because the body iterator is still consuming
    the generation-owned services when that block exits.

    This wrapper yields each chunk from the underlying iterator and
    releases the lease exactly once in its ``finally`` block, so
    client disconnects, transcoder exceptions, and finalization
    completion all release the lease.
    """
    try:
        async for chunk in iterator:
            yield chunk
    finally:
        await lease.release()


__all__ = [
    "GenerationBuildResult",
    "GenerationDiagnostics",
    "GenerationLease",
    "ProcessRuntime",
    "RuntimeDiagnostics",
    "RuntimeGeneration",
    "RuntimeGenerationBuilder",
    "RuntimeManager",
    "RuntimeManagerLeaseExhaustedError",
    "RuntimeManagerShutdownError",
    "attach_runtime_manager",
    "build_generation_from_config",
    "is_runtime_owned_attr",
    "leased_runtime",
    "wrap_stream_with_lease",
]
