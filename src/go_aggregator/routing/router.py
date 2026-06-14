"""Quota-aware account router."""

from __future__ import annotations

from typing import TYPE_CHECKING

from go_aggregator.quota.estimation import QuotaEstimator
from go_aggregator.quota.reservation import ReservationManager
from go_aggregator.quota.scorer import QuotaFairScorer, RoutingScore
from go_aggregator.routing.eligibility import get_eligible_accounts

if TYPE_CHECKING:
    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.accounts.state import AccountRuntimeState
    from go_aggregator.catalog.service import CatalogService
    from go_aggregator.health.health_manager import HealthManager


class Router:
    """Selects an account for routing with quota-aware scoring."""

    def __init__(
        self,
        registry: AccountRegistry,
        catalog: CatalogService,
        quota_estimator: QuotaEstimator | None = None,
        reservation_manager: ReservationManager | None = None,
        health_manager: HealthManager | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._quota_estimator = quota_estimator or QuotaEstimator()
        self._reservation_manager = reservation_manager or ReservationManager()
        self._health_manager = health_manager
        self._scorer = QuotaFairScorer(
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
        )

    def select_account(
        self, model_id: str, request_id: str | None = None
    ) -> AccountRuntimeState | None:
        """Select an account for the given model."""
        all_states = self._registry.get_enabled_states()

        # Build active request counts per account
        active_requests = {s.name: s.active_request_count for s in all_states}

        eligible = get_eligible_accounts(
            all_states, model_id, self._catalog.cache, self._health_manager
        )

        if not eligible:
            return None

        scores = self._scorer.score_accounts(
            [s.name for s in eligible], model_id, active_requests
        )

        best = self._scorer.select_account(scores)
        if best is None:
            return None

        for state in eligible:
            if state.name == best.account_name:
                return state

        return None

    def select_accounts_for_failover(
        self, model_id: str, max_accounts: int = 3
    ) -> list[tuple[AccountRuntimeState, RoutingScore]]:
        """Select multiple accounts for failover, ranked by score."""
        all_states = self._registry.get_enabled_states()
        active_requests = {s.name: s.active_request_count for s in all_states}

        eligible = get_eligible_accounts(
            all_states, model_id, self._catalog.cache, self._health_manager
        )

        if not eligible:
            return []

        scores = self._scorer.score_accounts(
            [s.name for s in eligible], model_id, active_requests
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
        """Create a reservation for an account."""
        self._reservation_manager.create_reservation(
            account_name=account_name,
            estimated_tokens=estimated_tokens,
            estimated_cost_microdollars=estimated_cost_microdollars,
            request_id=request_id,
        )

    def release_reservation(
        self, reservation_id: str, reason: str = "completed"
    ) -> None:
        """Release a reservation."""
        self._reservation_manager.release_reservation(reservation_id, reason)

    def record_usage(
        self,
        account_name: str,
        tokens: int,
        cost_microdollars: int,
    ) -> None:
        """Record usage for quota tracking."""
        self._quota_estimator.record_usage(account_name, tokens, cost_microdollars)

    def reconcile_reservations(self) -> int:
        """Reconcile expired reservations."""
        return self._reservation_manager.reconcile_reservations()

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
        max_daily_cost_microdollars: int | None = None,
        max_hourly_cost_microdollars: int | None = None,
        max_monthly_cost_microdollars: int | None = None,
    ) -> None:
        """Set quota limits for an account."""
        self._quota_estimator.set_account_limits(
            account_name,
            max_daily_cost_microdollars,
            max_hourly_cost_microdollars,
            max_monthly_cost_microdollars,
        )
