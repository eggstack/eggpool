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
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eggpool.constants import (
    MAX_REQUEST_COST_MICRODOLLARS,
    clamp_request_cost_microdollars,
    clamp_sqlite_integer,
)

if TYPE_CHECKING:
    from eggpool.db.repositories import UsageWindowRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QuotaWindow:
    """Rolling-window quota observations for one cost dimension.

    The ``observations`` deque holds ``(timestamp, tokens, cost_microdollars)``
    tuples. The deque is bounded by the ``window_seconds`` horizon: entries
    older than ``current_time - window_seconds`` are pruned on every write.

    Ordered timestamps are the normal path: expiry removes entries from the
    left edge and updates the cached totals incrementally. An out-of-order
    observation uses a bounded rebuild so clock corrections and test
    backfills remain correct without making every normal update expensive.
    """

    window_seconds: float
    used_tokens: int = 0
    used_cost_microdollars: int = 0
    observations: deque[tuple[float, int, int]] = field(
        default_factory=deque[tuple[float, int, int]]
    )
    _last_observation_timestamp: float | None = field(default=None, repr=False)

    def add_observation(self, timestamp: float, tokens: int, cost: int) -> None:
        """Add an observation to the window."""
        observation = (timestamp, tokens, cost)
        if (
            self._last_observation_timestamp is None
            or timestamp >= self._last_observation_timestamp
        ):
            self.observations.append(observation)
            self.used_tokens += tokens
            self.used_cost_microdollars += cost
            self._last_observation_timestamp = timestamp
            self._prune_old_observations(timestamp)
            return

        # Rare slow path for clock skew/backfills. Re-sort the bounded
        # retained window once, then restore the same cached-total invariant
        # used by the ordered path.
        logger.debug(
            "Out-of-order quota observation: timestamp=%s last=%s",
            timestamp,
            self._last_observation_timestamp,
        )
        observations = [*self.observations, observation]
        observations.sort(key=lambda item: item[0])
        self.observations = deque(observations)
        self._last_observation_timestamp = max(
            self._last_observation_timestamp,
            timestamp,
        )
        self._rebuild_totals_and_prune(self._last_observation_timestamp)

    def _prune_old_observations(self, current_time: float) -> None:
        """Remove observations older than the window from the left edge."""
        cutoff = current_time - self.window_seconds
        while self.observations and self.observations[0][0] < cutoff:
            _timestamp, tokens, cost = self.observations.popleft()
            self.used_tokens = max(0, self.used_tokens - tokens)
            self.used_cost_microdollars = max(0, self.used_cost_microdollars - cost)

    def _rebuild_totals_and_prune(self, current_time: float) -> None:
        """Rebuild totals for the bounded out-of-order slow path."""
        cutoff = current_time - self.window_seconds
        retained = [
            observation for observation in self.observations if observation[0] >= cutoff
        ]
        self.observations = deque(retained)
        self.used_tokens = max(0, sum(item[1] for item in retained))
        self.used_cost_microdollars = max(0, sum(item[2] for item in retained))

    def get_usage(self, current_time: float | None = None) -> tuple[int, int]:
        """Get current usage within the window.

        Not a pure read: expired observations are pruned (and the cached
        totals updated) before the totals are returned, so the caller always
        sees the window as of ``current_time``. Pruning is idempotent, and the
        single canonical event-loop thread makes the mutation race-free.
        """
        if current_time is None:
            current_time = time.time()
        self._prune_old_observations(current_time)
        return self.used_tokens, self.used_cost_microdollars


@dataclass(slots=True)
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


@dataclass(slots=True)
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


@dataclass(slots=True)
class PersistedWindowSnapshot:
    """Snapshot of persisted window usage for an account.

    The ``cost_*`` fields are retained for audit and dashboard display
    only; they are NOT consumed by the routing scorer. Routing uses the
    ``request_count_*`` and ``token_count_*`` fields because cost is
    unreliable across providers (zero reported, untrusted heuristics,
    unit confusion, etc.) and the metrics we actually care about for
    load balancing are request count and token count.
    """

    account_id: int
    cost_5h: int = 0
    cost_7d: int = 0
    cost_30d: int = 0
    request_count_5h: int = 0
    request_count_7d: int = 0
    request_count_30d: int = 0
    token_count_5h: int = 0
    token_count_7d: int = 0
    token_count_30d: int = 0
    loaded_at: float = field(default_factory=time.time)


# Soft default capacities for the request/token utilization signal. When
# an operator has not configured an explicit capacity, these defaults
# let the scorer produce a meaningful utilization ratio so load stays
# balanced across peer accounts. They are intentionally generous
# (roughly one request per ~7 seconds sustained for 5h; ~2B tokens in
# 30d) so a default-config deployment cannot accidentally cap a
# healthy account at zero.
DEFAULT_REQUEST_CAPACITY_5H = 2500
DEFAULT_REQUEST_CAPACITY_7D = 35_000
DEFAULT_REQUEST_CAPACITY_30D = 150_000
DEFAULT_TOKEN_CAPACITY_5H = 500_000_000
DEFAULT_TOKEN_CAPACITY_7D = 7_000_000_000
DEFAULT_TOKEN_CAPACITY_30D = 30_000_000_000


