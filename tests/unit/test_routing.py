"""Tests for routing and eligibility."""

from __future__ import annotations

import asyncio
import os

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.accounts.state import AccountRuntimeState
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.quota.estimation import AccountQuota, QuotaEstimator
from eggpool.quota.scorer import QuotaFairScorer, RoutingScore
from eggpool.routing.config import routing_stale_after_s
from eggpool.routing.eligibility import get_eligible_accounts
from eggpool.routing.router import Router


def test_eligible_accounts_basic() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    states = [
        AccountRuntimeState(name="acct1", enabled=True),
        AccountRuntimeState(name="acct2", enabled=False),
    ]
    eligible = get_eligible_accounts(states, "gpt-4", cache)
    assert len(eligible) == 1
    assert eligible[0].name == "acct1"


def test_eligible_accounts_excludes_cooldown() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    states = [
        AccountRuntimeState(
            name="acct1",
            health_state="cooldown",
            cooldown_until=9999999999,  # Far future
        ),
    ]
    eligible = get_eligible_accounts(states, "gpt-4", cache)
    assert len(eligible) == 0


def test_eligible_accounts_model_not_supported() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    states = [AccountRuntimeState(name="acct1", enabled=True)]
    eligible = get_eligible_accounts(states, "claude-3", cache)
    assert len(eligible) == 0


def test_routing_stale_gate_disabled_when_stale_catalog_allowed() -> None:
    config = AppConfig.model_validate(
        {
            "models": {
                "allow_stale_catalog": True,
                "stale_after_s": 7200,
            }
        }
    )

    assert routing_stale_after_s(config) is None


def test_routing_stale_gate_enabled_when_stale_catalog_disallowed() -> None:
    config = AppConfig.model_validate(
        {
            "models": {
                "allow_stale_catalog": False,
                "stale_after_s": 7200,
            }
        }
    )

    assert routing_stale_after_s(config) == 7200.0


def test_eligible_accounts_require_account_resolved_protocol() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "provider-a", [{"model_id": "shared-model", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2", "provider-b", [{"model_id": "shared-model", "protocol": None}]
    )
    states = [
        AccountRuntimeState(name="acct1", enabled=True),
        AccountRuntimeState(name="acct2", enabled=True),
    ]

    eligible = get_eligible_accounts(states, "shared-model", cache)

    assert [state.name for state in eligible] == ["acct1"]


def test_eligible_accounts_can_filter_by_requested_protocol() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "provider-a", [{"model_id": "shared-model", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [{"model_id": "shared-model", "protocol": "anthropic"}],
    )
    states = [
        AccountRuntimeState(name="acct1", enabled=True),
        AccountRuntimeState(name="acct2", enabled=True),
    ]

    eligible = get_eligible_accounts(
        states,
        "shared-model",
        cache,
        protocol="openai",
    )

    assert [state.name for state in eligible] == ["acct1"]


def test_eligible_accounts_can_filter_by_provider_protocol_policy() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "shared-model", "protocol": "anthropic"}],
    )
    states = [AccountRuntimeState(name="acct1", enabled=True)]

    eligible = get_eligible_accounts(
        states,
        "shared-model",
        cache,
        protocol="anthropic",
        account_supports_protocol=lambda _name, requested: requested == "openai",
    )

    assert eligible == []


def test_eligible_accounts_keeps_above_local_quota_in_score_only_mode() -> None:
    """Above-local-quota accounts remain eligible in the default score_only mode.

    This is the key Phase 1 invariant: local cost estimates must never
    hard-suppress routing by themselves. Only upstream-observed
    failures, explicit operator disablement, catalog/protocol
    incompatibility, or an opt-in hard_cap mode may make an account
    ineligible.
    """
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    states = [AccountRuntimeState(name="acct1", enabled=True)]

    estimator = QuotaEstimator()
    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_5h_microdollars=1_000_000,
    )
    estimator.record_usage("acct1", 1000, 5_000_000)

    eligible = get_eligible_accounts(
        states,
        "gpt-4",
        cache,
        quota_estimator=estimator,
        local_quota_mode="score_only",
    )

    assert [state.name for state in eligible] == ["acct1"]


