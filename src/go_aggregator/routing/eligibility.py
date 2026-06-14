"""Account eligibility checking.

An account is eligible only when all of the following are true:
- Enabled in configuration
- Credential loaded successfully
- Not in authentication-failed state
- Not in an active circuit-breaker cooldown
- Not locally considered exhausted for the relevant quota policy
- Supports the requested model
- Supports the requested protocol
- Has not exceeded any configured local concurrency ceiling
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.accounts.state import AccountRuntimeState
    from go_aggregator.catalog.cache import ModelCatalogCache
    from go_aggregator.health.health_manager import HealthManager


def get_eligible_accounts(
    all_states: list[AccountRuntimeState],
    model_id: str,
    catalog: ModelCatalogCache,
    health_manager: HealthManager | None = None,
) -> list[AccountRuntimeState]:
    """Get accounts eligible for routing a specific model.

    Checks:
    - enabled in configuration
    - not in authentication_failed state
    - not in quota_exhausted state
    - not in cooldown
    - circuit breaker allows requests (if health_manager provided)
    - supports the requested model
    """
    eligible = []
    for state in all_states:
        if not state.is_eligible():
            continue

        # Check circuit breaker via health manager
        if health_manager is not None and not health_manager.is_model_healthy(
            state.name, model_id
        ):
            continue

        supporting = catalog.get_supporting_accounts(model_id)
        if state.name in supporting:
            eligible.append(state)
    return eligible
