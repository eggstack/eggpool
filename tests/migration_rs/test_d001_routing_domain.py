"""D001 deterministic routing-domain contract observations."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.failure.quarantine import EvidenceProvenance, ModelQuarantine
from eggpool.health.backoff import compute_backoff_seconds
from eggpool.health.circuit_breaker import CircuitState
from eggpool.health.health_manager import HealthManager, classify_failure_category
from eggpool.model_router.affinity import (
    ModelRouterAffinity,
    session_identity_from_header,
)
from eggpool.model_router.config import ModelRouterConfig
from eggpool.model_router.registry import compile_model_router
from eggpool.model_router.selector import ModelSelection
from eggpool.models.config import AppConfig
from eggpool.quota.estimation import (
    AccountQuota,
    PersistedWindowSnapshot,
    QuotaEstimator,
)
from eggpool.quota.scorer import QuotaFairScorer
from eggpool.routing.eligibility import get_eligible_accounts
from eggpool.routing.fairness import FairnessKey, FairnessRotor
from eggpool.wire.ir import canonical_request_from_mapping
from tests.migration_rs.routing_domain_fixtures import (
    ACCOUNT_EXCLUSION_REASONS,
    AccountObservation,
    CatalogObservation,
    FakeClock,
    HealthObservation,
    ModelRouterObservation,
    QuotaObservation,
    RoutingDomainSnapshot,
    RoutingObservation,
    seeded_python_random,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SNAPSHOT_PATH = (
    Path(__file__).parents[2]
    / "migration-rs"
    / "fixtures"
    / "routing-domain"
    / "d001-python-observations.json"
)


def _config() -> AppConfig:
    return AppConfig.from_dict(
        {
            "providers": {
                "provider-a": {
                    "id": "provider-a",
                    "base_url": "https://provider-a.invalid/v1",
                    "protocols": ["openai", "anthropic"],
                    "routing_priority": 10,
                    "auth": {"mode": "none"},
                    "accounts": [
                        {
                            "name": "account-a",
                            "weight": 2.0,
                            "proxy_url": "http://proxy.invalid:8080",
                            "weekly_offset_microdollars": -11,
                        },
                        {"name": "disabled-a", "enabled": False},
                    ],
                },
                "provider-b": {
                    "id": "provider-b",
                    "base_url": "https://provider-b.invalid/v1",
                    "protocols": ["openai"],
                    "routing_priority": 5,
                    "auth": {"mode": "none"},
                    "accounts": [{"name": "account-b", "weight": 1.0}],
                },
            }
        }
    )


def _account_observations(config: AppConfig) -> tuple[AccountObservation, ...]:
    registry = AccountRegistry(config)
    ids = {
        account.name: index
        for index, account in enumerate(
            sorted(config.all_accounts(), key=lambda item: item.name), 1
        )
    }
    result = []
    for account_name in sorted(ids):
        state = registry.get_state(account_name)
        assert state is not None
        provider_id = registry.get_provider_for_account(account_name)
        assert provider_id is not None
        result.append(
            AccountObservation(
                account_id=ids[account_name],
                account_name=account_name,
                provider_id=provider_id,
                enabled=state.enabled,
                has_usable_credentials=registry.has_usable_credentials(account_name),
                weight=state.weight,
                priority=state.routing_priority,
                supported_protocols=tuple(
                    sorted(registry.get_provider_protocols(provider_id))
                ),
                supported_request_surfaces=tuple(
                    surface
                    for surface in ("chat_completions", "responses")
                    if registry.account_supports_request_surface(account_name, surface)
                ),
                quota_offsets=registry.get_account_offsets(account_name),
                validation_outcome="valid",
            )
        )
    return tuple(result)


def _catalog_observation(config: AppConfig, wall: FakeClock) -> CatalogObservation:
    cache = ModelCatalogCache()
    cache.set_config(config)
    model = {
        "model_id": "shared-model",
        "display_name": "Shared Model",
        "protocol": "openai",
        "protocol_source": "config",
        "capabilities": {"supports_tools": True},
        "source_metadata": {"fixture": "d001"},
        "effective_limits": {"context_tokens": 8192, "enforce": True},
    }
    sibling = {**model, "protocol": "anthropic", "protocol_source": "config"}
    first = {**model, "model_id": "withdrawn-model"}
    with patch("eggpool.catalog.cache.time.time", wall):
        cache.update_from_account(
            "account-a",
            "provider-a",
            [model, first],
            authoritative=True,
            allow_withdrawals=True,
        )
        cache.update_from_account(
            "account-b",
            "provider-b",
            [sibling],
            authoritative=True,
            allow_withdrawals=True,
        )
        partial = cache.update_from_account("account-a", "provider-a", [model])
        withdrawn = cache.update_from_account(
            "account-a",
            "provider-a",
            [model],
            authoritative=True,
            allow_withdrawals=True,
        )
    rows = []
    for model_id, provider_id in (
        ("shared-model", "provider-a"),
        ("shared-model", "provider-b"),
    ):
        entry = cache.get_provider_model_entry(model_id, provider_id)
        assert entry is not None
        rows.append(
            {
                "model_id": model_id,
                "provider_id": provider_id,
                "protocol": entry["protocol"],
                "protocol_source": entry["protocol_source"],
                "capabilities": entry["capabilities"],
                "effective_limits": entry["effective_limits"],
            }
        )
    return CatalogObservation(
        global_model_ids=tuple(sorted(cache._models)),  # noqa: SLF001
        provider_model_rows=tuple(rows),
        account_support={
            name: tuple(sorted(cache.get_supporting_accounts(name)))
            for name in sorted(cache._models)  # noqa: SLF001
        },
        account_provider={
            name: cache.get_provider_for_account(name) or ""
            for name in ("account-a", "account-b")
        },
        freshness={
            "account-a": {"age_s": 0.0, "status": "fresh"},
            "account-b": {"age_s": 0.0, "status": "fresh"},
        },
        refresh_outcomes=(
            "success_authoritative",
            "success_authoritative",
            "success_partial_preserve",
            "success_authoritative_withdraw",
        ),
        support_decisions=(
            {
                "account": "account-a",
                "added": first["model_id"],
                "preserved_by_partial": True,
                "withdrawn": withdrawn.withdrawn_support,
            },
            {
                "account": partial.account_name,
                "partial_preserved_support": partial.preserved_support,
            },
        ),
    )


async def _quota_observation() -> QuotaObservation:
    estimator = QuotaEstimator(
        accounts={
            "account-a": AccountQuota(
                account_name="account-a",
                weight=2.0,
                persisted_snapshot=PersistedWindowSnapshot(
                    account_id=1,
                    request_count_5h=10,
                    request_count_7d=20,
                    request_count_30d=30,
                    token_count_5h=1000,
                    token_count_7d=2000,
                    token_count_30d=3000,
                    cost_5h=999999,
                ),
                capacity_5h_requests=20,
                capacity_7d_requests=100,
                capacity_30d_requests=200,
                capacity_5h_tokens=2000,
                capacity_7d_tokens=10000,
                capacity_30d_tokens=20000,
                request_offset_5h=-1,
                reserved_requests=2,
                reserved_tokens=100,
                reserved_cost=123,
            ),
            "account-b": AccountQuota(
                account_name="account-b",
                weight=1.0,
                persisted_snapshot=PersistedWindowSnapshot(
                    account_id=2,
                    request_count_5h=1,
                    request_count_7d=2,
                    request_count_30d=3,
                    token_count_5h=100,
                    token_count_7d=200,
                    token_count_30d=300,
                    cost_5h=1,
                ),
                capacity_5h_requests=20,
                capacity_7d_requests=100,
                capacity_30d_requests=200,
                capacity_5h_tokens=2000,
                capacity_7d_tokens=10000,
                capacity_30d_tokens=20000,
            ),
        }
    )
    estimator._account_pending_requests["account-a"] = 1  # noqa: SLF001
    estimator._account_pending_tokens["account-a"] = 50  # noqa: SLF001
    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(
        ["account-a", "account-b"],
        request_estimates={"account-a": 200, "account-b": 200},
    )
    account_rows = []
    for name in ("account-a", "account-b"):
        quota = estimator.get_account_quota(name)
        assert quota is not None
        account_rows.append(
            {
                "account": name,
                "capacity": {
                    "requests_5h": quota.get_request_capacity_5h(),
                    "tokens_5h": quota.get_token_capacity_5h(),
                },
                "reserved": {
                    "requests": await estimator.get_account_reserved_load([name]),
                    "cost": await estimator.get_account_reserved_costs([name]),
                },
                "within_limits": quota.is_within_limits(),
                "remaining_capacity": quota.get_remaining_capacity(),
            }
        )
    return QuotaObservation(
        accounts=tuple(account_rows),
        score_components=tuple(
            {
                "account": score.account_name,
                "quota_score": score.quota_score,
                "final_score": score.final_score,
                "weight": score.weight,
                "eligible": score.is_eligible,
                "request_counts": [
                    score.request_count_5h,
                    score.request_count_7d,
                    score.request_count_30d,
                ],
                "token_counts": [
                    score.token_count_5h,
                    score.token_count_7d,
                    score.token_count_30d,
                ],
            }
            for score in scores
        ),
    )


def _entry_dict(entry: Any) -> dict[str, Any]:
    return {
        "state": entry.state.value,
        "account": entry.account_id,
        "model": entry.canonical_model_id,
        "protocol": entry.upstream_protocol,
        "provenance": entry.evidence_provenance.value,
        "observation_count": entry.observation_count,
        "expiry_kind": "bounded" if entry.expiry is not None else "terminal",
    }


def _health_observation(mono: FakeClock) -> HealthObservation:
    failures = tuple(
        {
            "error_class": error_class,
            "status_code": status,
            "category": classify_failure_category(error_class, status).value,
        }
        for error_class, status in (
            ("auth_failed", 401),
            ("anything", 402),
            ("timeout", 408),
            ("conflict", 409),
            ("unprocessable", 422),
            ("rate_limited", 429),
            ("server", 500),
        )
    )
    manager = HealthManager(clock=mono)
    with seeded_python_random(17):
        delay = manager.record_failure_with_policy("account-a", "connection_failure")
    manager.record_rate_limit("account-b", 0.0)
    manager.disable_model("account-a", "model-x", duration_seconds=60)
    manager.get_account_health("account-a").circuit_breaker.failure_threshold = 1
    manager.record_failure("account-a", reason="transient")
    circuit = manager.get_account_health("account-a").circuit_breaker
    mono.advance(300)
    probe_available = circuit.can_request()
    probe_acquired = manager.try_acquire_request("account-a", "model-x")
    manager.release_request("account-a")

    quarantine = ModelQuarantine(suspected_ttl=10, quarantined_ttl=20)
    suspected = quarantine.record_observation(
        provider_id="provider-a",
        account_id="account-a",
        canonical_model_id="model-x",
        upstream_model_id="upstream-x",
        upstream_protocol="openai",
        evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
        reason="model_unavailable",
        now=mono(),
    )
    quarantined = quarantine.record_observation(
        provider_id="provider-a",
        account_id="account-a",
        canonical_model_id="model-x",
        upstream_model_id="upstream-x",
        upstream_protocol="openai",
        evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
        reason="model_unavailable",
        now=mono() + 1,
    )
    expired_at = mono() + 22
    expired_before = quarantine.is_model_quarantined(
        "provider-a", "account-a", "model-x", "upstream-x", "openai", now=expired_at
    )
    terminal = quarantine.set_terminal_withdrawn(
        "provider-a",
        "account-a",
        "model-terminal",
        None,
        "openai",
        provenance=EvidenceProvenance.PROVIDER_CATALOG,
        now=mono(),
    )
    return HealthObservation(
        failures=failures
        + (
            {
                "category": "connection_failure",
                "deterministic_delay_s": compute_backoff_seconds(
                    "connection_failure", 1, jitter=False
                ),
                "observed_delay_s_bounded": delay is not None,
            },
        ),
        accounts=(
            {
                "account": name,
                "state": manager.get_account_health(name).health_state,
                "healthy_read_only": manager.is_account_healthy_read_only(name),
                "model_disabled": manager.get_account_health(name).is_model_disabled(
                    "model-x", mono()
                ),
            }
            for name in ("account-a", "account-b")
        ),
        circuits=(
            {
                "state_before_probe": CircuitState.OPEN.value,
                "can_request_without_mutation": probe_available,
                "probe_acquired": probe_acquired,
                "state_after_release": circuit.state.value,
            },
        ),
        quarantine=(
            _entry_dict(suspected),
            _entry_dict(quarantined),
            {"expired_is_quarantined": expired_before},
            _entry_dict(terminal),
        ),
    )


async def _routing_observation(config: AppConfig) -> RoutingObservation:
    registry = AccountRegistry(config)
    cache = ModelCatalogCache()
    cache.set_config(config)
    model = {
        "model_id": "route-model",
        "protocol": "openai",
        "protocol_source": "config",
        "capabilities": {},
    }
    cache.update_from_account(
        "account-a", "provider-a", [model], authoritative=True, allow_withdrawals=True
    )
    cache.update_from_account(
        "account-b", "provider-b", [model], authoritative=True, allow_withdrawals=True
    )
    exclusions: list[tuple[str, str]] = []
    eligible = get_eligible_accounts(
        registry.get_all_states(),
        "route-model",
        cache,
        provider_id=None,
        protocol="openai",
        exclusion_sink=exclusions,
        account_supports_protocol=registry.account_supports_protocol,
        account_supports_request_surface=registry.account_supports_request_surface,
    )
    estimator = QuotaEstimator(
        accounts={
            name: AccountQuota(account_name=name) for name in ("account-a", "account-b")
        }
    )
    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts([state.name for state in eligible])
    ranked = scorer.rank_accounts(scores)
    pairs = [(registry.get_state(score.account_name), score) for score in ranked]
    pairs = [(state, score) for state, score in pairs if state is not None]
    rotor = FairnessRotor()
    rotated, fairness = await rotor.rotate(
        FairnessKey("provider-a", "route-model", "openai", 10), pairs
    )
    return RoutingObservation(
        requested_model="route-model",
        requested_provider=None,
        requested_protocol="openai",
        request_surface="chat_completions",
        eligible_candidates=tuple(state.name for state in eligible),
        exclusions=tuple(
            {"account": name, "reason": reason} for name, reason in exclusions
        ),
        tier=10,
        score_components=tuple(
            {
                "account": score.account_name,
                "final_score": score.final_score,
                "requires_transcode": score.requires_transcode,
            }
            for score in ranked
        ),
        native_vs_transcode=tuple(
            {"account": score.account_name, "native": not score.requires_transcode}
            for score in ranked
        ),
        fairness=fairness.to_dict(),
        ordered_ranking=tuple(state.name for state, _score in rotated),
        selected_account=rotated[0][0].name if rotated else None,
        claim={
            "ownership_token": "local-fixture-claim-1",
            "active_request_delta": 1,
            "pending_request_delta": 1,
            "pending_token_delta": 128,
            "durable_persistence": "deferred:M7",
        },
    )


async def _model_router_observation(mono: FakeClock) -> ModelRouterObservation:
    router = compile_model_router(
        "virtual-route",
        ModelRouterConfig.model_validate(
            {
                "selector_model": "selector-model",
                "default_model": "model-default",
                "routes": {
                    "z-fast": {"model": "model-fast", "description": " Fast\tpath "},
                    "a-default": {
                        "model": "model-default",
                        "description": "Default\npath",
                    },
                },
                "affinity_ttl_s": 60,
                "max_input_bytes": 256,
            }
        ),
    )
    identity = session_identity_from_header("fixture-session")
    assert identity is not None
    affinity = ModelRouterAffinity(max_entries=2, clock=mono)
    route = router.route_by_id["0"]

    async def select() -> ModelSelection:
        return ModelSelection(
            virtual_model=router.virtual_model,
            route_id=route.route_id,
            route_label=route.label,
            concrete_model=route.model,
            source="default",
            selector_attempts=0,
            selector_latency_ms=0.0,
        )

    first = await affinity.resolve(router, identity, select)
    second = await affinity.resolve(router, identity, select)
    return ModelRouterObservation(
        virtual_model=router.virtual_model,
        route_ids=tuple(
            {
                "route_id": route.route_id,
                "label": route.label,
                "model": route.model,
                "description": route.description,
            }
            for route in router.routes
        ),
        selector_model=router.selector_model,
        default_model=router.default_model,
        static_policy_base64=base64.b64encode(router.static_policy).decode("ascii"),
        static_policy_length=len(router.static_policy),
        static_policy_digest=hashlib.sha256(router.static_policy).hexdigest(),
        config_fingerprint=router.config_fingerprint,
        sticky=router.sticky,
        affinity_ttl_s=router.affinity_ttl_s,
        max_input_bytes=router.max_input_bytes,
        affinity_key_digest=identity.digest.hex(),
        cache_outcome="miss_then_hit"
        if not first.cache_hit and second.cache_hit
        else "unexpected",
        selected_concrete_model=second.decision.concrete_model,
        cache_stats={
            "hits": affinity.stats.hits,
            "misses": affinity.stats.misses,
            "evictions": affinity.stats.evictions,
            "expirations": affinity.stats.expirations,
            "single_flight_leaders": affinity.stats.single_flight_leaders,
            "single_flight_joins": affinity.stats.single_flight_joins,
        },
    )


async def build_snapshot() -> RoutingDomainSnapshot:
    wall = FakeClock()
    mono = FakeClock(5000.0)
    config = _config()
    return RoutingDomainSnapshot(
        schema_version="m5-routing-domain-d001/v1",
        clocks={"wall_epoch": wall(), "monotonic": mono()},
        accounts=_account_observations(config),
        catalog=_catalog_observation(config, wall),
        quota=await _quota_observation(),
        health=_health_observation(mono),
        routing=await _routing_observation(config),
        model_router=await _model_router_observation(mono),
    )


def test_reusable_fake_clocks_are_independent() -> None:
    wall = FakeClock(100)
    mono = FakeClock(20)
    wall.advance(5)
    assert wall() == 105
    assert mono() == 20


def test_required_schema_families_are_structured() -> None:
    assert {field for field in AccountObservation.__dataclass_fields__} >= {
        "account_id",
        "has_usable_credentials",
        "quota_offsets",
    }
    assert {field for field in RoutingDomainSnapshot.__dataclass_fields__} >= {
        "accounts",
        "catalog",
        "quota",
        "health",
        "routing",
        "model_router",
    }


@pytest.mark.asyncio
async def test_d001_snapshot_is_repeatable_and_matches_committed_observation() -> None:
    first = (await build_snapshot()).dumps()
    second = (await build_snapshot()).dumps()
    assert first == second
    assert json.loads(first) == json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert "FIXTURE_API_KEY" not in first
    assert "proxy-password" not in first
    assert "secret" not in first.lower()


def test_fixture_matrix_covers_all_observation_families_and_is_secret_safe() -> None:
    matrix_path = SNAPSHOT_PATH.with_name("d001-fixture-matrix.json")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert set(matrix["cases"]) == {
        "accounts",
        "catalog",
        "quota",
        "health",
        "circuit_and_quarantine",
        "routing",
        "model_router",
    }
    assert all(matrix["cases"].values())
    assert matrix["secret_markers_forbidden"]
    for path in (
        SNAPSHOT_PATH,
        SNAPSHOT_PATH.with_name("schema54-routing-domain-seed.sql"),
    ):
        content = path.read_text(encoding="utf-8")
        assert "FIXTURE_API_KEY" not in content
        assert "proxy-password" not in content
        assert "Authorization: Bearer" not in content


def test_required_reason_vocabularies_are_stable() -> None:
    assert tuple(dict.fromkeys(ACCOUNT_EXCLUSION_REASONS)) == ACCOUNT_EXCLUSION_REASONS
    assert compute_backoff_seconds("context_limit_exceeded", 1, jitter=False) is None


@pytest.mark.asyncio
async def test_affinity_identity_never_retains_raw_content() -> None:
    request = canonical_request_from_mapping(
        {
            "model": "virtual-route",
            "messages": [{"role": "user", "content": "fixture raw content"}],
        },
        client_surface="chat_completions",
        protocol="openai",
    )
    from eggpool.model_router.affinity import automatic_session_identity

    identity = automatic_session_identity(request, client_surface="chat_completions")
    assert identity is not None
    assert b"fixture raw content" not in identity.digest


@pytest.mark.asyncio
async def test_schema54_seed_opens_through_python_database_layer(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "routing-domain.sqlite3"
    database = Database(path=str(database_path))
    await database.connect()
    try:
        await MigrationRunner(database).run()
        seed = (
            Path(__file__).parents[2]
            / "migration-rs"
            / "fixtures"
            / "routing-domain"
            / "schema54-routing-domain-seed.sql"
        ).read_text(encoding="utf-8")
    finally:
        await database.disconnect()
    raw = await aiosqlite.connect(str(database_path))
    try:
        await raw.executescript(seed)
        await raw.commit()
    finally:
        await raw.close()
    reopened = Database(path=str(database_path))
    await reopened.connect()
    try:
        counts = await reopened.fetch_all(
            "SELECT (SELECT COUNT(*) FROM accounts) AS accounts, "
            "(SELECT COUNT(*) FROM catalog_refresh_state) AS refreshes, "
            "(SELECT COUNT(*) FROM model_quarantine) AS quarantines"
        )
        assert dict(counts[0]) == {"accounts": 3, "refreshes": 2, "quarantines": 2}
    finally:
        await reopened.disconnect()
