"""Quota estimation module for tracking account usage and remaining capacity."""

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


@dataclass
class QuotaEstimator:
    """Estimates quota usage across all accounts."""

    accounts: dict[str, AccountQuota] = field(default_factory=dict)

    def record_usage(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
        timestamp: float | None = None,
    ) -> None:
        """Record usage for an account."""
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        self.accounts[account_name].record_usage(tokens, cost_microdollars, timestamp)

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

    def get_eligible_accounts(
        self, account_names: list[str]
    ) -> list[tuple[str, float]]:
        """Get eligible accounts with their remaining capacity scores.

        Returns list of (account_name, remaining_capacity) tuples sorted by
        remaining capacity (highest first).
        """
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
