"""Quota-fair scoring for routing decisions.

Implements the plan's routing score formula:

    score_i = max(p5_i, pw_i, pm_i)
              + mean_weight * mean(p5_i, pw_i, pm_i)
              + inflight_count_penalty
              + health_penalty

Where:
    p5_i = (observed_5h + offset_5h + reserved + estimate) / capacity_5h
    pw_i = (observed_7d + offset_week + reserved + estimate) / capacity_week
    pm_i = (observed_30d + offset_month + reserved + estimate) / capacity_month

Lower score = less utilized = preferred.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.health.health_manager import HealthManager
    from go_aggregator.quota.estimation import QuotaEstimator


@dataclass
class RoutingScore:
    """A routing score for an account."""

    account_name: str
    quota_score: float  # Combined quota utilization (lower is better)
    weight: float  # Account weight
    is_eligible: bool
    inflight_penalty: float = 0.0
    health_penalty: float = 0.0
    random_tiebreaker: float = field(default_factory=random.random)

    @property
    def final_score(self) -> float:
        """Calculate final score. Lower is better (less utilized)."""
        if not self.is_eligible:
            return float("inf")
        return self.quota_score + self.inflight_penalty + self.health_penalty


@dataclass
class QuotaFairScorer:
    """Scores accounts based on quota fairness and weights.

    Uses the plan's formula with three utilization windows (5h, 7d, 30d),
    mean weighting, inflight request penalties, and health penalties.
    """

    tiebreaker_range: float = 0.01
    mean_weight: float = 0.15
    inflight_penalty_per_request: float = 0.01
    health_penalty_value: float = 10.0
    quota_estimator: QuotaEstimator | None = None
    health_manager: HealthManager | None = None

    def score_accounts(
        self,
        account_names: list[str],
        model_name: str | None = None,
        active_requests: dict[str, int] | None = None,
        request_estimates: dict[str, int] | None = None,
    ) -> list[RoutingScore]:
        """Score all accounts for routing using the full formula."""
        active = active_requests or {}
        estimates = request_estimates or {}
        scores = []

        for name in account_names:
            weight = 1.0
            is_eligible = True
            p5 = 0.0
            pw = 0.0
            pm = 0.0

            if self.quota_estimator:
                quota = self.quota_estimator.get_account_quota(name)
                if quota:
                    weight = quota.weight
                    if not quota.is_within_limits():
                        is_eligible = False

                    # Use persisted window costs when available
                    cost_5h = quota.get_persisted_cost_5h()
                    cost_7d = quota.get_persisted_cost_7d()
                    cost_30d = quota.get_persisted_cost_30d()

                    # Get reserved cost from estimator
                    reserved = self.quota_estimator.get_account_reserved_cost(name)

                    # Get projected request estimate for this account
                    request_estimate = estimates.get(name, 0)

                    # Calculate utilization ratios per window with
                    # per-window offsets, reservations, and request estimate
                    p5 = self._calc_window_utilization(
                        cost_5h + reserved + request_estimate,
                        quota.five_hour_offset,
                        quota.capacity_5h_microdollars,
                    )
                    pw = self._calc_window_utilization(
                        cost_7d + reserved + request_estimate,
                        quota.weekly_offset,
                        quota.capacity_7d_microdollars,
                    )
                    pm = self._calc_window_utilization(
                        cost_30d + reserved + request_estimate,
                        quota.monthly_offset,
                        quota.capacity_30d_microdollars,
                    )

            # Base quota score: max of window utilizations
            # + mean-weighted average
            max_util = max(p5, pw, pm)
            mean_util = (p5 + pw + pm) / 3.0
            base_score = max_util + self.mean_weight * mean_util

            # Inflight request penalty
            count = active.get(name, 0)
            inflight = count * self.inflight_penalty_per_request

            # Health penalty
            health = 0.0
            if self.health_manager and not self.health_manager.is_account_healthy(name):
                health = self.health_penalty_value

            scores.append(
                RoutingScore(
                    account_name=name,
                    quota_score=base_score,
                    weight=weight,
                    is_eligible=is_eligible,
                    inflight_penalty=inflight,
                    health_penalty=health,
                )
            )

        return scores

    def _calc_window_utilization(
        self,
        used_cost: int,
        offset_cost: int,
        max_cost: int | None,
    ) -> float:
        """Calculate utilization ratio for a single window."""
        if max_cost is None or max_cost <= 0:
            return 0.0
        total = used_cost + offset_cost
        return total / max_cost

    def select_account(self, scores: list[RoutingScore]) -> RoutingScore | None:
        """Select best account. Lower final_score is better.

        Uses near-tie randomization to prevent all concurrent requests
        from selecting the same account.
        """
        eligible = [s for s in scores if s.is_eligible]
        if not eligible:
            return None

        # Sort by final score (lower is better)
        eligible.sort(key=lambda s: s.final_score)

        # Find near-ties (within tiebreaker_range of best score)
        best_score = eligible[0].final_score
        near_ties = [
            s
            for s in eligible
            if abs(s.final_score - best_score) < self.tiebreaker_range
        ]

        if len(near_ties) > 1:
            return random.choice(near_ties)

        return eligible[0]

    def rank_accounts(self, scores: list[RoutingScore]) -> list[RoutingScore]:
        """Rank accounts by score for fallback selection (lower is better)."""
        return sorted(scores, key=lambda s: s.final_score)
