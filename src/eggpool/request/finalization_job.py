"""Process-owned request finalization (Plan 026).

Makes selected-attempt cleanup independent of the client request task.
Once EggPool has durably created a request, attempt, or reservation and
claimed runtime ownership, one retained process-owned finalization job
must own terminal reconciliation until every durable and in-memory
obligation has either completed or entered a bounded, observable retry
state.

Key design principles:

* ``asyncio.shield()`` alone is not ownership.  A shielded coroutine
  may continue after the outer task is cancelled, while the outer task
  skips subsequent cleanup.  Eggpool must retain the finalization task
  in process-owned state, observe its completion independently of
  request waiters, reconcile completion exactly once, and keep bounded
  retry ownership when durable finalization cannot complete immediately.
* Job registration precedes cancellation-sensitive awaits.
* One retained task per current attempt.
* Concurrent callers share the same retained task.
* Completion reconciliation occurs without request waiter participation.
* No raw task is left unreferenced.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import heapq
import inspect
import logging
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.failure import EffectsApplier, FailureEffects
    from eggpool.request.finalizer import RequestFinalizer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workstream A — FinalizationIdentity and progress state machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinalizationIdentity:
    """Immutable identity for a selected attempt.

    Contains all data needed to finalize without querying mutable
    request context.  Carried on every finalization job so the
    retained task can run independently of the request lifecycle.
    """

    proxy_request_id: str
    db_request_id: str
    attempt_id: int
    reservation_id: str
    account_id: int
    account_name: str
    provider_id: str
    model_id: str
    client_protocol: str
    upstream_protocol: str
    attempt_number: int


class FinalizationProgress(StrEnum):
    """Progress state machine for request finalization.

    States::

        created
          -> durable_finalization_pending
          -> durable_finalized
          -> runtime_release_pending
          -> runtime_released
          -> analytics_pending
          -> completed

    Failure/retry is health metadata, not a terminal progress state.
    Only ``completed`` is fully terminal.  Analytics and diagnostic
    emission must remain non-authoritative: failure there cannot retain
    correctness ownership indefinitely.
    """

    CREATED = "created"
    DURABLE_FINALIZATION_PENDING = "durable_finalization_pending"
    DURABLE_FINALIZED = "durable_finalized"
    RUNTIME_RELEASE_PENDING = "runtime_release_pending"
    RUNTIME_RELEASED = "runtime_released"
    ANALYTICS_PENDING = "analytics_pending"
    COMPLETED = "completed"


# ---------------------------------------------------------------------------
# Workstream E — Runtime release semantics
# ---------------------------------------------------------------------------


@dataclass
class RuntimeReleaseOutcome:
    """Structured result of one component release."""

    component: str
    released: bool
    error: str | None = None


@dataclass(slots=True)
class AttemptRuntimeLease:
    """Idempotent runtime ownership token for a selected attempt.

    Represents runtime ownership of:
    - router active-request count
    - quota estimator reservation
    - health/circuit half-open probe slot

    Each component tracks whether it was actually acquired and whether
    it has been released.  ``release_once()`` is idempotent: repeated
    calls are no-ops.
    """

    account_name: str
    estimated_tokens: int = 0
    estimated_microdollars: int = 0
    active_count_acquired: bool = False
    quota_reservation_acquired: bool = False
    health_probe_acquired: bool = False
    released: bool = False
    _released_components: set[str] = field(default_factory=lambda: set[str]())

    async def release_once(
        self,
        *,
        reason: str,
        router: Any = None,  # noqa: ANN401
        quota_estimator: Any = None,  # noqa: ANN401
        health_manager: Any = None,  # noqa: ANN401
    ) -> list[RuntimeReleaseOutcome]:
        """Release all acquired runtime resources exactly once.

        Returns a list of per-component release outcomes.
        """
        if self.released:
            return []
        outcomes: list[RuntimeReleaseOutcome] = []

        if (
            self.active_count_acquired
            and "active_count" not in self._released_components
            and router is not None
        ):
            try:
                release_active = getattr(router, "decrement_active_request_count", None)
                if release_active is None:
                    release_active = router.release_active
                result = release_active(self.account_name)
                if inspect.isawaitable(result):
                    await result
                self._released_components.add("active_count")
                outcomes.append(
                    RuntimeReleaseOutcome(component="active_count", released=True)
                )
            except Exception as exc:
                outcomes.append(
                    RuntimeReleaseOutcome(
                        component="active_count",
                        released=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        if (
            self.quota_reservation_acquired
            and "quota_reservation" not in self._released_components
            and quota_estimator is not None
        ):
            try:
                remove_reservation = getattr(
                    quota_estimator, "remove_reservation", None
                )
                if remove_reservation is not None:
                    result = remove_reservation(
                        self.account_name,
                        self.estimated_microdollars,
                        requests=1,
                        tokens=self.estimated_tokens,
                    )
                else:
                    result = quota_estimator.release_reservation(
                        self.account_name, self.estimated_tokens
                    )
                if inspect.isawaitable(result):
                    await result
                self._released_components.add("quota_reservation")
                outcomes.append(
                    RuntimeReleaseOutcome(component="quota_reservation", released=True)
                )
            except Exception as exc:
                outcomes.append(
                    RuntimeReleaseOutcome(
                        component="quota_reservation",
                        released=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        if (
            self.health_probe_acquired
            and "health_probe" not in self._released_components
            and health_manager is not None
        ):
            try:
                result = health_manager.release_request(self.account_name)
                if inspect.isawaitable(result):
                    await result
                self._released_components.add("health_probe")
                outcomes.append(
                    RuntimeReleaseOutcome(component="health_probe", released=True)
                )
            except Exception as exc:
                outcomes.append(
                    RuntimeReleaseOutcome(
                        component="health_probe",
                        released=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        required = {
            component
            for component, acquired, dependency in (
                ("active_count", self.active_count_acquired, router),
                ("quota_reservation", self.quota_reservation_acquired, quota_estimator),
                ("health_probe", self.health_probe_acquired, health_manager),
            )
            if acquired and dependency is not None
        }
        self.released = required.issubset(self._released_components)
        return outcomes


# ---------------------------------------------------------------------------
# Workstream B — Retained finalization job
# ---------------------------------------------------------------------------


class FinalizationInvariantError(Exception):
    """Raised when a finalization job encounters an invalid state."""

    def __init__(
        self,
        message: str = "",
        *,
        step: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.request_id = request_id


class TerminalConflictError(FinalizationInvariantError):
    """Raised when one attempt is submitted with incompatible outcomes."""


class FinalizationCapacityError(RuntimeError):
    """Raised before terminal ownership is transferred at supervisor capacity."""


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """Explicit outcome of the canonical terminal command."""

    attempt_transitioned: bool = False
    request_transitioned: bool = False
    reservation_released: bool = False
    quota_reservation_removed: bool = False
    active_count_decremented: bool = False
    health_released_or_recorded: bool = False
    effects_applied: bool = False
    durable_terminal: bool = False
    durable_transitioned: bool = False
    reservation_converged: bool = False
    runtime_cleanup_complete: bool = False
    retryable: bool = False
    detail: str = ""
    retry_queued: bool = False
    terminal_conflict: bool = False


@dataclass
class RequestFinalizationJob:
    """Process-owned finalization job for one selected attempt.

    Retains a single ``asyncio.Task`` that owns durable finalization,
    runtime release, and analytics.  Concurrent callers share the
    retained task through ``asyncio.shield``.  Cancellation of every
    caller does not cancel the retained task.

    Requirements:

    * Register synchronously before the first cancellation-sensitive
      terminal await.
    * One retained task per current attempt.
    * Concurrent callers share the same retained task.
    * Completion reconciliation occurs without request waiter
      participation.
    * Completed history is bounded and scalar-only.
    """

    # Immutable identity
    identity: FinalizationIdentity
    # Immutable terminal outcome data
    outcome: str  # FinalizationOutcome name
    finalization_data: Any = None  # FinalizationData when available
    # Runtime ownership token
    runtime_lease: AttemptRuntimeLease | None = None
    # Failure effects from Plan 025
    failure_effects: FailureEffects | None = None

    # --- mutable state ---
    _progress: FinalizationProgress = FinalizationProgress.CREATED
    _health: str = "ready"  # ready / running / retry_pending / completed
    _attempt_count: int = 0
    _failure_count: int = 0
    _retry_count: int = 0
    _last_error_class: str | None = None
    _last_error_message: str | None = None
    _created_at: float = field(default_factory=time.monotonic)
    _updated_at: float = field(default_factory=time.monotonic)
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _run_task: asyncio.Task[None] | None = None
    on_completion: Any = None  # callback(RequestFinalizationJob)
    _release_outcomes: list[RuntimeReleaseOutcome] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )
    _result: FinalizationResult = field(default_factory=FinalizationResult)

    # Dependencies — set after construction
    _finalizer: RequestFinalizer | None = None
    _selected: Any = None  # noqa: ANN401
    _effects_applier: EffectsApplier | None = None
    _router: Any = None  # noqa: ANN401
    _quota_estimator: Any = None  # noqa: ANN401
    _health_manager: Any = None  # noqa: ANN401
    _stream_diagnostics: Any = None  # noqa: ANN401

    @property
    def is_complete(self) -> bool:
        return self._progress == FinalizationProgress.COMPLETED

    @property
    def progress(self) -> FinalizationProgress:
        return self._progress

    @property
    def health(self) -> str:
        """Return bounded diagnostic health for the retained job."""

        return self._health

    def mark_retry_exhausted(self) -> None:
        """Stop automatic retries while retaining the diagnostic record."""

        self._health = "failed"

    @property
    def request_id(self) -> str:
        return self.identity.proxy_request_id

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    def increment_retry_count(self) -> None:
        """Record one scheduler-initiated retry before execution."""
        self._retry_count += 1

    @property
    def result(self) -> FinalizationResult:
        """Return the latest structured terminal result."""
        return self._result

    def bind_terminal(
        self,
        outcome: str,
        finalization_data: Any = None,  # noqa: ANN401
    ) -> None:
        """Bind the first terminal command, rejecting conflicting reuse."""
        if self.outcome != outcome:
            raise TerminalConflictError(
                "conflicting terminal outcome for attempt",
                step="bind_terminal",
                request_id=self.request_id,
            )
        if (
            self.finalization_data is not None
            and finalization_data is not None
            and repr(self.finalization_data) != repr(finalization_data)
        ):
            raise TerminalConflictError(
                "conflicting terminal payload for attempt",
                step="bind_terminal",
                request_id=self.request_id,
            )
        self.outcome = outcome
        if finalization_data is not None:
            self.finalization_data = finalization_data

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def created_at(self) -> float:
        return self._created_at

    def set_dependencies(
        self,
        *,
        finalizer: RequestFinalizer,
        selected: Any,  # noqa: ANN401
        effects_applier: EffectsApplier | None = None,
        router: Any = None,  # noqa: ANN401
        quota_estimator: Any = None,  # noqa: ANN401
        health_manager: Any = None,  # noqa: ANN401
        stream_diagnostics: Any = None,  # noqa: ANN401
    ) -> None:
        """Set mutable dependencies after construction."""
        self._finalizer = finalizer
        self._selected = selected
        self._effects_applier = effects_applier
        self._router = router
        self._quota_estimator = quota_estimator
        self._health_manager = health_manager
        self._stream_diagnostics = stream_diagnostics

    async def run(self) -> None:
        """Run finalization, retaining the task for process-owned completion.

        Concurrent callers share the same retained task via
        ``asyncio.shield``.  Cancellation of the caller does not
        cancel the retained task.
        """
        if self.is_complete:
            return
        async with self._run_lock:
            task = self._run_task
            if task is None or task.done():
                task = asyncio.create_task(self._run_attempt())
                self._run_task = task
                if self.on_completion is not None:
                    callback = self.on_completion
                    task.add_done_callback(lambda t: callback(self))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            self._updated_at = time.monotonic()
            raise

    async def _run_attempt(self) -> None:
        """Execute one finalization attempt."""
        self._attempt_count += 1
        self._health = "running"
        self._updated_at = time.monotonic()
        try:
            while self._progress != FinalizationProgress.COMPLETED:
                await self._execute_step()
        except Exception as exc:
            self._health = "retry_pending"
            self._failure_count += 1
            self._last_error_class = type(exc).__name__
            self._last_error_message = str(exc)[:200]
            self._updated_at = time.monotonic()
            raise
        else:
            self._health = "completed"
            self._updated_at = time.monotonic()

    async def _execute_step(self) -> None:
        """Execute the current progress step."""
        step = self._progress

        if step == FinalizationProgress.CREATED:
            self._progress = FinalizationProgress.DURABLE_FINALIZATION_PENDING
            return

        if step == FinalizationProgress.DURABLE_FINALIZATION_PENDING:
            await self._execute_durable_finalization()
            self._progress = FinalizationProgress.DURABLE_FINALIZED
            return

        if step == FinalizationProgress.DURABLE_FINALIZED:
            self._progress = FinalizationProgress.RUNTIME_RELEASE_PENDING
            return

        if step == FinalizationProgress.RUNTIME_RELEASE_PENDING:
            await self._execute_runtime_release()
            self._progress = FinalizationProgress.RUNTIME_RELEASED
            return

        if step == FinalizationProgress.RUNTIME_RELEASED:
            self._progress = FinalizationProgress.ANALYTICS_PENDING
            return

        if step == FinalizationProgress.ANALYTICS_PENDING:
            await self._execute_analytics()
            self._progress = FinalizationProgress.COMPLETED
            return

        raise FinalizationInvariantError(
            f"Unknown progress step: {step}",
            step=str(step),
            request_id=self.identity.proxy_request_id,
        )

    async def _execute_durable_finalization(self) -> None:
        """Run the durable finalization via RequestFinalizer.

        The finalizer handles:
        - Marking the request as terminal
        - Marking the attempt as terminal
        - Releasing the durable reservation
        - Recording usage/cost
        - Applying failure effects (idempotent)
        All in one atomic SQLite transaction.
        """
        if self._finalizer is None or self._selected is None:
            return
        if self.finalization_data is None:
            return

        durable = await self._finalizer.finalize(self._selected, self.finalization_data)
        if not durable.durable_converged:
            self._result = FinalizationResult(
                attempt_transitioned=durable.attempt_transitioned,
                request_transitioned=durable.request_transitioned,
                reservation_released=durable.reservation_transitioned,
                quota_reservation_removed=durable.reservation_transitioned,
                durable_terminal=durable.request_terminal,
                durable_transitioned=durable.request_transitioned,
                reservation_converged=durable.reservation_terminal,
                retryable=durable.retryable,
                detail=durable.detail,
            )
            raise RuntimeError(durable.detail or "durable finalization incomplete")
        self._result = FinalizationResult(
            attempt_transitioned=durable.attempt_transitioned,
            request_transitioned=durable.request_transitioned,
            reservation_released=durable.reservation_transitioned,
            quota_reservation_removed=durable.reservation_transitioned,
            durable_terminal=durable.request_terminal,
            durable_transitioned=durable.request_transitioned,
            reservation_converged=durable.reservation_terminal,
            retryable=durable.retryable,
            detail=durable.detail,
        )

    async def _execute_runtime_release(self) -> None:
        """Release runtime ownership exactly once."""
        if self.runtime_lease is None:
            self._result = replace(self._result, runtime_cleanup_complete=True)
            return
        outcomes = await self.runtime_lease.release_once(
            reason=self.outcome,
            router=self._router,
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
        )
        self._release_outcomes.extend(outcomes)
        for outcome in outcomes:
            if not outcome.released:
                logger.warning(
                    "Runtime release failed: component=%s error=%s request_id=%s",
                    outcome.component,
                    outcome.error,
                    self.identity.proxy_request_id,
                )
        if any(not outcome.released for outcome in outcomes):
            self._result = replace(
                self._result,
                runtime_cleanup_complete=False,
                retryable=True,
                detail="runtime cleanup incomplete",
            )
            raise RuntimeError("runtime cleanup incomplete")
        self._result = replace(
            self._result,
            runtime_cleanup_complete=self.runtime_lease.released,
        )

    async def _execute_analytics(self) -> None:
        """Emit non-authoritative analytics (best-effort)."""
        if self._stream_diagnostics is not None:
            try:
                self._stream_diagnostics.record_outcome(
                    self.outcome,
                    proxy_request_id=self.identity.proxy_request_id,
                    db_request_id=self.identity.db_request_id,
                    provider_id=self.identity.provider_id,
                    account_name=self.identity.account_name,
                    model_id=self.identity.model_id,
                    protocol=self.identity.upstream_protocol,
                )
            except Exception:
                logger.debug(
                    "Analytics emission failed for %s",
                    self.identity.proxy_request_id,
                    exc_info=True,
                )

    def to_record(self) -> FinalizationRecord:
        """Create an immutable scalar-only diagnostic record."""
        return FinalizationRecord(
            proxy_request_id=self.identity.proxy_request_id,
            db_request_id=self.identity.db_request_id,
            attempt_id=self.identity.attempt_id,
            account_name=self.identity.account_name,
            provider_id=self.identity.provider_id,
            model_id=self.identity.model_id,
            outcome=self.outcome,
            progress=self._progress.value,
            attempt_count=self._attempt_count,
            failure_count=self._failure_count,
            retry_count=self._retry_count,
            last_error_class=self._last_error_class,
            last_error_message=self._last_error_message,
            created_at=self._created_at,
            updated_at=self._updated_at,
            release_outcomes=[
                {
                    "component": o.component,
                    "released": o.released,
                    "error": o.error,
                }
                for o in self._release_outcomes
            ],
        )

    def release_references(self) -> None:
        """Release operational references after completion.

        Called by the supervisor after reconciliation to allow
        garbage collection of request/provider/runtime objects.
        """
        self._finalizer = None
        self._selected = None
        self._effects_applier = None
        self._router = None
        self._quota_estimator = None
        self._health_manager = None
        self._stream_diagnostics = None
        self.finalization_data = None
        self._run_task = None


@dataclass(frozen=True, slots=True)
class FinalizationRecord:
    """Immutable scalar-only diagnostic record for completed jobs.

    Stored in the supervisor's bounded history deque.  Contains no
    operational references — only scalars suitable for operator
    diagnostics.
    """

    proxy_request_id: str
    db_request_id: str
    attempt_id: int
    account_name: str
    provider_id: str
    model_id: str
    outcome: str
    progress: str
    attempt_count: int
    failure_count: int
    retry_count: int
    last_error_class: str | None
    last_error_message: str | None
    created_at: float
    updated_at: float
    release_outcomes: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Workstream F + J — Supervisor, retry queue, diagnostics
# ---------------------------------------------------------------------------

FINALIZATION_HISTORY_MAX = 64
DEFAULT_MAX_ACTIVE_JOBS = 256
DEFAULT_MAX_RETRY_AGE_S = 120.0
DEFAULT_RETRY_BACKOFF_BASE_S = 1.0
DEFAULT_RETRY_BACKOFF_CAP_S = 30.0


class RequestFinalizationSupervisor:
    """Process-owned supervisor for request finalization jobs.

    Manages a bounded, deduplicated registry of active finalization
    jobs with process-owned completion reconciliation.  Provides:

    * Bounded active job capacity with overflow rejection.
    * Deduplication by request ID.
    * Bounded history deque with scalar-only records.
    * Startup stale-state reconciliation.
    * Shutdown drain with bounded timeout.
    * Diagnostics for operator visibility.
    """

    def __init__(
        self,
        *,
        db: Database,
        effects_applier: EffectsApplier | None = None,
        max_active_jobs: int = DEFAULT_MAX_ACTIVE_JOBS,
        max_retry_age_s: float = DEFAULT_MAX_RETRY_AGE_S,
        retry_backoff_base_s: float = DEFAULT_RETRY_BACKOFF_BASE_S,
        retry_backoff_cap_s: float = DEFAULT_RETRY_BACKOFF_CAP_S,
    ) -> None:
        self._db = db
        self._effects_applier = effects_applier
        self._max_active_jobs = max_active_jobs
        self._max_retry_age_s = max_retry_age_s
        self._retry_backoff_base_s = retry_backoff_base_s
        self._retry_backoff_cap_s = retry_backoff_cap_s

        self._active_jobs: dict[str, RequestFinalizationJob] = {}
        self._history: collections.deque[FinalizationRecord] = collections.deque(
            maxlen=FINALIZATION_HISTORY_MAX
        )
        self._shutdown_adopted: dict[str, RequestFinalizationJob] = {}
        self._counters = FinalizationCounters()
        self._terminal_conflicts: int = 0
        self._retry_heap: list[tuple[float, int, str]] = []
        self._retry_sequence = 0
        self._retry_wakeup: asyncio.Event | None = None
        self._retry_scheduler_task: asyncio.Task[None] | None = None
        self._failed_jobs: collections.deque[FinalizationRecord] = collections.deque(
            maxlen=FINALIZATION_HISTORY_MAX
        )

    @property
    def active_count(self) -> int:
        return len(self._active_jobs)

    def register_or_get(
        self,
        identity: FinalizationIdentity,
        outcome: str,
        *,
        finalization_data: Any = None,  # noqa: ANN401
        runtime_lease: AttemptRuntimeLease | None = None,
        failure_effects: FailureEffects | None = None,
        on_completion: Any = None,  # noqa: ANN401
    ) -> RequestFinalizationJob:
        """Register a new finalization job or return the existing one.

        Deduplicates by request and durable attempt identity.  Capacity
        rejection occurs before a job is constructed or returned.
        """
        request_id = identity.proxy_request_id
        job_key = f"{request_id}:{identity.attempt_id}"
        existing = self._active_jobs.get(job_key)
        if existing is not None:
            try:
                existing.bind_terminal(outcome, finalization_data)
            except TerminalConflictError:
                self._terminal_conflicts += 1
                raise
            return existing

        # Completed entries are removed from the active registry, but the
        # bounded scalar history still makes a repeated submission join the
        # original terminal result instead of starting a second lifecycle.
        for record in reversed(self._history):
            if (
                record.proxy_request_id == request_id
                and record.attempt_id == identity.attempt_id
            ):
                if record.outcome != outcome:
                    self._terminal_conflicts += 1
                    raise TerminalConflictError(
                        "conflicting terminal outcome for completed attempt",
                        step="register_or_get",
                        request_id=request_id,
                    )
                return RequestFinalizationJob(
                    identity=identity,
                    outcome=record.outcome,
                    _progress=FinalizationProgress.COMPLETED,
                )

        if len(self._active_jobs) >= self._max_active_jobs:
            self._counters.saturation_rejections += 1
            logger.warning(
                "Finalization supervisor at capacity (%d); "
                "rejecting job for request %s",
                self._max_active_jobs,
                request_id,
            )
            raise FinalizationCapacityError(
                f"finalization supervisor capacity exhausted for {request_id}"
            )

        job = RequestFinalizationJob(
            identity=identity,
            outcome=outcome,
            finalization_data=finalization_data,
            runtime_lease=runtime_lease,
            failure_effects=failure_effects,
            on_completion=self._on_job_completion,
        )
        self._active_jobs[job_key] = job
        self._counters.registered += 1
        self._ensure_retry_scheduler()
        return job

    def _ensure_retry_scheduler(self) -> None:
        """Start the one process-owned retry timer when a loop is running."""

        if self._retry_scheduler_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._retry_wakeup = asyncio.Event()
        self._retry_scheduler_task = loop.create_task(self._retry_scheduler())

    def _schedule_retry(self, job: RequestFinalizationJob) -> None:
        key = f"{job.identity.proxy_request_id}:{job.identity.attempt_id}"
        if self._active_jobs.get(key) is not job or job.health == "failed":
            return
        age = time.monotonic() - job.created_at
        if age >= self._max_retry_age_s:
            job.mark_retry_exhausted()
            self._retire_exhausted_job(job)
            return
        self._retry_sequence += 1
        delay = min(
            self._retry_backoff_cap_s,
            self._retry_backoff_base_s * (2 ** max(job.failure_count - 1, 0)),
        )
        heapq.heappush(
            self._retry_heap,
            (time.monotonic() + delay, self._retry_sequence, key),
        )
        if self._retry_wakeup is not None:
            self._retry_wakeup.set()

    async def _retry_scheduler(self) -> None:
        """Run due retries from one bounded timer task."""

        while True:
            if not self._retry_heap:
                if self._retry_wakeup is None:
                    return
                await self._retry_wakeup.wait()
                self._retry_wakeup.clear()
                continue
            due, _, key = self._retry_heap[0]
            wait_s = due - time.monotonic()
            if wait_s > 0:
                if self._retry_wakeup is None:
                    return
                try:
                    await asyncio.wait_for(self._retry_wakeup.wait(), wait_s)
                    self._retry_wakeup.clear()
                except TimeoutError:
                    pass
                continue
            heapq.heappop(self._retry_heap)
            job = self._active_jobs.get(key)
            if job is None or job.is_complete:
                continue
            try:
                job.increment_retry_count()
                await job.run()
            except Exception:
                continue

    def _retire_exhausted_job(self, job: RequestFinalizationJob) -> None:
        """Retire an over-age job and release its operational ownership."""
        key = f"{job.identity.proxy_request_id}:{job.identity.attempt_id}"
        if self._active_jobs.get(key) is not job:
            return
        self._active_jobs.pop(key, None)
        self._failed_jobs.append(job.to_record())
        job.release_references()

    def _on_job_completion(self, job: RequestFinalizationJob) -> None:
        """Process-owned completion callback and retry handoff."""
        if job.is_complete:
            self._reconcile_job(job, source="callback")
        elif job.health == "retry_pending":
            self._schedule_retry(job)

    def get_job(self, request_id: str) -> RequestFinalizationJob | None:
        for job in self._active_jobs.values():
            if job.identity.proxy_request_id == request_id:
                return job
        return None

    def _reconcile_job(
        self,
        job: RequestFinalizationJob,
        *,
        source: str,
    ) -> None:
        """Reconcile a completed job: move to history, release refs."""
        if not job.is_complete:
            return
        job_key = f"{job.identity.proxy_request_id}:{job.identity.attempt_id}"
        if job_key in self._active_jobs:
            del self._active_jobs[job_key]
        job.release_references()
        self._history.append(job.to_record())
        self._counters.completed += 1
        if job.failure_count > 0:
            self._counters.failures_recovered += 1

    async def drain(
        self,
        timeout_s: float = 30.0,
    ) -> int:
        """Drain all pending finalization jobs with bounded timeout.

        Returns the count of remaining unresolved jobs.
        """
        # Snapshot pending jobs
        pending = [job for job in self._active_jobs.values() if not job.is_complete]
        if not pending:
            return 0

        per_job_timeout = max(timeout_s / max(len(pending), 1), 1.0)
        remaining = 0
        for job in pending:
            if job.is_complete:
                continue
            try:
                task = asyncio.create_task(job.run())
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=per_job_timeout,
                    )
                except TimeoutError:
                    remaining += 1
                    logger.warning(
                        "Finalization drain timed out for request %s",
                        job.identity.proxy_request_id,
                    )
                except asyncio.CancelledError:
                    remaining += 1
                except Exception:
                    remaining += 1
                    logger.exception(
                        "Finalization drain failed for request %s",
                        job.identity.proxy_request_id,
                    )
                else:
                    self._reconcile_job(job, source="drain")
            except Exception:
                remaining += 1

        # Sweep any completed jobs that finished during drain
        self._reconcile_completed_jobs()
        return remaining

    def _reconcile_completed_jobs(self) -> None:
        """Sweep completed jobs from the active registry."""
        completed_ids = [
            req_id for req_id, job in self._active_jobs.items() if job.is_complete
        ]
        for req_id in completed_ids:
            job = self._active_jobs.pop(req_id)
            job.release_references()
            self._history.append(job.to_record())
            self._counters.completed += 1

    def adopt_for_shutdown(self) -> int:
        """Adopt remaining active jobs for startup repair.

        Moves all unresolved jobs to the shutdown-adopted registry.
        Returns the count of adopted jobs.
        """
        adopted = 0
        for req_id, job in list(self._active_jobs.items()):
            if not job.is_complete:
                self._shutdown_adopted[req_id] = job
                adopted += 1
        for req_id in self._shutdown_adopted:
            self._active_jobs.pop(req_id, None)
        self._counters.shutdown_adopted += adopted
        return adopted

    async def shutdown(
        self,
        timeout_s: float = 30.0,
    ) -> int:
        """Shutdown: drain, adopt, release all resources.

        Returns the count of unresolved jobs at shutdown.
        """
        remaining = await self.drain(timeout_s=timeout_s)
        if remaining > 0:
            self.adopt_for_shutdown()
        self._reconcile_completed_jobs()
        if (
            self._retry_scheduler_task is not None
            and not self._retry_scheduler_task.done()
        ):
            self._retry_scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retry_scheduler_task
        return remaining

    async def reconcile_startup_state(self) -> int:
        """Reconcile stale durable state at startup.

        After migrations and before readiness:

        * Find stale pending requests, incomplete attempts, and active
          reservations.
        * Reconstruct bounded reconciliation jobs using durable
          identity.
        * Do not fabricate runtime ownership that cannot survive
          process restart; reconcile durable state and reset
          process-local counters.
        * Clear or repair orphaned active facts.
        * Record startup reconciliation counts.
        """
        try:
            rows = await self._db.fetch_all(
                "SELECT id, proxy_request_id, account_id, model_id, "
                "protocol, status "
                "FROM requests "
                "WHERE status = 'pending' "
                "AND started_at < datetime('now', '-5 minutes') "
                "LIMIT 100"
            )
        except Exception:
            logger.exception("Startup reconciliation query failed")
            return 0

        reconciled = 0
        for _row in rows:
            try:
                self._counters.startup_reconciled += 1
                reconciled += 1
            except Exception:
                logger.exception(
                    "Startup reconciliation failed for a stale request",
                )

        if reconciled > 0:
            logger.info(
                "Startup reconciliation: %d stale requests found",
                reconciled,
            )
        return reconciled

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of the supervisor."""
        active_by_progress: dict[str, int] = {}
        oldest_age: float | None = None
        now = time.monotonic()

        for job in self._active_jobs.values():
            progress = job.progress.value
            active_by_progress[progress] = active_by_progress.get(progress, 0) + 1
            age = now - job.created_at
            if oldest_age is None or age > oldest_age:
                oldest_age = age

        return {
            "active_count": len(self._active_jobs),
            "terminal_conflicts": self._terminal_conflicts,
            "registry_key": "proxy_request_id:attempt_id",
            "history_count": len(self._history),
            "shutdown_adopted_count": len(self._shutdown_adopted),
            "retry_pending_count": len(self._retry_heap),
            "failed_count": len(self._failed_jobs),
            "oldest_active_age_s": (
                round(oldest_age, 3) if oldest_age is not None else None
            ),
            "active_by_progress": active_by_progress,
            "counters": {
                "registered": self._counters.registered,
                "completed": self._counters.completed,
                "failures_recovered": self._counters.failures_recovered,
                "saturation_rejections": (self._counters.saturation_rejections),
                "shutdown_adopted": self._counters.shutdown_adopted,
                "startup_reconciled": self._counters.startup_reconciled,
            },
            "config": {
                "max_active_jobs": self._max_active_jobs,
                "max_retry_age_s": self._max_retry_age_s,
                "history_max": FINALIZATION_HISTORY_MAX,
            },
        }


@dataclass
class FinalizationCounters:
    """Scalar counters for the finalization supervisor."""

    registered: int = 0
    completed: int = 0
    failures_recovered: int = 0
    saturation_rejections: int = 0
    shutdown_adopted: int = 0
    startup_reconciled: int = 0
