"""Quota estimation module for tracking account usage and remaining capacity.

Includes a 5-tier cost estimation hierarchy:
1. Account/model EWMA
2. Global model EWMA
3. Model-family moving average
4. Configured per-model fallback
5. Global unknown-request fallback
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class QuotaWindow:
    """A rolling window of quota usage for an account."""

    window_seconds: float
    used_tokens: int = 0
    used_cost_microdollars: int = 0
    observations: list[tuple[float, int, int]] = field(default_factory=list)

    def add_observation(self, timestamp: float, tokens: int, cost: int) -> None:
        """Add an observation to the window."""
        self.observations.append((timestamp, tokens, cost))
        self.used_tokens += tokens
        self.used_cost_microdollars += cost
        self._prune_old_observations(timestamp)

    def _prune_old_observations(self, current_time: float) -> None:
        """Remove observations older than the window."""
        cutoff = current_time - self.window_seconds
        while self.observations and self.observations[0][0] < cutoff:
            _, tokens, cost = self.observations.pop(0)
            self.used_tokens -= tokens
            self.used_cost_microdollars -= cost

    def get_usage(self, current_time: float | None = None) -> tuple[int, int]:
        """Get current usage within the window."""
        if current_time is None:
            current_time = time.time()
        self._prune_old_observations(current_time)
        return self.used_tokens, self.used_cost_microdollars


@dataclass
class ManualOffset:
    """Manual adjustment to an account's quota usage."""

    tokens: int = 0
    cost_microdollars: int = 0
    reason: str = ""
    applied_at: float = field(default_factory=time.time)


@dataclass
class EWMAEstimate:
    """EWMA estimate for a specific (account, model) pair or global model."""

    alpha: float = 0.2
    estimate_cost_per_token: float = 0.0
    sample_count: int = 0
    last_updated: float = field(default_factory=time.time)

    def update(self, observed_cost_per_token: float) -> None:
        """Update the EWMA with a new observation."""
        if self.sample_count == 0:
            self.estimate_cost_per_token = observed_cost_per_token
        else:
            self.estimate_cost_per_token = (
                self.alpha * observed_cost_per_token
                + (1 - self.alpha) * self.estimate_cost_per_token
            )
        self.sample_count += 1
        self.last_updated = time.time()


@dataclass
class AccountQuota:
    """Quota state for a single account."""

    account_name: str
    daily_window: QuotaWindow = field(
        default_factory=lambda: QuotaWindow(window_seconds=86400.0)
    )
    hourly_window: QuotaWindow = field(
        default_factory=lambda: QuotaWindow(window_seconds=3600.0)
    )
    manual_offset: ManualOffset = field(default_factory=ManualOffset)
    weight: float = 1.0
    max_daily_cost_microdollars: int | None = None
    max_hourly_cost_microdollars: int | None = None

    def record_usage(
        self, tokens: int, cost_microdollars: int, timestamp: float | None = None
    ) -> None:
        """Record usage for this account."""
        if timestamp is None:
            timestamp = time.time()
        self.daily_window.add_observation(timestamp, tokens, cost_microdollars)
        self.hourly_window.add_observation(timestamp, tokens, cost_microdollars)

    def get_effective_usage(self) -> tuple[int, int]:
        """Get effective usage including manual offsets."""
        daily_tokens, daily_cost = self.daily_window.get_usage()
        hourly_tokens, hourly_cost = self.hourly_window.get_usage()
        return (
            daily_tokens + self.manual_offset.tokens,
            daily_cost + self.manual_offset.cost_microdollars,
        )

    def is_within_limits(self) -> bool:
        """Check if account is within quota limits."""
        _, daily_cost = self.daily_window.get_usage()
        _, hourly_cost = self.hourly_window.get_usage()

        if (
            self.max_daily_cost_microdollars is not None
            and daily_cost >= self.max_daily_cost_microdollars
        ):
            return False
        return not (
            self.max_hourly_cost_microdollars is not None
            and hourly_cost >= self.max_hourly_cost_microdollars
        )

    def get_remaining_capacity(self) -> float:
        """Get remaining capacity as a normalized score (0.0 to 1.0)."""
        if self.max_daily_cost_microdollars is None:
            return 1.0

        _, daily_cost = self.daily_window.get_usage()
        used_ratio = daily_cost / self.max_daily_cost_microdollars
        return max(0.0, 1.0 - used_ratio)


