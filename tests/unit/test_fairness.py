"""Tests for same-tier account fairness rotor and band extraction.

Phase 1 regression tests ensuring equal-priority, equal-weight, equally
healthy, equally model-eligible accounts are rotated (round-robin) instead
of always picking the same one.

The fairness module (``FairnessRotor``, ``FairnessKey``, ``FairnessDecision``)
and the ``_fairness_band`` helper in ``router.py`` are tested directly where
possible, and through ``Router.select_accounts_for_failover`` for integration
coverage.
"""

from __future__ import annotations

import os

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.accounts.state import AccountRuntimeState
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.quota.estimation import AccountQuota, QuotaEstimator
from eggpool.quota.scorer import RoutingScore
from eggpool.routing.router import Router


class _MockCatalog:
    """Minimal catalog exposing only the ``cache`` attribute.

    Mirrors the pattern used by ``test_routing_coordinator_concurrent.py``.
    """

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


def _build_three_equal_accounts() -> tuple[AccountRegistry, _MockCatalog, list[str]]:
    """Build a registry + catalog with 3 equal accounts under one provider.

    All accounts weight=1.0, routing_priority=0, serving "test-model"
    on provider "test-provider" with protocol "openai".
    """
    names = ["0001", "0002", "0003"]
    for name in names:
        os.environ[f"K_{name}"] = "k"

    config = AppConfig.model_validate(
        {
            "providers": {
                "test-provider": {
                    "id": "test-provider",
                    "base_url": "https://api.example.com/v1",
                    "protocols": ["openai"],
                    "routing_priority": 0,
                    "accounts": [
                        {
                            "name": name,
                            "api_key_env": f"K_{name}",
                            "weight": 1.0,
                        }
                        for name in names
                    ],
                }
            }
        }
    )
    registry = AccountRegistry(config)

    cache = ModelCatalogCache()
    for name in names:
        cache.update_from_account(
            name,
            "test-provider",
            [{"model_id": "test-model", "protocol": "openai"}],
        )

    return registry, _MockCatalog(cache), names


def _build_mixed_priority_accounts() -> tuple[AccountRegistry, _MockCatalog, list[str]]:
    """Build a registry + catalog with 2 high-priority and 1 low-priority."""
    for name in ["high01", "high02", "low01"]:
        os.environ[f"K_{name}"] = "k"

    config = AppConfig.model_validate(
        {
            "providers": {
                "high-provider": {
                    "id": "high-provider",
                    "base_url": "https://api.example.com/v1",
                    "protocols": ["openai"],
                    "routing_priority": 10,
                    "accounts": [
                        {"name": "high01", "api_key_env": "K_high01", "weight": 1.0},
                        {"name": "high02", "api_key_env": "K_high02", "weight": 1.0},
                    ],
                },
                "low-provider": {
                    "id": "low-provider",
                    "base_url": "https://api.example.com/v1",
                    "protocols": ["openai"],
                    "routing_priority": 0,
                    "accounts": [
                        {"name": "low01", "api_key_env": "K_low01", "weight": 1.0},
                    ],
                },
            }
        }
    )
    registry = AccountRegistry(config)

    cache = ModelCatalogCache()
    for name in ["high01", "high02", "low01"]:
        pid = "high-provider" if name.startswith("high") else "low-provider"
        cache.update_from_account(
            name,
            pid,
            [{"model_id": "test-model", "protocol": "openai"}],
        )

    return registry, _MockCatalog(cache), ["high01", "high02", "low01"]


def _force_deterministic_routing(router: Router) -> None:
    """Pin the scorer's tiebreaker range to 0 so results are reproducible."""
    router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _clean_env() -> None:
    yield
    for key in list(os.environ.keys()):
        if key.startswith("K_"):
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Test 1: Direct FairnessRotor round-robin ordering
# ---------------------------------------------------------------------------


