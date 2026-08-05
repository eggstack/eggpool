"""Restart-safe database failure diagnostics.

Runtime SQLite invalidation is not recoverable inside a live worker. This
module keeps a small compatibility diagnostic surface for callers that need to
observe the fail-closed transition; systemd restart plus startup integrity and
crash reconciliation is the only recovery path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.db.connection import DatabaseLifecycleState

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.health.writable_probe import DatabaseWritableProbe
    from eggpool.models.config import DatabaseRecoveryConfig


@dataclass(frozen=True, slots=True)
class RecoveryAttemptResult:
    """Bounded diagnostic for the terminal worker failure."""

    state: DatabaseLifecycleState
    attempt_number: int
    started_at_monotonic: float
    completed_at_monotonic: float
    error_class: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Bounded diagnostic snapshot; it is never an admission authority."""

    lifecycle_state: DatabaseLifecycleState
    total_invalidation_count: int
    invalidation_reasons_by_class: tuple[tuple[str, int], ...]
    recovery_attempts: int
    successful_recoveries: int
    failed_recoveries: int
    last_attempt: RecoveryAttemptResult | None
    active_waiters: int
    pending_ambiguous_operations: int
    active_recovery: bool
    last_completed_at_monotonic: float | None
    time_to_recover_s: float | None
    failed_closed_reason: str | None
    admission_admitted: bool


@dataclass
class DatabaseRecoveryController:
    """Observe database invalidation and retain the worker-fatal contract."""

    db: Database
    config: DatabaseRecoveryConfig
    readiness_probe: DatabaseWritableProbe | None = None
    on_recovery_complete: Any = None  # noqa: ANN401
    _state: DatabaseLifecycleState = field(
        init=False, default=DatabaseLifecycleState.DISCONNECTED
    )
    _total_invalidation_count: int = field(init=False, default=0)
    _reasons: dict[str, int] = field(
        init=False, default_factory=lambda: dict[str, int]()
    )
    _failed_closed_reason: str | None = field(init=False, default=None)
    _last_attempt: RecoveryAttemptResult | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.db.attach_recovery_controller(self)
        self._state = self.db.lifecycle_state

    @property
    def state(self) -> DatabaseLifecycleState:
        """Return the observed lifecycle state."""
        return self._state

    @property
    def admission_admitted(self) -> bool:
        """Return the cached database admission fact."""
        return self.db.writes_admitted and self.db.reads_admitted

    async def handle_invalidation(
        self,
        *,
        reason: str,
        reason_class: str = "other",
    ) -> None:
        """Record failure and keep admission closed until worker restart."""
        self._total_invalidation_count += 1
        self._reasons[reason_class] = self._reasons.get(reason_class, 0) + 1
        self._state = DatabaseLifecycleState.FAILED_CLOSED
        self._failed_closed_reason = reason[:200]
        now = time.monotonic()
        self._last_attempt = RecoveryAttemptResult(
            state=self._state,
            attempt_number=1,
            started_at_monotonic=now,
            completed_at_monotonic=now,
            error_class=reason_class,
            error_message=reason[:200],
        )

    async def wait_for_ready(self, timeout_s: float = 0.0) -> bool:
        """Never wait for same-process recovery; restart is required."""
        del timeout_s
        return False

    def recover_blocking(self, timeout_s: float = 0.0) -> bool:
        """Compatibility method that reports no in-process recovery."""
        del timeout_s
        return False

    def snapshot(self) -> RecoverySnapshot:
        """Return bounded diagnostics without exposing request data."""
        return RecoverySnapshot(
            lifecycle_state=self._state,
            total_invalidation_count=self._total_invalidation_count,
            invalidation_reasons_by_class=tuple(sorted(self._reasons.items())),
            recovery_attempts=1 if self._last_attempt is not None else 0,
            successful_recoveries=0,
            failed_recoveries=1 if self._last_attempt is not None else 0,
            last_attempt=self._last_attempt,
            active_waiters=0,
            pending_ambiguous_operations=0,
            active_recovery=False,
            last_completed_at_monotonic=(
                self._last_attempt.completed_at_monotonic
                if self._last_attempt is not None
                else None
            ),
            time_to_recover_s=0.0 if self._last_attempt is not None else None,
            failed_closed_reason=self._failed_closed_reason,
            admission_admitted=self.admission_admitted,
        )

    async def shutdown(self) -> None:
        """Release the diagnostic binding during orderly shutdown."""
        self._state = DatabaseLifecycleState.SHUTTING_DOWN
