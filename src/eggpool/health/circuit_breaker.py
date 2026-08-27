"""Circuit breaker implementation for upstream failures."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass(slots=True)
class CircuitBreaker:
    """Circuit breaker for an account/model."""

    failure_threshold: int = 5
    recovery_timeout: float = 300.0  # 5 minutes
    success_threshold: int = 1  # One successful half-open probe closes it

    # Monotonic default: recovery timeouts are duration math and must not
    # jump when the wall clock is corrected (NTP, manual adjustment).
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)

    _state: CircuitState = CircuitState.CLOSED
    _failure_count: int = 0
    _success_count: int = 0
    _last_failure_time: float | None = None
    _last_state_change: float = field(init=False)
    _half_open_in_flight: bool = False
    _half_open_acquired_at: float | None = None

    def __post_init__(self) -> None:
        self._last_state_change = self.clock()

    @property
    def state(self) -> CircuitState:
        """Get current circuit state without mutating it."""
        return self._state

    def record_success(self) -> None:
        """Record a successful request."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            # Clear the in-flight flag after each successful probe
            # so the next probe can proceed.  If the threshold is
            # reached, close the circuit; otherwise remain half-open.
            self._half_open_in_flight = False
            self._half_open_acquired_at = None
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._success_count = 0
                self._last_state_change = self.clock()
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed request."""
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._last_failure_time = self.clock()
            self._last_state_change = self.clock()
            self._success_count = 0
            self._half_open_in_flight = False
            self._half_open_acquired_at = None
        elif self._state == CircuitState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._last_failure_time = self.clock()
                self._last_state_change = self.clock()

    def allow_request(self) -> bool:
        """Check if a request should be allowed and acquire the probe slot.

        Mutates state: transitions OPEN to HALF_OPEN and sets the
        half-open in-flight flag.  Use :meth:`can_request` for
        read-only health checks that must not consume the probe slot.
        """
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            # Allow only one test request at a time; subsequent
            # concurrent requests must wait for the test to complete.
            if self._half_open_in_flight and not self._probe_slot_is_stale():
                return False
            self._acquire_probe_slot()
            return True
        # OPEN state
        if self._should_attempt_reset():
            self._state = CircuitState.HALF_OPEN
            self._last_state_change = self.clock()
            self._acquire_probe_slot()
            return True
        return False

    def release_probe(self) -> None:
        """Release a half-open probe slot without recording success or failure.

        Use this when a request that consumed a half-open probe slot
        terminates through a path that does not warrant a circuit-breaker
        success or failure record (e.g. client cancellation, client error,
        rate-limit cooldown, quota-exhausted cooldown, or model-disabled).
        """
        self._half_open_in_flight = False
        self._half_open_acquired_at = None

    def _acquire_probe_slot(self) -> None:
        """Mark the single half-open probe slot as taken."""
        self._half_open_in_flight = True
        self._half_open_acquired_at = self.clock()

    def _probe_slot_is_stale(self) -> bool:
        """Report whether an in-flight probe slot was abandoned.

        The probe slot is released by :meth:`record_success`,
        :meth:`record_failure`, or :meth:`release_probe`.  A request that
        terminates through a path skipping all three would otherwise wedge
        the breaker forever, so a slot held beyond ``recovery_timeout`` is
        reclaimed.
        """
        if self._half_open_acquired_at is None:
            return True
        return self.clock() - self._half_open_acquired_at >= self.recovery_timeout

    def can_request(self) -> bool:
        """Check if a request would be allowed without mutating state.

        Returns the same decision as :meth:`allow_request` but never
        transitions OPEN → HALF_OPEN or sets the half-open in-flight
        flag.  Suitable for readiness probes, model listing, and
        candidate enumeration.
        """
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            return not self._half_open_in_flight or self._probe_slot_is_stale()
        # OPEN state
        return self._should_attempt_reset()

    def reset(self) -> None:
        """Reset the circuit breaker."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._last_state_change = self.clock()
        self._half_open_in_flight = False
        self._half_open_acquired_at = None

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset the circuit."""
        if self._last_failure_time is None:
            return False
        return self.clock() - self._last_failure_time >= self.recovery_timeout

    def get_stats(self) -> dict[str, float | int | str]:
        """Get circuit breaker statistics."""
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time or 0,
            "last_state_change": self._last_state_change,
        }
