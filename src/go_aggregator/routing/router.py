"""Quota-aware account router."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from go_aggregator.quota.estimation import QuotaEstimator
from go_aggregator.quota.scorer import QuotaFairScorer, RoutingScore
from go_aggregator.routing.eligibility import get_eligible_accounts

if TYPE_CHECKING:
    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.accounts.state import AccountRuntimeState
    from go_aggregator.catalog.service import CatalogService
    from go_aggregator.health.health_manager import HealthManager

logger = logging.getLogger(__name__)


class Router:
    """Selects an account for routing with quota-aware scoring."""

    def __init__(
        self,
        registry: AccountRegistry,
        catalog: CatalogService,
        quota_estimator: QuotaEstimator | None = None,
        health_manager: HealthManager | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._quota_estimator = quota_estimator or QuotaEstimator()
        self._health_manager = health_manager
        self._scorer = QuotaFairScorer(
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
        )

    def select_account(
        self,
        model_id: str,
        request_id: str | None = None,
        request_estimates: dict[str, int] | None = None,
        exclude_accounts: set[str] | None = None,
    ) -> AccountRuntimeState | None:
        """Select an account for the given model."""
        all_states = self._registry.get_enabled_states()

        # Build active request counts per account
        active_requests = {s.name: s.active_request_count for s in all_states}

        eligible = get_eligible_accounts(
            all_states, model_id, self._catalog.cache, self._health_manager
        )

        # Exclude already-attempted accounts
        if exclude_accounts:
            eligible = [s for s in eligible if s.name not in exclude_accounts]

        if not eligible:
            return None

        scores = self._scorer.score_accounts(
            [s.name for s in eligible], model_id, active_requests, request_estimates
        )

        best = self._scorer.select_account(scores)
        if best is None:
            return None

        for state in eligible:
            if state.name == best.account_name:
                return state

        return None

    def get_eligible_account_names(
        self,
        model_id: str,
        exclude_accounts: set[str] | None = None,
    ) -> list[str]:
        """Get eligible account names for a model.

        Uses the same eligibility logic as select_account() so estimate
        generation and selection cannot disagree.
        """
        all_states = self._registry.get_enabled_states()
        eligible = get_eligible_accounts(
            all_states, model_id, self._catalog.cache, self._health_manager
        )
        if exclude_accounts:
            eligible = [s for s in eligible if s.name not in exclude_accounts]
        return [s.name for s in eligible]

    def select_accounts_for_failover(
        self,
        model_id: str,
        max_accounts: int = 3,
        request_estimates: dict[str, int] | None = None,
        exclude_accounts: set[str] | None = None,
    ) -> list[tuple[AccountRuntimeState, RoutingScore]]:
        """Select multiple accounts for failover, ranked by score."""
        all_states = self._registry.get_enabled_states()
        active_requests = {s.name: s.active_request_count for s in all_states}

        eligible = get_eligible_accounts(
            all_states, model_id, self._catalog.cache, self._health_manager
        )

        # Exclude already-attempted accounts
        if exclude_accounts:
            eligible = [s for s in eligible if s.name not in exclude_accounts]

        if not eligible:
            return []

        scores = self._scorer.score_accounts(
            [s.name for s in eligible], model_id, active_requests, request_estimates
        )

        ranked = self._scorer.rank_accounts(scores)

        result = []
        for score in ranked[:max_accounts]:
            for state in eligible:
                if state.name == score.account_name:
                    result.append((state, score))
                    break

        return result

    def create_reservation(
        self,
        account_name: str,
        estimated_tokens: int,
        estimated_cost_microdollars: int,
        request_id: str,
    ) -> None:
        """Create a reservation for an account.

        .. deprecated::
            The coordinator now uses QuotaEstimator.add_reservation directly.
            This method is retained for backward compatibility only.
        """
        logger.debug("create_reservation called (deprecated path) for %s", account_name)

    def release_reservation(
        self, reservation_id: str, reason: str = "completed"
    ) -> None:
        """Release a reservation.

        .. deprecated::
            The coordinator now uses QuotaEstimator.remove_reservation directly.
            This method is retained for backward compatibility only.
        """
        logger.debug(
            "release_reservation called (deprecated path) for %s", reservation_id
        )

    def record_usage(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
    ) -> None:
        """Record usage for quota tracking."""
        self._quota_estimator.record_usage(account_name, tokens, cost_microdollars)

    def reconcile_reservations(self) -> int:
        """Reconcile expired reservations.

        .. deprecated::
            The coordinator and background tasks now handle reservation
            reconciliation via SQLite directly.
        """
        return 0

    def get_account_usage(self, account_name: str) -> tuple[int, int]:
        """Get account usage (tokens, cost)."""
        quota = self._quota_estimator.get_account_quota(account_name)
        if quota is None:
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

    def increment_active_request_count(self, account_name: str) -> None:
        """Increment the active request count for an account."""
        state = self._registry.get_state(account_name)
        if state is not None:
            state.active_request_count += 1

    def decrement_active_request_count(self, account_name: str) -> None:
        """Decrement the active request count for an account.

        Never allows the count to become negative.
        """
        state = self._registry.get_state(account_name)
        if state is not None and state.active_request_count > 0:
            state.active_request_count -= 1

    def has_eligible_pairing(self) -> bool:
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
            # Check quota within limits
            quota = self._quota_estimator.get_account_quota(state.name)
            if quota is not None and not quota.is_within_limits():
                continue
            # Check model availability with model-level health
            all_models = self._catalog.cache.get_all_models()
            for model_id in all_models:
                supporting = self._catalog.cache.get_supporting_accounts(model_id)
                if state.name not in supporting:
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
