"""Health management for accounts and models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from go_aggregator.health.circuit_breaker import CircuitBreaker


@dataclass
class AccountHealth:
    """Health state for an account."""

    account_name: str
    is_healthy: bool = True
    last_check: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    disabled_models: set[str] = field(default_factory=set)
    disabled_until: float | None = None
    disabled_reason: str = ""
    cooldown_until: float = 0.0
    health_state: str = "healthy"

    def is_disabled(self, current_time: float | None = None) -> bool:
        """Check if account is currently disabled."""
        if current_time is None:
            current_time = time.time()

        disabled = (
            self.disabled_until is not None and current_time < self.disabled_until
        )
        cooled = self.cooldown_until > 0 and current_time < self.cooldown_until
        return disabled or cooled

    def is_model_disabled(
        self, model_id: str, current_time: float | None = None
    ) -> bool:
        """Check if a model is disabled for this account."""
        if self.is_disabled(current_time):
            return True
        return model_id in self.disabled_models


@dataclass
class HealthManager:
    """Manages health state for all accounts."""

    _accounts: dict[str, AccountHealth] = field(default_factory=dict)
    _model_health: dict[str, dict[str, AccountHealth]] = field(
        default_factory=dict
    )  # model_id -> account_name -> health

    def get_account_health(self, account_name: str) -> AccountHealth:
        """Get or create account health."""
        if account_name not in self._accounts:
            self._accounts[account_name] = AccountHealth(account_name=account_name)
        return self._accounts[account_name]

    def record_success(self, account_name: str, model_id: str | None = None) -> None:
        """Record a successful request."""
        health = self.get_account_health(account_name)
        health.consecutive_failures = 0
        health.is_healthy = True
        health.health_state = "healthy"
        health.last_check = time.time()
        health.circuit_breaker.record_success()

    def record_failure(
        self,
        account_name: str,
        model_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record a failed request."""
        health = self.get_account_health(account_name)
        health.consecutive_failures += 1
        health.last_check = time.time()
        health.circuit_breaker.record_failure()
        if reason == "authentication_failed":
            health.health_state = "authentication_failed"
            health.is_healthy = False

    def record_quota_exhausted(
        self,
        account_name: str,
        cooldown_seconds: float,
    ) -> None:
        """Place account into a bounded quota-exhausted cooldown."""
        health = self.get_account_health(account_name)
        health.health_state = "quota_exhausted"
        health.cooldown_until = time.time() + cooldown_seconds
        health.is_healthy = False

    def record_rate_limit(self, account_name: str, retry_after_seconds: float) -> None:
        """Record a rate limit with explicit cooldown."""
        health = self.get_account_health(account_name)
        health.cooldown_until = time.time() + retry_after_seconds
        health.health_state = "rate_limited"

    def disable_account(
        self,
        account_name: str,
        reason: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Disable an account."""
        health = self.get_account_health(account_name)
        health.is_healthy = False
        health.disabled_reason = reason
        if duration_seconds:
            health.disabled_until = time.time() + duration_seconds

    def enable_account(self, account_name: str) -> None:
        """Enable an account."""
        health = self.get_account_health(account_name)
        health.is_healthy = True
        health.disabled_until = None
        health.disabled_reason = ""
        health.circuit_breaker.reset()

    def disable_model(
        self,
        account_name: str,
        model_id: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Disable a model for an account."""
        health = self.get_account_health(account_name)
        health.disabled_models.add(model_id)

    def enable_model(self, account_name: str, model_id: str) -> None:
        """Enable a model for an account."""
        health = self.get_account_health(account_name)
        health.disabled_models.discard(model_id)

    def is_account_healthy(self, account_name: str) -> bool:
        """Check if an account is healthy."""
        health = self.get_account_health(account_name)
        if (
            not health.is_healthy
            and health.cooldown_until > 0
            and not health.is_disabled()
        ):
            health.is_healthy = True
        return health.is_healthy and not health.is_disabled()

    def is_model_healthy(self, account_name: str, model_id: str) -> bool:
        """Check if a model is healthy for an account."""
        health = self.get_account_health(account_name)
        return (
            health.is_healthy
            and not health.is_disabled()
            and not health.is_model_disabled(model_id)
            and health.circuit_breaker.allow_request()
        )

    def get_healthy_accounts(self, account_names: list[str]) -> list[str]:
        """Get list of healthy accounts."""
        return [name for name in account_names if self.is_account_healthy(name)]

    def get_health_stats(
        self, account_name: str
    ) -> dict[str, float | int | str | bool]:
        """Get health statistics for an account."""
        health = self.get_account_health(account_name)
        return {
            "account_name": health.account_name,
            "is_healthy": health.is_healthy,
            "last_check": health.last_check,
            "consecutive_failures": health.consecutive_failures,
            "circuit_breaker": health.circuit_breaker.get_stats(),
            "disabled_models": list(health.disabled_models),
            "disabled_until": health.disabled_until or 0,
            "disabled_reason": health.disabled_reason,
        }
