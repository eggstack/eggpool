"""Quota-fair scoring for routing decisions.

Implements the plan's routing score formula:

    score_i = max(p5_i, pw_i, pm_i)
              + mean_weight * mean(p5_i, pw_i, pm_i)
              + inflight_count_penalty
              + health_penalty

Where each window utilization is the maximum of a request-count
utilization and a token-count utilization:

    p_request = (request_count + reserved_requests + 1) / request_capacity
    p_token   = (token_count + reserved_tokens + estimated_tokens) / token_capacity
    p_window  = max(p_request, p_token)

Routing is driven by request count and token count -- NOT cost --
because cost is unreliable (some upstreams report zero, prices are
inferred heuristically, unit confusion is a known failure mode) and
load balancing should track the metrics we actually care about. The
``cost_*`` fields on the persisted snapshot are retained for audit
and dashboard display only.

Lower score = less utilized = preferred.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.health.health_manager import HealthManager
    from eggpool.quota.estimation import QuotaEstimator


@dataclass(slots=True)
class RoutingScore:
    """A routing score for an account.

    Cost microdollar fields are retained for trace / dashboard
    compatibility but are NOT used by ``final_score``. The routing
    decision is driven by request count and token count utilization
    ratios because cost is unreliable.
    """

    account_name: str
    quota_score: float  # Combined quota utilization (lower is better)
    weight: float  # Account weight
    is_eligible: bool
    inflight_penalty: float = 0.0
    health_penalty: float = 0.0
    reserved_microdollars: int = 0  # In-flight reservation cost (audit)
    reserved_requests: int = 0
    reserved_tokens: int = 0
    cost_5h_microdollars: int = 0
    cost_7d_microdollars: int = 0
    cost_30d_microdollars: int = 0
    request_count_5h: int = 0
    request_count_7d: int = 0
    request_count_30d: int = 0
    token_count_5h: int = 0
    token_count_7d: int = 0
    token_count_30d: int = 0
    capacity_5h_microdollars: int = 0
    capacity_7d_microdollars: int = 0
    capacity_30d_microdollars: int = 0
    capacity_5h_requests: int = 0
    capacity_7d_requests: int = 0
    capacity_30d_requests: int = 0
    capacity_5h_tokens: int = 0
    capacity_7d_tokens: int = 0
    capacity_30d_tokens: int = 0
    active_request_count: int = 0
    random_tiebreaker: float = field(default_factory=random.random)
    # Tier boundary marker from the provider's routing_priority. Higher
    # tiers are preferred. Callers that want strict tier-bounded failover
    # can compare adjacent scores' ``tier`` and short-circuit at the
    # boundary. Zero means "no tier assigned" (e.g., synthesized
    # elsewhere).
    tier: int = 0
    requires_transcode: bool = False

    @property
    def final_score(self) -> float:
        """Calculate final score. Lower is better (less utilized)."""
        if not self.is_eligible:
            return float("inf")
        return self.quota_score + self.inflight_penalty + self.health_penalty


@dataclass(slots=True)
class QuotaFairScorer:
    """Scores accounts based on quota fairness and weights.

    Uses the plan's formula with three utilization windows (5h, 7d, 30d),
    mean weighting, inflight request penalties, and health penalties.
    """

    tiebreaker_range: float = 0.01
    mean_weight: float = 0.15
    inflight_penalty_per_request: float = 0.01
    health_penalty_value: float = 10.0
    prefer_native: bool = True
    quota_estimator: QuotaEstimator | None = None
    health_manager: HealthManager | None = None

    async def score_accounts(
        self,
        account_names: list[str],
        model_name: str | None = None,
        active_requests: dict[str, int] | None = None,
        request_estimates: dict[str, int] | None = None,
    ) -> list[RoutingScore]:
        """Score all accounts for routing using the full formula.

        ``request_estimates`` is now a mapping of account name to the
        projected token count of the incoming request (previously: a
        cost estimate in microdollars). Routing decisions should
        follow actual workload, so the incoming request's projected
        token count is folded into the per-window token utilization.
        """
        active = active_requests or {}
        estimates = request_estimates or {}
        scores: list[RoutingScore] = []

        if self.quota_estimator:
            reserved_by_name = await self.quota_estimator.get_account_reserved_costs(
                account_names
            )
            reserved_load_by_name = (
                await self.quota_estimator.get_account_reserved_load(account_names)
            )
        else:
            reserved_by_name = {}
            reserved_load_by_name = {}

        for name in account_names:
            weight = 0.0
            is_eligible = True
            p5 = 0.0
            pw = 0.0
            pm = 0.0
            reserved = reserved_by_name.get(name, 0)
            reserved_requests, reserved_tokens = reserved_load_by_name.get(name, (0, 0))
            cost_5h = 0
            cost_7d = 0
            cost_30d = 0
            requests_5h = 0
            requests_7d = 0
            requests_30d = 0
            tokens_5h = 0
            tokens_7d = 0
            tokens_30d = 0
            cap_5h_cost = 0
            cap_7d_cost = 0
            cap_30d_cost = 0
            cap_5h_req = 0
            cap_7d_req = 0
            cap_30d_req = 0
            cap_5h_tok = 0
            cap_7d_tok = 0
            cap_30d_tok = 0

            if self.quota_estimator:
                quota = self.quota_estimator.get_account_quota(name)
                if quota:
                    weight = quota.weight
                    # Above-capacity accounts remain scoreable with
                    # high utilization; they are not hard-gated here.
                    # Upstream quota_exhausted health makes them
                    # temporarily ineligible when authoritative.

                    # Cost fields are retained for audit; the scorer
                    # intentionally does NOT use them as a routing
                    # signal because cost is unreliable across
                    # providers.
                    cost_5h = quota.get_persisted_cost_5h()
                    cost_7d = quota.get_persisted_cost_7d()
                    cost_30d = quota.get_persisted_cost_30d()

                    requests_5h = quota.get_persisted_request_count_5h()
                    requests_7d = quota.get_persisted_request_count_7d()
                    requests_30d = quota.get_persisted_request_count_30d()
                    tokens_5h = quota.get_persisted_token_count_5h()
                    tokens_7d = quota.get_persisted_token_count_7d()
                    tokens_30d = quota.get_persisted_token_count_30d()

                    cap_5h_cost = (
                        quota.capacity_5h_microdollars
                        if quota.capacity_5h_microdollars is not None
                        else 0
                    )
                    cap_7d_cost = (
                        quota.capacity_7d_microdollars
                        if quota.capacity_7d_microdollars is not None
                        else 0
                    )
                    cap_30d_cost = (
                        quota.capacity_30d_microdollars
                        if quota.capacity_30d_microdollars is not None
                        else 0
                    )

                    cap_5h_req = quota.get_request_capacity_5h()
                    cap_7d_req = quota.get_request_capacity_7d()
                    cap_30d_req = quota.get_request_capacity_30d()
                    cap_5h_tok = quota.get_token_capacity_5h()
                    cap_7d_tok = quota.get_token_capacity_7d()
                    cap_30d_tok = quota.get_token_capacity_30d()

                    # Projected token count for the incoming request.
                    # Fall back to 0 so an unknown projection does not
                    # artificially inflate the score.
                    request_token_estimate = max(0, estimates.get(name, 0))

                    p5 = self._calc_window_utilization(
                        requests_5h,
                        reserved_requests,
                        1,
                        quota.request_offset_5h,
                        tokens_5h,
                        reserved_tokens,
                        request_token_estimate,
                        quota.token_offset_5h,
                        cap_5h_req,
                        cap_5h_tok,
                    )
                    pw = self._calc_window_utilization(
                        requests_7d,
                        reserved_requests,
                        1,
                        quota.request_offset_7d,
                        tokens_7d,
                        reserved_tokens,
                        request_token_estimate,
                        quota.token_offset_7d,
                        cap_7d_req,
                        cap_7d_tok,
                    )
                    pm = self._calc_window_utilization(
                        requests_30d,
                        reserved_requests,
                        1,
                        quota.request_offset_30d,
                        tokens_30d,
                        reserved_tokens,
                        request_token_estimate,
                        quota.token_offset_30d,
                        cap_30d_req,
                        cap_30d_tok,
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
                    reserved_microdollars=reserved,
                    reserved_requests=reserved_requests,
                    reserved_tokens=reserved_tokens,
                    cost_5h_microdollars=cost_5h,
                    cost_7d_microdollars=cost_7d,
                    cost_30d_microdollars=cost_30d,
                    request_count_5h=requests_5h,
                    request_count_7d=requests_7d,
                    request_count_30d=requests_30d,
                    token_count_5h=tokens_5h,
                    token_count_7d=tokens_7d,
                    token_count_30d=tokens_30d,
                    capacity_5h_microdollars=cap_5h_cost,
                    capacity_7d_microdollars=cap_7d_cost,
                    capacity_30d_microdollars=cap_30d_cost,
                    capacity_5h_requests=cap_5h_req,
                    capacity_7d_requests=cap_7d_req,
                    capacity_30d_requests=cap_30d_req,
                    capacity_5h_tokens=cap_5h_tok,
                    capacity_7d_tokens=cap_7d_tok,
                    capacity_30d_tokens=cap_30d_tok,
                    active_request_count=int(count),
                )
            )

        return scores

    def _calc_window_utilization(
        self,
        used_requests: int,
        reserved_requests: int,
        incoming_requests: int,
        request_offset: int,
        used_tokens: int,
        reserved_tokens: int,
        incoming_tokens: int,
        token_offset: int,
        request_capacity: int,
        token_capacity: int,
    ) -> float:
        """Calculate utilization ratio for a single window.

        Returns the maximum of the request-count utilization and the
        token-count utilization. The wider of the two ratios is the
        binding constraint for that window: an account can be cheap
        on token volume but hammered on request count, or vice versa.

        Per-window offsets (operator-supplied known load adjustments)
        are ADDED to the observed load, mirroring the legacy cost
        ladder where a positive offset inflates the numerator. A
        negative offset would *subtract* load, which is occasionally
        useful for discounting observed noise.
        """
        req_total = max(
            0,
            used_requests + reserved_requests + incoming_requests + request_offset,
        )
        tok_total = max(
            0,
            used_tokens + reserved_tokens + incoming_tokens + token_offset,
        )
        req_util = req_total / request_capacity if request_capacity > 0 else 0.0
        tok_util = tok_total / token_capacity if token_capacity > 0 else 0.0
        return max(req_util, tok_util)

    def select_account(self, scores: list[RoutingScore]) -> RoutingScore | None:
        """Select best account. Lower final_score is better.

        Uses near-tie randomization to prevent all concurrent requests
        from selecting the same account.
        """
        eligible = [s for s in scores if s.is_eligible]
        if not eligible:
            return None

        # Sort by final score (lower is better)
        if self.prefer_native:
            eligible.sort(
                key=lambda s: (
                    s.final_score,
                    0 if not s.requires_transcode else 1,
                )
            )
        else:
            eligible.sort(key=lambda s: s.final_score)

        # Find near-ties (within tiebreaker_range of best score)
        best_score = eligible[0].final_score
        best_requires_transcode = eligible[0].requires_transcode
        near_ties = [
            s
            for s in eligible
            if abs(s.final_score - best_score) < self.tiebreaker_range
            and s.requires_transcode == best_requires_transcode
        ]

        if len(near_ties) > 1:
            return random.choice(near_ties)

        return eligible[0]

    def rank_accounts(self, scores: list[RoutingScore]) -> list[RoutingScore]:
        """Rank accounts by score for fallback selection (lower is better)."""
        if self.prefer_native:
            ranked = sorted(
                scores,
                key=lambda s: (
                    s.final_score,
                    0 if not s.requires_transcode else 1,
                ),
            )
        else:
            ranked = sorted(scores, key=lambda s: s.final_score)
        if self.tiebreaker_range <= 0 or len(ranked) < 2:
            return ranked

        result: list[RoutingScore] = []
        index = 0
        while index < len(ranked):
            base_score = ranked[index].final_score
            base_requires_transcode = ranked[index].requires_transcode
            group: list[RoutingScore] = []
            while (
                index < len(ranked)
                and abs(ranked[index].final_score - base_score) < self.tiebreaker_range
                and ranked[index].requires_transcode == base_requires_transcode
            ):
                group.append(ranked[index])
                index += 1
            if len(group) > 1:
                random.shuffle(group)
            result.extend(group)
        return result