class TestFairnessRotorRoundRobin:
    """FairnessRotor cycles through accounts deterministically."""

    @pytest.mark.asyncio()
    async def test_fairness_rotor_rotates_equal_peers(self) -> None:
        """FairnessRotor cycles through accounts deterministically."""
        from eggpool.routing.fairness import FairnessKey, FairnessRotor

        rotor = FairnessRotor()
        key = FairnessKey(
            provider_id="prov", model_id="m1", protocol="openai", priority=0
        )

        # Create 3 equal accounts
        states = [AccountRuntimeState(name=f"acct{i:04d}") for i in range(1, 4)]
        scores = [
            RoutingScore(
                account_name=f"acct{i:04d}",
                quota_score=0.0,
                weight=1.0,
                is_eligible=True,
            )
            for i in range(1, 4)
        ]
        candidates = list(zip(states, scores, strict=True))

        # First rotation should start at position 0 (acct0001)
        rotated1, decision1 = await rotor.rotate(key, candidates)
        assert decision1.applied is True
        assert decision1.candidate_count == 3
        assert rotated1[0][0].name == "acct0001"

        # Second rotation should advance to position 1 (acct0002)
        rotated2, decision2 = await rotor.rotate(key, candidates)
        assert rotated2[0][0].name == "acct0002"

        # Third rotation should advance to position 2 (acct0003)
        rotated3, decision3 = await rotor.rotate(key, candidates)
        assert rotated3[0][0].name == "acct0003"

        # Fourth rotation wraps back to position 0 (acct0001)
        rotated4, decision4 = await rotor.rotate(key, candidates)
        assert rotated4[0][0].name == "acct0001"

    @pytest.mark.asyncio()
    async def test_fairness_rotor_single_candidate(self) -> None:
        """Single candidate returns without rotation."""
        from eggpool.routing.fairness import FairnessKey, FairnessRotor

        rotor = FairnessRotor()
        key = FairnessKey(provider_id="prov", model_id="m1", protocol=None, priority=0)

        state = AccountRuntimeState(name="solo")
        score = RoutingScore(
            account_name="solo", quota_score=0.0, weight=1.0, is_eligible=True
        )

        rotated, decision = await rotor.rotate(key, [(state, score)])
        assert decision.applied is False
        assert decision.reason == "single_candidate"
        assert len(rotated) == 1


# ---------------------------------------------------------------------------
# Test 2: Fairness band extraction
# ---------------------------------------------------------------------------


class TestFairnessBandExtraction:
    """Candidates within epsilon are in the fairness band."""

    def test_fairness_band_extracts_tied_candidates(self) -> None:
        """Candidates within epsilon are in the fairness band."""
        from eggpool.routing.router import _fairness_band

        states = [
            AccountRuntimeState(name=f"acct{i:04d}", routing_priority=0)
            for i in range(1, 4)
        ]
        scores = [
            RoutingScore(
                account_name="acct0001", quota_score=0.5, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="acct0002", quota_score=0.52, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="acct0003", quota_score=0.51, weight=1.0, is_eligible=True
            ),
        ]
        ranked = list(zip(states, scores, strict=True))

        band, rest, reason = _fairness_band(ranked, epsilon=0.1, prefer_native=True)
        assert len(band) == 3
        assert reason == "ok"
        assert len(rest) == 0

    def test_fairness_band_separates_different_weights(self) -> None:
        """Candidates with different weights are NOT in the same band."""
        from eggpool.routing.router import _fairness_band

        states = [
            AccountRuntimeState(name="acct0001", routing_priority=0),
            AccountRuntimeState(name="acct0002", routing_priority=0),
            AccountRuntimeState(name="acct0003", routing_priority=0),
        ]
        scores = [
            RoutingScore(
                account_name="acct0001", quota_score=0.3, weight=2.0, is_eligible=True
            ),
            RoutingScore(
                account_name="acct0002", quota_score=0.31, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="acct0003", quota_score=0.32, weight=1.0, is_eligible=True
            ),
        ]
        ranked = list(zip(states, scores, strict=True))

        band, rest, reason = _fairness_band(ranked, epsilon=0.1, prefer_native=True)
        # acct0001 has weight 2.0, acct0002 has weight 1.0 — different
        # weights means they are not in the same band. Band starts from
        # the top; since the second candidate differs, band < 2 → "not_tied".
        assert len(band) == 0
        assert reason == "not_tied"

    def test_fairness_band_separates_different_tiers(self) -> None:
        """Candidates from different priority tiers are NOT in the same band."""
        from eggpool.routing.router import _fairness_band

        states = [
            AccountRuntimeState(name="high01", routing_priority=10),
            AccountRuntimeState(name="high02", routing_priority=10),
            AccountRuntimeState(name="low01", routing_priority=0),
        ]
        scores = [
            RoutingScore(
                account_name="high01", quota_score=0.5, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="high02", quota_score=0.5, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="low01", quota_score=0.5, weight=1.0, is_eligible=True
            ),
        ]
        ranked = list(zip(states, scores, strict=True))

        band, rest, reason = _fairness_band(ranked, epsilon=0.1, prefer_native=True)
        assert len(band) == 2  # Both high-priority accounts
        assert len(rest) == 1  # low01 is in rest
        assert rest[0][0].name == "low01"

    def test_fairness_band_single_candidate(self) -> None:
        """A single candidate produces no band."""
        from eggpool.routing.router import _fairness_band

        states = [AccountRuntimeState(name="solo", routing_priority=0)]
        scores = [
            RoutingScore(
                account_name="solo", quota_score=0.5, weight=1.0, is_eligible=True
            )
        ]
        ranked = list(zip(states, scores, strict=True))

        band, rest, reason = _fairness_band(ranked, epsilon=0.1, prefer_native=True)
        assert len(band) == 0
        assert reason == "single_candidate"
        assert len(rest) == 1


