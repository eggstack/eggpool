"""Failover management for handling upstream failures."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from go_aggregator.retry.classification import RetryableError, RetryClassifier


@dataclass
class AttemptRecord:
    """Record of a request attempt."""

    attempt_number: int
    account_name: str
    model_id: str
    status_code: int
    error_category: str
    timestamp: float
    success: bool


@dataclass
class FailoverState:
    """State for failover tracking."""

    account_name: str
    model_id: str | None = None
    disabled_until: float | None = None
    disabled_reason: str = ""
    consecutive_failures: int = 0
    last_failure_time: float | None = None
    circuit_open: bool = False
    circuit_open_until: float | None = None

    def is_disabled(self, current_time: float | None = None) -> bool:
        """Check if this account/model is currently disabled."""
        if current_time is None:
            current_time = time.time()

        if self.disabled_until is not None and current_time < self.disabled_until:
            return True

        if self.circuit_open and self.circuit_open_until is not None:
            if current_time < self.circuit_open_until:
                return True
            else:
                self.circuit_open = False
                self.circuit_open_until = None

        return False


@dataclass
class FailoverManager:
    """Manages failover and retry decisions."""

    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    auth_disable_duration_seconds: float = 3600.0  # 1 hour
    circuit_breaker_threshold: int = 5
    circuit_breaker_duration_seconds: float = 300.0  # 5 minutes

    _classifier: RetryClassifier = field(default_factory=RetryClassifier)
    _failover_states: dict[str, FailoverState] = field(
        default_factory=dict[str, FailoverState]
    )
    _attempt_history: list[AttemptRecord] = field(default_factory=list[AttemptRecord])

    def should_retry(
        self, account_name: str, model_id: str, attempt_number: int
    ) -> bool:
        """Determine if we should retry with this account."""
        if attempt_number > self.max_retries:
            return False

        state = self._get_state(account_name, model_id)
        return not state.is_disabled()

    def record_attempt(
        self,
        account_name: str,
        model_id: str,
        status_code: int,
        attempt_number: int,
        success: bool,
        headers: dict[str, str] | None = None,
    ) -> RetryableError:
        """Record an attempt and return classification."""
        error = self._classifier.classify(status_code, headers)
        error.account_name = account_name
        error.model_id = model_id

        record = AttemptRecord(
            attempt_number=attempt_number,
            account_name=account_name,
            model_id=model_id,
            status_code=status_code,
            error_category=error.category.value,
            timestamp=time.time(),
            success=success,
        )
        self._attempt_history.append(record)

        # Update failover state
        state = self._get_state(account_name, model_id)
        if success:
            state.consecutive_failures = 0
            state.circuit_open = False
            state.circuit_open_until = None
        else:
            state.consecutive_failures += 1
            state.last_failure_time = time.time()

            # Check if we should disable the account
            if error.should_disable_account:
                state.disabled_until = time.time() + self.auth_disable_duration_seconds
                state.disabled_reason = error.message

            # Check if we should disable the model (404)
            if error.should_remove_model and model_id:
                state.disabled_until = time.time() + self.auth_disable_duration_seconds
                state.disabled_reason = error.message

            # Check if we should open circuit breaker
            if state.consecutive_failures >= self.circuit_breaker_threshold:
                state.circuit_open = True
                state.circuit_open_until = (
                    time.time() + self.circuit_breaker_duration_seconds
                )

        return error

    def get_retry_delay(self, error: RetryableError) -> float:
        """Get retry delay for an error."""
        if error.retry_after is not None:
            return error.retry_after
        return self.retry_backoff_seconds

    def get_attempt_history(
        self, account_name: str | None = None, model_id: str | None = None
    ) -> list[AttemptRecord]:
        """Get attempt history, optionally filtered."""
        history = self._attempt_history
        if account_name:
            history = [r for r in history if r.account_name == account_name]
        if model_id:
            history = [r for r in history if r.model_id == model_id]
        return history

    def clear_attempt_history(self) -> None:
        """Clear attempt history."""
        self._attempt_history.clear()

    def disable_account(
        self, account_name: str, reason: str, duration_seconds: float | None = None
    ) -> None:
        """Manually disable an account."""
        state = self._get_state(account_name)
        state.disabled_until = time.time() + (
            duration_seconds or self.auth_disable_duration_seconds
        )
        state.disabled_reason = reason

    def enable_account(self, account_name: str) -> None:
        """Manually enable an account."""
        state = self._get_state(account_name)
        state.disabled_until = None
        state.disabled_reason = ""
        state.circuit_open = False
        state.circuit_open_until = None
        state.consecutive_failures = 0

    def disable_model(self, account_name: str, model_id: str) -> None:
        """Disable a specific model for an account."""
        state = self._get_state(account_name, model_id)
        state.disabled_until = time.time() + self.auth_disable_duration_seconds
        state.disabled_reason = f"Model {model_id} not found"

    def is_account_disabled(self, account_name: str) -> bool:
        """Check if an account is disabled."""
        for key, state in self._failover_states.items():
            if key.startswith(f"{account_name}:") and state.is_disabled():
                return True
        return False

    def is_model_disabled(self, account_name: str, model_id: str) -> bool:
        """Check if a model is disabled for an account."""
        state = self._get_state(account_name, model_id)
        return state.is_disabled()

    def _get_state(
        self, account_name: str, model_id: str | None = None
    ) -> FailoverState:
        """Get or create failover state."""
        key = f"{account_name}:{model_id or 'all'}"
        if key not in self._failover_states:
            self._failover_states[key] = FailoverState(
                account_name=account_name, model_id=model_id
            )
        return self._failover_states[key]
