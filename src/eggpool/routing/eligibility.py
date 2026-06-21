"""Account eligibility checking.

An account is eligible only when all of the following are true:
- Enabled in configuration
- Credential loaded successfully
- Not in authentication-failed state
- Not in an active circuit-breaker cooldown
- Not locally considered exhausted for the relevant quota policy
- Supports the requested model (with recent catalog refresh)
- Supports the requested protocol
- Has not exceeded any configured local concurrency ceiling
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from eggpool.accounts.state import AccountRuntimeState
    from eggpool.catalog.cache import ModelCatalogCache
    from eggpool.health.health_manager import HealthManager
    from eggpool.quota.estimation import QuotaEstimator


def get_eligible_accounts(
    all_states: list[AccountRuntimeState],
    model_id: str,
    catalog: ModelCatalogCache,
    health_manager: HealthManager | None = None,
    stale_after_s: float | None = None,
    provider_id: str | None = None,
    protocol: str | None = None,
    account_supports_protocol: Callable[[str, str], bool] | None = None,
    quota_estimator: QuotaEstimator | None = None,
) -> list[AccountRuntimeState]:
    """Get accounts eligible for routing a specific model.

    Checks:
    - enabled in configuration
    - not in authentication_failed state
    - not in quota_exhausted state
    - not in cooldown
    - circuit breaker allows requests (if health_manager provided)
    - supports the requested model (with recent catalog refresh when
      stale_after_s is provided)
    - supports the requested protocol (if protocol is given)
    - belongs to a provider configured for that protocol (if available)
    - belongs to the specified provider (if provider_id is given)
    - configured local quota capacity is not exceeded (when
      quota_estimator is supplied)
    """
    eligible: list[AccountRuntimeState] = []
    for state in all_states:
        if not state.is_eligible():
            continue

        # Honor operator-configured local quota capacity thresholds
        # before exposing the account to upstream.  Without this, an
        # account over its 30-day limit keeps being selected, the
        # upstream returns 402/429, and only then does the health
        # manager cool it down.
        if quota_estimator is not None:
            quota = quota_estimator.get_account_quota(state.name)
            if quota is not None and not quota.is_within_limits():
                continue

        # Filter by provider if a specific provider was requested
        if provider_id is not None:
            account_provider = catalog.get_provider_for_account(state.name)
            if account_provider != provider_id:
                continue

        if (
            protocol is not None
            and account_supports_protocol is not None
            and not account_supports_protocol(state.name, protocol)
        ):
            continue

        # Check circuit breaker via health manager
        if health_manager is not None and not health_manager.is_model_healthy(
            state.name, model_id
        ):
            continue

        if catalog.is_account_model_available(
            state.name,
            model_id,
            max_age_s=stale_after_s,
            protocol=protocol,
        ):
            eligible.append(state)
    return eligible