def test_eligible_accounts_excludes_above_local_quota_in_hard_cap_mode() -> None:
    """The hard_cap mode preserves the legacy suppression behavior."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    states = [AccountRuntimeState(name="acct1", enabled=True)]

    estimator = QuotaEstimator()
    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_5h_microdollars=1_000_000,
    )
    estimator.record_usage("acct1", 1000, 5_000_000)

    eligible = get_eligible_accounts(
        states,
        "gpt-4",
        cache,
        quota_estimator=estimator,
        local_quota_mode="hard_cap",
    )

    assert eligible == []


@pytest.mark.asyncio()
async def test_router_selects_account() -> None:
    os.environ["TEST_ROUTER_KEY"] = "key"
    try:
        config = AppConfig.from_dict(
            {
                "accounts": [
                    {"name": "acct1", "api_key_env": "TEST_ROUTER_KEY"},
                ]
            }
        )
        registry = AccountRegistry(config)
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
        )

        # Create a mock catalog service with the cache
        class MockCatalog:
            def __init__(self, c: ModelCatalogCache) -> None:
                self._cache = c

            @property
            def cache(self) -> ModelCatalogCache:
                return self._cache

        catalog = MockCatalog(cache)
        router = Router(registry, catalog)  # type: ignore[arg-type]
        selected = await router.select_account("gpt-4")
        assert selected is not None
        assert selected.name == "acct1"
    finally:
        del os.environ["TEST_ROUTER_KEY"]


@pytest.mark.asyncio()
async def test_router_no_eligible_account() -> None:
    os.environ["TEST_ROUTER_KEY_2"] = "key"
    try:
        config = AppConfig.from_dict(
            {
                "accounts": [
                    {
                        "name": "acct1",
                        "api_key_env": "TEST_ROUTER_KEY_2",
                        "enabled": False,
                    },
                ]
            }
        )
        registry = AccountRegistry(config)
        cache = ModelCatalogCache()

        class MockCatalog:
            def __init__(self, c: ModelCatalogCache) -> None:
                self._cache = c

            @property
            def cache(self) -> ModelCatalogCache:
                return self._cache

        catalog = MockCatalog(cache)
        router = Router(registry, catalog)  # type: ignore[arg-type]
        selected = await router.select_account("gpt-4")
        assert selected is None
    finally:
        del os.environ["TEST_ROUTER_KEY_2"]


@pytest.mark.asyncio()
async def test_router_filters_selection_by_requested_protocol() -> None:
    os.environ["TEST_ROUTER_KEY_A"] = "key-a"
    os.environ["TEST_ROUTER_KEY_B"] = "key-b"
    try:
        config = AppConfig.from_dict(
            {
                "providers": {
                    "provider-a": {
                        "id": "provider-a",
                        "base_url": "https://provider-a.example/v1",
                        "protocols": ["openai"],
                        "accounts": [
                            {
                                "name": "acct1",
                                "api_key_env": "TEST_ROUTER_KEY_A",
                            }
                        ],
                    },
                    "provider-b": {
                        "id": "provider-b",
                        "base_url": "https://provider-b.example/v1",
                        "protocols": ["anthropic"],
                        "accounts": [
                            {
                                "name": "acct2",
                                "api_key_env": "TEST_ROUTER_KEY_B",
                            }
                        ],
                    },
                }
            }
        )
        registry = AccountRegistry(config)
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct1",
            "provider-a",
            [{"model_id": "shared-model", "protocol": "openai"}],
        )
        cache.update_from_account(
            "acct2",
            "provider-b",
            [{"model_id": "shared-model", "protocol": "anthropic"}],
        )

        class MockCatalog:
            def __init__(self, c: ModelCatalogCache) -> None:
                self._cache = c

            @property
            def cache(self) -> ModelCatalogCache:
                return self._cache

        router = Router(registry, MockCatalog(cache))  # type: ignore[arg-type]

        selected = await router.select_account("shared-model", protocol="openai")

        assert selected is not None
        assert selected.name == "acct1"
    finally:
        del os.environ["TEST_ROUTER_KEY_A"]
        del os.environ["TEST_ROUTER_KEY_B"]


@pytest.mark.asyncio()
async def test_router_filters_selection_by_provider_protocol_policy() -> None:
    os.environ["TEST_ROUTER_POLICY_KEY"] = "key"
    try:
        config = AppConfig.from_dict(
            {
                "providers": {
                    "provider-a": {
                        "id": "provider-a",
                        "base_url": "https://provider-a.example/v1",
                        "protocols": ["openai"],
                        "accounts": [
                            {
                                "name": "acct1",
                                "api_key_env": "TEST_ROUTER_POLICY_KEY",
                            }
                        ],
                    }
                }
            }
        )
        registry = AccountRegistry(config)
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct1",
            "provider-a",
            [{"model_id": "shared-model", "protocol": "anthropic"}],
        )

        class MockCatalog:
            def __init__(self, c: ModelCatalogCache) -> None:
                self._cache = c

            @property
            def cache(self) -> ModelCatalogCache:
                return self._cache

        router = Router(registry, MockCatalog(cache))  # type: ignore[arg-type]

        selected = await router.select_account("shared-model", protocol="anthropic")

        assert selected is None
        assert (
            router.get_eligible_account_names("shared-model", protocol="anthropic")
            == []
        )
    finally:
        del os.environ["TEST_ROUTER_POLICY_KEY"]


def test_has_eligible_pairing_uses_provider_specific_protocol() -> None:
    os.environ["TEST_ROUTER_PROVIDER_KEY"] = "key"
    try:
        config = AppConfig.from_dict(
            {
                "providers": {
                    "provider-a": {
                        "id": "provider-a",
                        "base_url": "https://provider-a.example/v1",
                        "protocols": ["openai"],
                        "accounts": [
                            {
                                "name": "acct1",
                                "api_key_env": "TEST_ROUTER_PROVIDER_KEY",
                            }
                        ],
                    }
                }
            }
        )
        registry = AccountRegistry(config)
        cache = ModelCatalogCache()
        cache.load_model(
            model_id="shared-model",
            display_name="Shared Model",
            protocol="",
            capabilities={},
            source_metadata={},
        )
        cache.set_account_provider("acct1", "provider-a")
        cache.set_provider_model_entry(
            "shared-model",
            "provider-a",
            {
                "model_id": "shared-model",
                "protocol": "openai",
                "capabilities": {},
                "source_metadata": {},
            },
        )
        cache.add_account_support("shared-model", "acct1")

        class MockCatalog:
            def __init__(self, c: ModelCatalogCache) -> None:
                self._cache = c

            @property
            def cache(self) -> ModelCatalogCache:
                return self._cache

        router = Router(registry, MockCatalog(cache))  # type: ignore[arg-type]

        assert router.has_eligible_pairing() is True
    finally:
        del os.environ["TEST_ROUTER_PROVIDER_KEY"]


def _make_mock_catalog(model_id: str = "gpt-4") -> ModelCatalogCache:
    """Create a mock catalog cache with a model."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": model_id, "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2", "opencode-go", [{"model_id": model_id, "protocol": "openai"}]
    )
    return cache