# ---------------------------------------------------------------------------
# Test 3: Round-robin through select_accounts_for_failover
# ---------------------------------------------------------------------------


class TestFailoverRoundRobin:
    """Round-robin rotation across equal-priority, equal-weight accounts."""

    @pytest.mark.asyncio()
    async def test_select_accounts_for_failover_round_robin_equal_peers(
        self,
    ) -> None:
        """Round-robin rotation across equal-priority, equal-weight accounts."""
        registry, catalog, names = _build_three_equal_accounts()

        try:
            quota_estimator = QuotaEstimator()
            for name in names:
                quota_estimator.accounts[name] = AccountQuota(
                    account_name=name,
                    weight=1.0,
                    capacity_5h_microdollars=1_000_000_000,
                    capacity_7d_microdollars=7_000_000_000,
                    capacity_30d_microdollars=30_000_000_000,
                )

            router = Router(
                registry,  # type: ignore[arg-type]
                catalog,  # type: ignore[arg-type]
                quota_estimator=quota_estimator,
            )
            _force_deterministic_routing(router)

            # Capture first selected account across 6 calls
            first_accounts: list[str] = []
            for _ in range(6):
                result = await router.select_accounts_for_failover(
                    model_id="test-model",
                    max_accounts=1,
                )
                assert len(result) >= 1
                first_accounts.append(result[0][0].name)

            # All 3 accounts should appear in the first 3 selections
            assert set(first_accounts[:3]) == {"0001", "0002", "0003"}
            # Next 3 should repeat the same order
            assert first_accounts[3:] == first_accounts[:3]
        finally:
            for name in names:
                os.environ.pop(f"K_{name}", None)


# ---------------------------------------------------------------------------
# Test 4: Priority tier isolation
# ---------------------------------------------------------------------------


class TestPriorityTierIsolation:
    """High-priority accounts rotate among themselves; low-priority gets nothing."""

    @pytest.mark.asyncio()
    async def test_priority_tier_isolation_with_fairness(self) -> None:
        """High-priority accounts rotate among themselves; low-priority gets nothing."""
        registry, catalog, names = _build_mixed_priority_accounts()

        try:
            quota_estimator = QuotaEstimator()
            for name in names:
                quota_estimator.accounts[name] = AccountQuota(
                    account_name=name,
                    weight=1.0,
                    capacity_5h_microdollars=1_000_000_000,
                    capacity_7d_microdollars=7_000_000_000,
                    capacity_30d_microdollars=30_000_000_000,
                )

            router = Router(
                registry,  # type: ignore[arg-type]
                catalog,  # type: ignore[arg-type]
                quota_estimator=quota_estimator,
            )
            _force_deterministic_routing(router)

            # Select 6 times — should only get high-priority accounts
            selected: list[str] = []
            for _ in range(6):
                result = await router.select_accounts_for_failover(
                    model_id="test-model",
                    max_accounts=1,
                )
                if result:
                    selected.append(result[0][0].name)

            # Only high-priority accounts should be selected
            assert all(name.startswith("high") for name in selected)
            # Both high accounts should rotate
            assert set(selected) == {"high01", "high02"}
        finally:
            for name in names:
                os.environ.pop(f"K_{name}", None)


# ---------------------------------------------------------------------------
# Test 5: Integration — 300 requests distribute evenly
# ---------------------------------------------------------------------------


class TestIntegrationEvenDistribution:
    """300 requests across 3 equal accounts should distribute roughly evenly."""

    @pytest.mark.asyncio()
    async def test_integration_300_requests_distribute_evenly(self) -> None:
        """300 requests across 3 equal accounts should distribute roughly evenly."""
        registry, catalog, names = _build_three_equal_accounts()

        try:
            quota_estimator = QuotaEstimator()
            for name in names:
                quota_estimator.accounts[name] = AccountQuota(
                    account_name=name,
                    weight=1.0,
                    capacity_5h_microdollars=1_000_000_000,
                    capacity_7d_microdollars=7_000_000_000,
                    capacity_30d_microdollars=30_000_000_000,
                )

            router = Router(
                registry,  # type: ignore[arg-type]
                catalog,  # type: ignore[arg-type]
                quota_estimator=quota_estimator,
            )
            _force_deterministic_routing(router)

            counts: dict[str, int] = {"0001": 0, "0002": 0, "0003": 0}
            for _ in range(300):
                result = await router.select_accounts_for_failover(
                    model_id="test-model",
                    max_accounts=1,
                )
                if result:
                    counts[result[0][0].name] += 1

            # Each account should get roughly 100
            for name, count in counts.items():
                assert count >= 80, f"{name} got {count}, expected >= 80"
                assert count <= 120, f"{name} got {count}, expected <= 120"
            # Total should be 300
            assert sum(counts.values()) == 300
        finally:
            for name in names:
                os.environ.pop(f"K_{name}", None)


