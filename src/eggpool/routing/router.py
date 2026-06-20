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


@dataclass(frozen=True)
class RoutingCandidates:
    """Eligible account states and their lookup index for one routing decision."""

    states: list[AccountRuntimeState]
    by_name: dict[str, AccountRuntimeState]

    @property
    def names(self) -> list[str]:
        """Return candidate account names in eligibility order."""
        return [state.name for state in self.states]


class Router:
    """Selects an account for routing with quota-aware scoring."""

    def __init__(
        self,
        registry: AccountRegistry,
        catalog: CatalogService,
        quota_estimator: QuotaEstimator | None = None,
        health_manager: HealthManager | None = None,
        stale_after_s: float | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._quota_estimator = quota_estimator or QuotaEstimator()
        self._health_manager = health_manager
        self._stale_after_s = stale_after_s
        # Serializes increment/decrement of active_request_count so
        # concurrent coordinators and cleanup tasks cannot lose updates.
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
        """Select an account for the given model."""
        candidates = self._selection_candidates(
            model_id, exclude_accounts, provider_id, protocol
        )
        if not candidates.states:
            return None

        scores = await self._score_eligible_accounts(
            candidates, model_id, request_estimates
        )
        best = self._scorer.select_account(scores)
        if best is None:
            return None

        return candidates.by_name.get(best.account_name)

    def get_eligible_account_names(
        self,
        model_id: str,
        exclude_accounts: set[str] | None = None,
        provider_id: str | None = None,
        protocol: str | None = None,
    ) -> list[str]:
        """Get eligible account names for a model.

        Uses the same eligibility logic as select_account() so estimate
        generation and selection cannot disagree.
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
        """Select multiple accounts for failover, ranked by score."""
        candidates = self._selection_candidates(
            model_id, exclude_accounts, provider_id, protocol
        )
        if not candidates.states:
            return []

        scores = await self._score_eligible_accounts(
            candidates, model_id, request_estimates
        )
        ranked = self._scorer.rank_accounts(scores)

        result: list[tuple[AccountRuntimeState, RoutingScore]] = []
        for score in ranked[:max_accounts]:
            state = candidates.by_name.get(score.account_name)
            if state is not None:
                result.append((state, score))

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
        """Score eligible states with their current active request counts."""
        active_requests = {
            state.name: state.active_request_count for state in candidates.states
        }
        return await self._scorer.score_accounts(
            candidates.names,
            model_id,
            active_requests,
            request_estimates,
        )

    def record_usage(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
    ) -> None:
        """Record usage for quota tracking."""
        self._quota_estimator.record_usage(account_name, tokens, cost_microdollars)

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

        for state in all_states:
            if not state.is_eligible():
                continue
            # Check credential loaded
            if not self._registry.get_api_key(state.name):
                continue
            # Check account-level health (disabled, cooled, etc.) so we
            # don't report an eligible pairing for an account that would
            # be rejected by the routing eligibility check.
            if (
                self._health_manager is not None
                and not self._health_manager.is_account_healthy(state.name)
            ):
                continue
            # Check model availability with model-level health
            all_models = self._catalog.cache.get_all_models()
            for model_id in all_models:
                if self._stale_after_s is not None:
                    supporting = self._catalog.cache.get_fresh_supporting_accounts(
                        model_id, self._stale_after_s
                    )
                else:
                    supporting = self._catalog.cache.get_supporting_accounts(model_id)
                if state.name not in supporting:
                    continue
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
