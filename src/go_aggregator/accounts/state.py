"""Account runtime state and health management."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AccountRuntimeState:
    """Mutable runtime state for an account."""

    name: str
    enabled: bool = True
    weight: float = 1.0

    health_state: str = "healthy"
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    active_request_count: int = 0
    reserved_microdollars: int = 0

    # Account-specific model availability: model_id -> available
    model_availability: dict[str, bool] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=dict
    )

    def is_eligible(self) -> bool:
        """Check if account is eligible for routing."""
        if not self.enabled:
            return False
        if self.health_state in ("authentication_failed", "quota_exhausted"):
            return False
        return self.cooldown_until <= time.time()

    def record_success(self) -> None:
        """Record a successful request."""
        self.consecutive_failures = 0
        self.last_success_at = time.time()
        if self.health_state == "cooldown":
            self.health_state = "healthy"

    def record_failure(self, error_class: str) -> None:
        """Record a failed request and update health state."""
        self.consecutive_failures += 1
        self.last_failure_at = time.time()

        if error_class == "authentication":
            self.health_state = "authentication_failed"
        elif error_class == "quota_exhausted":
            self.health_state = "quota_exhausted"
            self.cooldown_until = time.time() + 300  # 5 min cooldown
        elif error_class in (
            "rate_limited",
            "connect_timeout",
            "read_timeout",
            "connection_failure",
        ):
            self.health_state = "cooldown"
            # Exponential backoff: 30s, 60s, 120s, ... max 10 min
            backoff = min(30 * (2 ** (self.consecutive_failures - 1)), 600)
            self.cooldown_until = time.time() + backoff
        # upstream_server_error, internal_error, etc. - no cooldown

    def reset_health(self) -> None:
        """Reset health state to healthy."""
        self.health_state = "healthy"
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
