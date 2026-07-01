"""Quota-aware account router."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eggpool.quota.estimation import QuotaEstimator
from eggpool.quota.scorer import QuotaFairScorer, RoutingScore
from eggpool.routing.eligibility import get_eligible_accounts
from eggpool.routing.fairness import FairnessDecision, FairnessKey, FairnessRotor

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from eggpool.accounts.registry import AccountRegistry
    from eggpool.accounts.state import AccountRuntimeState
    from eggpool.catalog.capabilities import ThinkingRequestRequirement
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


def _fairness_band(
    ranked: list[tuple[AccountRuntimeState, RoutingScore]],
    *,
    epsilon: float,
    prefer_native: bool,
) -> tuple[
    list[tuple[AccountRuntimeState, RoutingScore]],
    list[tuple[AccountRuntimeState, RoutingScore]],
    str,
]:
    """Extract the top fairness band from a ranked tier.

    Returns ``(band, rest, reason)`` where *band* contains candidates
    within *epsilon* of the best score in the same priority tier with
    the same weight and transcode status.  If the band has fewer than
    two members, returns ``([], ranked, reason)`` so the caller falls
    back to score-ordered ranking.
    """
    if len(ranked) < 2:
        return [], ranked, "single_candidate"

    best_state, best_score = ranked[0]
    if not math.isfinite(best_score.final_score):
        return [], ranked, "non_finite_score"

    band: list[tuple[AccountRuntimeState, RoutingScore]] = []
    for state, score in ranked:
        if state.routing_priority != best_state.routing_priority:
            break
        if prefer_native and score.requires_transcode != best_score.requires_transcode:
            break
        if abs(score.weight - best_score.weight) > 1e-9:
            break
        if abs(score.final_score - best_score.final_score) > epsilon:
            break
        band.append((state, score))

    if len(band) < 2:
        return [], ranked, "not_tied"

    return band, ranked[len(band) :], "ok"


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class RoutingExclusion:
    """Record of one account being excluded from a routing decision."""

    account_name: str
    reason: str  # e.g. "circuit_open", "circuit_half_open_full", "already_attempted"


@dataclass(frozen=True, slots=True)
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
    score_components: Mapping[str, Any] | None = None

    def to_exclude_reasons_json(self) -> str:
        """Serialize exclusions to a JSON array string for persistence."""
        import json

        return json.dumps(
            [
                {"account": ex.account_name, "reason": ex.reason}
                for ex in self.exclusions
            ]
        )

    def to_score_components_json(self) -> str:
        """Serialize per-account score components to a JSON object string.

        ``score_components`` carries the full breakdown computed by
        ``QuotaFairScorer.score_accounts`` for the selected account
        plus the top near-tie candidates.  Used by the dashboard to
        answer "why account A?" without rescoring from quota tables.
        """
        import json

        payload: dict[str, Any] = dict(self.score_components or {})
        return json.dumps(payload)


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
        fairness_mode: str = "round_robin",
        fairness_epsilon: float | None = None,
        fairness_scope: str = "provider_model_protocol",
        missing_account_recovery_callback: Callable[[str], None] | None = None,
        missing_account_recovery_min_interval_s: float = 60.0,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._quota_estimator = quota_estimator or QuotaEstimator()
        self._health_manager = health_manager
        self._stale_after_s = stale_after_s
        self._local_quota_mode = local_quota_mode
        self._fairness_mode = fairness_mode
        self._fairness_epsilon = fairness_epsilon
        self._fairness_scope = fairness_scope
        self._fairness_rotor = FairnessRotor()
        self._last_fairness_decision: FairnessDecision | None = None
        self._last_fairness_band_names: frozenset[str] = frozenset()
        self._active_count_lock = asyncio.Lock()
        self._scorer = QuotaFairScorer(
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
        )
        # Per-account catalog-recovery trigger. When a configured+healthy
        # account is excluded from eligibility purely because the
        # catalog cache has not seen it recently (e.g. an upstream
        # refresh failed and ``_account_last_refresh`` aged out), the
        # router fires this callback so the catalog service can run a
        # one-shot refresh and re-pool the account. Rate-limited per
        # account via ``missing_account_recovery_min_interval_s`` so a
        # persistent upstream failure does not become a refresh storm.
        self._missing_account_recovery_callback = missing_account_recovery_callback
        self._missing_account_recovery_min_interval_s = (
            missing_account_recovery_min_interval_s
        )
        self._missing_account_recovery_attempt_at: dict[str, float] = {}
        self._missing_account_recovery_lock = asyncio.Lock()

    def _fairness_effective_epsilon(self) -> float:
        """Return fairness epsilon, falling back to scorer tiebreaker_range."""
        if self._fairness_epsilon is not None:
            return self._fairness_epsilon
        return self._scorer.tiebreaker_range

    def _fairness_key(
        self,
        *,
        provider_id: str | None,
        model_id: str,
        protocol: str | None,
        priority: int,
        client_protocol: str | None,
    ) -> FairnessKey:
        """Build a FairnessKey respecting the current fairness_scope.

        Scope semantics:
        - ``provider_model_protocol``: includes provider, model, routed
          protocol, priority tier, and client protocol.
        - ``provider_model``: includes provider, model, priority tier,
          and client protocol; intentionally collapses protocol groups.
        - ``priority_model_protocol``: excludes provider, includes model,
          routed protocol, priority tier, and client protocol.
        """
        return FairnessKey(
            provider_id=(
                None
                if self._fairness_scope == "priority_model_protocol"
                else provider_id
            ),
            model_id=model_id,
            protocol=(None if self._fairness_scope == "provider_model" else protocol),
            priority=priority,
            client_protocol=client_protocol,
        )

    @property
    def last_fairness_decision(self) -> FairnessDecision | None:
        """Return the most recent fairness rotation decision, if any."""
        return self._last_fairness_decision

    @property
    def last_fairness_band_names(self) -> frozenset[str]:
        """Return account names in the most recent fairness band."""
        return self._last_fairness_band_names

    async def select_account(
        self,
        model_id: str,
        request_estimates: dict[str, int] | None = None,
        exclude_accounts: set[str] | None = None,
        provider_id: str | None = None,
        protocol: str | None = None,
        transcode_eligibility: set[str] | None = None,
        client_protocol: str | None = None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> AccountRuntimeState | None:
        """Select an account for the given model.

        Eligible accounts are grouped into priority tiers (highest first).
        The highest non-empty tier is selected and the existing
        ``QuotaFairScorer`` is used to load balance within it.
        """
        candidates = self._selection_candidates(
            model_id,
            exclude_accounts,
            provider_id,
            protocol,
            transcode_eligibility,
            thinking_requirement=thinking_requirement,
            capability_policy=capability_policy,
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
                tier_candidates,
                model_id,
                request_estimates,
                client_protocol=client_protocol,
                transcode_eligibility=transcode_eligibility,
            )
            ranked_scores = self._scorer.rank_accounts(scores)
            ranked_pairs: list[tuple[AccountRuntimeState, RoutingScore]] = []
            for score in ranked_scores:
                state = tier_candidates.by_name.get(score.account_name)
                if state is not None:
                    ranked_pairs.append((state, score))

            if self._fairness_mode != "off" and len(ranked_pairs) >= 2:
                epsilon = self._fairness_effective_epsilon()
                band, rest, _band_reason = _fairness_band(
                    ranked_pairs,
                    epsilon=epsilon,
                    prefer_native=self._scorer.prefer_native,
                )
                if band and self._fairness_mode == "round_robin":
                    key = self._fairness_key(
                        provider_id=provider_id,
                        model_id=model_id,
                        protocol=protocol,
                        priority=_priority,
                        client_protocol=client_protocol,
                    )
                    band, _ = await self._fairness_rotor.rotate(key, band)
                elif band and self._fairness_mode == "random":
                    import random as _random

                    _random.shuffle(band)
                ranked_pairs = band + rest

            if ranked_pairs:
                return ranked_pairs[0][0]
        return None

    async def score_accounts_for_model(
        self,
        model_id: str,
        *,
        provider_id: str | None = None,
        protocol: str | None = None,
        client_protocol: str | None = None,
        transcode_eligibility: set[str] | None = None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> list[tuple[AccountRuntimeState, RoutingScore]]:
        """Score and rank eligible accounts for a model.

        Returns ``(state, score)`` pairs in score order.  Used by
        ``eggpool accounts explain --scores`` to surface routing
        diagnostics without exposing private scoring internals.
        """
        candidates = self._selection_candidates(
            model_id,
            None,
            provider_id,
            protocol,
            transcode_eligibility,
            thinking_requirement=thinking_requirement,
            capability_policy=capability_policy,
        )
        tiers = candidates.tiered()
        result: list[tuple[AccountRuntimeState, RoutingScore]] = []
        for _priority, tier_states in tiers:
            tier_candidates = RoutingCandidates(
                states=tier_states,
                by_name={s.name: s for s in tier_states},
            )
            scores = await self._score_eligible_accounts(
                tier_candidates,
                model_id,
                None,
                client_protocol=client_protocol,
                transcode_eligibility=transcode_eligibility,
            )
            ranked_scores = self._scorer.rank_accounts(scores)
            for score in ranked_scores:
                state = tier_candidates.by_name.get(score.account_name)
                if state is not None:
                    result.append((state, score))
        return result

    async def explain_account_eligibility(
        self,
        *,
        model_id: str,
        provider_id: str | None = None,
        protocol: str | None = None,
        transcode_eligibility: set[str] | None = None,
        include_gates: bool = False,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return one row per registered account explaining eligibility.

        Each row carries the account name, an ``eligible`` boolean,
        a ``reason_code`` string (``"ok"`` when eligible; one of
        ``"disabled"``, ``"auth_failed"``, ``"quota_exhausted"``,
        ``"cooldown"``, ``"rate_limited"``, ``"circuit_open"``,
        ``"no_provider"``, ``"wrong_provider"``, ``"no_model"``,
        ``"model_stale"``, ``"no_protocol"``, ``"protocol_mismatch"``,
        ``"thinking_unsupported"``, ``"thinking_unknown"``,
        ``"thinking_conflicting"`` otherwise) and a short ``reason_detail``
        for dashboard display.
        Used by ``eggpool accounts explain`` to surface why a model
        is routing only to a subset of accounts.

        When ``include_gates`` is true, the row also carries a
        ``gates`` dict that exposes every gate's pass/fail status
        (config, credentials, health, circuit, provider, protocol,
        model support, freshness, provider-metadata, protocol match,
        local quota, thinking support). The dict is informational — the
        canonical decision still comes from ``_classify_eligibility``.
        """
        all_states: list[AccountRuntimeState] = []
        seen: set[str] = set()
        for state in self._registry._states.values():  # pyright: ignore[reportPrivateUsage]
            if state.name in seen:
                continue
            seen.add(state.name)
            all_states.append(state)

        rows: list[dict[str, Any]] = []
        for state in all_states:
            eligible, code, detail = self._classify_eligibility(
                state=state,
                model_id=model_id,
                provider_id=provider_id,
                protocol=protocol,
                transcode_eligibility=transcode_eligibility,
                thinking_requirement=thinking_requirement,
                capability_policy=capability_policy,
            )
            row: dict[str, Any] = {
                "account_name": state.name,
                "eligible": eligible,
                "reason_code": code,
                "reason_detail": detail,
            }
            if include_gates:
                gates = self._collect_gate_status(
                    state=state,
                    model_id=model_id,
                    provider_id=provider_id,
                    protocol=protocol,
                    transcode_eligibility=transcode_eligibility,
                    thinking_requirement=thinking_requirement,
                    capability_policy=capability_policy,
                )
                gates["final_eligible"] = eligible
                row["gates"] = gates
            rows.append(row)
        return rows

    def _collect_gate_status(
        self,
        *,
        state: AccountRuntimeState,
        model_id: str,
        provider_id: str | None,
        protocol: str | None,
        transcode_eligibility: set[str] | None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Collect pass/fail booleans for every routing gate on one account.

        The dict mirrors the order of checks in
        ``_classify_eligibility`` so an operator can see exactly which
        gate is failing. The canonical decision still comes from
        ``_classify_eligibility`` — this dict is a diagnostic
        breakdown, not a re-implementation of the gate logic.
        """
        cache = self._catalog.cache
        state_provider = cache.get_provider_for_account(
            state.name
        ) or self._registry.get_provider_for_account(state.name)
        provider_supports_protocol: bool | None = None
        if protocol is not None and state_provider is not None:
            provider_supports_protocol = protocol in set(
                self._registry.get_provider_protocols(state_provider)
            )
        elif protocol is None:
            provider_supports_protocol = None
        else:
            provider_supports_protocol = False

        protocol_match: bool | None = None
        provider_model_protocol: str | None = None
        provider_model_metadata_exists: bool | None = None
        if state_provider is not None:
            entry = cache.get_provider_model_entry(model_id, state_provider)
            if entry is not None:
                provider_model_metadata_exists = True
                provider_model_protocol = (
                    str(entry.get("protocol"))
                    if entry.get("protocol") is not None
                    else None
                )
                if protocol is not None and provider_model_protocol is not None:
                    protocol_match = provider_model_protocol == protocol
                else:
                    protocol_match = None
            else:
                provider_model_metadata_exists = False

        supporting = cache.get_supporting_accounts(model_id)
        model_support_row = state.name in supporting
        fresh_support = model_support_row and not cache.is_account_stale(
            state.name, self._stale_after_s or 0.0
        )
        circuit_closed = (
            self._health_manager is None
            or self._health_manager.is_model_healthy(state.name, model_id)
        )
        local_quota_gate: bool | None = None
        if self._local_quota_mode == "hard_cap":
            # Conservative signal: report ``False`` only when the
            # account has no remaining capacity at all. This mirrors
            # the gate in ``eligibility.get_eligible_accounts``; the
            # router itself never hard-excludes accounts, the gate is
            # purely advisory and only applies in hard_cap mode.
            quota = self._quota_estimator.get_account_quota(state.name)
            local_quota_gate = (
                quota.get_remaining_capacity() > 0.0 if quota is not None else True
            )

        # Thinking support gate. Missing capability metadata is reported
        # as ``unknown`` so the dashboard/explanation surfaces capability
        # uncertainty rather than silently hiding it.
        thinking_support: str | None = None
        if thinking_requirement is not None and thinking_requirement.required:
            account_provider = cache.get_provider_for_account(state.name)
            if account_provider is not None:
                entry = cache.get_provider_model_entry(model_id, account_provider)
                from eggpool.catalog.capabilities import (
                    extract_thinking_status_from_entry,
                )

                thinking_support = extract_thinking_status_from_entry(entry)

        return {
            "config_enabled": state.enabled,
            "credentials_usable": self._registry.has_usable_credentials(state.name),
            "health_state": state.health_state,
            "circuit_closed": circuit_closed,
            "provider_id_registry": self._registry.get_provider_for_account(state.name),
            "provider_id_catalog": cache.get_provider_for_account(state.name),
            "provider_match": (
                None if provider_id is None else state_provider == provider_id
            ),
            "provider_supports_protocol": provider_supports_protocol,
            "model_support_row": model_support_row,
            "model_support_enabled": model_support_row,
            "fresh_support": fresh_support,
            "provider_model_metadata_exists": provider_model_metadata_exists,
            "provider_model_protocol": provider_model_protocol,
            "protocol_match": protocol_match,
            "local_quota_gate": local_quota_gate,
            "thinking_support": thinking_support,
            "final_eligible": None,
        }

    def _classify_eligibility(
        self,
        *,
        state: AccountRuntimeState,
        model_id: str,
        provider_id: str | None,
        protocol: str | None,
        transcode_eligibility: set[str] | None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> tuple[bool, str, str]:
        """Decide whether ``state`` can serve ``model_id`` and why not.

        Mirrors the filter chain in
        ``eggpool.routing.eligibility.get_eligible_accounts`` so the
        explanation matches the live routing path. ``get_eligible_accounts``
        applies the filters in order; we collapse each short-circuit
        branch into a stable reason_code so the caller can group by
        cause on the dashboard. ``reason_detail`` carries the specific
        identifiers an operator needs to act on the diagnosis (provider
        names, configured protocols, model id, staleness window).
        """
        if not state.enabled:
            return (
                False,
                "disabled",
                f"Account {state.name!r} is disabled in configuration.",
            )
        if state.health_state == "authentication_failed":
            return (
                False,
                "auth_failed",
                (
                    f"Account {state.name!r} previously failed "
                    f"authentication and is locked."
                ),
            )
        if state.health_state == "quota_exhausted":
            return (
                False,
                "quota_exhausted",
                (
                    f"Account {state.name!r}: upstream reported quota "
                    f"exhaustion; cooldown active."
                ),
            )
        if state.health_state == "cooldown":
            return (
                False,
                "cooldown",
                (
                    f"Account {state.name!r} is in cooldown after repeated "
                    f"transient failures."
                ),
            )
        if state.health_state == "rate_limited":
            return (
                False,
                "rate_limited",
                (
                    f"Account {state.name!r}: upstream reported rate "
                    f"limiting; retry-after cooldown active."
                ),
            )

        if provider_id is not None:
            state_provider = self._catalog.cache.get_provider_for_account(
                state.name
            ) or self._registry.get_provider_for_account(state.name)
            if state_provider != provider_id:
                return (
                    False,
                    "wrong_provider",
                    (
                        f"Account {state.name!r} belongs to provider "
                        f"{state_provider!r}; requested provider {provider_id!r}."
                    ),
                )

        if (
            protocol is not None
            and not self._registry.account_supports_protocol(state.name, protocol)
            and not (
                transcode_eligibility
                and any(
                    self._registry.account_supports_protocol(state.name, p)
                    for p in transcode_eligibility
                )
            )
        ):
            state_provider = self._registry.get_provider_for_account(state.name)
            declared_protocols: tuple[str, ...] = (
                tuple(sorted(self._registry.get_provider_protocols(state_provider)))
                if state_provider
                else ()
            )
            transcoder_hint = ""
            if transcode_eligibility:
                transcoder_hint = (
                    f"; no transcoder eligible for {sorted(transcode_eligibility)} "
                    f"covers {protocol!r} either"
                )
            return (
                False,
                "no_protocol",
                (
                    f"Account {state.name!r} (provider {state_provider!r}) "
                    f"declares protocols {list(declared_protocols)}; "
                    f"requested protocol {protocol!r} is not in that list"
                    f"{transcoder_hint}."
                ),
            )

        if (
            self._health_manager is not None
            and not self._health_manager.is_model_healthy(state.name, model_id)
        ):
            return (
                False,
                "circuit_open",
                (
                    f"Circuit breaker is open for model {model_id!r} on "
                    f"account {state.name!r}; recent upstream failures "
                    f"have temporarily excluded it."
                ),
            )

        if not self._catalog.cache.is_account_model_available(
            state.name,
            model_id,
            max_age_s=self._stale_after_s,
            protocol=protocol,
        ):
            # Distinguish "no entry" vs "stale entry" for the dashboard.
            if state.name not in self._catalog.cache.get_supporting_accounts(model_id):
                # Surface which providers DO advertise the model so the
                # operator can pivot to a working account or refresh
                # the offending provider's catalog.
                supporting = self._catalog.cache.get_supporting_accounts(model_id)
                providers_with_model: list[str] = sorted(
                    {
                        provider_id
                        for provider_id in (
                            self._catalog.cache.get_provider_for_account(name)
                            for name in supporting
                        )
                        if provider_id is not None
                    }
                )
                if providers_with_model:
                    provider_hint = (
                        f"; model is advertised by providers "
                        f"{providers_with_model} on other accounts"
                    )
                else:
                    provider_hint = "; no provider currently advertises this model"
                return (
                    False,
                    "no_model",
                    (
                        f"Account {state.name!r} has no catalog entry for "
                        f"model {model_id!r}{provider_hint}."
                    ),
                )
            stale_window = (
                f"{self._stale_after_s:.0f}s"
                if isinstance(self._stale_after_s, (int, float))
                else "configured stale window"
            )
            return (
                False,
                "model_stale",
                (
                    f"Catalog entry for {model_id!r} on account "
                    f"{state.name!r} is older than the stale window "
                    f"({stale_window}); run `eggpool models refresh`."
                ),
            )

        # Capability-aware routing: check thinking support. Missing metadata
        # is treated as ``status="unknown"`` so the configured policy decides
        # whether the candidate stays eligible or is rejected up-front.
        if thinking_requirement is not None and thinking_requirement.required:
            from eggpool.catalog.capabilities import (
                check_candidate_thinking_eligibility,
                extract_thinking_status_from_entry,
            )

            account_provider = self._catalog.cache.get_provider_for_account(state.name)
            if account_provider is not None:
                entry = self._catalog.cache.get_provider_model_entry(
                    model_id, account_provider
                )
                status = extract_thinking_status_from_entry(entry)
                policy = capability_policy or {}
                if not check_candidate_thinking_eligibility(
                    status,
                    unsupported_action=policy.get("unsupported_thinking", "reject"),
                    unknown_action=policy.get("unknown_thinking", "reject"),
                    mixed_action=policy.get("mixed_collapsed_thinking", "filter"),
                ):
                    label = status.replace(" ", "_")
                    return (
                        False,
                        f"thinking_{label}",
                        (
                            f"Account {state.name!r} has thinking "
                            f"status {status!r} for model "
                            f"{model_id!r}; client requested "
                            f"thinking controls "
                            f"({thinking_requirement.fields!r})."
                        ),
                    )

        return True, "ok", "Account is eligible to serve this request."

    def get_eligible_account_names(
        self,
        model_id: str,
        exclude_accounts: set[str] | None = None,
        provider_id: str | None = None,
        protocol: str | None = None,
        transcode_eligibility: set[str] | None = None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> list[str]:
        """Get eligible account names for a model.

        Uses the same eligibility logic as select_account() so estimate
        generation and selection cannot disagree. Names are returned in
        eligibility order, not priority order.
        """
        candidates = self._selection_candidates(
            model_id,
            exclude_accounts,
            provider_id,
            protocol,
            transcode_eligibility,
            thinking_requirement=thinking_requirement,
            capability_policy=capability_policy,
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
        transcode_eligibility: set[str] | None = None,
        client_protocol: str | None = None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
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
            model_id,
            exclude_accounts,
            provider_id,
            protocol,
            transcode_eligibility,
            thinking_requirement=thinking_requirement,
            capability_policy=capability_policy,
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
                tier_candidates,
                model_id,
                request_estimates,
                client_protocol=client_protocol,
                transcode_eligibility=transcode_eligibility,
            )
            ranked_scores = self._scorer.rank_accounts(scores)
            ranked_pairs: list[tuple[AccountRuntimeState, RoutingScore]] = []
            for score in ranked_scores:
                state = tier_candidates.by_name.get(score.account_name)
                if state is not None:
                    ranked_pairs.append((state, score))

            if self._fairness_mode != "off" and len(ranked_pairs) >= 2:
                epsilon = self._fairness_effective_epsilon()
                band, rest, band_reason = _fairness_band(
                    ranked_pairs,
                    epsilon=epsilon,
                    prefer_native=self._scorer.prefer_native,
                )
                if band and self._fairness_mode == "round_robin":
                    key = self._fairness_key(
                        provider_id=provider_id,
                        model_id=model_id,
                        protocol=protocol,
                        priority=_priority,
                        client_protocol=client_protocol,
                    )
                    band, fairness_decision = await self._fairness_rotor.rotate(
                        key, band, scope=self._fairness_scope
                    )
                elif band and self._fairness_mode == "random":
                    import random as _random

                    _random.shuffle(band)
                    key = self._fairness_key(
                        provider_id=provider_id,
                        model_id=model_id,
                        protocol=protocol,
                        priority=_priority,
                        client_protocol=client_protocol,
                    )
                    fairness_decision = FairnessDecision(
                        mode="random",
                        applied=True,
                        key=key.to_key_string(),
                        candidate_count=len(band),
                        scope=self._fairness_scope,
                        reason="ok",
                    )
                else:
                    key = self._fairness_key(
                        provider_id=provider_id,
                        model_id=model_id,
                        protocol=protocol,
                        priority=_priority,
                        client_protocol=client_protocol,
                    )
                    fairness_decision = FairnessDecision(
                        mode=self._fairness_mode,
                        applied=False,
                        key=key.to_key_string(),
                        candidate_count=len(ranked_pairs),
                        scope=self._fairness_scope,
                        reason=band_reason,
                    )
                self._last_fairness_decision = fairness_decision
                self._last_fairness_band_names = (
                    frozenset(s.name for s, _ in band) if band else frozenset()
                )
                ranked_pairs = band + rest
            else:
                key = self._fairness_key(
                    provider_id=provider_id,
                    model_id=model_id,
                    protocol=protocol,
                    priority=_priority,
                    client_protocol=client_protocol,
                )
                self._last_fairness_decision = FairnessDecision(
                    mode=self._fairness_mode,
                    applied=False,
                    key=key.to_key_string(),
                    candidate_count=len(ranked_pairs),
                    scope=self._fairness_scope,
                    reason=(
                        "disabled"
                        if self._fairness_mode == "off"
                        else "single_candidate"
                    ),
                )
                self._last_fairness_band_names = frozenset()

            for state, score in ranked_pairs:
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
        transcode_eligibility: set[str] | None = None,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
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
            transcode_eligibility=transcode_eligibility,
            account_supports_protocol=self._registry.account_supports_protocol,
            quota_estimator=self._quota_estimator,
            local_quota_mode=self._local_quota_mode,
            thinking_requirement=thinking_requirement,
            capability_policy=capability_policy,
        )
        if exclude_accounts:
            eligible = [
                state for state in eligible if state.name not in exclude_accounts
            ]
        self._maybe_trigger_missing_account_recovery(
            model_id=model_id,
            provider_id=provider_id,
            eligible=eligible,
        )
        eligible = self._filter_mixed_collapsed_thinking(
            eligible,
            model_id,
            thinking_requirement=thinking_requirement,
            capability_policy=capability_policy,
        )
        return RoutingCandidates(
            states=eligible,
            by_name={state.name: state for state in eligible},
        )

    def _filter_mixed_collapsed_thinking(
        self,
        eligible: list[AccountRuntimeState],
        model_id: str,
        *,
        thinking_requirement: ThinkingRequestRequirement | None = None,
        capability_policy: dict[str, str] | None = None,
    ) -> list[AccountRuntimeState]:
        """Filter mixed-provider models under ``mixed_collapsed_thinking``.

        When ``mixed_action="filter"`` and a model is served by multiple
        providers, silently drop providers whose thinking status is not
        ``"supported"``.  If no providers remain after filtering, return
        the original unfiltered list so the request falls through to the
        standard rejection path.
        """
        if thinking_requirement is None or not thinking_requirement.required:
            return eligible

        policy = capability_policy or {}
        mixed_action = policy.get("mixed_collapsed_thinking", "filter")
        if mixed_action != "filter":
            return eligible

        from eggpool.catalog.capabilities import dict_to_model_capabilities

        provider_support: dict[str, list[AccountRuntimeState]] = {}
        for state in eligible:
            acct_provider = self._catalog.cache.get_provider_for_account(state.name)
            provider_support.setdefault(acct_provider or "", []).append(state)

        if len(provider_support) <= 1:
            return eligible

        thinking_capable: list[AccountRuntimeState] = []
        for _provider_id, accounts in provider_support.items():
            for state in accounts:
                acct_provider = self._catalog.cache.get_provider_for_account(state.name)
                if acct_provider is None:
                    continue
                entry = self._catalog.cache.get_provider_model_entry(
                    model_id, acct_provider
                )
                if entry is None:
                    continue
                caps_raw = entry.get("capabilities", {})
                if not isinstance(caps_raw, dict) or "thinking" not in caps_raw:
                    continue
                caps = dict_to_model_capabilities({"thinking": caps_raw["thinking"]})
                if caps.thinking.status == "supported":
                    thinking_capable.append(state)

        if not thinking_capable:
            return eligible

        return thinking_capable

    def _maybe_trigger_missing_account_recovery(
        self,
        *,
        model_id: str,
        provider_id: str | None,
        eligible: list[AccountRuntimeState],
    ) -> None:
        """Schedule a one-shot catalog refresh for accounts that are
        configured and healthy but missing from the eligible set.

        Without this, a transient per-account refresh failure (network
        blip, upstream 5xx) can leave a sibling's
        ``_account_last_refresh`` aged past ``stale_after_s``. The
        account stays enabled, has valid credentials, and reports
        healthy in ``HealthManager``, but the catalog cache treats it
        as unsupported for the model — so the router always picks a
        different account and traffic skews dramatically. Fire a
        one-shot refresh to recover.
        """
        if self._missing_account_recovery_callback is None:
            return
        eligible_names = {state.name for state in eligible}
        target_provider_ids: set[str | None] = set()
        for state in eligible:
            target_provider_ids.add(self._registry.get_provider_for_account(state.name))
        if provider_id is not None:
            target_provider_ids.add(provider_id)
        for pid in target_provider_ids:
            if pid is None:
                continue
            for state in self._registry.get_enabled_states():
                if state.name in eligible_names:
                    continue
                if not state.is_eligible():
                    continue
                if not self._registry.has_usable_credentials(state.name):
                    continue
                if self._registry.get_provider_for_account(state.name) != pid:
                    continue
                self._schedule_missing_account_recovery(state.name)

    def _schedule_missing_account_recovery(self, account_name: str) -> None:
        """Rate-limited dispatch of the configured recovery callback.

        The dispatch is non-blocking: a single asyncio task is created
        per call. The internal ``_missing_account_recovery_attempt_at``
        map enforces a per-account minimum interval so a persistent
        upstream failure cannot trigger a refresh storm.
        """
        now = time.monotonic()
        last = self._missing_account_recovery_attempt_at.get(account_name, 0.0)
        if now - last < self._missing_account_recovery_min_interval_s:
            return
        self._missing_account_recovery_attempt_at[account_name] = now
        callback = self._missing_account_recovery_callback
        if callback is None:
            return
        callback(account_name)

    async def _score_eligible_accounts(
        self,
        candidates: RoutingCandidates,
        model_id: str,
        request_estimates: dict[str, int] | None,
        *,
        client_protocol: str | None = None,
        transcode_eligibility: set[str] | None = None,
    ) -> list[RoutingScore]:
        """Score eligible states with their current active request counts.

        Annotates each returned score with the tier (``routing_priority``)
        from the corresponding ``AccountRuntimeState`` so callers can
        short-circuit at tier boundaries during failover.

        When *client_protocol* and *transcode_eligibility* are provided,
        each score is annotated with ``requires_transcode`` so the
        ``QuotaFairScorer`` can rank native-protocol accounts above
        transcodable ones when ``prefer_native`` is enabled.
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
                if client_protocol is not None and transcode_eligibility is not None:
                    score.requires_transcode = not (
                        self._registry.account_supports_protocol(
                            score.account_name,
                            client_protocol,
                        )
                    )
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
