"""Account runtime state and health management."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Default cooldown durations used when the caller does not supply an
# explicit value. These mirror the configured defaults in
# ``RoutingConfig`` and exist only as a safety net for tests or
# command-line tools that instantiate ``AccountRuntimeState``
# directly. Production code paths should pass the configured cooldown
# explicitly so the runtime state stays in lock-step with the
# authoritative ``HealthManager``.
DEFAULT_QUOTA_EXHAUSTED_COOLDOWN_SECONDS = 300.0
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_MAX_SECONDS = 3600.0  # 1 hour max backoff for rate limits


def _failure_backoff(consecutive_failures: int) -> float:
    """Return capped exponential backoff without constructing huge integers."""
    if consecutive_failures <= 1:
        return DEFAULT_BACKOFF_BASE_SECONDS
    max_doublings = int(
        DEFAULT_BACKOFF_MAX_SECONDS / DEFAULT_BACKOFF_BASE_SECONDS
    ).bit_length()
    doublings = min(consecutive_failures - 1, max_doublings)
    return min(
        DEFAULT_BACKOFF_BASE_SECONDS * (2**doublings),
        DEFAULT_BACKOFF_MAX_SECONDS,
    )


@dataclass(slots=True)
class AccountRuntimeState:
    """Mutable runtime state for an account."""

    name: str
    enabled: bool = True
    weight: float = 1.0
    routing_priority: int = 0

    health_state: str = "healthy"
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_failure_category: str = ""

    active_request_count: int = 0
    reserved_microdollars: int = 0

    # Account-specific model availability: model_id -> available
    model_availability: dict[str, bool] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=dict
    )

    def refresh_transient_state(self, now: float | None = None) -> None:
        """Clear transient cooldown status when it expires.

        Auto-recoverable states ("rate_limited", "cooldown") are
        cleared either when the configured cooldown has elapsed or
        when no cooldown is set. "quota_exhausted" recovers
        automatically when the cooldown expires.
        """
        if now is None:
            now = time.time()
        if self.health_state == "quota_exhausted":
            if self.cooldown_until > 0 and now >= self.cooldown_until:
                self.health_state = "healthy"
                self.cooldown_until = 0.0
                self.consecutive_failures = 0
            return
        if self.health_state in ("rate_limited", "cooldown") and (
            self.cooldown_until == 0.0 or now >= self.cooldown_until
        ):
            self.health_state = "healthy"
            self.cooldown_until = 0.0
            self.consecutive_failures = 0

    def is_eligible(self) -> bool:
        """Check if account is eligible for routing."""
        if not self.enabled:
            return False
        self.refresh_transient_state()
        if self.health_state in (
            "authentication_failed",
            "quota_exhausted",
            "cooldown",
            "rate_limited",
        ):
            return False
        return self.cooldown_until <= time.time()

    def record_success(self) -> None:
        """Record a successful request."""
        self.consecutive_failures = 0
        self.last_success_at = time.time()
        self.last_failure_category = ""
        if self.health_state in ("cooldown", "rate_limited", "quota_exhausted"):
            self.health_state = "healthy"
            self.cooldown_until = 0.0

    def record_failure(
        self,
        error_class: str,
        *,
        cooldown_seconds: float | None = None,
        rate_limit_retry_after: float | None = None,
    ) -> None:
        """Record a failed request and update health state.

        ``cooldown_seconds`` is the configured quota-exhausted cooldown
        duration; the same value used by the authoritative
        ``HealthManager`` must be passed here so the two cooldown
        representations cannot diverge. ``rate_limit_retry_after`` is
        the parsed ``Retry-After`` value for 429 responses; when
        supplied, it takes precedence over the exponential backoff
        schedule. Authentication failures remain terminal until
        explicitly reset.
        """
        self.consecutive_failures += 1
        self.last_failure_at = time.time()

        if self.last_failure_category and error_class != self.last_failure_category:
            self.consecutive_failures = 1
        self.last_failure_category = error_class

        if error_class in ("authentication_failed", "authentication"):
            self.health_state = "authentication_failed"
        elif error_class == "quota_exhausted":
            self.health_state = "quota_exhausted"
            duration = (
                cooldown_seconds
                if cooldown_seconds is not None
                else DEFAULT_QUOTA_EXHAUSTED_COOLDOWN_SECONDS
            )
            self.cooldown_until = time.time() + duration
        elif error_class == "rate_limited":
            # Mirror HealthManager.record_rate_limit so both state
            # machines expose the same label for the same event.
            self.health_state = "rate_limited"
            if rate_limit_retry_after is not None:
                self.cooldown_until = time.time() + max(0.0, rate_limit_retry_after)
            else:
                self.cooldown_until = time.time() + _failure_backoff(
                    self.consecutive_failures
                )
        elif error_class in (
            "connect_timeout",
            "read_timeout",
            "connection_failure",
            "connection_error",
        ):
            self.health_state = "cooldown"
            self.cooldown_until = time.time() + _failure_backoff(
                self.consecutive_failures
            )
        # upstream_server_error, protocol_error, unknown, etc. - no cooldown

    def reset_health(self) -> None:
        """Reset health state to healthy."""
        self.health_state = "healthy"
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
        self.last_failure_category = ""
        # Per-model disable map must be cleared alongside the
        # account-level state so reset_health mirrors the same
        # reset semantics as HealthManager.enable_account.
        self.model_availability.clear()
