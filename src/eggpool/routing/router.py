"""Quota-aware account router."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eggpool.quota.estimation import QuotaEstimator
from eggpool.quota.scorer import QuotaFairScorer, RoutingScore
from eggpool.routing.eligibility import get_eligible_accounts

if TYPE_CHECKING:
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.accounts.state import AccountRuntimeState
    from eggpool.catalog.service import CatalogService
    from eggpool.health.health_manager import HealthManager

logger = logging.getLogger(__name__)


def _group_by_priority(
    states: list[AccountRuntimeState],
) -> list[list[AccountRuntimeState]]:
    """Group eligible states into priority tiers (highest first).

    Returns a list of tiers, each a list of states sharing the same
    ``routing_priority``. The tier list is sorted by descending priority
    so the first tier is the most preferred. Within a tier, the original
    eligibility order is preserved.
    """
    sorted_states = sorted(states, key=lambda s: s.routing_priority, reverse=True)
    tiers: list[list[AccountRuntimeState]] = []
    if not sorted_states:
        return tiers
    current_tier: list[AccountRuntimeState] = [sorted_states[0]]
    current_priority = sorted_states[0].routing_priority
    for state in sorted_states[1:]:
        if state.routing_priority == current_priority:
            current_tier.append(state)
            continue
        tiers.append(current_tier)
        current_tier = [state]
        current_priority = state.routing_priority
    tiers.append(current_tier)
    return tiers


@dataclass(frozen=True)
class RoutingCandidates:
    """Eligible account states and their lookup index for one routing decision."""

    states: list[AccountRuntimeState]
    by_name: dict[str, AccountRuntimeState]

    @property
    def names(self) -> list[str]:
        """Return candidate account names in eligibility order."""
        return [state.name for state in self.states]

    def tiered(self) -> list[tuple[int, list[AccountRuntimeState]]]:
        """Return eligible states grouped into priority tiers (highest first).

        Each entry is ``(priority, states_in_tier)``. The list contains only
        tiers with at least one state. Within a tier, eligibility order is
        preserved.
        """
        tiers = _group_by_priority(self.states)
        return [(tier[0].routing_priority, tier) for tier in tiers]


@dataclass(frozen=True)
class RoutingExclusion:
    """Record of one account being excluded from a routing decision."""

    account_name: str
    reason: str  # e.g. "circuit_open", "circuit_half_open_full", "already_attempted"


@dataclass(frozen=True)
class RoutingDecisionTrace:
    """Trace of one routing decision for observability.

    Built by the coordinator after the selection step completes.
    Persisted via :class:`RoutingDecisionRepository` inside the same
    transaction as the request_attempts row so the trace and the
    attempt can never disagree.
    """

    model_id: str
    provider_id: str | None
    protocol: str | None
    selected_account_name: str | None
    selected_account_id: int | None
    selected_tier: int | None
    selected_score: float | None
    eligible_count: int
    scored_count: int
    attempted_excluded_count: int
    top_score: float | None
    top_score_account_name: str | None
    exclusions: tuple[RoutingExclusion, ...] = ()

    def to_exclude_reasons_json(self) -> str:
        """Serialize exclusions to a JSON array string for persistence."""
        import json

        return json.dumps(
            [
                {"account": ex.account_name, "reason": ex.reason}
                for ex in self.exclusions
            ]
        )


class Router:
    """Selects an account for routing with quota-aware scoring."""

    def __init__(
        self,
        registry: AccountRegistry,
        catalog: CatalogService,
        quota_estimator: QuotaEstimator | None = None,
        health_manager: HealthManager | None = None,
        stale_after_s: float | None = None,
        local_quota_mode: str = "score_only",
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._quota_estimator = quota_estimator or QuotaEstimator()
        self._health_manager = health_manager
        self._stale_after_s = stale_after_s
        # ``score_only`` keeps above-capacity accounts eligible (rank-only);
        # ``hard_cap`` restores pre-suppression behavior as an opt-in.
        self._local_quota_mode = local_quota_mode
        # Counter updates are tiny and infrequent compared with upstream I/O.
        # One stable lock avoids lock-replacement races when a counter reaches
        # zero while another coroutine is already waiting to increment it.
        self._active_count_lock = asyncio.Lock()
        self._scorer = QuotaFairScorer(
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
        )

    async def select_account(
        self,
        model_id: str,
        request_estimates: dict[str, int] | None = None,
        exclude_accounts: set[str] | None = None,
        provider_id: str | None = None,
        protocol: str | None = None,
    ) -> AccountRuntimeState | None:
        """Select an account for the given model.

        Eligible accounts are grouped into priority tiers (highest first).
        The highest non-empty tier is selected and the existing
        ``QuotaFairScorer`` is used to load balance within it.
        """
        candidates = self._selection_candidates(
            model_id, exclude_accounts, provider_id, protocol
        )
        tiers = candidates.tiered()
        if not tiers:
            return None

        for _priority, tier_states in tiers:
            tier_candidates = RoutingCandidates(
                states=tier_states,
                by_name={state.name: state for state in tier_states},
            )
            scores = await self._score_eligible_accounts(
                tier_candidates, model_id, request_estimates
            )
            best = self._scorer.select_account(scores)
            if best is not None:
                return tier_candidates.by_name.get(best.account_name)
        return None

    def get_eligible_account_names(
        self,
        model_id: str,
        exclude_accounts: set[str] | None = None,
        provider_id: str | None = None,
        protocol: str | None = None,
    ) -> list[str]:
        """Get eligible account names for a model.

        Uses the same eligibility logic as select_account() so estimate
        generation and selection cannot disagree. Names are returned in
        eligibility order, not priority order.
        """
        candidates = self._selection_candidates(
            model_id, exclude_accounts, provider_id, protocol
        )
        return candidates.names

    async def select_accounts_for_failover(
        self,
        model_id: str,
        max_accounts: int = 3,
        request_estimates: dict[str, int] | None = None,
        exclude_accounts: set[str] | None = None,
        provider_id: str | None = None,
        protocol: str | None = None,
    ) -> list[tuple[AccountRuntimeState, RoutingScore]]:
        """Select multiple accounts for failover, ranked by score.

        Results are returned in priority order (highest tier first); within
        each tier, accounts are ranked by the quota-fair scorer so the
        coordinator's retry loop can prefer the best account in the best
        available tier. Failover between tiers is allowed: callers that want
        strict tier-bounded failover can stop at the first tier boundary
        using the per-account priority from the returned
        ``AccountRuntimeState``.
        """
        if max_accounts <= 0:
            return []

        candidates = self._selection_candidates(
            model_id, exclude_accounts, provider_id, protocol
        )
        tiers = candidates.tiered()
        if not tiers:
            return []

        result: list[tuple[AccountRuntimeState, RoutingScore]] = []
        for _priority, tier_states in tiers:
            tier_candidates = RoutingCandidates(
                states=tier_states,
                by_name={state.name: state for state in tier_states},
            )
            scores = await self._score_eligible_accounts(
                tier_candidates, model_id, request_estimates
            )
            ranked = self._scorer.rank_accounts(scores)
            for score in ranked:
                state = tier_candidates.by_name.get(score.account_name)
                if state is None:
                    continue
                result.append((state, score))
                if len(result) >= max_accounts:
                    return result
        return result

    def _selection_candidates(
        self,
        model_id: str,
        exclude_accounts: set[str] | None,
        provider_id: str | None,
        protocol: str | None,
    ) -> RoutingCandidates:
        """Return eligible runtime states and indexes for a routing decision."""
        eligible = get_eligible_accounts(
            self._registry.get_enabled_states(),
            model_id,
            self._catalog.cache,
            self._health_manager,
            stale_after_s=self._stale_after_s,
            provider_id=provider_id,
            protocol=protocol,
            account_supports_protocol=self._registry.account_supports_protocol,
            quota_estimator=self._quota_estimator,
            local_quota_mode=self._local_quota_mode,
        )
        if exclude_accounts:
            eligible = [
                state for state in eligible if state.name not in exclude_accounts
            ]
        return RoutingCandidates(
            states=eligible,
            by_name={state.name: state for state in eligible},
        )

    async def _score_eligible_accounts(
        self,
        candidates: RoutingCandidates,
        model_id: str,
        request_estimates: dict[str, int] | None,
    ) -> list[RoutingScore]:
        """Score eligible states with their current active request counts.

        Annotates each returned score with the tier (``routing_priority``)
        from the corresponding ``AccountRuntimeState`` so callers can
        short-circuit at tier boundaries during failover.
        """
        active_requests = {
            state.name: state.active_request_count for state in candidates.states
        }
        scores = await self._scorer.score_accounts(
            candidates.names,
            model_id,
            active_requests,
            request_estimates,
        )
        for score in scores:
            state = candidates.by_name.get(score.account_name)
            if state is not None:
                score.tier = state.routing_priority
        return scores

    async def record_usage(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
    ) -> None:
        """Record usage for quota tracking via the underlying estimator."""
        await self._quota_estimator.record_usage_and_snapshot(
            account_name,
            tokens=tokens,
            cost_microdollars=cost_microdollars,
        )

    @property
    def quota_estimator(self) -> QuotaEstimator:
        """Return the quota service used by this router."""
        return self._quota_estimator

    def get_account_usage(self, account_name: str) -> tuple[int, int]:
        """Get account usage (tokens, cost)."""
        quota = self._quota_estimator.get_account_quota(account_name)
        if quota is None:
            logger.debug(
                "No quota entry for account %r; returning zero usage",
                account_name,
            )
            return 0, 0
        return quota.get_effective_usage()

    def set_account_weight(self, account_name: str, weight: float) -> None:
        """Set account weight for weighted routing."""
        self._quota_estimator.set_account_weight(account_name, weight)

    def set_account_limits(
        self,
        account_name: str,
        capacity_7d_microdollars: int | None = None,
        capacity_5h_microdollars: int | None = None,
        capacity_30d_microdollars: int | None = None,
    ) -> None:
        """Set quota limits for an account."""
        self._quota_estimator.set_account_limits(
            account_name,
            capacity_7d_microdollars,
            capacity_5h_microdollars,
            capacity_30d_microdollars,
        )

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
        """Configure the full quota policy for an account."""
        self._quota_estimator.configure_account_policy(
            account_name,
            weight=weight,
            capacity_5h_microdollars=capacity_5h_microdollars,
            capacity_7d_microdollars=capacity_7d_microdollars,
            capacity_30d_microdollars=capacity_30d_microdollars,
            offset_5h_microdollars=offset_5h_microdollars,
            offset_7d_microdollars=offset_7d_microdollars,
            offset_30d_microdollars=offset_30d_microdollars,
        )

    async def increment_active_request_count(self, account_name: str) -> None:
        """Increment the active request count for an account."""
        state = self._registry.get_state(account_name)
        if state is not None:
            async with self._active_count_lock:
                state.active_request_count += 1

    async def decrement_active_request_count(self, account_name: str) -> None:
        """Decrement the active request count for an account.

        Never allows the count to become negative.
        """
        state = self._registry.get_state(account_name)
        if state is not None:
            async with self._active_count_lock:
                if state.active_request_count > 0:
                    state.active_request_count -= 1

    def has_eligible_pairing(
        self,
        protocol: str | None = None,
    ) -> bool:
        """Check if at least one eligible account-model pairing exists.

        Verifies at least one combination where:
        - Account enabled
        - Credential loaded
        - Account and model healthy (circuit breaker, model disable)
        - Model available to account
        - Model protocol resolved
        - Account not excluded by quota policy
        """
        all_states = self._registry.get_enabled_states()
        if not all_states:
            return False

        # Snapshot the catalog once so each account iteration does not
        # allocate a fresh dict.  Per-account fresh-supporting sets are
        # cheap to compute but a 50-account readiness probe otherwise
        # re-snapshots the full catalog on every iteration.
        all_models = self._catalog.cache.get_all_models()
        for state in all_states:
            if not state.is_eligible():
                continue
            # Check credential loaded
            if not self._registry.has_usable_credentials(state.name):
                continue
            # Check account-level health (disabled, cooled, etc.) so we
            # don't report an eligible pairing for an account that would
            # be rejected by the routing eligibility check.
            if (
                self._health_manager is not None
                and not self._health_manager.is_account_healthy(state.name)
            ):
                continue
            # is_account_model_available performs support, freshness, and
            # protocol checks together. Avoid materializing the same fresh
            # support set once here and again inside that method.
            for model_id in all_models:
                if not self._catalog.cache.is_account_model_available(
                    state.name,
                    model_id,
                    max_age_s=self._stale_after_s,
                    protocol=protocol,
                ):
                    continue
                # Use model-level health check (includes circuit breaker
                # and model-specific disable, matching routing behavior)
                if (
                    self._health_manager is not None
                    and not self._health_manager.is_model_healthy(state.name, model_id)
                ):
                    continue
                return True
        return False
