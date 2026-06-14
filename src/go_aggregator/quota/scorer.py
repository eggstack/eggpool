"""Quota-fair scoring for routing decisions."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.quota.estimation import QuotaEstimator


@dataclass
class RoutingScore:
    """A routing score for an account."""

    account_name: str
    quota_score: float  # 0.0 to 1.0 (higher is better)
    weight: float  # Account weight
    is_eligible: bool
    random_tiebreaker: float = field(default_factory=random.random)

    @property
    def final_score(self) -> float:
        """Calculate final weighted score with tiebreaker."""
        if not self.is_eligible:
            return 0.0
        return self.quota_score * self.weight


@dataclass
class QuotaFairScorer:
    """Scores accounts based on quota fairness and weights."""

    tiebreaker_range: float = 0.01  # Range for random tiebreaker
    quota_estimator: QuotaEstimator | None = None

    def score_accounts(
        self,
        account_names: list[str],
        model_name: str | None = None,
    ) -> list[RoutingScore]:
        """Score all accounts for routing."""
        scores = []
        for name in account_names:
            quota_score = 1.0
            weight = 1.0
            is_eligible = True

            if self.quota_estimator:
                quota = self.quota_estimator.get_account_quota(name)
                if quota:
                    quota_score = quota.get_remaining_capacity()
                    weight = quota.weight
                    if not quota.is_within_limits():
                        is_eligible = False

            scores.append(
                RoutingScore(
                    account_name=name,
                    quota_score=quota_score,
                    weight=weight,
                    is_eligible=is_eligible,
                )
            )

        return scores

    def select_account(self, scores: list[RoutingScore]) -> RoutingScore | None:
        """Select best account using weighted random selection.

        Uses near-tie randomization to prevent all concurrent requests
        from selecting the same account.
        """
        eligible = [s for s in scores if s.is_eligible]
        if not eligible:
            return None

        # Sort by final score
        eligible.sort(key=lambda s: s.final_score, reverse=True)

        # Find near-ties (within tiebreaker_range of top score)
        top_score = eligible[0].final_score
        near_ties = [
            s
            for s in eligible
            if abs(s.final_score - top_score) < self.tiebreaker_range
        ]

        if len(near_ties) > 1:
            # Random selection among near-ties
            return random.choice(near_ties)

        # Single best account
        return eligible[0] if eligible else None

    def rank_accounts(self, scores: list[RoutingScore]) -> list[RoutingScore]:
        """Rank accounts by score for fallback selection."""
        return sorted(scores, key=lambda s: s.final_score, reverse=True)