@pytest.mark.asyncio()
async def test_5h_usage_changes_selection() -> None:
    """Five-hour usage on one account should route to the other."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", capacity_5h_microdollars=10_000_000)
    estimator.set_account_limits("acct2", capacity_5h_microdollars=10_000_000)
    # Record usage only on acct1
    estimator.record_usage("acct1", 1000, 5_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # acct1 has usage, acct2 has none -- acct2 should score lower (better)
    assert scores[1].quota_score < scores[0].quota_score


@pytest.mark.asyncio()
async def test_7d_usage_changes_selection() -> None:
    """Seven-day usage on one account should route to the other."""
    estimator = QuotaEstimator()
    from eggpool.quota.estimation import PersistedWindowSnapshot

    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_7d_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=5_000_000, cost_30d=0
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        capacity_7d_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    assert scores[1].quota_score < scores[0].quota_score


@pytest.mark.asyncio()
async def test_30d_usage_changes_selection() -> None:
    """Thirty-day usage on one account should route to the other."""
    estimator = QuotaEstimator()
    from eggpool.quota.estimation import PersistedWindowSnapshot

    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_30d_microdollars=60_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=0, cost_30d=30_000_000
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        capacity_30d_microdollars=60_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    assert scores[1].quota_score < scores[0].quota_score


@pytest.mark.asyncio()
async def test_offsets_apply_to_correct_windows() -> None:
    """Manual offset on 5h should not affect 7d routing."""
    estimator = QuotaEstimator()
    from eggpool.quota.estimation import PersistedWindowSnapshot

    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_5h_microdollars=10_000_000,
        capacity_7d_microdollars=10_000_000,
        five_hour_offset=8_000_000,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        capacity_5h_microdollars=10_000_000,
        capacity_7d_microdollars=10_000_000,
        five_hour_offset=0,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # acct1 has 80% offset on 5h window, acct2 has none
    # acct1 should score higher (more utilized)
    assert scores[0].quota_score > scores[1].quota_score


@pytest.mark.asyncio()
async def test_weights_scale_capacities() -> None:
    """Account with weight=2 should have double the capacity."""
    estimator = QuotaEstimator()
    estimator.set_account_weight("acct1", 2.0)
    estimator.set_account_weight("acct2", 1.0)
    estimator.set_account_limits("acct1", capacity_7d_microdollars=20_000_000)
    estimator.set_account_limits("acct2", capacity_7d_microdollars=10_000_000)
    # Equal usage on both accounts
    estimator.record_usage("acct1", 1000, 5_000_000)
    estimator.record_usage("acct2", 1000, 5_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # Both have 50% of their own capacity, so scores should be similar
    # but acct1 has weight=2 vs acct2 weight=1
    assert scores[0].weight == 2.0
    assert scores[1].weight == 1.0


@pytest.mark.asyncio()
async def test_reservations_affect_selection() -> None:
    """Active reservation should make an account less preferred."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", capacity_5h_microdollars=10_000_000)
    estimator.set_account_limits("acct2", capacity_5h_microdollars=10_000_000)
    # Add reservation on acct1
    await estimator.add_reservation("acct1", 4_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # acct1 has reservation, acct2 has none -- acct2 should score lower
    assert scores[1].quota_score < scores[0].quota_score


def test_near_ties_randomize() -> None:
    """Near-tie accounts should not always select the same one."""
    scorer = QuotaFairScorer(tiebreaker_range=0.5)
    # Create scores with very close values
    import random

    random.seed(42)
    selected_names = set()
    for _ in range(100):
        scores = [
            RoutingScore("acct1", 0.5, 1.0, True),
            RoutingScore("acct2", 0.501, 1.0, True),
        ]
        selected = scorer.select_account(scores)
        if selected:
            selected_names.add(selected.account_name)

    # With 100 iterations, both should be selected at least once
    assert len(selected_names) >= 2


@pytest.mark.asyncio()
async def test_restart_hydration_preserves_behavior() -> None:
    """Persisted windows should produce same routing after reload."""
    from eggpool.quota.estimation import PersistedWindowSnapshot

    estimator = QuotaEstimator()
    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_7d_microdollars=10_000_000,
        five_hour_offset=2_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=1_000_000, cost_7d=2_000_000, cost_30d=3_000_000
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        capacity_7d_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # Rebuild estimator with same state (simulating restart)
    estimator2 = QuotaEstimator()
    estimator2.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_7d_microdollars=10_000_000,
        five_hour_offset=2_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=1_000_000, cost_7d=2_000_000, cost_30d=3_000_000
        ),
    )
    estimator2.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        capacity_7d_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer2 = QuotaFairScorer(quota_estimator=estimator2)
    scores2 = await scorer2.score_accounts(["acct1", "acct2"])

    assert scores[0].quota_score == scores2[0].quota_score
    assert scores[1].quota_score == scores2[1].quota_score