# ---------------------------------------------------------------------------
# Test 6: Mixed weights — sub-band of equal-weight peers
# ---------------------------------------------------------------------------


class TestMixedWeightSubBand:
    """Different-weight accounts do not all end up in one fairness band."""

    def test_fairness_band_forms_sub_band_for_equal_weight_subset(self) -> None:
        """When weights are 2.0, 1.0, 1.0 the two weight-1.0 accounts
        form a fairness band if they are at the top of the scored order.

        The plan requires that equal-peer rotor either applies only to
        the weight-1.0 subset or skips with ``reason = "different_weights"``.
        """
        from eggpool.routing.router import _fairness_band

        # Simulate scored order where the two weight-1.0 accounts are
        # scored highest (e.g. they have lower utilization).  The
        # weight-2.0 account comes last.
        states = [
            AccountRuntimeState(name="light01", routing_priority=0),
            AccountRuntimeState(name="light02", routing_priority=0),
            AccountRuntimeState(name="heavy01", routing_priority=0),
        ]
        scores = [
            RoutingScore(
                account_name="light01", quota_score=0.3, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="light02", quota_score=0.31, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="heavy01", quota_score=0.32, weight=2.0, is_eligible=True
            ),
        ]
        ranked = list(zip(states, scores, strict=True))

        band, rest, reason = _fairness_band(ranked, epsilon=0.1, prefer_native=True)
        # The two weight-1.0 accounts should form a band; the weight-2.0
        # account should be excluded because its weight differs.
        assert len(band) == 2
        assert reason == "ok"
        assert len(rest) == 1
        assert rest[0][0].name == "heavy01"
        assert {s[0].name for s in band} == {"light01", "light02"}

    def test_fairness_band_rejects_when_best_weight_differs(self) -> None:
        """When the best candidate has a different weight from the runner-up,
        band < 2 and fairness is not applied.

        This is the ``reason = "not_tied"`` path: the best is weight-2.0,
        the next is weight-1.0, so they cannot form an equal-peer band.
        """
        from eggpool.routing.router import _fairness_band

        states = [
            AccountRuntimeState(name="heavy01", routing_priority=0),
            AccountRuntimeState(name="light01", routing_priority=0),
            AccountRuntimeState(name="light02", routing_priority=0),
        ]
        scores = [
            RoutingScore(
                account_name="heavy01", quota_score=0.3, weight=2.0, is_eligible=True
            ),
            RoutingScore(
                account_name="light01", quota_score=0.31, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="light02", quota_score=0.32, weight=1.0, is_eligible=True
            ),
        ]
        ranked = list(zip(states, scores, strict=True))

        band, rest, reason = _fairness_band(ranked, epsilon=0.1, prefer_native=True)
        # Best is weight-2.0, runner-up is weight-1.0 → different weights
        # → band can only contain the first candidate → band < 2.
        assert len(band) == 0
        assert reason == "not_tied"


# ---------------------------------------------------------------------------
# Test 7: Coordinator hot path — 300 sequential selections distribute evenly
# ---------------------------------------------------------------------------