@dataclass(slots=True)
class AccountQuota:
    """Quota state for a single account.

    Cost microdollar fields are retained for audit / dashboard display
    only for scoring. The routing scorer consumes ``request_count`` and
    ``token_count`` from the persisted snapshot (and their
    offsets/reservations) because cost is unreliable and we want load
    balancing to track the metrics we actually care about: requests served
    and tokens processed. Local hard-cap checks include active reservations
    in every configured horizon so concurrent work cannot oversubscribe a
    longer-lived limit.
    """

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
    capacity_5h_requests: int | None = None
    capacity_7d_requests: int | None = None
    capacity_30d_requests: int | None = None
    capacity_5h_tokens: int | None = None
    capacity_7d_tokens: int | None = None
    capacity_30d_tokens: int | None = None
    persisted_snapshot: PersistedWindowSnapshot | None = None
    five_hour_offset: int = 0
    weekly_offset: int = 0
    monthly_offset: int = 0
    request_offset_5h: int = 0
    request_offset_7d: int = 0
    request_offset_30d: int = 0
    token_offset_5h: int = 0
    token_offset_7d: int = 0
    token_offset_30d: int = 0
    reserved_cost: int = 0
    reserved_requests: int = 0
    reserved_tokens: int = 0

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

        Checks all three dimensions — cost, request count, and token
        count — across the configured windows because the scorer
        treats each as an independent pressure signal.
        """
        cost_5h = (
            self.get_persisted_cost_5h() + self.five_hour_offset + self.reserved_cost
        )
        cost_7d = self.get_persisted_cost_7d() + self.weekly_offset
        cost_30d = self.get_persisted_cost_30d() + self.monthly_offset
        requests_5h = (
            self.get_persisted_request_count_5h()
            + self.reserved_requests
            + self.request_offset_5h
        )
        requests_7d = self.get_persisted_request_count_7d() + self.request_offset_7d
        requests_30d = self.get_persisted_request_count_30d() + self.request_offset_30d
        tokens_5h = (
            self.get_persisted_token_count_5h()
            + self.reserved_tokens
            + self.token_offset_5h
        )
        tokens_7d = self.get_persisted_token_count_7d() + self.token_offset_7d
        tokens_30d = self.get_persisted_token_count_30d() + self.token_offset_30d

        # Exhaustion semantics: ``>=`` matches the existing pre-extension
        # behaviour (an account exactly at capacity is reported as
        # exhausted) so existing callers and tests see no change.
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
        if (
            self.capacity_30d_microdollars is not None
            and cost_30d >= self.capacity_30d_microdollars
        ):
            return False
        if (
            self.capacity_5h_requests is not None
            and requests_5h >= self.capacity_5h_requests
        ):
            return False
        if (
            self.capacity_7d_requests is not None
            and requests_7d >= self.capacity_7d_requests
        ):
            return False
        if (
            self.capacity_30d_requests is not None
            and requests_30d >= self.capacity_30d_requests
        ):
            return False
        if self.capacity_5h_tokens is not None and tokens_5h >= self.capacity_5h_tokens:
            return False
        if self.capacity_7d_tokens is not None and tokens_7d >= self.capacity_7d_tokens:
            return False
        return not (
            self.capacity_30d_tokens is not None
            and tokens_30d >= self.capacity_30d_tokens
        )

    def get_remaining_capacity(self) -> float:
        """Get remaining capacity as a normalized score (0.0 to 1.0).

        Returns the minimum remaining capacity across all configured
        windows and dimensions (cost, request count, token count) so
        that a tight short-term capacity limits routing even when
        long-term capacity is ample.
        """
        capacities: list[float] = []

        if self.capacity_5h_microdollars is not None:
            cost_5h = (
                self.get_persisted_cost_5h()
                + self.five_hour_offset
                + self.reserved_cost
            )
            capacity = self.capacity_5h_microdollars
            used_ratio = cost_5h / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_7d_microdollars is not None:
            cost_7d = self.get_persisted_cost_7d() + self.weekly_offset
            capacity = self.capacity_7d_microdollars
            used_ratio = cost_7d / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_30d_microdollars is not None:
            cost_30d = self.get_persisted_cost_30d() + self.monthly_offset
            capacity = self.capacity_30d_microdollars
            used_ratio = cost_30d / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_5h_requests is not None:
            requests_5h = (
                self.get_persisted_request_count_5h()
                + self.reserved_requests
                + self.request_offset_5h
            )
            capacity = self.capacity_5h_requests
            used_ratio = requests_5h / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_7d_requests is not None:
            requests_7d = self.get_persisted_request_count_7d() + self.request_offset_7d
            capacity = self.capacity_7d_requests
            used_ratio = requests_7d / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_30d_requests is not None:
            requests_30d = (
                self.get_persisted_request_count_30d() + self.request_offset_30d
            )
            capacity = self.capacity_30d_requests
            used_ratio = requests_30d / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_5h_tokens is not None:
            tokens_5h = (
                self.get_persisted_token_count_5h()
                + self.reserved_tokens
                + self.token_offset_5h
            )
            capacity = self.capacity_5h_tokens
            used_ratio = tokens_5h / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_7d_tokens is not None:
            tokens_7d = self.get_persisted_token_count_7d() + self.token_offset_7d
            capacity = self.capacity_7d_tokens
            used_ratio = tokens_7d / capacity if capacity > 0 else float("inf")
            capacities.append(max(0.0, 1.0 - used_ratio))

        if self.capacity_30d_tokens is not None:
            tokens_30d = self.get_persisted_token_count_30d() + self.token_offset_30d
            capacity = self.capacity_30d_tokens
            used_ratio = tokens_30d / capacity if capacity > 0 else float("inf")
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
        return 0

    def get_persisted_cost_30d(self) -> int:
        """Get 30d cost from persisted snapshot, or 0 if unavailable."""
        if self.persisted_snapshot is not None:
            return self.persisted_snapshot.cost_30d
        return 0

    def get_persisted_request_count_5h(self) -> int:
        """5h request count from the persisted snapshot, or 0."""
        if self.persisted_snapshot is None:
            return 0
        return self.persisted_snapshot.request_count_5h

    def get_persisted_request_count_7d(self) -> int:
        """7d request count from the persisted snapshot, or 0."""
        if self.persisted_snapshot is None:
            return 0
        return self.persisted_snapshot.request_count_7d

    def get_persisted_request_count_30d(self) -> int:
        """30d request count from the persisted snapshot, or 0."""
        if self.persisted_snapshot is None:
            return 0
        return self.persisted_snapshot.request_count_30d

    def get_persisted_token_count_5h(self) -> int:
        """5h token count from the persisted snapshot, or the hourly window."""
        if self.persisted_snapshot is not None:
            return self.persisted_snapshot.token_count_5h
        tokens, _ = self.hourly_window.get_usage()
        return tokens

    def get_persisted_token_count_7d(self) -> int:
        """7d token count from the persisted snapshot, or 0 if unavailable.

        No in-memory 7d window exists. Falling back to the 24h daily
        window would understate weekly utilization by roughly 7x and
        bias routing toward freshly added accounts.
        """
        if self.persisted_snapshot is not None:
            return self.persisted_snapshot.token_count_7d
        return 0

    def get_persisted_token_count_30d(self) -> int:
        """30d token count from the persisted snapshot, or 0.

        No in-memory 30d window exists, so this falls back to zero
        when no persisted snapshot is present.
        """
        if self.persisted_snapshot is None:
            return 0
        return self.persisted_snapshot.token_count_30d

    def get_request_capacity_5h(self) -> int:
        """Effective 5h request capacity (configured or soft default)."""
        if self.capacity_5h_requests is not None:
            return self.capacity_5h_requests
        return DEFAULT_REQUEST_CAPACITY_5H

    def get_request_capacity_7d(self) -> int:
        """Effective 7d request capacity (configured or soft default)."""
        if self.capacity_7d_requests is not None:
            return self.capacity_7d_requests
        return DEFAULT_REQUEST_CAPACITY_7D

    def get_request_capacity_30d(self) -> int:
        """Effective 30d request capacity (configured or soft default)."""
        if self.capacity_30d_requests is not None:
            return self.capacity_30d_requests
        return DEFAULT_REQUEST_CAPACITY_30D

    def get_token_capacity_5h(self) -> int:
        """Effective 5h token capacity (configured or soft default)."""
        if self.capacity_5h_tokens is not None:
            return self.capacity_5h_tokens
        return DEFAULT_TOKEN_CAPACITY_5H

    def get_token_capacity_7d(self) -> int:
        """Effective 7d token capacity (configured or soft default)."""
        if self.capacity_7d_tokens is not None:
            return self.capacity_7d_tokens
        return DEFAULT_TOKEN_CAPACITY_7D

    def get_token_capacity_30d(self) -> int:
        """Effective 30d token capacity (configured or soft default)."""
        if self.capacity_30d_tokens is not None:
            return self.capacity_30d_tokens
        return DEFAULT_TOKEN_CAPACITY_30D


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

# Global unknown-request fallback (dollars per 1M tokens). The
# conservative minimum of the two bounds is used by Tier 5 so a
# single misrouted request cannot over-reserve by ~10x. Routing
# scoring still treats the unknown tier as informational; weighted
# fair-share scoring is not influenced by it.
GLOBAL_FALLBACK = (3.0, 15.0)
GLOBAL_FALLBACK_FLOOR_MICRODOLLARS_PER_TOKEN = 0.5

# Reservation cost is a routing safety budget, not a billed value.
# Bound every cost returned by :meth:`QuotaEstimator.estimate_cost`
# so a runaway EWMA or override cannot inflate a tiny request into a
# multi-dollar reservation. This is much smaller than
# ``MAX_REQUEST_COST_MICRODOLLARS`` ($250) which guards canonical
# billed cost only — reservation fallback should never be large
# enough to create canonical spend if downstream logic regresses.
_QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS = (
    MAX_REQUEST_COST_MICRODOLLARS // 100
)  # $2.50

# Per-token ceiling for any reservation estimate.  Mirrors the
# ``_MAX_ESTIMATED_COST_PER_TOKEN_MICRODOLLARS`` used by the
# canonicalization helpers in :mod:`eggpool.catalog.pricing` so the
# router and finalizer agree on what a ``plausible`` estimate looks
# like.
_QUOTA_ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS = 100  # $0.10 / token


def _finalize_estimate(
    raw_cost_microdollars: int,
    *,
    estimated_tokens: int,
    source: str,
    model_id: str,
    account_name: str,
) -> int:
    """Clamp a raw cost through every reservation safety guard.

    Centralises the per-tier bounds so a future regression in any
    single tier cannot let an absurd value escape into the
    :meth:`QuotaEstimator.estimate_cost` callers (the request
    coordinator, capacity scoring, ``add_reservation``).  The helpers
    never raise; they emit a structured log line and fall through to
    the conservative global fallback so the reservation ceiling is
    always defensible.

    The same helper is reused by ``record_usage`` to validate the
    first EWMA observation: a single implausible seed would otherwise
    persist into every future reservation for the same model.
    """
    if estimated_tokens <= 0:
        logger.debug(
            "Skipping reservation estimate for %s/%s: zero or negative token "
            "count (raw_cost=%s).",
            account_name,
            model_id,
            raw_cost_microdollars,
        )
        return 0
    safe = max(0, raw_cost_microdollars)
    # Per-token ceiling — rejects rates that dwarf even the most
    # expensive frontier model.
    ceiling_per_token = _QUOTA_ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS
    safe_per_token = min(safe, estimated_tokens * ceiling_per_token)
    if safe != safe_per_token:
        logger.warning(
            "Suppressing implausible reservation from %s for %s/%s: "
            "raw %s microdollars exceeds %s microdollars/token ceiling for "
            "%s tokens.",
            source,
            account_name,
            model_id,
            safe,
            ceiling_per_token,
            estimated_tokens,
        )
        safe = safe_per_token
    # Absolute reservation ceiling. Distinct from
    # ``MAX_REQUEST_COST_MICRODOLLARS`` because reservation fallback
    # should not be capable of creating multi-dollar canonical spend.
    if safe > _QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS:
        logger.warning(
            "Capping reservation from %s for %s/%s: %s microdollars exceeds "
            "%s microdollars reservation ceiling.",
            source,
            account_name,
            model_id,
            safe,
            _QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS,
        )
        safe = _QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS
    # Defensive layering: ``safe`` is already bounded by
    # ``_QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS`` ($2.50), well
    # below ``MAX_REQUEST_COST_MICRODOLLARS`` ($250). ``clamp_...``
    # keeps the result non-negative and within the SQLite INTEGER
    # range in case a future caller passes a negative ``raw_cost``.
    return clamp_request_cost_microdollars(safe)


# Hard caps for the in-memory EWMA tables (Phase 2 of the memory
# footprint plan). These bound the worst-case memory of the two
# ``dict`` structures on ``QuotaEstimator`` so a fleet with a long
# tail of distinct (account, model) keys cannot grow without
# limit. When a cap is hit, the least-recently-touched entry is
# evicted; on LRU miss the cost estimator transparently falls
# through to the next tier (model override, family fallback, or
# global unknown fallback).
EWMA_HARD_CAP = 4096
GLOBAL_EWMA_HARD_CAP = 1024


@dataclass(slots=True)
class QuotaEstimator:
    """Estimates quota usage across all accounts.

    Includes 5-tier cost estimation hierarchy for reservation sizing.
    Optionally uses persisted UsageWindowRepository for actual usage windows.
    """

    accounts: dict[str, AccountQuota] = field(default_factory=dict[str, AccountQuota])
    # Tier 1: account/model EWMA. Backing store is an OrderedDict so we
    # can evict the least-recently-touched (account, model) entry on
    # insert overflow (Phase 2 of the memory footprint plan).
    # OrderedDict is a ``dict`` subclass, so the API surface is
    # unchanged for callers that treat the field as a plain mapping.
    account_model_ewma: OrderedDict[str, OrderedDict[str, EWMAEstimate]] = field(
        default_factory=lambda: OrderedDict[str, OrderedDict[str, EWMAEstimate]]()
    )
    # Tier 2: global model EWMA. OrderedDict for the same reason above.
    global_model_ewma: OrderedDict[str, EWMAEstimate] = field(
        default_factory=lambda: OrderedDict[str, EWMAEstimate]()
    )
    _global_outlier_counts: OrderedDict[str, int] = field(
        default_factory=lambda: OrderedDict[str, int](), repr=False
    )
    # Memory caps for the EWMA tables. Tunable for tests; production
    # code uses the module-level defaults ``EWMA_HARD_CAP`` and
    # ``GLOBAL_EWMA_HARD_CAP``.
    ewma_hard_cap: int = EWMA_HARD_CAP
    global_ewma_hard_cap: int = GLOBAL_EWMA_HARD_CAP
    # Tier 3: configured per-model overrides
    model_overrides: dict[str, tuple[float, float]] = field(
        default_factory=dict[str, tuple[float, float]]
    )
    # Provider-specific pricing resolved to account names. Accounts belong to
    # exactly one provider, so this keeps the hot estimate path lookup-only.
    account_model_overrides: dict[str, dict[str, tuple[float, float]]] = field(
        default_factory=dict[str, dict[str, tuple[float, float]]]
    )
    # Config
    default_safety_factor: float = 1.15
    default_unknown_reservation_microdollars: int = 1_000_000
    # Outlier rejection band for EWMA updates. An incoming observation
    # whose ``cost_per_token`` diverges from the existing EWMA estimate
    # by more than this factor (either above or below) is treated as an
    # outlier and excluded from the rolling estimate. Without this guard
    # a single misread upstream price (e.g. misclassified dollars/M as
    # dollars/token) permanently inflates the model reservation and
    # contaminates downstream cost floors. The band is generous enough
    # to admit genuine price changes (e.g. switching providers) while
    # tight enough to filter the catastrophic 1,000,000x class of bug.
    ewma_outlier_max_ratio: float = 100.0
    # Optional persisted window repo for loading actual usage
    _usage_window_repo: UsageWindowRepository | None = field(default=None, repr=False)
    # In-memory reservation tracking for scorer
    _account_reserved_cost: dict[str, int] = field(default_factory=dict[str, int])
    _account_reserved_requests: dict[str, int] = field(default_factory=dict[str, int])
    _account_reserved_tokens: dict[str, int] = field(default_factory=dict[str, int])
    # Claims are made visible to the scorer before durable dispatch
    # persistence starts.  These counters are provisional ownership only;
    # publication converts them into the canonical reservation mirrors.
    _account_pending_requests: dict[str, int] = field(default_factory=dict[str, int])
    _account_pending_tokens: dict[str, int] = field(default_factory=dict[str, int])
    _account_pending_cost: dict[str, int] = field(default_factory=dict[str, int])
    # Serializes record_usage + persisted_snapshot updates so concurrent
    # finalizers cannot interleave between the two updates and lose cost
    # increments.
    _snapshot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def set_usage_window_repo(self, repo: UsageWindowRepository) -> None:
        """Set the persisted usage window repo for loading actual usage."""
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

        # Update EWMA estimates if model and token data available. The
        # LRU helpers above bound memory growth at ``ewma_hard_cap`` per-bucket
        # and at the outer-dict level; cost estimation falls through to
        # the next tier on cache miss.
        if model_id and tokens > 0 and cost_microdollars > 0:
            cost_per_token = cost_microdollars / tokens
            # Absolute pre-seed guard.  The :meth:`_is_outlier` band
            # only kicks in once there is a running estimate to
            # compare against; without this guard the very first
            # observation for a model would silently seed the EWMA
            # with an implausible rate and inflate every future
            # reservation for the same model.
            if cost_per_token > _QUOTA_ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS:
                logger.warning(
                    "Refusing to seed EWMA with implausible rate for %s/%s: "
                    "%s microdollars/token exceeds %s microdollars/token "
                    "ceiling (tokens=%s, cost=%s microdollars).",
                    account_name,
                    model_id,
                    cost_per_token,
                    _QUOTA_ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS,
                    tokens,
                    cost_microdollars,
                )
                return
            self._record_account_model_ewma(account_name, model_id, cost_per_token)
            self._record_global_model_ewma(model_id, cost_per_token)

    def _is_outlier(self, cost_per_token: float, model_id: str) -> bool:
        """Reject EWMA observations that diverge from the running estimate.

        An outlier is any observation whose ``cost_per_token`` is more
        than ``ewma_outlier_max_ratio`` times the existing estimate in
        either direction. The first observation for a model is never an
        outlier (no prior to compare against); the band only kicks in
        once we have enough history to know what "normal" looks like.
        """
        existing = self.global_model_ewma.get(model_id)
        if existing is None or existing.sample_count < 1:
            return False
        baseline = existing.estimate_cost_per_token
        if baseline <= 0:
            return False
        ratio = cost_per_token / baseline
        return ratio > self.ewma_outlier_max_ratio or ratio < (
            1.0 / self.ewma_outlier_max_ratio
        )

    def _record_account_model_ewma(
        self, account_name: str, model_id: str, cost_per_token: float
    ) -> None:
        """Insert/move-to-end in ``account_model_ewma``, evicting LRU on overflow.

        Two cap checks guard memory growth:
          * the per-account bucket is capped at ``ewma_hard_cap`` model
            entries, so a misbehaving account cannot grow a single
            bucket without bound;
          * the outer dict is capped at ``ewma_hard_cap`` accounts, so
            a long tail of distinct account names cannot grow the
            outer dict without bound. A new account pushes out the
            least-recently-touched account's entire bucket.

        Outlier observations are silently dropped from the rolling
        estimate; the raw usage is still recorded on the daily/hourly
        windows by ``record_usage`` so quota accounting reflects the
        actual spend. Only the EWMA used by future cost estimation is
        protected from contamination.
        """
        if self._is_outlier(cost_per_token, model_id):
            return
        bucket = self.account_model_ewma.get(account_name)
        if bucket is None:
            bucket = OrderedDict[str, EWMAEstimate]()
            self.account_model_ewma[account_name] = bucket
            if len(self.account_model_ewma) > self.ewma_hard_cap:
                self.account_model_ewma.popitem(last=False)
        if model_id in bucket:
            bucket.move_to_end(model_id)
        else:
            bucket[model_id] = EWMAEstimate()
            if len(bucket) > self.ewma_hard_cap:
                bucket.popitem(last=False)
        bucket[model_id].update(cost_per_token)

    def _record_global_model_ewma(self, model_id: str, cost_per_token: float) -> None:
        """Insert/move-to-end in ``global_model_ewma``, evicting LRU on overflow.

        See :meth:`_record_account_model_ewma` for the outlier-rejection
        rationale. We compute the outlier check against the *global*
        estimate so that a localized account with one bad observation
        cannot poison its peers.
        """
        if self._is_outlier(cost_per_token, model_id):
            count = self._global_outlier_counts.get(model_id, 0) + 1
            self._global_outlier_counts[model_id] = count
            self._global_outlier_counts.move_to_end(model_id)
            if len(self._global_outlier_counts) > self.global_ewma_hard_cap:
                self._global_outlier_counts.popitem(last=False)
            if count < 3:
                return
            # A sustained change is allowed to replace a stale baseline;
            # otherwise a legitimate price change can starve this model's
            # global fallback forever.
            self._global_outlier_counts.pop(model_id, None)
            self.global_model_ewma[model_id] = EWMAEstimate()
        else:
            self._global_outlier_counts.pop(model_id, None)
        if model_id in self.global_model_ewma:
            self.global_model_ewma.move_to_end(model_id)
        else:
            self.global_model_ewma[model_id] = EWMAEstimate()
            if len(self.global_model_ewma) > self.global_ewma_hard_cap:
                self.global_model_ewma.popitem(last=False)
        self.global_model_ewma[model_id].update(cost_per_token)

    async def record_usage_and_snapshot(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
        model_id: str | None = None,
    ) -> None:
        """Record usage and atomically refresh the persisted snapshot."""
        persisted_account_id: int | None = None
        async with self._snapshot_lock:
            self.record_usage(
                account_name,
                tokens=tokens,
                cost_microdollars=cost_microdollars,
                model_id=model_id,
            )
            # Lifecycle: ``persisted_snapshot`` is set exclusively by
            # :meth:`load_persisted_windows` during startup and is
            # never replaced afterwards. Reading it under
            # ``_snapshot_lock`` guarantees the snapshot observed here
            # is the same one ``record_usage`` mutated.
            quota = self.get_account_quota(account_name)
            if quota is not None and quota.persisted_snapshot is not None:
                if self._usage_window_repo is None:
                    safe_cost = max(0, cost_microdollars)
                    quota.persisted_snapshot.cost_5h += safe_cost
                    quota.persisted_snapshot.cost_7d += safe_cost
                    quota.persisted_snapshot.cost_30d += safe_cost
                    safe_tokens = max(0, tokens)
                    quota.persisted_snapshot.request_count_5h += 1
                    quota.persisted_snapshot.request_count_7d += 1
                    quota.persisted_snapshot.request_count_30d += 1
                    quota.persisted_snapshot.token_count_5h += safe_tokens
                    quota.persisted_snapshot.token_count_7d += safe_tokens
                    quota.persisted_snapshot.token_count_30d += safe_tokens
                else:
                    persisted_account_id = quota.persisted_snapshot.account_id

        if self._usage_window_repo is not None and persisted_account_id is not None:
            values = await self._usage_window_repo.get_account_usage_window_snapshot(
                persisted_account_id,
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            )
            async with self._snapshot_lock:
                quota = self.get_account_quota(account_name)
                persisted = quota.persisted_snapshot if quota is not None else None
                if persisted is None:
                    return
                for field_name, value in values.items():
                    setattr(persisted, field_name, value)
                persisted.loaded_at = time.time()

    def estimate_cost(
        self,
        account_name: str,
        model_id: str,
        estimated_tokens: int,
    ) -> int:
        """Estimate cost using the 5-tier hierarchy.

        Returns estimated cost in microdollars. Every tier routes its
        raw value through :func:`_finalize_estimate` so a single
        misconfigured override or poisoned EWMA seed cannot escape the
        reservation safety ceiling.
        """
        # Tier 1: Account/model EWMA. On miss, fall through to Tier 2;
        # the LRU helpers above bound memory for the hit case.
        account_estimates = self.account_model_ewma.get(
            account_name, OrderedDict[str, EWMAEstimate]()
        )
        am_estimate = account_estimates.get(model_id)
        if am_estimate and am_estimate.sample_count >= 5:
            cost = int(
                estimated_tokens
                * am_estimate.estimate_cost_per_token
                * self.default_safety_factor
            )
            return max(
                _finalize_estimate(
                    cost,
                    estimated_tokens=estimated_tokens,
                    source="account_model_ewma",
                    model_id=model_id,
                    account_name=account_name,
                ),
                1,
            )

        # Tier 2: Global model EWMA
        global_est = self.global_model_ewma.get(model_id)
        if global_est and global_est.sample_count >= 5:
            cost = int(
                estimated_tokens
                * global_est.estimate_cost_per_token
                * self.default_safety_factor
            )
            return max(
                _finalize_estimate(
                    cost,
                    estimated_tokens=estimated_tokens,
                    source="global_model_ewma",
                    model_id=model_id,
                    account_name=account_name,
                ),
                1,
            )

        # Tier 3: Configured per-model override
        override = self.account_model_overrides.get(account_name, {}).get(model_id)
        if override is None:
            override = self.model_overrides.get(model_id)
        if override is not None:
            input_rate, output_rate = override
            avg_rate = (input_rate + output_rate) / 2.0
            cost_per_token = avg_rate
            cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
            return max(
                _finalize_estimate(
                    cost,
                    estimated_tokens=estimated_tokens,
                    source="model_override",
                    model_id=model_id,
                    account_name=account_name,
                ),
                1,
            )

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
            return max(
                _finalize_estimate(
                    cost,
                    estimated_tokens=estimated_tokens,
                    source="model_family_fallback",
                    model_id=model_id,
                    account_name=account_name,
                ),
                1,
            )

        # Tier 5: Global unknown-request fallback. Use the conservative
        # lower bound of the unknown-price range, clamped by a small
        # floor, so the reservation does not balloon for models whose
        # actual cost is sub-1 microdollars/token.
        cost_per_token = max(
            GLOBAL_FALLBACK[0],
            GLOBAL_FALLBACK_FLOOR_MICRODOLLARS_PER_TOKEN,
        )
        cost = int(estimated_tokens * cost_per_token * self.default_safety_factor)
        return max(
            _finalize_estimate(
                cost,
                estimated_tokens=estimated_tokens,
                source="global_unknown_fallback",
                model_id=model_id,
                account_name=account_name,
            ),
            1,
        )

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

    def _sync_reservation_mirrors(self, account_name: str) -> None:
        """Keep the diagnostic reservation fields aligned with all ownership."""
        quota = self.get_account_quota(account_name)
        if quota is None:
            return
        quota.reserved_cost = self._account_reserved_cost.get(
            account_name, 0
        ) + self._account_pending_cost.get(account_name, 0)
        quota.reserved_requests = self._account_reserved_requests.get(
            account_name, 0
        ) + self._account_pending_requests.get(account_name, 0)
        quota.reserved_tokens = self._account_reserved_tokens.get(
            account_name, 0
        ) + self._account_pending_tokens.get(account_name, 0)

    def add_pending_claim(
        self, account_name: str, *, tokens: int, cost: int = 0
    ) -> None:
        """Publish provisional request/token load for a claimed account.

        This method is intentionally synchronous and database-free.  The
        coordinator calls it while holding ``_selection_claim_lock`` so a
        subsequent selector cannot score the account between the claim and
        its durable dispatch persistence.
        """
        if tokens < 0:
            raise ValueError("pending claim tokens must be non-negative")
        if cost < 0:
            raise ValueError("pending claim cost must be non-negative")
        self.accounts.setdefault(account_name, AccountQuota(account_name=account_name))
        self._account_pending_requests[account_name] = clamp_sqlite_integer(
            self._account_pending_requests.get(account_name, 0) + 1
        )
        self._account_pending_tokens[account_name] = clamp_sqlite_integer(
            self._account_pending_tokens.get(account_name, 0) + tokens
        )
        self._account_pending_cost[account_name] = clamp_sqlite_integer(
            self._account_pending_cost.get(account_name, 0) + cost
        )
        self._sync_reservation_mirrors(account_name)

    def release_pending_claim(
        self, account_name: str, *, tokens: int, cost: int = 0
    ) -> None:
        """Release one provisional claim, surfacing ownership underflow."""
        if tokens < 0:
            raise ValueError("pending claim tokens must be non-negative")
        if cost < 0:
            raise ValueError("pending claim cost must be non-negative")
        pending_requests = self._account_pending_requests.get(account_name, 0)
        pending_tokens = self._account_pending_tokens.get(account_name, 0)
        pending_cost = self._account_pending_cost.get(account_name, 0)
        if pending_requests < 1 or pending_tokens < tokens:
            raise RuntimeError(
                "pending claim ownership underflow for "
                f"account={account_name!r} requests={pending_requests} "
                f"tokens={pending_tokens} release_tokens={tokens}"
            )
        if pending_cost < cost:
            raise RuntimeError(
                "pending claim cost ownership underflow for "
                f"account={account_name!r} cost={pending_cost} release_cost={cost}"
            )
        self._account_pending_requests[account_name] = pending_requests - 1
        self._account_pending_tokens[account_name] = pending_tokens - tokens
        self._account_pending_cost[account_name] = pending_cost - cost
        self._sync_reservation_mirrors(account_name)

    def convert_pending_claim(
        self,
        account_name: str,
        cost: int,
        *,
        tokens: int,
    ) -> None:
        """Convert provisional load into one canonical reservation.

        The operation is synchronous so the coordinator can perform the
        replacement as one local transition while holding the selection
        claim lock.  It performs no SQLite I/O and never creates a second
        representation of the same request.
        """
        self.release_pending_claim(account_name, tokens=tokens, cost=cost)
        self._account_reserved_cost[account_name] = clamp_sqlite_integer(
            self._account_reserved_cost.get(account_name, 0) + cost
        )
        self._account_reserved_requests[account_name] = clamp_sqlite_integer(
            self._account_reserved_requests.get(account_name, 0) + 1
        )
        self._account_reserved_tokens[account_name] = clamp_sqlite_integer(
            self._account_reserved_tokens.get(account_name, 0) + tokens
        )
        self._sync_reservation_mirrors(account_name)

    def get_account_weight(self, account_name: str) -> float:
        """Get account weight for weighted routing."""
        quota = self.accounts.get(account_name)
        if quota is None:
            return 1.0
        return quota.weight

    def set_account_weight(self, account_name: str, weight: float) -> None:
        """Set account weight for weighted routing."""
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Account weight must be finite and greater than zero")
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
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Account weight must be finite and greater than zero")
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

    def set_model_override(
        self, model_id: str, input_price: float, output_price: float
    ) -> None:
        """Set a configured per-model price override (Tier 4)."""
        self.model_overrides[model_id] = (input_price, output_price)

    def set_account_model_override(
        self,
        account_name: str,
        model_id: str,
        input_price: float,
        output_price: float,
    ) -> None:
        """Set provider-specific model pricing for one provider account."""
        self.account_model_overrides.setdefault(account_name, {})[model_id] = (
            input_price,
            output_price,
        )

    async def add_reservation(
        self,
        account_name: str,
        cost: int,
        *,
        requests: int = 1,
        tokens: int = 0,
    ) -> None:
        """Track an active reservation for scoring.

        ``cost`` is retained for backward compatibility and accounting
        (it backs the legacy cost-mirror path even though the scorer
        no longer reads it). The scorer now consumes ``requests`` (a
        single reservation counts as one in-flight request) and
        ``tokens`` (the projected token volume for the reservation).
        Both default to one / zero so existing call sites keep working
        without modification.
        """
        async with self._snapshot_lock:
            if account_name not in self._account_reserved_cost:
                self._account_reserved_cost[account_name] = 0
            self._account_reserved_cost[account_name] = clamp_sqlite_integer(
                self._account_reserved_cost[account_name] + cost
            )
            if account_name not in self._account_reserved_requests:
                self._account_reserved_requests[account_name] = 0
            self._account_reserved_requests[account_name] = clamp_sqlite_integer(
                self._account_reserved_requests[account_name] + requests
            )
            if account_name not in self._account_reserved_tokens:
                self._account_reserved_tokens[account_name] = 0
            self._account_reserved_tokens[account_name] = clamp_sqlite_integer(
                self._account_reserved_tokens[account_name] + tokens
            )
            quota = self.get_account_quota(account_name)
            if quota is not None:
                self._sync_reservation_mirrors(account_name)

    async def remove_reservation(
        self,
        account_name: str,
        cost: int,
        *,
        requests: int = 1,
        tokens: int = 0,
    ) -> None:
        """Remove a reservation's cost / request / token tracking."""
        async with self._snapshot_lock:
            if account_name in self._account_reserved_cost:
                self._account_reserved_cost[account_name] = max(
                    0, self._account_reserved_cost[account_name] - cost
                )
            if account_name in self._account_reserved_requests:
                self._account_reserved_requests[account_name] = max(
                    0, self._account_reserved_requests[account_name] - requests
                )
            if account_name in self._account_reserved_tokens:
                self._account_reserved_tokens[account_name] = max(
                    0, self._account_reserved_tokens[account_name] - tokens
                )
            quota = self.get_account_quota(account_name)
            if quota is not None:
                self._sync_reservation_mirrors(account_name)

    async def get_account_reserved_cost(self, account_name: str) -> int:
        """Get total reserved cost for a single account.

        Prefer :meth:`get_account_reserved_costs` when scoring many
        accounts in a row to avoid one lock acquisition per name.
        """
        async with self._snapshot_lock:
            return self._account_reserved_cost.get(account_name, 0)

    async def get_account_reserved_costs(
        self, account_names: list[str]
    ) -> dict[str, int]:
        """Snapshot reserved costs for ``account_names`` in one lock acquisition.

        Names with no recorded reservation map to ``0``. Retained for
        backward compatibility with the cost-mirror audit path; the
        routing scorer no longer reads cost, see
        :meth:`get_account_reserved_load`.
        """
        async with self._snapshot_lock:
            return {
                name: self._account_reserved_cost.get(name, 0) for name in account_names
            }

    async def get_account_reserved_load(
        self, account_names: list[str]
    ) -> dict[str, tuple[int, int]]:
        """Snapshot reserved (requests, tokens) for ``account_names``.

        Single lock acquisition. The routing scorer uses this to fold
        in-flight reservations into the per-account load without
        serializing per-name. Names with no recorded reservation map
        to ``(0, 0)``.
        """
        async with self._snapshot_lock:
            return {
                name: (
                    self._account_reserved_requests.get(name, 0)
                    + self._account_pending_requests.get(name, 0),
                    self._account_reserved_tokens.get(name, 0)
                    + self._account_pending_tokens.get(name, 0),
                )
                for name in account_names
            }

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
        from eggpool.db.repositories import AccountRepository

        acct_repo = AccountRepository(self._usage_window_repo.db)
        enabled = await acct_repo.list_enabled()
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        all_windows = await self._usage_window_repo.get_all_usage_windows(now_iso)
        for acct in enabled:
            name = acct["name"]
            if name not in self.accounts:
                self.accounts[name] = AccountQuota(account_name=name)
            self.accounts[name].weight = acct.get("weight", 1.0)
            windows = all_windows.get(
                acct["id"],
                {
                    "5h": 0,
                    "7d": 0,
                    "30d": 0,
                    "request_count_5h": 0,
                    "request_count_7d": 0,
                    "request_count_30d": 0,
                    "token_count_5h": 0,
                    "token_count_7d": 0,
                    "token_count_30d": 0,
                },
            )
            self.accounts[name].persisted_snapshot = PersistedWindowSnapshot(
                account_id=acct["id"],
                cost_5h=windows["5h"],
                cost_7d=windows["7d"],
                cost_30d=windows["30d"],
                request_count_5h=windows["request_count_5h"],
                request_count_7d=windows["request_count_7d"],
                request_count_30d=windows["request_count_30d"],
                token_count_5h=windows["token_count_5h"],
                token_count_7d=windows["token_count_7d"],
                token_count_30d=windows["token_count_30d"],
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
