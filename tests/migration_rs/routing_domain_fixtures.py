"""Deterministic Python observations for the M5 routing-domain boundary.

The objects in this module are deliberately small, JSON-only adapters around
the existing Python policy objects.  They are the side-by-side oracle input
for D002-D008; they are not a second routing implementation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

ACCOUNT_EXCLUSION_REASONS = (
    "disabled",
    "auth_failed",
    "quota_exhausted",
    "cooldown",
    "rate_limited",
    "circuit_open",
    "no_provider",
    "wrong_provider",
    "no_model",
    "model_stale",
    "no_protocol",
    "protocol_mismatch",
    "thinking_unsupported",
    "thinking_unknown",
    "thinking_conflicting",
    "no_surface",
    "model_quarantined",
)

FAILURE_CATEGORIES = (
    "authentication_failed",
    "quota_exhausted",
    "rate_limited",
    "model_unavailable",
    "connect_timeout",
    "connection_failure",
    "upstream_server_error",
    "protocol_error",
    "context_limit_exceeded",
    "unknown",
)


class FakeClock:
    """Independently controllable wall or monotonic clock."""

    def __init__(self, value: float = 10_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake clocks cannot move backwards")
        self.value += seconds


class SeededRandom:
    """Small injectable RNG surface used by deterministic contract cases."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def choice(self, values: list[Any]) -> Any:
        return self._random.choice(values)

    def shuffle(self, values: list[Any]) -> None:
        self._random.shuffle(values)

    def uniform(self, low: float, high: float) -> float:
        return self._random.uniform(low, high)


@contextlib.contextmanager
def seeded_python_random(seed: int) -> Iterator[None]:
    """Temporarily seed Python's module RNG without leaking test state."""
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


def _sorted_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in sorted(value)}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AccountObservation:
    account_id: int
    account_name: str
    provider_id: str
    enabled: bool
    has_usable_credentials: bool
    weight: float
    priority: int
    supported_protocols: tuple[str, ...]
    supported_request_surfaces: tuple[str, ...]
    quota_offsets: dict[str, int]
    validation_outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "provider_id": self.provider_id,
            "enabled": self.enabled,
            "has_usable_credentials": self.has_usable_credentials,
            "weight": self.weight,
            "priority": self.priority,
            "supported_protocols": list(self.supported_protocols),
            "supported_request_surfaces": list(self.supported_request_surfaces),
            "quota_offsets": _sorted_mapping(self.quota_offsets),
            "validation_outcome": self.validation_outcome,
        }


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    global_model_ids: tuple[str, ...]
    provider_model_rows: tuple[dict[str, Any], ...]
    account_support: dict[str, tuple[str, ...]]
    account_provider: dict[str, str]
    freshness: dict[str, dict[str, Any]]
    refresh_outcomes: tuple[str, ...]
    support_decisions: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_model_ids": list(self.global_model_ids),
            "provider_model_rows": list(self.provider_model_rows),
            "account_support": {
                key: list(self.account_support[key])
                for key in sorted(self.account_support)
            },
            "account_provider": _sorted_mapping(self.account_provider),
            "freshness": _sorted_mapping(self.freshness),
            "refresh_outcomes": list(self.refresh_outcomes),
            "support_decisions": list(self.support_decisions),
        }


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    accounts: tuple[dict[str, Any], ...]
    score_components: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accounts": list(self.accounts),
            "score_components": list(self.score_components),
        }


@dataclass(frozen=True, slots=True)
class HealthObservation:
    failures: tuple[dict[str, Any], ...]
    accounts: tuple[dict[str, Any], ...]
    circuits: tuple[dict[str, Any], ...]
    quarantine: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "failures": list(self.failures),
            "accounts": list(self.accounts),
            "circuits": list(self.circuits),
            "quarantine": list(self.quarantine),
        }