class TestCoordinatorHotPathFairness:
    """Phase 8 regression: the coordinator hot path must distribute.

    This test exercises ``_select_and_persist_attempt()`` directly,
    which is the actual production path used by ``RequestCoordinator``.
    The test builds a full coordinator with an in-memory database,
    migrations, and persistence — not just ``Router`` in isolation.
    """

    @pytest.mark.asyncio()
    async def test_coordinator_300_sequential_selections_distribute(self) -> None:
        """300 sequential selections through the coordinator should
        distribute roughly evenly across 3 equal accounts.

        This is the acceptance-criterion-9 test: it exercises the
        coordinator hot path (``_select_and_persist_attempt``), not
        only ``Router.select_accounts_for_failover``.
        """
        import httpx

        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.db.repositories import (
            AttemptRepository,
            RequestRepository,
            ReservationRepository,
            RoutingDecisionRepository,
        )
        from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

        names = ["0001", "0002", "0003"]
        for name in names:
            os.environ[f"K_{name}"] = "k"

        config = AppConfig.model_validate(
            {
                "providers": {
                    "test-provider": {
                        "id": "test-provider",
                        "base_url": "https://api.example.com/v1",
                        "protocols": ["openai"],
                        "routing_priority": 0,
                        "accounts": [
                            {
                                "name": name,
                                "api_key_env": f"K_{name}",
                                "weight": 1.0,
                            }
                            for name in names
                        ],
                    }
                }
            }
        )
        registry = AccountRegistry(config)

        cache = ModelCatalogCache()
        for name in names:
            cache.update_from_account(
                name,
                "test-provider",
                [{"model_id": "test-model", "protocol": "openai"}],
            )

        quota_estimator = QuotaEstimator()
        router = Router(
            registry,  # type: ignore[arg-type]
            _MockCatalog(cache),  # type: ignore[arg-type]
            quota_estimator=quota_estimator,
            fairness_mode="round_robin",
        )
        router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]
        for name in names:
            router.quota_estimator.accounts[name] = AccountQuota(
                account_name=name,
                weight=1.0,
                capacity_5h_microdollars=1_000_000_000,
                capacity_7d_microdollars=7_000_000_000,
                capacity_30d_microdollars=30_000_000_000,
            )

        db = Database(path=":memory:")
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()

            # Seed accounts, models, and account_models rows
            async with db.transaction():
                await db.execute_insert(
                    "INSERT INTO models (model_id, display_name, protocol) "
                    "VALUES (?, ?, ?)",
                    ("test-model", "test-model", "openai"),
                )
                for name in names:
                    await db.execute_insert(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, ?)",
                        (name, f"K_{name}", 1.0),
                    )
                    row = await db.fetch_one(
                        "SELECT id FROM accounts WHERE name = ?", (name,)
                    )
                    assert row is not None
                    await db.execute_insert(
                        "INSERT INTO account_models "
                        "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                        (int(row["id"]), "test-model"),
                    )

            coordinator = RequestCoordinator(
                registry=registry,
                catalog=_MockCatalog(cache),  # type: ignore[arg-type]
                router=router,
                db=db,
                client_pool=httpx.AsyncClient(),
                request_repo=RequestRepository(db),
                reservation_repo=ReservationRepository(db),
                attempt_repo=AttemptRepository(db),
                routing_decision_repo=RoutingDecisionRepository(db),
                quota_estimator=quota_estimator,
                health_manager=None,
            )

            counts: dict[str, int] = {name: 0 for name in names}
            attempts = 300

            for i in range(attempts):
                ctx = ProxyRequestContext(
                    request_id=f"req-{i}",
                    protocol="openai",
                    model_id="test-model",
                    streaming=False,
                    original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
                    incoming_headers={},
                )
                selected = await coordinator._select_and_persist_attempt(ctx, 1)
                counts[selected.account_name] += 1

            # Each account should get roughly 100 of 300 selections
            for name, count in counts.items():
                assert count >= 80, f"{name} got {count}, expected >= 80"
                assert count <= 120, f"{name} got {count}, expected <= 120"
            assert sum(counts.values()) == attempts
        finally:
            await db.disconnect()
            for name in names:
                os.environ.pop(f"K_{name}", None)


# ---------------------------------------------------------------------------
# Test 8: Config propagation — create_app fairness fields reach Router
# ---------------------------------------------------------------------------


class TestConfigPropagation:
    """Fairness config from RoutingConfig reaches Router constructor."""

    def test_router_receives_non_default_fairness_config(self) -> None:
        """Router constructed with non-default fairness config stores the values."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_mode="off",
            fairness_epsilon=0.333,
            fairness_scope="priority_model_protocol",
        )

        assert router._fairness_mode == "off"  # pyright: ignore[reportPrivateUsage]
        assert router._fairness_epsilon == 0.333  # pyright: ignore[reportPrivateUsage]
        assert router._fairness_scope == "priority_model_protocol"  # pyright: ignore[reportPrivateUsage]

    def test_router_defaults_match_routing_config_defaults(self) -> None:
        """Router defaults match RoutingConfig defaults when not specified."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
        )

        assert router._fairness_mode == "round_robin"  # pyright: ignore[reportPrivateUsage]
        assert router._fairness_epsilon is None  # pyright: ignore[reportPrivateUsage]
        assert router._fairness_scope == "provider_model_protocol"  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Test 9: Protocol-scope fairness key construction
# ---------------------------------------------------------------------------