# Model family fallback costs (dollars per 1M tokens)
MODEL_FAMILY_FALLBACKS: dict[str, tuple[float, float]] = {
    "gpt-4": (30.0, 60.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-3.5-turbo": (0.5, 1.5),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-sonnet": (3.0, 15.0),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3.5-sonnet": (3.0, 15.0),
}

# Global unknown-request fallback (dollars per 1M tokens)
GLOBAL_FALLBACK = (3.0, 15.0)


@dataclass
class QuotaEstimator:
    """Estimates quota usage across all accounts.

    Includes 5-tier cost estimation hierarchy for reservation sizing.
    """

    accounts: dict[str, AccountQuota] = field(default_factory=dict)
    # Tier 1: account/model EWMA
    account_model_ewma: dict[str, dict[str, EWMAEstimate]] = field(default_factory=dict)
    # Tier 2: global model EWMA
    global_model_ewma: dict[str, EWMAEstimate] = field(default_factory=dict)
    # Tier 4: configured per-model overrides
    model_overrides: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Config
    default_safety_factor: float = 1.15
    default_unknown_reservation_microdollars: int = 1_000_000

    def record_usage(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
        timestamp: float | None = None,
        model_id: str | None = None,
    ) -> None:
        """Record usage for an account and update EWMA estimates."""
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        self.accounts[account_name].record_usage(tokens, cost_microdollars, timestamp)

        # Update EWMA estimates if model and token data available
        if model_id and tokens > 0 and cost_microdollars > 0:
            cost_per_token = cost_microdollars / tokens

            # Tier 1: account/model EWMA
            if account_name not in self.account_model_ewma:
                self.account_model_ewma[account_name] = {}
            am_key = model_id
            if am_key not in self.account_model_ewma[account_name]:
                self.account_model_ewma[account_name][am_key] = EWMAEstimate()
            self.account_model_ewma[account_name][am_key].update(cost_per_token)

            # Tier 2: global model EWMA
            if model_id not in self.global_model_ewma:
                self.global_model_ewma[model_id] = EWMAEstimate()
            self.global_model_ewma[model_id].update(cost_per_token)

    def estimate_cost(
        self,
        account_name: str,
        model_id: str,
        estimated_tokens: int,
    ) -> int:
        """Estimate cost using the 5-tier hierarchy.

        Returns estimated cost in microdollars.
        """
        # Tier 1: Account/model EWMA
        account_estimates = self.account_model_ewma.get(account_name, {})
        am_estimate = account_estimates.get(model_id)
        if am_estimate and am_estimate.sample_count >= 5:
            cost = int(
                estimated_tokens
                * am_estimate.estimate_cost_per_token
                * self.default_safety_factor
            )
            return max(cost, 1)

        # Tier 2: Global model EWMA
        global_est = self.global_model_ewma.get(model_id)
        if global_est and global_est.sample_count >= 5:
            cost = int(
                estimated_tokens
                * global_est.estimate_cost_per_token
                * self.default_safety_factor
            )
            return max(cost, 1)

        # Tier 3: Model-family moving average
        family_cost = self._get_family_estimate(model_id)
        if family_cost is not None:
            input_rate, output_rate = family_cost
            avg_rate = (input_rate + output_rate) / 2.0
            # Convert from dollars/1M tokens to microdollars/token
            cost_per_token = (avg_rate / 1_000_000) * 1_000_000
            cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
            return max(cost, 1)

        # Tier 4: Configured per-model override
        override = self.model_overrides.get(model_id)
        if override is not None:
            input_rate, output_rate = override
            avg_rate = (input_rate + output_rate) / 2.0
            cost_per_token = (avg_rate / 1_000_000) * 1_000_000
            cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
            return max(cost, 1)

        # Tier 5: Global unknown-request fallback
        avg_rate = (GLOBAL_FALLBACK[0] + GLOBAL_FALLBACK[1]) / 2.0
        cost_per_token = (avg_rate / 1_000_000) * 1_000_000
        cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
        return max(cost, 1)

    def _get_family_estimate(self, model_id: str) -> tuple[float, float] | None:
        """Get model-family fallback estimate."""
        model_lower = model_id.lower()
        for family, rates in MODEL_FAMILY_FALLBACKS.items():
            if family in model_lower:
                return rates
        return None

    def get_account_quota(self, account_name: str) -> AccountQuota | None:
        """Get quota state for an account."""
        return self.accounts.get(account_name)

    def get_account_weight(self, account_name: str) -> float:
        """Get account weight for weighted routing."""
        quota = self.accounts.get(account_name)
        if quota is None:
            return 1.0
        return quota.weight

    def set_account_weight(self, account_name: str, weight: float) -> None:
        """Set account weight for weighted routing."""
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        self.accounts[account_name].weight = weight

    def set_account_limits(
        self,
        account_name: str,
        max_daily_cost_microdollars: int | None = None,
        max_hourly_cost_microdollars: int | None = None,
    ) -> None:
        """Set quota limits for an account."""
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        quota = self.accounts[account_name]
        quota.max_daily_cost_microdollars = max_daily_cost_microdollars
        quota.max_hourly_cost_microdollars = max_hourly_cost_microdollars

    def apply_manual_offset(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
        reason: str = "",
    ) -> None:
        """Apply manual offset to an account's quota."""
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        quota = self.accounts[account_name]
        quota.manual_offset = ManualOffset(
            tokens=tokens,
            cost_microdollars=cost_microdollars,
            reason=reason,
        )

    def set_model_override(
        self, model_id: str, input_price: float, output_price: float
    ) -> None:
        """Set a configured per-model price override (Tier 4)."""
        self.model_overrides[model_id] = (input_price, output_price)

    def get_eligible_accounts(
        self, account_names: list[str]
    ) -> list[tuple[str, float]]:
        """Get eligible accounts with their remaining capacity scores."""
        eligible = []
        for name in account_names:
            quota = self.accounts.get(name)
            if quota is None:
                eligible.append((name, 1.0))
                continue
            if not quota.is_within_limits():
                continue
            capacity = quota.get_remaining_capacity()
            if capacity > 0:
                eligible.append((name, capacity))

        return sorted(eligible, key=lambda x: x[1], reverse=True)
