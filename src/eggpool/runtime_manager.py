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
- :class:`RuntimeGenerationBuilder` -- wraps pre-built services into
  a :class:`RuntimeGeneration`.  Used by the factory (Phase 5).
- :class:`RuntimeGenerationCandidate` -- a typed container that makes
  ownership of reload-created resources explicit from the moment each
  resource is constructed (Phase 4).

The shared runtime-generation factory (:mod:`eggpool.generation_factory`)
eliminates behavior drift between startup and reload by constructing
all generation-owned services through a single authoritative path.

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
- Any failure before successful publication must close the complete
  candidate graph (Phase 4 abort contract).
- Successful publication must transfer ownership exactly once to the
  runtime manager (Phase 4 transfer contract).

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
import enum
import logging
import time
from collections.abc import (  # noqa: TC003 - used at runtime
    AsyncGenerator,
    AsyncIterator,
    Callable,
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
# Candidate resource ownership (Phase 4)
# ---------------------------------------------------------------------------


class CandidateOwnershipState(enum.Enum):
    """Lifecycle state of a :class:`RuntimeGenerationCandidate`.

    Transitions::

        building → prepared → transferred
        building → aborted
        prepared → aborted

    ``building`` is the initial state while resources are being
    constructed and registered.  ``prepared`` means all resources are
    registered and the candidate is ready for publication.
    ``transferred`` means ownership has been handed to the runtime
    manager.  ``aborted`` means the candidate closed all registered
    resources.
    """

    BUILDING = "building"
    PREPARED = "prepared"
    TRANSFERRED = "transferred"
    ABORTED = "aborted"


@dataclass(frozen=True)
class CleanupDiagnostics:
    """Structured diagnostics captured during candidate abort.

    Emitted by :meth:`RuntimeGenerationCandidate.abort` so operators
    and tests can verify cleanup completed without inspecting logs.
    Never includes API keys, provider tokens, full URLs, or config
    secrets.
    """

    generation_id: int
    ownership_state: str
    resource_types_registered: tuple[str, ...]
    resource_types_closed: tuple[str, ...]
    close_duration_s: float
    close_errors: tuple[str, ...]
    timed_out: bool
    primary_failure: str
    primary_failure_stage: str = "unknown"
    close_errors_by_type: tuple[tuple[str, str], ...] = ()
    ownership_state_at_failure: str = "unknown"


@dataclass
class _RegisteredResource:
    """A closeable resource tracked by the candidate container.

    ``name`` is a human-readable label for diagnostics (e.g.
    ``"client_pool"``).  ``close_callback`` is an async callable that
    closes the resource; called once during abort in reverse
    registration order.
    """

    name: str
    close_callback: Callable[[], Any]


class RuntimeGenerationCandidate:
    """Typed container for a candidate generation under construction.

    Makes ownership of reload-created resources explicit from the
    moment each resource is constructed.  Any failure before
    successful publication must close the complete candidate graph.

    Usage::

        candidate = RuntimeGenerationCandidate(generation_id=42)
        candidate.register_resource("client_pool", pool.close)
        candidate.register_resource("outbound_manager", manager.aclose)
        # ... build the generation ...
        candidate.mark_prepared()
        # ... publish ...
        candidate.transfer_to_runtime_manager()

    If any step raises before ``transfer_to_runtime_manager()``::

        await candidate.abort(cause=exc)

    Abort closes all registered resources in reverse registration
    order, collects close errors without masking the primary error,
    and emits :class:`CleanupDiagnostics`.
    """

    def __init__(self, generation_id: int) -> None:
        self._generation_id = generation_id
        self._state = CandidateOwnershipState.BUILDING
        self._resources: list[_RegisteredResource] = []
        self._close_count = 0
        self._abort_lock = asyncio.Lock()
        self._diagnostics: CleanupDiagnostics | None = None
        #: Stored after build_initial completes; set by the reload builder.
        self._built_generation: RuntimeGeneration | None = None
        #: Stored after build_initial completes; set by the reload builder.
        self._process_ref: ProcessRuntime | None = None
        #: Stored after build_initial completes; set by the reload builder.
        self._diff_ref: Any = None

    @property
    def generation_id(self) -> int:
        return self._generation_id

    @property
    def ownership_state(self) -> CandidateOwnershipState:
        return self._state

    @property
    def diagnostics(self) -> CleanupDiagnostics | None:
        """Return cleanup diagnostics after abort, or ``None``."""
        return self._diagnostics

    def register_resource(
        self,
        name: str,
        close_callback: Callable[[], Any],
    ) -> None:
        """Register a closeable resource immediately after construction.

        Must be called before the next await that could fail.  The
        callback may be sync or async; the candidate will handle both.
        """
        if self._state is not CandidateOwnershipState.BUILDING:
            raise RuntimeError(f"Cannot register resource in state {self._state.value}")
        self._resources.append(
            _RegisteredResource(name=name, close_callback=close_callback)
        )

    def mark_prepared(self) -> None:
        """Mark the candidate as ready for publication."""
        if self._state is not CandidateOwnershipState.BUILDING:
            raise RuntimeError(f"Cannot mark prepared in state {self._state.value}")
        self._state = CandidateOwnershipState.PREPARED

    def transfer_to_runtime_manager(self) -> None:
        """Detach candidate cleanup after successful publication.

        Must be called exactly once after the runtime manager has
        accepted the candidate generation.  After transfer, calling
        :meth:`abort` is a no-op.
        """
        if self._state is not CandidateOwnershipState.PREPARED:
            raise RuntimeError(f"Cannot transfer in state {self._state.value}")
        self._state = CandidateOwnershipState.TRANSFERRED
        self._resources.clear()

    async def abort(
        self,
        cause: BaseException,
        *,
        failure_stage: str = "unknown",
    ) -> CleanupDiagnostics:
        """Close all registered resources in reverse registration order.

        Idempotent: a second call returns the same diagnostics.
        Collects close errors without masking the primary error.
        Leaves process-owned resources untouched.

        Args:
            cause: The primary exception that triggered the abort.
            failure_stage: The reload stage at which the failure
                occurred (e.g. ``"build"``, ``"reconcile"``,
                ``"commit"``, ``"unknown"``).
        """
        async with self._abort_lock:
            if self._state is CandidateOwnershipState.ABORTED:
                return self._diagnostics or CleanupDiagnostics(
                    generation_id=self._generation_id,
                    ownership_state=self._state.value,
                    resource_types_registered=(),
                    resource_types_closed=(),
                    close_duration_s=0.0,
                    close_errors=(),
                    timed_out=False,
                    primary_failure=str(cause),
                    primary_failure_stage=failure_stage,
                    ownership_state_at_failure=self._state.value,
                )

            ownership_state_at_failure = self._state.value
            registered_names = tuple(r.name for r in self._resources)
            closed_names: list[str] = []
            close_errors: list[str] = []
            close_errors_by_type: list[tuple[str, str]] = []
            close_start = time.monotonic()
            timed_out = False

            # Close in reverse registration order.
            for resource in reversed(self._resources):
                try:
                    result = resource.close_callback()
                    if asyncio.iscoroutine(result):
                        try:
                            await asyncio.wait_for(result, timeout=5.0)
                        except TimeoutError:
                            timed_out = True
                            close_errors.append(
                                f"{resource.name}: close timed out after 5s"
                            )
                            close_errors_by_type.append((resource.name, "TimeoutError"))
                            continue
                except Exception as exc:  # noqa: BLE001 -- close path must not raise
                    error_type = type(exc).__name__
                    close_errors.append(f"{resource.name}: {exc!r}")
                    close_errors_by_type.append((resource.name, error_type))
                    logger.warning(
                        "Candidate %d resource %s close failed: %r",
                        self._generation_id,
                        resource.name,
                        exc,
                    )
                else:
                    closed_names.append(resource.name)

            close_duration = time.monotonic() - close_start
            self._resources.clear()
            self._state = CandidateOwnershipState.ABORTED

            self._diagnostics = CleanupDiagnostics(
                generation_id=self._generation_id,
                ownership_state=self._state.value,
                resource_types_registered=registered_names,
                resource_types_closed=tuple(closed_names),
                close_duration_s=close_duration,
                close_errors=tuple(close_errors),
                timed_out=timed_out,
                primary_failure=str(cause),
                primary_failure_stage=failure_stage,
                close_errors_by_type=tuple(close_errors_by_type),
                ownership_state_at_failure=ownership_state_at_failure,
            )

            if close_errors:
                logger.warning(
                    "Candidate %d abort: %d/%d resources closed, "
                    "%d close error(s), duration=%.3fs",
                    self._generation_id,
                    len(closed_names),
                    len(registered_names),
                    len(close_errors),
                    close_duration,
                )
            else:
                logger.info(
                    "Candidate %d abort: %d resources closed in %.3fs",
                    self._generation_id,
                    len(closed_names),
                    close_duration,
                )

            return self._diagnostics


# ---------------------------------------------------------------------------
# Slot lifecycle state (Phase 3)
# ---------------------------------------------------------------------------


class SlotState(enum.Enum):
    """Explicit lifecycle state for a generation slot.

    Transitions::

        active → retiring → closing → closed
        active → retiring → closing → failed_close

    A slot in ``retiring`` no longer accepts new leases but may still
    hold active leases.  ``closing`` means the close sequence is in
    progress.  ``closed`` and ``failed_close`` are terminal states.
    """

    ACTIVE = "active"
    RETIRING = "retiring"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED_CLOSE = "failed_close"


# ---------------------------------------------------------------------------
# Precomputed immutable request state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImmutableRequestState:
    """Precomputed immutable lookup state for the request hot path.

    Built once during generation construction and invalidated naturally
    through generation swap.  Avoids repeated set/dict construction on
    every request.
    """

    provider_ids: frozenset[str]
    account_names: frozenset[str]
    hop_by_hop_headers: frozenset[str]
    local_credential_headers: frozenset[str]


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
    event_loop_lag_monitor: Any = None  # noqa: ANN401 — EventLoopLagMonitor


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
    immutable_request_state: ImmutableRequestState = field(
        default_factory=lambda: ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        ),
    )


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
       :attr:`state` transitions away from ``ACTIVE`` (typically because
       a new generation was published).
    3. Waits for active leases to drain (or a hard deadline elapses
       on process shutdown) and then performs the ordered teardown.
    """

    generation: RuntimeGeneration
    state: SlotState = SlotState.ACTIVE
    active_leases: int = 0
    accepting_leases: bool = True  # kept for backward compat; derived from state
    retirement_started: bool = False  # kept for backward compat; derived from state
    retirement_complete: asyncio.Event = field(default_factory=asyncio.Event)
    drain_event: asyncio.Event = field(default_factory=asyncio.Event)
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_close_error: str | None = None
    forced_close: bool = False
    retirement_start_time: float | None = None
    close_start_time: float | None = None
    close_complete_time: float | None = None
    drain_deadline_s: float | None = None


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
        post-finalization task is silently ignored.  Signals the
        slot's drain event when the count reaches zero so retirement
        tasks can proceed without polling.
        """
        async with self.release_lock:
            if self.released:
                return
            self.released = True
            self.slot.active_leases -= 1
            if self.slot.active_leases <= 0:
                self.slot.active_leases = 0
                self.slot.drain_event.set()
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
    state: str = "active"
    forced_close: bool = False
    retirement_start_time: float | None = None
    drain_deadline_s: float | None = None
    close_start_time: float | None = None
    close_complete_time: float | None = None


