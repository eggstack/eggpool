"""Basic account router for Phase 3."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from go_aggregator.routing.eligibility import get_eligible_accounts

if TYPE_CHECKING:
    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.accounts.state import AccountRuntimeState
    from go_aggregator.catalog.service import CatalogService


class Router:
    """Selects an account for routing."""

    def __init__(self, registry: AccountRegistry, catalog: CatalogService) -> None:
        self._registry = registry
        self._catalog = catalog

    def select_account(self, model_id: str) -> AccountRuntimeState | None:
        """Select an account for the given model.

        Phase 3: Basic weighted random selection.
        Phase 6 will implement quota-fair scoring.
        """
        all_states = self._registry.get_enabled_states()
        eligible = get_eligible_accounts(all_states, model_id, self._catalog.cache)

        if not eligible:
            return None

        # Simple weighted random selection
        weights = [s.weight for s in eligible]
        return random.choices(eligible, weights=weights, k=1)[0]
