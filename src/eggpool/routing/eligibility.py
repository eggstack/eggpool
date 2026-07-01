"""Account eligibility checking.

An account is eligible only when all of the following are true:
- Enabled in configuration
- Credential loaded successfully
- Not in authentication-failed state
- Not in an active circuit-breaker cooldown
- Supports the requested model (with recent catalog refresh)
- Supports the requested protocol
- Has not exceeded any configured local concurrency ceiling
- Supports thinking if explicitly requested (capability-aware routing)

Note: local quota estimates are advisory in the default routing mode
("score_only"). They influence rank but must not hard-exclude accounts
from eligibility. Only upstream-observed failures, explicit operator
disablement, catalog/protocol incompatibility, or an explicit
``hard_cap`` mode may make an account ineligible. See the
``upstream-authoritative-suppression`` plan for context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from eggpool.accounts.state import AccountRuntimeState
    from eggpool.catalog.cache import ModelCatalogCache
    from eggpool.catalog.capabilities import ThinkingRequestRequirement
    from eggpool.health.health_manager import HealthManager
    from eggpool.quota.estimation import QuotaEstimator

logger = logging.getLogger(__name__)


def get_eligible_accounts(
    all_states: list[AccountRuntimeState],
    model_id: str,
    catalog: ModelCatalogCache,
    health_manager: HealthManager | None = None,
    stale_after_s: float | None = None,
    provider_id: str | None = None,
    protocol: str | None = None,
    transcode_eligibility: set[str] | None = None,
    account_supports_protocol: Callable[[str, str], bool] | None = None,
    quota_estimator: QuotaEstimator | None = None,
    local_quota_mode: str = "score_only",
    thinking_requirement: ThinkingRequestRequirement | None = None,
    capability_policy: dict[str, str] | None = None,
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
    - when ``local_quota_mode="hard_cap"``, configured local quota
      capacity is not exceeded (when ``quota_estimator`` is supplied)
    - supports thinking when explicitly requested (capability-aware routing)

    In the default ``local_quota_mode="score_only"`` mode, local quota
    estimates influence routing rank only and never hard-exclude
    accounts. Switch to ``"hard_cap"`` to restore the pre-suppression
    behavior where locally over-quota accounts are excluded.
    """
    from eggpool.catalog.capabilities import (
        check_candidate_thinking_eligibility,
        extract_thinking_status_from_entry,
    )

    eligible: list[AccountRuntimeState] = []
    apply_local_quota_gate = local_quota_mode == "hard_cap"
    policy = capability_policy or {}
    unsupported_action = policy.get("unsupported_thinking", "reject")
    unknown_action = policy.get("unknown_thinking", "reject")
    mixed_action = policy.get("mixed_collapsed_thinking", "filter")

    for state in all_states:
        if not state.is_eligible():
            continue

        # Optional operator opt-in: honor configured local quota
        # capacity thresholds before exposing the account to upstream.
        # Without ``hard_cap``, local usage may be high but the account
        # remains eligible; upstream ``quota_exhausted`` or
        # ``rate_limited`` health transitions are authoritative.
        if apply_local_quota_gate and quota_estimator is not None:
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
            and (
                transcode_eligibility is None
                or not any(
                    account_supports_protocol(state.name, p)
                    for p in transcode_eligibility
                )
            )
        ):
            continue

        # Check circuit breaker via health manager
        if health_manager is not None and not health_manager.is_model_healthy(
            state.name, model_id
        ):
            continue

        if not catalog.is_account_model_available(
            state.name,
            model_id,
            max_age_s=stale_after_s,
            protocol=protocol,
        ):
            continue

        # Capability-aware routing: filter candidates by thinking support
        # when the client explicitly requested thinking. Missing capability
        # metadata semantically equals an explicit ``unknown`` status — the
        # configured ``unknown_thinking`` policy decides whether to reject,
        # warn, or allow best-effort.
        if thinking_requirement is not None and thinking_requirement.required:
            account_provider = catalog.get_provider_for_account(state.name)
            if account_provider is not None:
                entry = catalog.get_provider_model_entry(model_id, account_provider)
                status = extract_thinking_status_from_entry(entry)
                if not check_candidate_thinking_eligibility(
                    status,
                    unsupported_action=unsupported_action,
                    unknown_action=unknown_action,
                    mixed_action=mixed_action,
                ):
                    continue
                _log_capability_warning(
                    state=state,
                    model_id=model_id,
                    account_provider=account_provider,
                    status=status,
                    unsupported_action=unsupported_action,
                    unknown_action=unknown_action,
                )

        eligible.append(state)
    return eligible


def _log_capability_warning(
    *,
    state: AccountRuntimeState,
    model_id: str,
    account_provider: str | None,
    status: str,
    unsupported_action: str,
    unknown_action: str,
) -> None:
    """Emit a warning when a non-reject capability policy allows a candidate.

    ``warn_drop`` and ``allow_with_warning`` let an ``unsupported`` or
    ``unknown`` candidate through the eligibility gate so operators can
    keep traffic flowing while the catalog catches up.  That silent
    fall-through defeats the purpose of capability-aware routing, so we
    log a structured warning each time it happens.  ``reject`` and
    ``route_best_effort`` produce no warning (the former drops, the
    latter explicitly opts out of awareness).
    """
    if status == "unsupported" and unsupported_action == "warn_drop":
        logger.warning(
            "capability_routing: account=%s model=%s provider=%s thinking=unsupported "
            "policy=warn_drop (candidate retained with warning)",
            state.name,
            model_id,
            account_provider,
        )
    elif status == "unknown" and unknown_action == "allow_with_warning":
        logger.warning(
            "capability_routing: account=%s model=%s provider=%s thinking=unknown "
            "policy=allow_with_warning (candidate retained with warning)",
            state.name,
            model_id,
            account_provider,
        )
