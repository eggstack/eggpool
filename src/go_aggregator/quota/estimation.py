"""Quota estimation module for tracking account usage and remaining capacity.

Includes a 5-tier cost estimation hierarchy:
1. Account/model EWMA
2. Global model EWMA
3. Configured per-model override
4. Model-family moving average
5. Global unknown-request fallback

Optionally uses persisted UsageWindowRepository for actual 5h/7d/30d
usage from SQLite instead of in-memory hourly/daily windows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.db.repositories import UsageWindowRepository

logger = logging.getLogger(__name__)


@dataclass
class QuotaWindow:
    """A rolling window of quota usage for an account."""

    window_seconds: float
    used_tokens: int = 0
    used_cost_microdollars: int = 0
    observations: deque[tuple[float, int, int]] = field(
        default_factory=deque[tuple[float, int, int]]
    )

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
            _, tokens, cost = self.observations.popleft()
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
    """Manual adjustment to an account's quota usage.

    .. deprecated::
        The scorer does not read this field. Per-window explicit offsets
        (five_hour_offset, weekly_offset, monthly_offset) are the canonical
        adjustment mechanism. This class is retained for backward compatibility
        only.
    """

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
class PersistedWindowSnapshot:
    """Snapshot of persisted window usage for an account."""

    account_id: int
    cost_5h: int = 0
    cost_7d: int = 0
    cost_30d: int = 0
    loaded_at: float = field(default_factory=time.time)


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
    capacity_5h_microdollars: int | None = None
    capacity_7d_microdollars: int | None = None
    capacity_30d_microdollars: int | None = None
    persisted_snapshot: PersistedWindowSnapshot | None = None
    five_hour_offset: int = 0
    weekly_offset: int = 0
    monthly_offset: int = 0
    reserved_cost: int = 0

    def record_usage(
        self,
        tokens: int,
        cost_microdollars: int,
        timestamp: float | None = None,
    ) -> None:
        """Record usage for this account."""
        if timestamp is None:
            timestamp = time.time()
        self.daily_window.add_observation(timestamp, tokens, cost_microdollars)
        self.hourly_window.add_observation(timestamp, tokens, cost_microdollars)

    def get_effective_usage(self) -> tuple[int, int]:
        """Get effective usage from the daily window.

        ``manual_offset`` is deprecated and is intentionally not
        included here so that the reported usage matches what the
        routing scorer observes.
        """
        daily_tokens, daily_cost = self.daily_window.get_usage()
        _hourly_tokens, _hourly_cost = self.hourly_window.get_usage()
        return daily_tokens, daily_cost

    def is_within_limits(self) -> bool:
        """Check if account is within configured quota capacity thresholds.

        Used as a scoring input (utilization > 1.0) rather than a hard
        eligibility gate.  Above-capacity accounts remain routable;
        upstream ``quota_exhausted`` health makes them temporarily
        ineligible when authoritative.
        """
        cost_5h = (
            self.get_persisted_cost_5h() + self.five_hour_offset + self.reserved_cost
        )
        cost_7d = self.get_persisted_cost_7d() + self.weekly_offset
        cost_30d = self.get_persisted_cost_30d() + self.monthly_offset

        # Consider exhausted if any configured capacity is exceeded
        if (
            self.capacity_5h_microdollars is not None
            and cost_5h >= self.capacity_5h_microdollars
        ):
            return False
        if (
            self.capacity_7d_microdollars is not None
            and cost_7d >= self.capacity_7d_microdollars
        ):
            return False
        return not (
            self.capacity_30d_microdollars is not None
            and cost_30d >= self.capacity_30d_microdollars
        )

    def get_remaining_capacity(self) -> float:
        """Get remaining capacity as a normalized score (0.0 to 1.0).

        Returns the minimum remaining capacity across all configured
        windows so that a tight short-term capacity limits routing
        even when long-term capacity is ample.
        """
        capacities: list[float] = []

        if self.capacity_5h_microdollars:
            cost_5h = (
                self.get_persisted_cost_5h()
                + self.five_hour_offset
                + self.reserved_cost
            )
            used_ratio = cost_5h / self.capacity_5h_microdollars
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_7d_microdollars:
            cost_7d = self.get_persisted_cost_7d() + self.weekly_offset
            used_ratio = cost_7d / self.capacity_7d_microdollars
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_30d_microdollars:
            cost_30d = self.get_persisted_cost_30d() + self.monthly_offset
            used_ratio = cost_30d / self.capacity_30d_microdollars
            capacities.append(max(0.0, 1.0 - used_ratio))

        if not capacities:
            return 1.0

        return min(capacities)

    def get_persisted_cost_5h(self) -> int:
        """Get 5h cost from persisted snapshot, or fall back to hourly window."""
        if self.persisted_snapshot is not None:
            return self.persisted_snapshot.cost_5h
        _, cost = self.hourly_window.get_usage()
        return cost

    def get_persisted_cost_7d(self) -> int:
        """Get 7d cost from persisted snapshot, or 0 if unavailable."""
        if self.persisted_snapshot is not None:
            return self.persisted_snapshot.cost_7d
        _, cost = self.daily_window.get_usage()
        return cost

    def get_persisted_cost_30d(self) -> int:
        """Get 30d cost from persisted snapshot, or 0 if unavailable."""
        if self.persisted_snapshot is not None:
            return self.persisted_snapshot.cost_30d
        _, cost = self.daily_window.get_usage()
        return cost


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
    Optionally uses persisted UsageWindowRepository for actual usage windows.
    """

    accounts: dict[str, AccountQuota] = field(default_factory=dict[str, AccountQuota])
    # Tier 1: account/model EWMA
    account_model_ewma: dict[str, dict[str, EWMAEstimate]] = field(
        default_factory=dict[str, dict[str, EWMAEstimate]]
    )
    # Tier 2: global model EWMA
    global_model_ewma: dict[str, EWMAEstimate] = field(
        default_factory=dict[str, EWMAEstimate]
    )
    # Tier 3: configured per-model overrides
    model_overrides: dict[str, tuple[float, float]] = field(
        default_factory=dict[str, tuple[float, float]]
    )
    # Config
    default_safety_factor: float = 1.15
    default_unknown_reservation_microdollars: int = 1_000_000
    # Optional persisted window repo for loading actual usage
    _usage_window_repo: UsageWindowRepository | None = field(default=None, repr=False)
    # In-memory reservation tracking for scorer
    _account_reserved_cost: dict[str, int] = field(default_factory=dict[str, int])
    # Serializes record_usage + persisted_snapshot updates so concurrent
    # finalizers cannot interleave between the two updates and lose cost
    # increments.
    _snapshot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def set_usage_window_repo(self, repo: UsageWindowRepository) -> None:
        """Set the persisted usage window repository."""
        self._usage_window_repo = repo

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

            # Tier 2: Global model EWMA
            if model_id not in self.global_model_ewma:
                self.global_model_ewma[model_id] = EWMAEstimate()
            self.global_model_ewma[model_id].update(cost_per_token)

    async def record_usage_and_snapshot(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
        model_id: str | None = None,
    ) -> None:
        """Record usage and atomically refresh the persisted snapshot.

        Combines :meth:`record_usage` with the per-window snapshot
        increment so concurrent finalizers cannot interleave between
        the two updates and lose cost increments.
        """
        async with self._snapshot_lock:
            self.record_usage(
                account_name,
                tokens=tokens,
                cost_microdollars=cost_microdollars,
                model_id=model_id,
            )
            quota = self.get_account_quota(account_name)
            if quota is not None and quota.persisted_snapshot is not None:
                safe_cost = max(0, cost_microdollars)
                quota.persisted_snapshot.cost_5h += safe_cost
                quota.persisted_snapshot.cost_7d += safe_cost
                quota.persisted_snapshot.cost_30d += safe_cost

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

        # Tier 3: Configured per-model override
        override = self.model_overrides.get(model_id)
        if override is not None:
            input_rate, output_rate = override
            avg_rate = (input_rate + output_rate) / 2.0
            cost_per_token = avg_rate
            cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
            return max(cost, 1)

        # Tier 4: Model-family moving average
        family_cost = self._get_family_estimate(model_id)
        if family_cost is not None:
            input_rate, output_rate = family_cost
            avg_rate = (input_rate + output_rate) / 2.0
            # avg_rate is dollars/1M tokens; numerically equal to
            # microdollars/token ($/1M tokens = microdollars/token),
            # so we can use it directly as cost_per_token in
            # microdollars/token.
            cost_per_token = avg_rate
            cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
            return max(cost, 1)

        # Tier 5: Global unknown-request fallback
        avg_rate = (GLOBAL_FALLBACK[0] + GLOBAL_FALLBACK[1]) / 2.0
        cost_per_token = avg_rate
        cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
        return max(cost, 1)

    def _get_family_estimate(self, model_id: str) -> tuple[float, float] | None:
        """Get model-family fallback estimate."""
        model_lower = model_id.lower()
        # Match longer (more specific) family names first so e.g.
        # "gpt-4o-mini" resolves to the mini rate rather than the
        # generic "gpt-4" rate.
        for family in sorted(MODEL_FAMILY_FALLBACKS, key=len, reverse=True):
            if family in model_lower:
                return MODEL_FAMILY_FALLBACKS[family]
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
        capacity_7d_microdollars: int | None = None,
        capacity_5h_microdollars: int | None = None,
        capacity_30d_microdollars: int | None = None,
    ) -> None:
        """Set quota limits for an account."""
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        quota = self.accounts[account_name]
        quota.capacity_7d_microdollars = capacity_7d_microdollars
        quota.capacity_5h_microdollars = capacity_5h_microdollars
        quota.capacity_30d_microdollars = capacity_30d_microdollars

    def configure_account_policy(
        self,
        account_name: str,
        *,
        weight: float,
        capacity_5h_microdollars: int,
        capacity_7d_microdollars: int,
        capacity_30d_microdollars: int,
        offset_5h_microdollars: int,
        offset_7d_microdollars: int,
        offset_30d_microdollars: int,
    ) -> None:
        """Configure the full quota policy for an account.

        Creates the account quota if it does not already exist, then sets
        all seven values: weight, three capacities, and three offsets.
        """
        if account_name not in self.accounts:
            self.accounts[account_name] = AccountQuota(account_name=account_name)
        quota = self.accounts[account_name]
        quota.weight = weight
        quota.capacity_5h_microdollars = capacity_5h_microdollars
        quota.capacity_7d_microdollars = capacity_7d_microdollars
        quota.capacity_30d_microdollars = capacity_30d_microdollars
        quota.five_hour_offset = offset_5h_microdollars
        quota.weekly_offset = offset_7d_microdollars
        quota.monthly_offset = offset_30d_microdollars

    def apply_manual_offset(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
        reason: str = "",
    ) -> None:
        """Apply manual offset to an account's quota.

        .. deprecated::
            The scorer does not read manual_offset. Use per-window explicit
            offsets (five_hour_offset, weekly_offset, monthly_offset) instead.
        """
        raise NotImplementedError(
            "QuotaEstimator.apply_manual_offset is deprecated; use per-window "
            "explicit offsets (five_hour_offset, weekly_offset, monthly_offset) "
            "via configure_account_policy instead."
        )

    def set_model_override(
        self, model_id: str, input_price: float, output_price: float
    ) -> None:
        """Set a configured per-model price override (Tier 4)."""
        self.model_overrides[model_id] = (input_price, output_price)

    async def add_reservation(self, account_name: str, cost: int) -> None:
        """Track an active reservation's estimated cost for scoring."""
        async with self._snapshot_lock:
            if account_name not in self._account_reserved_cost:
                self._account_reserved_cost[account_name] = 0
            self._account_reserved_cost[account_name] += cost
            # Keep AccountQuota in sync for eligibility checks
            quota = self.get_account_quota(account_name)
            if quota is not None:
                quota.reserved_cost = self._account_reserved_cost[account_name]

    async def remove_reservation(self, account_name: str, cost: int) -> None:
        """Remove a reservation's cost from tracking."""
        async with self._snapshot_lock:
            if account_name in self._account_reserved_cost:
                self._account_reserved_cost[account_name] = max(
                    0, self._account_reserved_cost[account_name] - cost
                )
                # Keep AccountQuota in sync for eligibility checks
                quota = self.get_account_quota(account_name)
                if quota is not None:
                    quota.reserved_cost = self._account_reserved_cost[account_name]

    async def get_account_reserved_cost(self, account_name: str) -> int:
        """Get total reserved cost for an account from active reservations."""
        async with self._snapshot_lock:
            return self._account_reserved_cost.get(account_name, 0)

    async def load_persisted_windows(
        self, offsets: dict[str, dict[str, int]] | None = None
    ) -> None:
        """Load persisted usage windows from the database.

        Args:
            offsets: Optional mapping of account_name -> per-window offsets.
                     Keys: "five_hour", "weekly", "monthly".
        """
        if self._usage_window_repo is None:
            return
        from go_aggregator.db.repositories import AccountRepository

        acct_repo = AccountRepository(
            self._usage_window_repo._db  # pyright: ignore[reportPrivateUsage]
        )
        enabled = await acct_repo.list_enabled()
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        for acct in enabled:
            name = acct["name"]
            if name not in self.accounts:
                self.accounts[name] = AccountQuota(account_name=name)
            self.accounts[name].weight = acct.get("weight", 1.0)
            windows = await self._usage_window_repo.get_usage_windows(
                acct["id"], now_iso
            )
            self.accounts[name].persisted_snapshot = PersistedWindowSnapshot(
                account_id=acct["id"],
                cost_5h=windows["5h"],
                cost_7d=windows["7d"],
                cost_30d=windows["30d"],
            )
            if offsets and name in offsets:
                acct_offsets = offsets[name]
                self.accounts[name].five_hour_offset = acct_offsets.get("five_hour", 0)
                self.accounts[name].weekly_offset = acct_offsets.get("weekly", 0)
                self.accounts[name].monthly_offset = acct_offsets.get("monthly", 0)
        logger.info("Loaded persisted usage windows for %d accounts", len(enabled))

    def get_eligible_accounts(
        self, account_names: list[str]
    ) -> list[tuple[str, float]]:
        """Get eligible accounts with their remaining capacity scores.

        Above-capacity accounts are included with zero remaining
        capacity so they can still be scored; upstream quota_exhausted
        health makes them temporarily ineligible when authoritative.
        """
        eligible: list[tuple[str, float]] = []
        for name in account_names:
            quota = self.accounts.get(name)
            if quota is None:
                eligible.append((name, 1.0))
                continue
            capacity = quota.get_remaining_capacity()
            eligible.append((name, capacity))

        return sorted(eligible, key=lambda x: x[1], reverse=True)

    def get_window_costs(self, account_name: str) -> tuple[int, int, int]:
        """Get the 5h, 7d, 30d costs for an account.

        Uses persisted snapshot when available, falls back to
        in-memory windows.
        """
        quota = self.accounts.get(account_name)
        if quota is None:
            return 0, 0, 0
        return (
            quota.get_persisted_cost_5h(),
            quota.get_persisted_cost_7d(),
            quota.get_persisted_cost_30d(),
        )