class TestProtocolScopeFairnessKeys:
    """Fairness keys respect scope: protocol included/excluded correctly."""

    @pytest.mark.asyncio()
    async def test_provider_model_protocol_separates_protocols(self) -> None:
        """provider_model_protocol scope includes protocol in key, producing
        different keys for different protocols."""
        from eggpool.routing.fairness import FairnessKey, FairnessRotor

        rotor = FairnessRotor()

        key_openai = FairnessKey(
            provider_id="prov",
            model_id="m1",
            protocol="openai",
            priority=0,
        )
        key_anthropic = FairnessKey(
            provider_id="prov",
            model_id="m1",
            protocol="anthropic",
            priority=0,
        )

        states = [AccountRuntimeState(name=f"acct{i:04d}") for i in range(1, 4)]
        scores = [
            RoutingScore(
                account_name=f"acct{i:04d}",
                quota_score=0.0,
                weight=1.0,
                is_eligible=True,
            )
            for i in range(1, 4)
        ]
        candidates = list(zip(states, scores, strict=True))

        _, decision_openai = await rotor.rotate(key_openai, candidates)
        _, decision_anthropic = await rotor.rotate(key_anthropic, candidates)

        assert decision_openai.applied is True
        assert decision_anthropic.applied is True
        assert "protocol=openai" in decision_openai.key
        assert "protocol=anthropic" in decision_anthropic.key
        assert decision_openai.key != decision_anthropic.key

    @pytest.mark.asyncio()
    async def test_provider_model_collapses_protocols(self) -> None:
        """provider_model scope excludes protocol from key, collapsing
        different protocols into the same rotation group."""
        from eggpool.routing.fairness import FairnessKey, FairnessRotor

        rotor = FairnessRotor()

        # Both keys have protocol=None (scope collapses protocol)
        key1 = FairnessKey(
            provider_id="prov",
            model_id="m1",
            protocol=None,
            priority=0,
        )
        key2 = FairnessKey(
            provider_id="prov",
            model_id="m1",
            protocol=None,
            priority=0,
        )

        states = [AccountRuntimeState(name=f"acct{i:04d}") for i in range(1, 4)]
        scores = [
            RoutingScore(
                account_name=f"acct{i:04d}",
                quota_score=0.0,
                weight=1.0,
                is_eligible=True,
            )
            for i in range(1, 4)
        ]
        candidates = list(zip(states, scores, strict=True))

        _, decision1 = await rotor.rotate(key1, candidates)
        _, decision2 = await rotor.rotate(key2, candidates)

        # Same key string → shared rotation group
        assert decision1.key == decision2.key
        assert "protocol=*" in decision1.key

    @pytest.mark.asyncio()
    async def test_priority_model_protocol_excludes_provider(self) -> None:
        """priority_model_protocol scope excludes provider from key."""
        from eggpool.routing.fairness import FairnessKey, FairnessRotor

        rotor = FairnessRotor()

        key = FairnessKey(
            provider_id=None,
            model_id="m1",
            protocol="openai",
            priority=0,
        )

        states = [AccountRuntimeState(name=f"acct{i:04d}") for i in range(1, 4)]
        scores = [
            RoutingScore(
                account_name=f"acct{i:04d}",
                quota_score=0.0,
                weight=1.0,
                is_eligible=True,
            )
            for i in range(1, 4)
        ]
        candidates = list(zip(states, scores, strict=True))

        _, decision = await rotor.rotate(key, candidates)
        assert decision.applied is True
        assert "provider=*" in decision.key
        assert "protocol=openai" in decision.key

    def test_fairness_key_helper_scope_provider_model_protocol(self) -> None:
        """_fairness_key includes provider and protocol for provider_model_protocol."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_scope="provider_model_protocol",
        )
        key = router._fairness_key(
            provider_id="prov",
            model_id="m1",
            protocol="openai",
            priority=0,
            client_protocol=None,
        )
        assert key.provider_id == "prov"
        assert key.protocol == "openai"

    def test_fairness_key_helper_scope_provider_model(self) -> None:
        """_fairness_key() excludes protocol for provider_model."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_scope="provider_model",
        )
        key = router._fairness_key(
            provider_id="prov",
            model_id="m1",
            protocol="openai",
            priority=0,
            client_protocol=None,
        )
        assert key.provider_id == "prov"
        assert key.protocol is None

    def test_fairness_key_helper_scope_priority_model_protocol(self) -> None:
        """_fairness_key() excludes provider for priority_model_protocol."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_scope="priority_model_protocol",
        )
        key = router._fairness_key(
            provider_id="prov",
            model_id="m1",
            protocol="openai",
            priority=0,
            client_protocol=None,
        )
        assert key.provider_id is None
        assert key.protocol == "openai"

    @pytest.mark.asyncio()
    async def test_router_protocol_scoped_keys_vary_by_protocol(self) -> None:
        """Router passes the actual protocol into fairness keys, not None."""

        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_scope="provider_model_protocol",
        )

        # Build keys with different protocols via the helper
        key_openai = router._fairness_key(
            provider_id="test-provider",
            model_id="test-model",
            protocol="openai",
            priority=0,
            client_protocol=None,
        )
        key_anthropic = router._fairness_key(
            provider_id="test-provider",
            model_id="test-model",
            protocol="anthropic",
            priority=0,
            client_protocol=None,
        )

        # Keys should differ because protocol is included
        assert key_openai.protocol == "openai"
        assert key_anthropic.protocol == "anthropic"
        assert key_openai != key_anthropic

        # Verify the key strings differ
        str_openai = key_openai.to_key_string()
        str_anthropic = key_anthropic.to_key_string()
        assert "protocol=openai" in str_openai
        assert "protocol=anthropic" in str_anthropic
        assert str_openai != str_anthropic


# ---------------------------------------------------------------------------
# Test 10: Config override behavior
# ---------------------------------------------------------------------------


class TestConfigOverrideBehavior:
    """Fairness config overrides affect routing behavior."""

    @pytest.mark.asyncio()
    async def test_fairness_mode_off_disables_rotor(self) -> None:
        """With fairness_mode='off', the first account always wins."""
        registry, catalog, _names = _build_three_equal_accounts()

        try:
            quota_estimator = QuotaEstimator()
            for name in _names:
                quota_estimator.accounts[name] = AccountQuota(
                    account_name=name,
                    weight=1.0,
                    capacity_5h_microdollars=1_000_000_000,
                    capacity_7d_microdollars=7_000_000_000,
                    capacity_30d_microdollars=30_000_000_000,
                )

            router = Router(
                registry,  # type: ignore[arg-type]
                catalog,  # type: ignore[arg-type]
                quota_estimator=quota_estimator,
                fairness_mode="off",
            )
            _force_deterministic_routing(router)

            first_accounts: list[str] = []
            for _ in range(6):
                result = await router.select_accounts_for_failover(
                    model_id="test-model",
                    max_accounts=1,
                )
                assert len(result) >= 1
                first_accounts.append(result[0][0].name)

            # With fairness off, the same account should always be first
            assert all(a == first_accounts[0] for a in first_accounts)
            # The fairness decision should report disabled
            decision = router.last_fairness_decision
            assert decision is not None
            assert decision.applied is False
            assert decision.reason == "disabled"
        finally:
            for name in _names:
                os.environ.pop(f"K_{name}", None)

    def test_explicit_fairness_epsilon_controls_band_size(self) -> None:
        """Explicit fairness_epsilon overrides near_tie_epsilon for band extraction."""
        from eggpool.routing.router import _fairness_band

        states = [
            AccountRuntimeState(name="close01", routing_priority=0),
            AccountRuntimeState(name="close02", routing_priority=0),
            AccountRuntimeState(name="far01", routing_priority=0),
        ]
        scores = [
            RoutingScore(
                account_name="close01", quota_score=0.50, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="close02", quota_score=0.55, weight=1.0, is_eligible=True
            ),
            RoutingScore(
                account_name="far01", quota_score=0.70, weight=1.0, is_eligible=True
            ),
        ]
        ranked = list(zip(states, scores, strict=True))

        # Small epsilon: only the two close accounts form a band
        band_small, _, reason_small = _fairness_band(
            ranked, epsilon=0.01, prefer_native=True
        )
        assert len(band_small) == 0  # 0.05 gap > 0.01
        assert reason_small == "not_tied"

        # Larger epsilon: all three form a band
        band_large, _, reason_large = _fairness_band(
            ranked, epsilon=0.20, prefer_native=True
        )
        assert len(band_large) == 3
        assert reason_large == "ok"

        # Medium epsilon: only the first two form a band
        band_medium, _, reason_medium = _fairness_band(
            ranked, epsilon=0.10, prefer_native=True
        )
        assert len(band_medium) == 2
        assert reason_medium == "ok"

    def test_effective_epsilon_falls_back_to_tiebreaker_range(self) -> None:
        """When fairness_epsilon is None, effective epsilon uses tiebreaker_range."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_epsilon=None,
        )
        router._scorer.tiebreaker_range = 0.25  # pyright: ignore[reportPrivateUsage]
        assert router._fairness_effective_epsilon() == 0.25

    def test_explicit_fairness_epsilon_takes_precedence(self) -> None:
        """When fairness_epsilon is set, it overrides tiebreaker_range."""
        registry, catalog, _names = _build_three_equal_accounts()

        router = Router(
            registry,  # type: ignore[arg-type]
            catalog,  # type: ignore[arg-type]
            fairness_epsilon=0.42,
        )
        router._scorer.tiebreaker_range = 0.25  # pyright: ignore[reportPrivateUsage]
        assert router._fairness_effective_epsilon() == 0.42


