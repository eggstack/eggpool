"""Health management for accounts and models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from eggpool.health.circuit_breaker import CircuitBreaker


class FailureCategory(StrEnum):
    """Normalized failure categories used across health and runtime state."""

    AUTHENTICATION_FAILED = "authentication_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONNECT_TIMEOUT = "connect_timeout"
    CONNECTION_FAILURE = "connection_failure"
    UPSTREAM_SERVER_ERROR = "upstream_server_error"
    PROTOCOL_ERROR = "protocol_error"
    CONTEXT_LIMIT_EXCEEDED = "context_limit_exceeded"
    UNKNOWN = "unknown"


def classify_failure_category(
    error_class: str | None,
    status_code: int | None = None,
) -> FailureCategory:
    """Map an error class string or status code to a normalized failure category.

    Real upstream HTTP 402 (Payment Required) responses map to
    QUOTA_EXHAUSTED regardless of error class, because providers often
    return generic 402 bodies without a structured error class. The
    quota-exhausted substring check is intentionally permissive to
    accept vendor-specific spellings (``quotaexhausted`` or
    ``quota_exhausted``).

    Phase 6 explicit handling:

    * HTTP 408 (request timeout) maps to ``CONNECT_TIMEOUT`` so a
      timed-out upstream request is treated as transport pressure
      rather than a client mistake.
    * HTTP 409 and 422 are intentionally mapped to ``UNKNOWN``; they
      are provider-specific and must not trigger account suppression
      unless the error class explicitly matches a known category.
    """
    if error_class is None and status_code is None:
        return FailureCategory.UNKNOWN
    if status_code == 402:
        return FailureCategory.QUOTA_EXHAUSTED
    if status_code == 408:
        return FailureCategory.CONNECT_TIMEOUT
    if status_code in (409, 422):
        # Provider-specific; do not blindly suppress account health
        # unless the body / error class explicitly identifies a
        # category the caller already knows about.
        return FailureCategory.UNKNOWN
    if error_class is None:
        # status_code already handled above; any other code without
        # an error class falls through to the generic catch-all below.
        if status_code is not None and 500 <= status_code < 600:
            return FailureCategory.UPSTREAM_SERVER_ERROR
        return FailureCategory.UNKNOWN
    ec = error_class.lower()
    if "contextlimitexceeded" in ec or "context_limit_exceeded" in ec:
        return FailureCategory.CONTEXT_LIMIT_EXCEEDED
    if "auth" in ec:
        return FailureCategory.AUTHENTICATION_FAILED
    if "quotaexhausted" in ec or "quota_exhausted" in ec:
        return FailureCategory.QUOTA_EXHAUSTED
    if "ratelimit" in ec or "rate_limit" in ec or status_code == 429:
        return FailureCategory.RATE_LIMITED
    if "modelunavailable" in ec or "model_not_found" in ec:
        return FailureCategory.MODEL_UNAVAILABLE
    if "connecttimeout" in ec or "connect_timeout" in ec:
        return FailureCategory.CONNECT_TIMEOUT
    conn_terms = (
        "connectionfailure",
        "connection_failure",
        "connectionerror",
        "connecterror",
    )
    if any(s in ec for s in conn_terms):
        return FailureCategory.CONNECTION_FAILURE
    if "timeout" in ec:
        return FailureCategory.CONNECT_TIMEOUT
    if "temporary" in ec or "transient" in ec:
        if status_code is not None and 500 <= status_code < 600:
            return FailureCategory.UPSTREAM_SERVER_ERROR
        return FailureCategory.UNKNOWN
    if status_code is not None and 500 <= status_code < 600:
        return FailureCategory.UPSTREAM_SERVER_ERROR
    return FailureCategory.UNKNOWN


@dataclass(slots=True)
class AccountHealth:
    """Health state for an account."""

    account_name: str
    is_healthy: bool = True
    last_check: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    # model_id -> disabled_until timestamp (``None`` means disabled
    # indefinitely, matching the account-level ``disabled_until``
    # convention).
    disabled_models: dict[str, float | None] = field(
        default_factory=dict[str, float | None]
    )
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
        if model_id not in self.disabled_models:
            return False
        until = self.disabled_models[model_id]
        if until is None:
            return True
        now = current_time if current_time is not None else time.time()
        if now >= until:
            self.disabled_models.pop(model_id, None)
            return False
        return True


@dataclass(slots=True)
class HealthManager:
    """Manages health state for all accounts."""

    _accounts: dict[str, AccountHealth] = field(
        default_factory=dict[str, AccountHealth]
    )

    def get_account_health(self, account_name: str) -> AccountHealth:
        """Get or create account health."""
        if account_name not in self._accounts:
            self._accounts[account_name] = AccountHealth(account_name=account_name)
        return self._accounts[account_name]

    def record_success(self, account_name: str, model_id: str | None = None) -> None:
        """Record a successful request."""
        health = self.get_account_health(account_name)
        health.consecutive_failures = 0
        health.last_check = time.time()
        # An in-flight request may succeed after an operator disables its
        # account. Do not let that completion undo an explicit disable.
        if (
            health.health_state != "authentication_failed"
            and not health.disabled_reason
        ):
            health.is_healthy = True
            health.health_state = "healthy"
            health.cooldown_until = 0.0
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
        elif reason in ("rate_limited", "quota_exhausted"):
            health.health_state = reason
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
        health.cooldown_until = time.time() + max(0.0, retry_after_seconds)
        health.health_state = "rate_limited"
        health.is_healthy = False

    def record_failure_with_policy(
        self,
        account_name: str,
        reason: str,
        *,
        retry_after: float | None = None,
    ) -> float | None:
        """Apply a reason-specific backoff policy to the account.

        Routes through the dedicated :mod:`eggpool.health.backoff`
        layer so the policy table is testable and reviewable in
        isolation. Returns the cooldown duration in seconds when one
        was applied, ``None`` otherwise (terminal or no-op reasons).

        Parameters
        ----------
        account_name:
            Target account. The method is a no-op for unknown
            accounts unless the call later creates one via the health
            manager's lookup.
        reason:
            A :class:`BackoffReason` value or string. Unknown reasons
            fall through to :meth:`record_failure` with the same
            ``reason``.
        retry_after:
            Optional upstream ``Retry-After`` value (seconds). Honored
            for ``rate_limited`` and ``quota_exhausted``.

        Notes
        -----
        ``model_unavailable`` is handled by calling
        :meth:`disable_model` for the account-wide scope only. The
        per-model key is intentionally not threaded through this
        method; the coordinator must invoke :meth:`disable_model`
        directly with the specific ``model_id`` so the catalog cache
        and health manager stay in sync.
        """
        # Local import keeps the health manager usable in environments
        # where the backoff module is intentionally mocked out.
        from eggpool.health.backoff import (
            BackoffPolicy,
            compute_backoff_seconds,
            get_backoff_policy,
        )

        if reason == "authentication_failed":
            self.record_failure(account_name, reason="authentication_failed")
            return None

        if reason == "context_limit_exceeded":
            # No account-level suppression for context-limit errors.
            return None

        policy: BackoffPolicy | None = get_backoff_policy(reason)
        if policy is None:
            # Unknown reason: fall back to the legacy record_failure
            # path so existing behavior is preserved.
            self.record_failure(account_name, reason=reason)
            return None

        if policy.base_delay <= 0 or policy.cap <= 0:
            # Terminal (auth) or zero-policy reasons already routed
            # above; this branch handles ``model_unavailable`` whose
            # policy exists but uses an empty base to signal "no
            # exponential backoff".
            if reason == "model_unavailable":
                # Per-model disable lives outside this method because
                # it requires the model id; the coordinator calls
                # ``disable_model`` directly.
                return None
            return None

        health = self.get_account_health(account_name)
        delay = compute_backoff_seconds(
            reason,
            consecutive_failures=health.consecutive_failures + 1,
            retry_after=retry_after,
            jitter=True,
        )
        if delay is None:
            return None

        if reason == "quota_exhausted":
            self.record_quota_exhausted(account_name, delay)
            return delay
        if reason == "rate_limited":
            self.record_rate_limit(account_name, delay)
            return delay

        # Generic transient category: bounded cooldown with the
        # generic "cooldown" health state so the runtime view stays
        # consistent with ``AccountRuntimeState``.
        health.cooldown_until = time.time() + delay
        health.health_state = "cooldown"
        health.is_healthy = False
        health.consecutive_failures += 1
        health.last_check = time.time()
        return delay

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
        health.disabled_until = (
            None
            if duration_seconds is None
            else time.time() + max(0.0, duration_seconds)
        )

    def enable_account(self, account_name: str) -> None:
        """Enable an account."""
        health = self.get_account_health(account_name)
        health.is_healthy = True
        health.health_state = "healthy"
        health.disabled_until = None
        health.disabled_reason = ""
        health.cooldown_until = 0.0
        health.circuit_breaker.reset()

    def disable_model(
        self,
        account_name: str,
        model_id: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Disable a model for an account.

        Mirrors :meth:`disable_account`: when ``duration_seconds`` is
        provided, the disable expires after that interval. ``None``
        disables the model indefinitely until :meth:`enable_model` is
        called.
        """
        health = self.get_account_health(account_name)
        health.disabled_models[model_id] = (
            None
            if duration_seconds is None
            else time.time() + max(0.0, duration_seconds)
        )

    def enable_model(self, account_name: str, model_id: str) -> None:
        """Enable a model for an account."""
        health = self.get_account_health(account_name)
        health.disabled_models.pop(model_id, None)

    def prune_disabled_models(
        self,
        account_name: str,
        advertised_models: set[str],
    ) -> int:
        """Drop ``disabled_models`` entries no longer advertised upstream.

        ``disabled_models`` keys are ``model_id`` strings. An entry whose
        model has been withdrawn upstream is dead weight — the model is
        no longer reachable through this account, so the disable is
        moot. Returns the number of entries removed (intended for log
        diagnostics). Unknown accounts and empty ``disabled_models``
        are no-ops.
        """
        health = self._accounts.get(account_name)
        if health is None or not health.disabled_models:
            return 0
        stale = [mid for mid in health.disabled_models if mid not in advertised_models]
        for mid in stale:
            del health.disabled_models[mid]
        return len(stale)

    def _refresh_transient_state(self, health: AccountHealth) -> None:
        """Restore transient health states after cooldown expiration."""
        now = time.time()
        if (
            health.cooldown_until > 0
            and now >= health.cooldown_until
            and health.health_state in ("quota_exhausted", "rate_limited", "cooldown")
        ):
            health.health_state = "healthy"
            health.is_healthy = True
            health.cooldown_until = 0
        # Clear expired timed disable, but do not override terminal states
        # such as authentication_failed which require explicit enable.
        if (
            health.disabled_until is not None
            and now >= health.disabled_until
            and health.health_state != "authentication_failed"
        ):
            health.disabled_until = None
            health.disabled_reason = ""
            health.is_healthy = True

    def is_account_healthy(self, account_name: str) -> bool:
        """Check if an account is healthy."""
        health = self.get_account_health(account_name)
        self._refresh_transient_state(health)
        return health.is_healthy and not health.is_disabled()

    def is_model_healthy(self, account_name: str, model_id: str) -> bool:
        """Check if a model is healthy for an account.

        Uses the non-mutating :meth:`CircuitBreaker.can_request` so
        readiness probes and candidate enumeration never consume the
        half-open probe slot.
        """
        if not self.is_account_healthy(account_name):
            return False
        health = self.get_account_health(account_name)
        return (
            not health.is_disabled()
            and not health.is_model_disabled(model_id)
            and health.circuit_breaker.can_request()
        )

    def try_acquire_request(self, account_name: str, model_id: str) -> bool:
        """Attempt to acquire a circuit-breaker probe slot for dispatch.

        Calls the mutating :meth:`CircuitBreaker.allow_request` which
        transitions OPEN → HALF_OPEN and sets the in-flight flag when
        appropriate.  Returns ``True`` when the request may proceed,
        ``False`` when the circuit breaker rejects it (account should
        be excluded from this routing round).
        """
        if not self.is_account_healthy(account_name):
            return False
        health = self.get_account_health(account_name)
        if health.is_disabled() or health.is_model_disabled(model_id):
            return False
        return health.circuit_breaker.allow_request()

    def release_request(self, account_name: str) -> None:
        """Release the circuit-breaker half-open probe slot for an account.

        Call this when a request that acquired a probe slot terminates
        through a path that does not call :meth:`record_success` or
        :meth:`record_failure` (client cancellation, client error,
        rate-limit/quota cooldown, model disabled).
        """
        health = self.get_account_health(account_name)
        health.circuit_breaker.release_probe()

    def get_healthy_accounts(self, account_names: list[str]) -> list[str]:
        """Get list of healthy accounts."""
        return [name for name in account_names if self.is_account_healthy(name)]

    def get_health_stats(self, account_name: str) -> dict[str, Any]:
        """Get health statistics for an account."""
        health = self.get_account_health(account_name)
        self._refresh_transient_state(health)
        for model_id in list(health.disabled_models):
            health.is_model_disabled(model_id)
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
