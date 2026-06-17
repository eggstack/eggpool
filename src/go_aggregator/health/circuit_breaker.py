"""Circuit breaker implementation for upstream failures."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker for an account/model."""

    failure_threshold: int = 5
    recovery_timeout: float = 300.0  # 5 minutes
    success_threshold: int = 3  # Successes needed to close circuit

    _state: CircuitState = CircuitState.CLOSED
    _failure_count: int = 0
    _success_count: int = 0
    _last_failure_time: float | None = None
    _last_state_change: float = time.time()

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        if self._state == CircuitState.OPEN and self._should_attempt_reset():
            self._state = CircuitState.HALF_OPEN
            self._last_state_change = time.time()
        return self._state

    def record_success(self) -> None:
        """Record a successful request."""
        # Check if OPEN circuit should transition to HALF_OPEN
        if self._state == CircuitState.OPEN and self._should_attempt_reset():
            self._state = CircuitState.HALF_OPEN
            self._last_state_change = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._success_count = 0
                self._last_state_change = time.time()
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed request."""
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._last_failure_time = time.time()
            self._last_state_change = time.time()
            self._success_count = 0
        elif self._state == CircuitState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._last_failure_time = time.time()
                self._last_state_change = time.time()

    def allow_request(self) -> bool:
        """Check if a request should be allowed."""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            return True  # Allow one test request
        # OPEN state
        if self._should_attempt_reset():
            self._state = CircuitState.HALF_OPEN
            self._last_state_change = time.time()
            return True
        return False

    def reset(self) -> None:
        """Reset the circuit breaker."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._last_state_change = time.time()

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset the circuit."""
        if self._last_failure_time is None:
            return False
        return time.time() - self._last_failure_time >= self.recovery_timeout

    def get_stats(self) -> dict[str, float | int | str]:
        """Get circuit breaker statistics."""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time or 0,
            "last_state_change": self._last_state_change,
        }
