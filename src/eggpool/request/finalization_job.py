"""Generation-owned request finalization (Plan 026/080).

Makes selected-attempt cleanup independent of the client request task.
Once EggPool has durably created a request, attempt, or reservation and
claimed runtime ownership, one retained generation-owned finalization job
must own terminal reconciliation until every durable and in-memory
obligation has either completed or entered a bounded, observable retry
state.

Key design principles:

* ``asyncio.shield()`` alone is not ownership.  A shielded coroutine
  may continue after the outer task is cancelled, while the outer task
  skips subsequent cleanup.  Eggpool must retain the finalization task
  in generation-owned state, observe its completion independently of
  request waiters, reconcile completion exactly once, and keep bounded
  retry ownership when durable finalization cannot complete immediately.
* Job registration precedes cancellation-sensitive awaits.
* One retained task per current attempt.
* Concurrent callers share the same retained task.
* Completion reconciliation occurs without request waiter participation.
* No raw task is left unreferenced.

The supervisor is owned by the runtime generation that constructed it. A
registered job retains that generation until durable state and all required
generation-local runtime obligations converge. Diagnostic history is scalar
only and never retains a generation.
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
from typing import TYPE_CHECKING, Any, Literal

from eggpool.security.redaction import safe_exception_detail

if TYPE_CHECKING:
    from collections.abc import Callable

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
    db_request_id: str | None
    attempt_id: int | None
    reservation_id: str | None
    account_id: int | None
    account_name: str
    provider_id: str
    model_id: str
    client_protocol: str
    upstream_protocol: str
    attempt_number: int | None


TerminalIdentity = FinalizationIdentity


@dataclass(slots=True)
class RuntimePublicationReceipt:
    """Runtime components acquired while publishing a durable claim.

    The pending fields represent the provisional request/token load that is
    visible to routing while SQLite persistence is in progress.  Exactly one
    of ``pending_load_converted`` or ``pending_load_released`` must become
    true after a pending claim is acquired.
    """

    pending_request_added: bool = False
    pending_tokens_added: bool = False
    pending_load_converted: bool = False
    pending_load_released: bool = False
    active_count_added: bool = False
    quota_reservation_added: bool = False
    health_probe_acquired: bool = False
    health_probe_released: bool = False


@dataclass(frozen=True, slots=True)
class FailedAttemptCleanupSubmission:
    """Immutable facts for a retryable failed-attempt cleanup command."""

    identity: FinalizationIdentity
    status_code: int | None
    error_class: str | None
    retry_category: str | None
    bytes_received: int
    latency_ms: int
    failure_effects: FailureEffects | None = None


@dataclass(slots=True)
class FailedAttemptCleanupProgress:
    """Resumable progress for failed-attempt cleanup."""

    durable_transition_checked: bool = False
    durable_attempt_transitioned: bool = False
    durable_reservation_converged: bool = False
    runtime_cleanup_required: bool = False
    quota_released: bool = False
    active_count_released: bool = False
    health_effect_applied: bool = False
    probe_released: bool = False
    effect_progress: FailureEffectProgress | None = field(default=None, repr=False)
    completed: bool = False


@dataclass(frozen=True, slots=True)
class ClaimCompensationSubmission:
    """Immutable facts for post-commit selection-claim compensation."""

    identity: FinalizationIdentity
    account_name: str
    estimated_tokens: int
    estimated_microdollars: int
    bytes_received: int
    latency_ms: int
    receipt: RuntimePublicationReceipt


@dataclass(slots=True)
class ClaimCompensationProgress:
    """Resumable progress for post-commit claim compensation."""

    pending_load_released: bool = False
    active_count_released: bool = False
    quota_reservation_released: bool = False
    durable_attempt_finalized: bool = False
    durable_reservation_converged: bool = False
    probe_released: bool = False
    completed: bool = False


TerminalCommandKind = Literal[
    "selected_request_finalization",
    "failed_attempt_cleanup",
    "claim_compensation",
]
TerminalCommandSubmission = FailedAttemptCleanupSubmission | ClaimCompensationSubmission
TerminalCommandProgress = FailedAttemptCleanupProgress | ClaimCompensationProgress


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from eggpool.failure import FailureEffectProgress

    TerminalCommandRunner = Callable[
        [TerminalCommandSubmission, TerminalCommandProgress], Awaitable[None]
    ]


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
    # Terminal outcome obligations are bound when the terminal command is
    # registered.  ``None`` keeps direct legacy callers compatible until
    # their finalization data supplies the facts.
    usage_outcome_required: bool | None = None
    health_outcome_required: bool | None = None
    account_runtime_outcome_required: bool | None = None
    released: bool = False
    _released_components: set[str] = field(default_factory=lambda: set[str]())

    def component_complete(self, component: str) -> bool:
        """Return whether a runtime component has converged."""
        return component in self._released_components

    def mark_component_complete(self, component: str) -> None:
        """Record successful convergence of one runtime component."""
        self._released_components.add(component)

    def bind_outcome_obligations(
        self,
        *,
        usage_required: bool,
        health_required: bool,
        account_runtime_required: bool,
    ) -> None:
        """Bind and validate process-local terminal outcome ownership."""
        facts = (
            ("usage_outcome_required", usage_required),
            ("health_outcome_required", health_required),
            ("account_runtime_outcome_required", account_runtime_required),
        )
        for field_name, value in facts:
            existing = getattr(self, field_name)
            if existing is not None and existing != value:
                raise FinalizationInvariantError(
                    "incompatible runtime outcome obligations",
                    step="bind_outcome_obligations",
                )
            setattr(self, field_name, value)

    @property
    def completed_components(self) -> frozenset[str]:
        """Return an immutable view of converged runtime components."""
        return frozenset(self._released_components)

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
        ):
            if router is None:
                outcomes.append(
                    RuntimeReleaseOutcome(
                        component="active_count",
                        released=False,
                        error="missing dependency: router",
                    )
                )
            else:
                try:
                    release_active = getattr(
                        router, "decrement_active_request_count", None
                    )
                    if not callable(release_active):
                        release_active = getattr(router, "release_active", None)
                    if not callable(release_active):
                        raise AttributeError("missing active-count release method")
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
                            error=safe_exception_detail(
                                exc, stage="release:active_count"
                            ),
                        )
                    )

        if (
            self.quota_reservation_acquired
            and "quota_reservation" not in self._released_components
        ):
            if quota_estimator is None:
                outcomes.append(
                    RuntimeReleaseOutcome(
                        component="quota_reservation",
                        released=False,
                        error="missing dependency: quota_estimator",
                    )
                )
            else:
                try:
                    remove_reservation = getattr(
                        quota_estimator, "remove_reservation", None
                    )
                    if callable(remove_reservation):
                        result = remove_reservation(
                            self.account_name,
                            self.estimated_microdollars,
                            requests=1,
                            tokens=self.estimated_tokens,
                        )
                    else:
                        release_reservation = getattr(
                            quota_estimator, "release_reservation", None
                        )
                        if not callable(release_reservation):
                            raise AttributeError(
                                "missing quota-reservation release method"
                            )
                        result = release_reservation(
                            self.account_name, self.estimated_tokens
                        )
                    if inspect.isawaitable(result):
                        await result
                    self._released_components.add("quota_reservation")
                    outcomes.append(
                        RuntimeReleaseOutcome(
                            component="quota_reservation", released=True
                        )
                    )
                except Exception as exc:
                    outcomes.append(
                        RuntimeReleaseOutcome(
                            component="quota_reservation",
                            released=False,
                            error=safe_exception_detail(
                                exc, stage="release:quota_reservation"
                            ),
                        )
                    )

        if (
            self.health_probe_acquired
            and "health_probe" not in self._released_components
        ):
            if health_manager is None:
                outcomes.append(
                    RuntimeReleaseOutcome(
                        component="health_probe",
                        released=False,
                        error="missing dependency: health_manager",
                    )
                )
            else:
                try:
                    release_request = getattr(health_manager, "release_request", None)
                    if not callable(release_request):
                        raise AttributeError("missing health-probe release method")
                    result = release_request(self.account_name)
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
                            error=safe_exception_detail(
                                exc, stage="release:health_probe"
                            ),
                        )
                    )

        required = {
            component
            for component, acquired in (
                ("active_count", self.active_count_acquired),
                ("quota_reservation", self.quota_reservation_acquired),
                ("health_probe", self.health_probe_acquired),
            )
            if acquired
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


_UNSET = object()


def _terminal_scalar(value: object) -> object:
    """Return a bounded scalar suitable for terminal semantic comparison."""
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    enum_value = getattr(value, "value", _UNSET)
    if enum_value is not _UNSET and enum_value is not value:
        return _terminal_scalar(enum_value)
    return type(value).__name__


def _failure_effects_key(effects: object) -> tuple[object, ...] | None:
    if effects is None:
        return None
    return tuple(
        _terminal_scalar(getattr(effects, field_name, None))
        for field_name in (
            "retry",
            "retry_scope",
            "client_outcome",
            "account_effect",
            "model_effect",
            "circuit_penalty",
            "persist_backoff",
            "backoff_reason",
            "backoff_until",
            "release_probe_only",
            "evidence_class",
            "circuit_transition",
            "probe_convergence",
            "provider_attributable",
            "source",
            "response_signal",
            "retry_after_s",
        )
    )


def _normalized_usage_key(usage: object) -> tuple[object, ...] | None:
    if usage is None:
        return None
    return tuple(
        _terminal_scalar(getattr(usage, field_name, None))
        for field_name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
            "reasoning_tokens",
            "cache_counter_status",
        )
    )


def _runtime_lease_key(lease: AttemptRuntimeLease | None) -> tuple[object, ...] | None:
    if lease is None:
        return None
    return tuple(
        _terminal_scalar(getattr(lease, field_name, None))
        for field_name in (
            "account_name",
            "estimated_tokens",
            "estimated_microdollars",
            "active_count_acquired",
            "quota_reservation_acquired",
            "health_probe_acquired",
            "usage_outcome_required",
            "health_outcome_required",
            "account_runtime_outcome_required",
        )
    )


def _terminal_semantic_key(
    identity: FinalizationIdentity,
    outcome: str,
    finalization_data: object,
    runtime_lease: AttemptRuntimeLease | None,
    failure_effects: object,
) -> tuple[object, ...]:
    """Build a bounded, secret-free key for duplicate terminal commands."""
    identity_key = tuple(
        _terminal_scalar(getattr(identity, field_name))
        for field_name in (
            "proxy_request_id",
            "db_request_id",
            "attempt_id",
            "reservation_id",
            "account_id",
            "account_name",
            "provider_id",
            "model_id",
            "client_protocol",
            "upstream_protocol",
            "attempt_number",
        )
    )
    data_key = (
        None
        if finalization_data is None
        else tuple(
            _terminal_scalar(getattr(finalization_data, field_name, None))
            for field_name in (
                "outcome",
                "status_code",
                "error_class",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "thinking_characters",
                "bytes_emitted",
                "downstream_started",
                "bytes_received",
                "upstream_request_id",
                "release_reason",
                "health_already_applied",
                "provider_cost_microdollars",
                "provider_cost_source",
                "upstream_protocol",
                "transcoded",
            )
        ),
    )
    data_failure_effects = (
        _failure_effects_key(getattr(finalization_data, "failure_effects", None))
        if finalization_data is not None
        else None
    )
    return (
        identity_key,
        _terminal_scalar(outcome),
        data_key,
        _normalized_usage_key(
            getattr(finalization_data, "normalized_usage", None)
            if finalization_data is not None
            else None
        ),
        data_failure_effects,
        _failure_effects_key(failure_effects),
        _runtime_lease_key(runtime_lease),
    )


@dataclass
class RequestFinalizationJob:
    """Generation-owned finalization job for one selected attempt.

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
    _durable_result: Any = field(default=None, repr=False)  # noqa: ANN401
    _terminal_semantic_key: tuple[object, ...] = field(init=False, repr=False)

    # Dependencies — set after construction
    _finalizer: RequestFinalizer | None = None
    _selected: Any = None  # noqa: ANN401
    _effects_applier: EffectsApplier | None = None
    _router: Any = None  # noqa: ANN401
    _quota_estimator: Any = None  # noqa: ANN401
    _health_manager: Any = None  # noqa: ANN401
    _stream_diagnostics: Any = None  # noqa: ANN401

    def __post_init__(self) -> None:
        self._terminal_semantic_key = _terminal_semantic_key(
            self.identity,
            self.outcome,
            self.finalization_data,
            self.runtime_lease,
            self.failure_effects,
        )

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
        if self.is_complete:
            return
        self._retry_count += 1

    @property
    def result(self) -> FinalizationResult:
        """Return the latest structured terminal result."""
        return self._result

    def bind_terminal(
        self,
        outcome: str,
        finalization_data: Any = None,  # noqa: ANN401
        *,
        runtime_lease: AttemptRuntimeLease | None | object = _UNSET,
        failure_effects: FailureEffects | None | object = _UNSET,
    ) -> None:
        """Bind the first terminal command, rejecting conflicting reuse."""
        candidate_lease = (
            self.runtime_lease if runtime_lease is _UNSET else runtime_lease
        )
        candidate_effects = (
            self.failure_effects if failure_effects is _UNSET else failure_effects
        )
        candidate_key = _terminal_semantic_key(
            self.identity,
            outcome,
            finalization_data,
            candidate_lease,  # type: ignore[arg-type]
            candidate_effects,
        )
        if candidate_key != self._terminal_semantic_key:
            raise TerminalConflictError(
                "conflicting terminal submission for attempt",
                step="bind_terminal",
                request_id=self.request_id,
            )

    def bind_runtime_lease(self, runtime_lease: AttemptRuntimeLease | None) -> None:
        """Join duplicate registration only when ownership facts agree."""
        if runtime_lease is None:
            return
        candidate_key = _terminal_semantic_key(
            self.identity,
            self.outcome,
            self.finalization_data,
            runtime_lease,
            self.failure_effects,
        )
        if candidate_key != self._terminal_semantic_key:
            raise TerminalConflictError(
                "incompatible runtime ownership for duplicate terminal submission",
                step="bind_runtime_lease",
                request_id=self.request_id,
            )

    def bind_failure_effects(self, failure_effects: FailureEffects | None) -> None:
        """Join duplicate registration only when effects decisions agree."""
        candidate_key = _terminal_semantic_key(
            self.identity,
            self.outcome,
            self.finalization_data,
            self.runtime_lease,
            failure_effects,
        )
        if candidate_key != self._terminal_semantic_key:
            raise TerminalConflictError(
                "conflicting failure effects for duplicate terminal submission",
                step="bind_failure_effects",
                request_id=self.request_id,
            )

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
        """Run finalization, retaining the task for generation-owned completion.

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

        validate_identity = getattr(self._finalizer, "validate_terminal_identity", None)
        if validate_identity is not None:
            try:
                await validate_identity(self._selected, self.finalization_data)
            except Exception as exc:
                if type(exc).__name__ == "DurableTerminalConflictError":
                    raise TerminalConflictError(
                        str(exc),
                        step="durable_terminal_identity",
                        request_id=self.request_id,
                    ) from exc
                raise

        durable = await self._finalizer.finalize(self._selected, self.finalization_data)
        self._durable_result = durable
        if not durable.durable_converged:
            self._result = FinalizationResult(
                attempt_transitioned=durable.attempt_transitioned,
                request_transitioned=durable.request_transitioned,
                reservation_released=durable.reservation_transitioned,
                quota_reservation_removed=False,
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
            quota_reservation_removed=False,
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
        if self._finalizer is None or self._selected is None:
            outcomes = await self.runtime_lease.release_once(
                reason=self.outcome,
                router=self._router,
                quota_estimator=self._quota_estimator,
                health_manager=self._health_manager,
            )
            self._release_outcomes.extend(outcomes)
            runtime_obligations_complete = all(
                not required or marker in self.runtime_lease.completed_components
                for marker, required in (
                    ("usage", self.runtime_lease.usage_outcome_required),
                    ("health", self.runtime_lease.health_outcome_required),
                    (
                        "account_runtime",
                        self.runtime_lease.account_runtime_outcome_required,
                    ),
                )
            )
            if (
                any(not outcome.released for outcome in outcomes)
                or not self.runtime_lease.released
                or not runtime_obligations_complete
            ):
                self._refresh_runtime_result_from_lease()
                self._result = replace(
                    self._result,
                    runtime_cleanup_complete=False,
                    retryable=True,
                    detail="runtime cleanup incomplete",
                )
                raise RuntimeError("runtime cleanup incomplete")
        else:
            durable = self._durable_result
            if durable is None:
                raise FinalizationInvariantError(
                    "runtime convergence has no durable result",
                    step="runtime_release",
                    request_id=self.request_id,
                )
            try:
                await self._finalizer.apply_runtime_convergence(
                    selected=self._selected,
                    data=self.finalization_data,
                    durable=durable,
                    runtime_lease=self.runtime_lease,
                )
            except Exception:
                self._refresh_runtime_result_from_lease()
                self._result = replace(
                    self._result,
                    runtime_cleanup_complete=False,
                    retryable=True,
                    detail="runtime cleanup incomplete",
                )
                raise
        self._refresh_runtime_result_from_lease()

    def _refresh_runtime_result_from_lease(self) -> None:
        """Project current lease progress onto the structured result."""
        if self.runtime_lease is None:
            return
        components = self.runtime_lease.completed_components
        runtime_cleanup_complete = self.runtime_lease.released
        health_probe_complete = (
            not self.runtime_lease.health_probe_acquired or "health_probe" in components
        )
        health_outcome_complete = (
            self.runtime_lease.health_outcome_required is not True
            or "health" in components
        )
        self._result = replace(
            self._result,
            quota_reservation_removed="quota_reservation" in components,
            active_count_decremented="active_count" in components,
            health_released_or_recorded=(
                health_probe_complete and health_outcome_complete
            ),
            runtime_cleanup_complete=runtime_cleanup_complete,
            retryable=False if runtime_cleanup_complete else self._result.retryable,
            detail="" if runtime_cleanup_complete else self._result.detail,
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
        if self.identity.db_request_id is None or self.identity.attempt_id is None:
            raise FinalizationInvariantError(
                "selected finalization identity is incomplete",
                step="finalization_record",
                request_id=self.request_id,
            )
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


@dataclass(frozen=True, slots=True)
class TerminalCommandRecord:
    """Scalar-only diagnostic record for a completed terminal command."""

    command_kind: TerminalCommandKind
    proxy_request_id: str
    attempt_id: int | None
    progress: str
    attempt_count: int
    failure_count: int
    retry_count: int
    last_error_class: str | None
    last_error_message: str | None
    created_at: float
    updated_at: float


@dataclass(slots=True)
class TerminalCommand:
    """One retained command owned by the generation supervisor."""

    kind: TerminalCommandKind
    identity: FinalizationIdentity
    submission: TerminalCommandSubmission
    progress: TerminalCommandProgress
    runner: TerminalCommandRunner
    _health: str = "ready"
    _attempt_count: int = 0
    _failure_count: int = 0
    _retry_count: int = 0
    _last_error_class: str | None = None
    _last_error_message: str | None = None
    _created_at: float = field(default_factory=time.monotonic)
    _updated_at: float = field(default_factory=time.monotonic)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def key(self) -> str:
        """Return the stable kind-qualified command key."""
        attempt = (
            "unallocated"
            if self.identity.attempt_id is None
            else str(self.identity.attempt_id)
        )
        return f"{self.identity.proxy_request_id}:{attempt}:{self.kind}"

    @property
    def request_id(self) -> str:
        return self.identity.proxy_request_id

    @property
    def is_complete(self) -> bool:
        return self.progress.completed

    @property
    def health(self) -> str:
        return self._health

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def created_at(self) -> float:
        return self._created_at

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    def mark_retry_exhausted(self) -> None:
        self._health = "failed"

    def increment_retry_count(self) -> None:
        if self.is_complete:
            return
        self._retry_count += 1

    def get_task(self) -> asyncio.Task[None] | None:
        return self._task

    def set_task(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def begin_attempt(self) -> None:
        self._attempt_count += 1
        self._health = "running"
        self._updated_at = time.monotonic()

    def record_failure(self, exc: Exception) -> None:
        self._health = "retry_pending"
        self._failure_count += 1
        self._last_error_class = type(exc).__name__
        self._last_error_message = str(exc)[:200]
        self._updated_at = time.monotonic()

    def mark_completed(self) -> None:
        self._health = "completed"
        self._updated_at = time.monotonic()

    def to_record(self) -> TerminalCommandRecord:
        return TerminalCommandRecord(
            command_kind=self.kind,
            proxy_request_id=self.identity.proxy_request_id,
            attempt_id=self.identity.attempt_id,
            progress="completed" if self.is_complete else "incomplete",
            attempt_count=self._attempt_count,
            failure_count=self._failure_count,
            retry_count=self._retry_count,
            last_error_class=self._last_error_class,
            last_error_message=self._last_error_message,
            created_at=self._created_at,
            updated_at=self._updated_at,
        )


# ---------------------------------------------------------------------------
# Workstream F + J — Supervisor, retry queue, diagnostics
# ---------------------------------------------------------------------------

FINALIZATION_HISTORY_MAX = 64
DEFAULT_MAX_ACTIVE_JOBS = 256
DEFAULT_MAX_RETRY_AGE_S = 120.0
DEFAULT_RETRY_BACKOFF_BASE_S = 1.0
DEFAULT_RETRY_BACKOFF_CAP_S = 30.0


class RequestFinalizationSupervisor:
    """Generation-owned supervisor for all live terminal work.

    The bounded registry covers selected-request finalization,
    failed-attempt cleanup, and post-commit claim compensation. Every
    accepted command retains its generation until its typed progress record
    proves convergence. One retry heap, capacity limit, diagnostics surface,
    and shutdown drain cover all command kinds.
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
        retain_generation: Callable[[], None] | None = None,
        release_generation: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._effects_applier = effects_applier
        self._max_active_jobs = max_active_jobs
        self._max_retry_age_s = max_retry_age_s
        self._retry_backoff_base_s = retry_backoff_base_s
        self._retry_backoff_cap_s = retry_backoff_cap_s
        self._retain_generation = retain_generation
        self._release_generation = release_generation

        self._active_jobs: dict[str, RequestFinalizationJob] = {}
        self._active_commands: dict[str, TerminalCommand] = {}
        self._history: collections.deque[FinalizationRecord] = collections.deque(
            maxlen=FINALIZATION_HISTORY_MAX
        )
        self._command_history: collections.deque[TerminalCommandRecord] = (
            collections.deque(maxlen=FINALIZATION_HISTORY_MAX)
        )
        self._shutdown_adopted: dict[str, RequestFinalizationJob | TerminalCommand] = {}
        self._counters = FinalizationCounters()
        self._terminal_conflicts: int = 0
        self._retry_heap: list[tuple[float, int, str]] = []
        self._retry_sequence = 0
        self._retry_wakeup: asyncio.Event | None = None
        self._retry_scheduler_task: asyncio.Task[None] | None = None
        self._failed_jobs: collections.deque[FinalizationRecord] = collections.deque(
            maxlen=FINALIZATION_HISTORY_MAX
        )
        self._generation_reference_keys: set[str] = set()

    def bind_generation_reference_callbacks(
        self,
        *,
        retain_generation: Callable[[], None],
        release_generation: Callable[[], None],
    ) -> None:
        """Bind the owning generation's synchronous retirement callbacks."""
        if self._generation_reference_keys:
            raise RuntimeError("cannot rebind finalization ownership with active jobs")
        self._retain_generation = retain_generation
        self._release_generation = release_generation

    @property
    def active_count(self) -> int:
        return len(self._active_jobs) + len(self._active_commands)

    @staticmethod
    def _job_key(identity: FinalizationIdentity) -> str:
        attempt = (
            "unallocated" if identity.attempt_id is None else str(identity.attempt_id)
        )
        return f"{identity.proxy_request_id}:{attempt}:selected_request_finalization"

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
        job_key = self._job_key(identity)
        existing = self._active_jobs.get(job_key)
        if existing is not None:
            try:
                existing.bind_terminal(
                    outcome,
                    finalization_data,
                    runtime_lease=runtime_lease,
                    failure_effects=failure_effects,
                )
                existing.bind_runtime_lease(runtime_lease)
                existing.bind_failure_effects(failure_effects)
            except TerminalConflictError:
                self._terminal_conflicts += 1
                raise
            return existing

        if self.active_count >= self._max_active_jobs:
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

        retain_generation = self._retain_generation
        if retain_generation is not None:
            retain_generation()
            self._generation_reference_keys.add(job_key)
        try:
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
        except BaseException:
            if job_key in self._generation_reference_keys:
                self._generation_reference_keys.remove(job_key)
                if self._release_generation is not None:
                    self._release_generation()
            raise

    def _register_terminal_command(
        self,
        *,
        kind: TerminalCommandKind,
        identity: FinalizationIdentity,
        submission: TerminalCommandSubmission,
        progress: TerminalCommandProgress,
        runner: TerminalCommandRunner,
    ) -> TerminalCommand:
        """Register or join one typed non-request terminal command."""
        if kind == "selected_request_finalization":
            raise FinalizationInvariantError(
                "selected request finalization must use register_or_get",
                step="register_terminal_command",
                request_id=identity.proxy_request_id,
            )
        key = self._command_key(identity, kind)
        existing = self._active_commands.get(key)
        if existing is not None:
            compatible = self._commands_compatible(existing, kind, submission)
            if not compatible or type(existing.progress) is not type(progress):
                self._terminal_conflicts += 1
                raise TerminalConflictError(
                    "conflicting terminal command submission",
                    step="register_terminal_command",
                    request_id=identity.proxy_request_id,
                )
            return existing

        if self.active_count >= self._max_active_jobs:
            self._counters.saturation_rejections += 1
            raise FinalizationCapacityError(
                "finalization supervisor capacity exhausted for "
                f"{identity.proxy_request_id}"
            )

        retain_generation = self._retain_generation
        if retain_generation is not None:
            retain_generation()
            self._generation_reference_keys.add(key)
        try:
            command = TerminalCommand(
                kind=kind,
                identity=identity,
                submission=submission,
                progress=progress,
                runner=runner,
            )
            self._active_commands[key] = command
            self._counters.registered += 1
            self._ensure_retry_scheduler()
            return command
        except BaseException:
            if key in self._generation_reference_keys:
                self._generation_reference_keys.remove(key)
                if self._release_generation is not None:
                    self._release_generation()
            raise

    @staticmethod
    def _commands_compatible(
        existing: TerminalCommand,
        kind: TerminalCommandKind,
        submission: TerminalCommandSubmission,
    ) -> bool:
        """Compare ownership facts while ignoring bounded diagnostics."""
        if existing.kind != kind:
            return False
        current = existing.submission
        if kind == "failed_attempt_cleanup":
            if not isinstance(
                current, FailedAttemptCleanupSubmission
            ) or not isinstance(submission, FailedAttemptCleanupSubmission):
                return False
            return (
                current.identity == submission.identity
                and current.status_code == submission.status_code
                and current.error_class == submission.error_class
                and current.retry_category == submission.retry_category
                and current.failure_effects == submission.failure_effects
            )
        if kind == "claim_compensation":
            if not isinstance(current, ClaimCompensationSubmission) or not isinstance(
                submission, ClaimCompensationSubmission
            ):
                return False
            return (
                current.identity == submission.identity
                and current.account_name == submission.account_name
                and current.estimated_tokens == submission.estimated_tokens
                and current.estimated_microdollars == submission.estimated_microdollars
                and current.receipt == submission.receipt
            )
        return False

    def register_failed_attempt_cleanup(
        self,
        submission: FailedAttemptCleanupSubmission,
        runner: TerminalCommandRunner,
    ) -> TerminalCommand:
        """Register or join retryable failed-attempt cleanup."""
        return self._register_terminal_command(
            kind="failed_attempt_cleanup",
            identity=submission.identity,
            submission=submission,
            progress=FailedAttemptCleanupProgress(),
            runner=runner,
        )

    def register_claim_compensation(
        self,
        submission: ClaimCompensationSubmission,
        runner: TerminalCommandRunner,
    ) -> TerminalCommand:
        """Register or join post-commit claim compensation."""
        return self._register_terminal_command(
            kind="claim_compensation",
            identity=submission.identity,
            submission=submission,
            progress=ClaimCompensationProgress(),
            runner=runner,
        )

    @staticmethod
    def _command_key(identity: FinalizationIdentity, kind: TerminalCommandKind) -> str:
        attempt = (
            "unallocated" if identity.attempt_id is None else str(identity.attempt_id)
        )
        return f"{identity.proxy_request_id}:{attempt}:{kind}"

    def get_terminal_command(
        self,
        proxy_request_id: str,
        attempt_id: int | None,
        kind: TerminalCommandKind,
    ) -> TerminalCommand | None:
        """Return an active non-request command for cancellation/rejoin."""
        identity = FinalizationIdentity(
            proxy_request_id=proxy_request_id,
            db_request_id=None,
            attempt_id=attempt_id,
            reservation_id=None,
            account_id=None,
            account_name="",
            provider_id="",
            model_id="",
            client_protocol="",
            upstream_protocol="",
            attempt_number=None,
        )
        return self._active_commands.get(self._command_key(identity, kind))

    async def run_terminal_command(self, command: TerminalCommand) -> None:
        """Run a retained command while shielding it from waiter cancellation."""
        if command.is_complete:
            self._reconcile_terminal_command(command)
            return
        task = command.get_task()
        if task is None or task.done():
            task = asyncio.create_task(self._run_terminal_command(command))
            command.set_task(task)
            task.add_done_callback(
                lambda _: self._on_terminal_command_completion(command)
            )
        try:
            await asyncio.shield(task)
        except (Exception, asyncio.CancelledError):
            if command.is_complete:
                self._reconcile_terminal_command(command)
            raise
        if command.is_complete:
            self._reconcile_terminal_command(command)

    async def _run_terminal_command(self, command: TerminalCommand) -> None:
        command.begin_attempt()
        try:
            await command.runner(command.submission, command.progress)
            if not command.progress.completed:
                raise FinalizationInvariantError(
                    "terminal command returned before progress converged",
                    step="terminal_command_completion",
                    request_id=command.request_id,
                )
        except Exception as exc:
            command.record_failure(exc)
            raise
        else:
            command.mark_completed()

    def _on_terminal_command_completion(self, command: TerminalCommand) -> None:
        if command.is_complete:
            self._reconcile_terminal_command(command)
        elif command.health == "retry_pending":
            self._schedule_retry(command)

    def _ensure_retry_scheduler(self) -> None:
        """Start the one generation-owned retry timer when a loop is running."""

        if self._retry_scheduler_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._retry_wakeup = asyncio.Event()
        self._retry_scheduler_task = loop.create_task(self._retry_scheduler())

    def _schedule_retry(self, job: RequestFinalizationJob | TerminalCommand) -> None:
        key = (
            self._job_key(job.identity)
            if isinstance(job, RequestFinalizationJob)
            else job.key
        )
        active = (
            self._active_jobs.get(key)
            if isinstance(job, RequestFinalizationJob)
            else self._active_commands.get(key)
        )
        if active is not job or job.health == "failed":
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
        deadline = job.created_at + self._max_retry_age_s
        heapq.heappush(
            self._retry_heap,
            (min(time.monotonic() + delay, deadline), self._retry_sequence, key),
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
            job: RequestFinalizationJob | TerminalCommand | None = (
                self._active_jobs.get(key)
            )
            if job is None:
                job = self._active_commands.get(key)
            if job is None or job.is_complete:
                continue
            if time.monotonic() >= job.created_at + self._max_retry_age_s:
                job.mark_retry_exhausted()
                self._retire_exhausted_job(job)
                continue
            try:
                job.increment_retry_count()
                if job.is_complete:
                    continue
                if isinstance(job, RequestFinalizationJob):
                    await job.run()
                else:
                    await self.run_terminal_command(job)
            except Exception:
                continue

    def _retire_exhausted_job(
        self, job: RequestFinalizationJob | TerminalCommand
    ) -> None:
        """Retire an over-age job and release its operational ownership."""
        key = (
            self._job_key(job.identity)
            if isinstance(job, RequestFinalizationJob)
            else job.key
        )
        active = (
            self._active_jobs.get(key)
            if isinstance(job, RequestFinalizationJob)
            else self._active_commands.get(key)
        )
        if active is not job:
            return
        if isinstance(job, RequestFinalizationJob):
            self._active_jobs.pop(key, None)
            self._failed_jobs.append(job.to_record())
        else:
            self._active_commands.pop(key, None)
            self._command_history.append(job.to_record())
        self._release_generation_reference(key)
        if isinstance(job, RequestFinalizationJob):
            job.release_references()

    def _on_job_completion(self, job: RequestFinalizationJob) -> None:
        """Generation-owned completion callback and retry handoff."""
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
        job_key = self._job_key(job.identity)
        if job_key in self._active_jobs:
            del self._active_jobs[job_key]
        self._release_generation_reference(job_key)
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
        pending_jobs = [
            job for job in self._active_jobs.values() if not job.is_complete
        ]
        pending_commands = [
            command
            for command in self._active_commands.values()
            if not command.is_complete
        ]
        pending = [*pending_jobs, *pending_commands]
        if not pending:
            return 0

        per_job_timeout = max(timeout_s / max(len(pending), 1), 1.0)
        remaining = 0
        for job in pending:
            if job.is_complete:
                continue
            try:
                if isinstance(job, RequestFinalizationJob):
                    task = asyncio.create_task(job.run())
                else:
                    task = asyncio.create_task(self.run_terminal_command(job))
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
                    if isinstance(job, RequestFinalizationJob):
                        self._reconcile_job(job, source="drain")
                    else:
                        self._reconcile_terminal_command(job)
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
            self._release_generation_reference(req_id)
            job.release_references()
            self._history.append(job.to_record())
            self._counters.completed += 1
        for _key, command in list(self._active_commands.items()):
            if command.is_complete:
                self._reconcile_terminal_command(command)

    def _reconcile_terminal_command(self, command: TerminalCommand) -> None:
        """Move one converged non-request command to scalar history."""
        if not command.is_complete:
            return
        key = command.key
        if self._active_commands.get(key) is not command:
            return
        self._active_commands.pop(key, None)
        self._release_generation_reference(key)
        self._command_history.append(command.to_record())
        self._counters.completed += 1
        if command.failure_count > 0:
            self._counters.failures_recovered += 1

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
        for key, command in list(self._active_commands.items()):
            if not command.is_complete:
                self._shutdown_adopted[key] = command
                adopted += 1
        for req_id in self._shutdown_adopted:
            self._active_jobs.pop(req_id, None)
            self._active_commands.pop(req_id, None)
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

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of the supervisor."""
        active_by_progress: dict[str, int] = {}
        active_by_command_kind: dict[str, int] = {
            "selected_request_finalization": 0,
            "failed_attempt_cleanup": 0,
            "claim_compensation": 0,
        }
        command_kind_counts: dict[str, dict[str, int]] = {
            kind: {"active": 0, "retry_pending": 0, "completed_history_count": 0}
            for kind in active_by_command_kind
        }
        oldest_age: float | None = None
        now = time.monotonic()

        for job in self._active_jobs.values():
            active_by_command_kind["selected_request_finalization"] += 1
            command_kind_counts["selected_request_finalization"]["active"] += 1
            if job.health == "retry_pending":
                command_kind_counts["selected_request_finalization"][
                    "retry_pending"
                ] += 1
            progress = job.progress.value
            active_by_progress[progress] = active_by_progress.get(progress, 0) + 1
            age = now - job.created_at
            if oldest_age is None or age > oldest_age:
                oldest_age = age
        for command in self._active_commands.values():
            active_by_command_kind[command.kind] += 1
            command_kind_counts[command.kind]["active"] += 1
            if command.health == "retry_pending":
                command_kind_counts[command.kind]["retry_pending"] += 1
            progress = "completed" if command.is_complete else command.health
            active_by_progress[progress] = active_by_progress.get(progress, 0) + 1
            age = now - command.created_at
            if oldest_age is None or age > oldest_age:
                oldest_age = age
        for _record in self._history:
            command_kind_counts["selected_request_finalization"][
                "completed_history_count"
            ] += 1
        for record in self._command_history:
            command_kind_counts[record.command_kind]["completed_history_count"] += 1

        return {
            "active_count": self.active_count,
            "terminal_conflicts": self._terminal_conflicts,
            "registry_key": "proxy_request_id:attempt_id:command_kind",
            "history_count": len(self._history) + len(self._command_history),
            "shutdown_adopted_count": len(self._shutdown_adopted),
            "retry_pending_count": len(self._retry_heap),
            "failed_count": len(self._failed_jobs),
            "oldest_active_age_s": (
                round(oldest_age, 3) if oldest_age is not None else None
            ),
            "active_by_progress": active_by_progress,
            "active_by_command_kind": active_by_command_kind,
            "command_kind_counts": command_kind_counts,
            "completed_history_count": len(self._history) + len(self._command_history),
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

    def _release_generation_reference(self, job_key: str) -> None:
        """Release one accepted job's generation reference exactly once."""
        if job_key not in self._generation_reference_keys:
            return
        self._generation_reference_keys.remove(job_key)
        if self._release_generation is not None:
            self._release_generation()


@dataclass
class FinalizationCounters:
    """Scalar counters for the finalization supervisor."""

    registered: int = 0
    completed: int = 0
    failures_recovered: int = 0
    saturation_rejections: int = 0
    shutdown_adopted: int = 0
    startup_reconciled: int = 0