@dataclass(frozen=True)
class ActiveGenerationMetadata:
    """Immutable metadata about the active generation without service references.

    Returned by :meth:`RuntimeManager.active_metadata` for short
    synchronous diagnostics that need generation identity without
    acquiring a lease or retaining service references.  The snapshot
    is invalidated atomically at publication.
    """

    generation_id: int
    config_digest: str
    created_at_monotonic: float
    created_at_epoch: float


@dataclass(frozen=True)
class ActiveGenerationView:
    """Compatibility view of the active generation's services.

    Returned by :meth:`RuntimeManager.snapshot_active_values` for
    handlers that need multiple generation-owned references without
    acquiring a lease.  The view is snapshot at call time; the
    underlying generation may retire after the snapshot is taken.

    .. deprecated::
        Prefer acquiring a lease via :meth:`RuntimeManager.acquire`
        for any operation that awaits while using generation-owned
        services.  This view exists only for short synchronous
        diagnostics and backward-compatible dashboard routes.
    """

    generation_id: int
    config_digest: str
    config: Any  # noqa: ANN401
    registry: Any  # noqa: ANN401
    catalog: Any  # noqa: ANN401
    router: Any  # noqa: ANN401
    coordinator: Any  # noqa: ANN401
    health_manager: Any  # noqa: ANN401
    stats: Any  # noqa: ANN401
    model_info: Any  # noqa: ANN401
    transcoder_policy: Any  # noqa: ANN401
    compression_policy: Any  # noqa: ANN401
    client_pool: Any  # noqa: ANN401
    outbound_manager: Any  # noqa: ANN401
    cost_calculator: Any  # noqa: ANN401
    account_backoff_repo: Any  # noqa: ANN401
    dispatch_overhead_recorder: Any  # noqa: ANN401
    dispatch_span_recorder: Any  # noqa: ANN401
    stream_diagnostics: Any  # noqa: ANN401
    finalization_retry_queue: Any  # noqa: ANN401
    routing_trace_guard: Any  # noqa: ANN401
    supervisor: Any  # noqa: ANN401


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
    - ``retirement_task_count``: number of tracked retirement tasks
      (Phase 3).
    """

    active: GenerationDiagnostics | None
    retiring: tuple[GenerationDiagnostics, ...]
    shutdown_in_progress: bool
    next_generation_id: int
    retirement_task_count: int = 0


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
        self._retirement_tasks: dict[int, asyncio.Task[None]] = {}  # Phase 3

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

        Publication returns promptly — the old generation's retirement
        (lease drain + resource close) runs as a tracked background
        task.  Call :meth:`wait_for_retirement` to wait for a specific
        generation's retirement to complete.

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
            await self._spawn_retirement_task(old_slot, drain_timeout_s)

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

    def is_accepting_leases(self) -> bool:
        """Return ``True`` if the active generation accepts new leases.

        Returns ``False`` before ``install_initial``, during retirement
        of the active slot, or after ``shutdown``.
        """
        slot = self._active
        return slot is not None and slot.accepting_leases

    def active_metadata(self) -> ActiveGenerationMetadata:
        """Return immutable metadata about the active generation.

        Safe for short synchronous diagnostics (e.g. config/status
        endpoints) that only need the generation ID, digest, and
        timestamps without retaining service references.  Raises
        :class:`RuntimeManagerShutdownError` when no generation is
        installed.
        """
        slot = self._active
        if slot is None:
            raise RuntimeManagerShutdownError("No active runtime generation installed")
        gen = slot.generation
        return ActiveGenerationMetadata(
            generation_id=gen.generation_id,
            config_digest=gen.config_digest,
            created_at_monotonic=gen.created_at_monotonic,
            created_at_epoch=gen.created_at_epoch,
        )

    def snapshot_active_values(self) -> ActiveGenerationView:
        """Snapshot the active generation's services for synchronous diagnostics.

        Returns an :class:`ActiveGenerationView` that captures
        references to the active generation's services at call time.
        The underlying generation may retire after the snapshot is
        taken; callers must not retain the view across await points
        or slow operations.

        Prefer :meth:`acquire` for any operation that awaits while
        using generation-owned services.

        Raises :class:`RuntimeManagerShutdownError` when no generation
        is installed.
        """
        slot = self._active
        if slot is None:
            raise RuntimeManagerShutdownError("No active runtime generation installed")
        gen = slot.generation
        return ActiveGenerationView(
            generation_id=gen.generation_id,
            config_digest=gen.config_digest,
            config=gen.config,
            registry=gen.registry,
            catalog=gen.catalog,
            router=gen.router,
            coordinator=gen.coordinator,
            health_manager=gen.health_manager,
            stats=gen.stats_service,
            model_info=getattr(gen, "model_info", None),
            transcoder_policy=gen.transcoder_policy,
            compression_policy=gen.compression_policy,
            client_pool=gen.client_pool,
            outbound_manager=gen.outbound_manager,
            cost_calculator=gen.cost_calculator,
            account_backoff_repo=gen.account_backoff_repo,
            dispatch_overhead_recorder=gen.dispatch_overhead_recorder,
            dispatch_span_recorder=gen.dispatch_span_recorder,
            stream_diagnostics=getattr(gen, "stream_diagnostics", None),
            finalization_retry_queue=gen.finalization_retry_queue,
            routing_trace_guard=gen.routing_trace_guard,
            supervisor=gen.supervisor,
        )

    def retirement_snapshot(
        self, generation_id: int | None = None
    ) -> GenerationDiagnostics | tuple[GenerationDiagnostics, ...]:
        """Return retirement diagnostics for one or all retiring generations.

        When *generation_id* is provided, returns the
        :class:`GenerationDiagnostics` for that specific generation if
        it is currently retiring, or raises :class:`ValueError` if no
        retiring slot matches.

        When *generation_id* is ``None`` (default), returns a tuple of
        diagnostics for all currently retiring generations.

        Safe for short synchronous diagnostics; no locks are held.
        """
        now = time.monotonic()
        if generation_id is not None:
            for slot in self._retiring:
                if slot.generation.generation_id == generation_id:
                    return _slot_diagnostics(slot, now)
            raise ValueError(f"No retiring generation with id {generation_id!r}")
        return tuple(_slot_diagnostics(slot, now) for slot in self._retiring)

    # -- retirement ---------------------------------------------------------

    async def _spawn_retirement_task(
        self, slot: _GenerationSlot, drain_timeout_s: float
    ) -> None:
        """Create and register a background retirement task for *slot*.

        The task runs :meth:`begin_retirement` and removes itself from
        the registry on completion.  Called by :meth:`install_candidate`
        so publication returns promptly while retirement proceeds in
        the background.
        """
        gen_id = slot.generation.generation_id

        async def _retire() -> None:
            try:
                await self.begin_retirement(slot, drain_timeout_s=drain_timeout_s)
            finally:
                self._retirement_tasks.pop(gen_id, None)

        task = asyncio.create_task(_retire(), name=f"retire-gen-{gen_id}")
        self._retirement_tasks[gen_id] = task

    async def wait_for_retirement(
        self, generation_id: int, *, timeout_s: float = 300.0
    ) -> None:
        """Wait for a specific generation's retirement task to complete.

        Raises :class:`asyncio.TimeoutError` if the task does not
        complete within *timeout_s*.  No-op when no task is tracked for
        the given *generation_id* (already completed or never existed).
        """
        task = self._retirement_tasks.get(generation_id)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)

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
            slot.state = SlotState.RETIRING
            slot.retirement_start_time = time.monotonic()
            slot.drain_deadline_s = drain_timeout_s
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
        # Event-based drain: wait for drain_event or deadline, whichever
        # comes first.  The drain_event is set by GenerationLease.release()
        # when the lease count reaches zero.
        while slot.active_leases > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                slot.forced_close = True
                logger.warning(
                    "Runtime generation %d retirement drain timed out with "
                    "%d active leases; forcing close",
                    slot.generation.generation_id,
                    slot.active_leases,
                )
                break
            try:
                await asyncio.wait_for(
                    slot.drain_event.wait(), timeout=min(0.5, remaining)
                )
                break  # drain_event set — leases reached zero
            except TimeoutError:
                continue  # re-check deadline
        # Close generation-owned resources in the documented order.
        slot.state = SlotState.CLOSING
        slot.close_start_time = time.monotonic()
        await self._close_slot_resources(slot)
        slot.close_complete_time = time.monotonic()
        slot.retirement_complete.set()
        async with self._lock:
            slot.state = SlotState.CLOSED
            with contextlib.suppress(ValueError):
                self._retiring.remove(slot)
        logger.info(
            "Runtime generation %d retirement complete (forced=%s)",
            slot.generation.generation_id,
            slot.forced_close,
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
        request handlers fail closed rather than hang.  All tracked
        retirement tasks are joined within a bounded deadline.
        """
        shutdown_deadline_s = 10.0
        async with self._lock:
            if self._shutdown_in_progress:
                return
            self._shutdown_in_progress = True
            active = self._active
        if active is not None:
            await self._spawn_retirement_task(active, drain_timeout_s=5.0)
        # Join all tracked retirement tasks within a bounded deadline.
        # Force-cancel any tasks that do not complete in time.
        if self._retirement_tasks:
            tasks = list(self._retirement_tasks.values())
            _, pending = await asyncio.wait(tasks, timeout=shutdown_deadline_s)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Wait for cancelled tasks to finish their finally blocks
            # so _retirement_tasks is cleaned up.
            if self._retirement_tasks:
                await asyncio.wait(
                    list(self._retirement_tasks.values()),
                    timeout=2.0,
                )

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
            retirement_task_count=len(self._retirement_tasks),
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
        state=slot.state.value,
        forced_close=slot.forced_close,
        retirement_start_time=slot.retirement_start_time,
        drain_deadline_s=slot.drain_deadline_s,
        close_start_time=slot.close_start_time,
        close_complete_time=slot.close_complete_time,
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

        # F9: build precomputed immutable request state from registry.
        from eggpool.proxy.client import (  # noqa: PLC0415
            HOP_BY_HOP_HEADERS,
            LOCAL_CREDENTIAL_HEADERS,
        )

        registry = services["registry"]
        immutable_request_state = ImmutableRequestState(
            provider_ids=frozenset(registry.get_provider_ids()),
            account_names=frozenset(
                state.name for state in registry.get_enabled_states()
            ),
            hop_by_hop_headers=HOP_BY_HOP_HEADERS,
            local_credential_headers=LOCAL_CREDENTIAL_HEADERS,
        )

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
            immutable_request_state=immutable_request_state,
        )
        return GenerationBuildResult(
            generation=generation,
            process=process,
        )

    async def cleanup_partial(self, process: ProcessRuntime) -> None:
        """Best-effort cleanup after a partial build failure.

        .. deprecated::
            Superseded by :class:`RuntimeGenerationCandidate.abort` (Phase 4).
            The candidate container now tracks and closes resources in reverse
            registration order.  This method is retained for backward
            compatibility.

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
        "local_pre_upstream_recorder",
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
    "ActiveGenerationMetadata",
    "ActiveGenerationView",
    "CandidateOwnershipState",
    "CleanupDiagnostics",
    "GenerationBuildResult",
    "GenerationDiagnostics",
    "GenerationLease",
    "ProcessRuntime",
    "RuntimeDiagnostics",
    "RuntimeGeneration",
    "RuntimeGenerationBuilder",
    "RuntimeGenerationCandidate",
    "RuntimeManager",
    "RuntimeManagerLeaseExhaustedError",
    "RuntimeManagerShutdownError",
    "SlotState",
    "attach_runtime_manager",
    "build_generation_from_config",
    "is_runtime_owned_attr",
    "leased_runtime",
    "wrap_stream_with_lease",
]