@dataclass(frozen=True, slots=True)
class RoutingObservation:
    requested_model: str
    requested_provider: str | None
    requested_protocol: str | None
    request_surface: str
    eligible_candidates: tuple[str, ...]
    exclusions: tuple[dict[str, str], ...]
    tier: int | None
    score_components: tuple[dict[str, Any], ...]
    native_vs_transcode: tuple[dict[str, Any], ...]
    fairness: dict[str, Any]
    ordered_ranking: tuple[str, ...]
    selected_account: str | None
    claim: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "requested_protocol": self.requested_protocol,
            "request_surface": self.request_surface,
            "eligible_candidates": list(self.eligible_candidates),
            "exclusions": list(self.exclusions),
            "tier": self.tier,
            "score_components": list(self.score_components),
            "native_vs_transcode": list(self.native_vs_transcode),
            "fairness": self.fairness,
            "ordered_ranking": list(self.ordered_ranking),
            "selected_account": self.selected_account,
            "claim": self.claim,
        }


@dataclass(frozen=True, slots=True)
class ModelRouterObservation:
    virtual_model: str
    route_ids: tuple[dict[str, str], ...]
    selector_model: str
    default_model: str
    static_policy_base64: str
    static_policy_length: int
    static_policy_digest: str
    config_fingerprint: str
    sticky: bool
    affinity_ttl_s: float
    max_input_bytes: int
    affinity_key_digest: str | None
    cache_outcome: str
    selected_concrete_model: str
    cache_stats: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "virtual_model": self.virtual_model,
            "route_ids": list(self.route_ids),
            "selector_model": self.selector_model,
            "default_model": self.default_model,
            "static_policy_base64": self.static_policy_base64,
            "static_policy_length": self.static_policy_length,
            "static_policy_digest": self.static_policy_digest,
            "config_fingerprint": self.config_fingerprint,
            "sticky": self.sticky,
            "affinity_ttl_s": self.affinity_ttl_s,
            "max_input_bytes": self.max_input_bytes,
            "affinity_key_digest": self.affinity_key_digest,
            "cache_outcome": self.cache_outcome,
            "selected_concrete_model": self.selected_concrete_model,
            "cache_stats": _sorted_mapping(self.cache_stats),
        }


@dataclass(frozen=True, slots=True)
class RoutingDomainSnapshot:
    """Complete deterministic observation bundle for one fixture case."""

    schema_version: str
    clocks: dict[str, float]
    accounts: tuple[AccountObservation, ...]
    catalog: CatalogObservation
    quota: QuotaObservation
    health: HealthObservation
    routing: RoutingObservation
    model_router: ModelRouterObservation
    parity: dict[str, str] = field(
        default_factory=lambda: {
            "identity": "exact",
            "reason_codes": "exact",
            "candidate_order": "exact",
            "persisted_semantics": "exact",
            "container_representation": "semantic",
            "wall_clock_age": "semantic",
            "request_persistence": "deferred:M7",
            "selector_dispatch": "deferred:M7",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "clocks": _sorted_mapping(self.clocks),
            "accounts": [item.to_dict() for item in self.accounts],
            "catalog": self.catalog.to_dict(),
            "quota": self.quota.to_dict(),
            "health": self.health.to_dict(),
            "routing": self.routing.to_dict(),
            "model_router": self.model_router.to_dict(),
            "parity": _sorted_mapping(self.parity),
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def digest_text(value: str) -> str:
    """Return a stable digest for explicit or automatic affinity input."""
    return _digest(value)


__all__ = [
    "AccountObservation",
    "ACCOUNT_EXCLUSION_REASONS",
    "CatalogObservation",
    "FAILURE_CATEGORIES",
    "FakeClock",
    "HealthObservation",
    "ModelRouterObservation",
    "QuotaObservation",
    "RoutingDomainSnapshot",
    "RoutingObservation",
    "SeededRandom",
    "digest_text",
    "seeded_python_random",
]