@pytest.mark.asyncio()
async def test_offset_does_not_affect_wrong_window() -> None:
    """5h offset should not affect 7d or 30d scores."""
    from eggpool.quota.estimation import PersistedWindowSnapshot

    estimator = QuotaEstimator()
    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        capacity_5h_microdollars=10_000_000,
        capacity_7d_microdollars=10_000_000,
        capacity_30d_microdollars=60_000_000,
        five_hour_offset=5_000_000,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        capacity_5h_microdollars=10_000_000,
        capacity_7d_microdollars=10_000_000,
        capacity_30d_microdollars=60_000_000,
        five_hour_offset=0,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # acct1 has 5h offset only, 7d and 30d should be identical
    assert scores[0].quota_score != scores[1].quota_score

    # Verify 5h offset is the differentiator by checking without it
    estimator.accounts["acct1"].five_hour_offset = 0
    scores_equal = await scorer.score_accounts(["acct1", "acct2"])
    assert scores_equal[0].quota_score == scores_equal[1].quota_score


@pytest.mark.asyncio()
async def test_request_estimate_affects_projected_score() -> None:
    """Incoming request estimate should be included in scoring."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", capacity_5h_microdollars=10_000_000)
    estimator.set_account_limits("acct2", capacity_5h_microdollars=10_000_000)
    # Both accounts start with zero usage
    estimator.record_usage("acct1", 0, 0)
    estimator.record_usage("acct2", 0, 0)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    # Without estimate, both should score equally
    scores_no_est = await scorer.score_accounts(["acct1", "acct2"])
    assert scores_no_est[0].quota_score == scores_no_est[1].quota_score

    # With estimate on acct1, it should score higher (more utilized)
    scores_with_est = await scorer.score_accounts(
        ["acct1", "acct2"], request_estimates={"acct1": 5_000_000}
    )
    assert scores_with_est[0].quota_score > scores_with_est[1].quota_score


@pytest.mark.asyncio()
async def test_utilization_above_one_is_visible() -> None:
    """Utilization above 1.0 should not be clamped."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", capacity_5h_microdollars=10_000_000)
    # Record usage that exceeds capacity
    estimator.record_usage("acct1", 1000, 15_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1"])

    # 150% utilization should produce score > 1.0
    assert scores[0].quota_score > 1.0


@pytest.mark.asyncio()
async def test_utilization_above_one_compare() -> None:
    """150% utilization should score higher than 110% utilization."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", capacity_5h_microdollars=10_000_000)
    estimator.set_account_limits("acct2", capacity_5h_microdollars=10_000_000)
    # acct1 at 150%, acct2 at 110%
    estimator.record_usage("acct1", 1000, 15_000_000)
    estimator.record_usage("acct2", 1000, 11_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = await scorer.score_accounts(["acct1", "acct2"])

    # 150% should score higher than 110%
    assert scores[0].quota_score > scores[1].quota_score


@pytest.mark.asyncio()
async def test_active_request_count_increments_and_returns_to_zero() -> None:
    """Active request count should increment and decrement correctly."""
    os.environ["TEST_ROUTER_ACCT_KEY"] = "key"
    try:
        config = AppConfig.from_dict(
            {
                "accounts": [
                    {"name": "acct1", "api_key_env": "TEST_ROUTER_ACCT_KEY"},
                ]
            }
        )
        registry = AccountRegistry(config)
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
        )

        class MockCatalog:
            def __init__(self, c: ModelCatalogCache) -> None:
                self._cache = c

            @property
            def cache(self) -> ModelCatalogCache:
                return self._cache

        catalog = MockCatalog(cache)
        router = Router(registry, catalog)  # type: ignore[arg-type]

        # Initially zero
        state = registry.get_state("acct1")
        assert state is not None
        assert state.active_request_count == 0

        # Increment twice
        await router.increment_active_request_count("acct1")
        assert state.active_request_count == 1
        await router.increment_active_request_count("acct1")
        assert state.active_request_count == 2

        # Decrement once
        await router.decrement_active_request_count("acct1")
        assert state.active_request_count == 1

        # Decrement back to zero
        await router.decrement_active_request_count("acct1")
        assert state.active_request_count == 0

        # Decrement below zero should not go negative
        await router.decrement_active_request_count("acct1")
        assert state.active_request_count == 0
    finally:
        del os.environ["TEST_ROUTER_ACCT_KEY"]


@pytest.mark.asyncio()
async def test_active_request_count_updates_are_serialized() -> None:
    """Concurrent lifecycle updates must leave the counter balanced."""
    config = AppConfig.from_dict(
        {"accounts": [{"name": "acct1", "api_key": "test-key"}]}
    )
    registry = AccountRegistry(config)
    cache = ModelCatalogCache()

    class MockCatalog:
        @property
        def cache(self) -> ModelCatalogCache:
            return cache

    router = Router(registry, MockCatalog())  # type: ignore[arg-type]

    await asyncio.gather(
        *(router.increment_active_request_count("acct1") for _ in range(100))
    )
    await asyncio.gather(
        *(router.decrement_active_request_count("acct1") for _ in range(100))
    )

    state = registry.get_state("acct1")
    assert state is not None
    assert state.active_request_count == 0


@pytest.mark.asyncio()
async def test_failover_selection_honors_zero_limit() -> None:
    """A zero-sized failover request must not return a candidate."""
    config = AppConfig.from_dict(
        {"accounts": [{"name": "acct1", "api_key": "test-key"}]}
    )
    registry = AccountRegistry(config)
    cache = ModelCatalogCache()

    class MockCatalog:
        @property
        def cache(self) -> ModelCatalogCache:
            return cache

    router = Router(registry, MockCatalog())  # type: ignore[arg-type]

    assert await router.select_accounts_for_failover("gpt-4", max_accounts=0) == []
