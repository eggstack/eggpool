"""Account eligibility checking."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.accounts.state import AccountRuntimeState
    from go_aggregator.catalog.cache import ModelCatalogCache


def get_eligible_accounts(
    all_states: list[AccountRuntimeState],
    model_id: str,
    catalog: ModelCatalogCache,
) -> list[AccountRuntimeState]:
    """Get accounts eligible for routing a specific model.

    An account is eligible when:
    - enabled in configuration
    - not in authentication_failed state
    - not in quota_exhausted state
    - not in cooldown
    - supports the requested model
    """
    eligible = []
    for state in all_states:
        if not state.is_eligible():
            continue
        supporting = catalog.get_supporting_accounts(model_id)
        if state.name in supporting:
            eligible.append(state)
    return eligible