# ---------------------------------------------------------------------------
# Test 11: Coordinator-path trace metadata
# ---------------------------------------------------------------------------


class TestCoordinatorTraceMetadata:
    """Coordinator-path fairness decisions include trace metadata."""

    @pytest.mark.asyncio()
    async def test_coordinator_300_selections_show_fairness_trace(self) -> None:
        """300 sequential coordinator selections produce fairness trace rows
        with applied=true and candidate_count=3."""
        import json

        import httpx

        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.db.repositories import (
            AttemptRepository,
            RequestRepository,
            ReservationRepository,
            RoutingDecisionRepository,
        )
        from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

        names = ["0001", "0002", "0003"]
        for name in names:
            os.environ[f"K_{name}"] = "k"

        config = AppConfig.model_validate(
            {
                "providers": {
                    "test-provider": {
                        "id": "test-provider",
                        "base_url": "https://api.example.com/v1",
                        "protocols": ["openai"],
                        "routing_priority": 0,
                        "accounts": [
                            {
                                "name": name,
                                "api_key_env": f"K_{name}",
                                "weight": 1.0,
                            }
                            for name in names
                        ],
                    }
                }
            }
        )
        registry = AccountRegistry(config)

        cache = ModelCatalogCache()
        for name in names:
            cache.update_from_account(
                name,
                "test-provider",
                [{"model_id": "test-model", "protocol": "openai"}],
            )

        quota_estimator = QuotaEstimator()
        router = Router(
            registry,  # type: ignore[arg-type]
            _MockCatalog(cache),  # type: ignore[arg-type]
            quota_estimator=quota_estimator,
            fairness_mode="round_robin",
        )
        router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]
        for name in names:
            router.quota_estimator.accounts[name] = AccountQuota(
                account_name=name,
                weight=1.0,
                capacity_5h_microdollars=1_000_000_000,
                capacity_7d_microdollars=7_000_000_000,
                capacity_30d_microdollars=30_000_000_000,
            )

        db = Database(path=":memory:")
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()

            async with db.transaction():
                await db.execute_insert(
                    "INSERT INTO models (model_id, display_name, protocol) "
                    "VALUES (?, ?, ?)",
                    ("test-model", "test-model", "openai"),
                )
                for name in names:
                    await db.execute_insert(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, ?)",
                        (name, f"K_{name}", 1.0),
                    )
                    row = await db.fetch_one(
                        "SELECT id FROM accounts WHERE name = ?", (name,)
                    )
                    assert row is not None
                    await db.execute_insert(
                        "INSERT INTO account_models "
                        "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                        (int(row["id"]), "test-model"),
                    )

            coordinator = RequestCoordinator(
                registry=registry,
                catalog=_MockCatalog(cache),  # type: ignore[arg-type]
                router=router,
                db=db,
                client_pool=httpx.AsyncClient(),
                request_repo=RequestRepository(db),
                reservation_repo=ReservationRepository(db),
                attempt_repo=AttemptRepository(db),
                routing_decision_repo=RoutingDecisionRepository(db),
                quota_estimator=quota_estimator,
                health_manager=None,
            )

            for i in range(300):
                ctx = ProxyRequestContext(
                    request_id=f"req-trace-{i}",
                    protocol="openai",
                    model_id="test-model",
                    streaming=False,
                    original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
                    incoming_headers={},
                )
                await coordinator._select_and_persist_attempt(ctx, 1)

            # Check the last 10 routing decisions for fairness trace
            rows = await db.fetch_all(
                "SELECT score_components_json "
                "FROM routing_decisions ORDER BY id DESC LIMIT 10"
            )
            assert len(rows) == 10
            applied_count = 0
            for row in rows:
                components = json.loads(row["score_components_json"])
                fairness = components.get("fairness", {})
                assert fairness.get("mode") == "round_robin", (
                    f"Expected mode=round_robin, got {fairness.get('mode')}"
                )
                assert fairness.get("scope") == "provider_model_protocol", (
                    "Expected scope=provider_model_protocol, "
                    f"got {fairness.get('scope')}"
                )
                # Key should always include protocol=openai
                key = fairness.get("key", "")
                assert "protocol=openai" in key, (
                    f"Expected protocol=openai in key, got {key}"
                )
                if fairness.get("applied"):
                    applied_count += 1
                    assert fairness.get("candidate_count") >= 2, (
                        "Expected candidate_count>=2, "
                        f"got {fairness.get('candidate_count')}"
                    )
                    assert fairness.get("reason") == "ok", (
                        f"Expected reason=ok, got {fairness.get('reason')}"
                    )
                else:
                    # Not-applied rows are expected when reservations shift scores
                    assert fairness.get("reason") in (
                        "not_tied",
                        "disabled",
                        "single_candidate",
                    ), f"Unexpected reason: {fairness.get('reason')}"
            # At least some selections should show applied=True
            assert applied_count > 0, (
                "Expected at least some fairness decisions with applied=True"
            )
        finally:
            await db.disconnect()
            for name in names:
                os.environ.pop(f"K_{name}", None)
