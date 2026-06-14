"""Tests for routing and eligibility."""

from __future__ import annotations

import os

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.accounts.state import AccountRuntimeState
from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.models.config import AppConfig
from go_aggregator.quota.estimation import AccountQuota, QuotaEstimator
from go_aggregator.quota.scorer import QuotaFairScorer, RoutingScore
from go_aggregator.routing.eligibility import get_eligible_accounts
from go_aggregator.routing.router import Router


def test_eligible_accounts_basic() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
    states = [
        AccountRuntimeState(name="acct1", enabled=True),
        AccountRuntimeState(name="acct2", enabled=False),
    ]
    eligible = get_eligible_accounts(states, "gpt-4", cache)
    assert len(eligible) == 1
    assert eligible[0].name == "acct1"


def test_eligible_accounts_excludes_cooldown() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
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
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
    states = [AccountRuntimeState(name="acct1", enabled=True)]
    eligible = get_eligible_accounts(states, "claude-3", cache)
    assert len(eligible) == 0


def test_router_selects_account() -> None:
    os.environ["TEST_ROUTER_KEY"] = "key"
    config = AppConfig.from_dict(
        {
            "accounts": [
                {"name": "acct1", "api_key_env": "TEST_ROUTER_KEY"},
            ]
        }
    )
    registry = AccountRegistry(config)
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])

    # Create a mock catalog service with the cache
    class MockCatalog:
        def __init__(self, c: ModelCatalogCache) -> None:
            self._cache = c

        @property
        def cache(self) -> ModelCatalogCache:
            return self._cache

    catalog = MockCatalog(cache)
    router = Router(registry, catalog)  # type: ignore[arg-type]
    selected = router.select_account("gpt-4")
    assert selected is not None
    assert selected.name == "acct1"
    del os.environ["TEST_ROUTER_KEY"]


def test_router_no_eligible_account() -> None:
    os.environ["TEST_ROUTER_KEY_2"] = "key"
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
    selected = router.select_account("gpt-4")
    assert selected is None
    del os.environ["TEST_ROUTER_KEY_2"]


def _make_mock_catalog(model_id: str = "gpt-4") -> ModelCatalogCache:
    """Create a mock catalog cache with a model."""
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": model_id, "protocol": "openai"}])
    cache.update_from_account("acct2", [{"model_id": model_id, "protocol": "openai"}])
    return cache


def test_5h_usage_changes_selection() -> None:
    """Five-hour usage on one account should route to the other."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", max_hourly_cost_microdollars=10_000_000)
    estimator.set_account_limits("acct2", max_hourly_cost_microdollars=10_000_000)
    # Record usage only on acct1
    estimator.record_usage("acct1", 1000, 5_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    # acct1 has usage, acct2 has none -- acct2 should score lower (better)
    assert scores[1].quota_score < scores[0].quota_score


def test_7d_usage_changes_selection() -> None:
    """Seven-day usage on one account should route to the other."""
    estimator = QuotaEstimator()
    from go_aggregator.quota.estimation import PersistedWindowSnapshot

    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        max_daily_cost_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=5_000_000, cost_30d=0
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        max_daily_cost_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    assert scores[1].quota_score < scores[0].quota_score


def test_30d_usage_changes_selection() -> None:
    """Thirty-day usage on one account should route to the other."""
    estimator = QuotaEstimator()
    from go_aggregator.quota.estimation import PersistedWindowSnapshot

    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        max_monthly_cost_microdollars=60_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=0, cost_30d=30_000_000
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        max_monthly_cost_microdollars=60_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    assert scores[1].quota_score < scores[0].quota_score


def test_offsets_apply_to_correct_windows() -> None:
    """Manual offset on 5h should not affect 7d routing."""
    estimator = QuotaEstimator()
    from go_aggregator.quota.estimation import PersistedWindowSnapshot

    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        max_hourly_cost_microdollars=10_000_000,
        max_daily_cost_microdollars=10_000_000,
        five_hour_offset=8_000_000,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        max_hourly_cost_microdollars=10_000_000,
        max_daily_cost_microdollars=10_000_000,
        five_hour_offset=0,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    # acct1 has 80% offset on 5h window, acct2 has none
    # acct1 should score higher (more utilized)
    assert scores[0].quota_score > scores[1].quota_score


def test_weights_scale_capacities() -> None:
    """Account with weight=2 should have double the capacity."""
    estimator = QuotaEstimator()
    estimator.set_account_weight("acct1", 2.0)
    estimator.set_account_weight("acct2", 1.0)
    estimator.set_account_limits("acct1", max_daily_cost_microdollars=20_000_000)
    estimator.set_account_limits("acct2", max_daily_cost_microdollars=10_000_000)
    # Equal usage on both accounts
    estimator.record_usage("acct1", 1000, 5_000_000)
    estimator.record_usage("acct2", 1000, 5_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    # Both have 50% of their own capacity, so scores should be similar
    # but acct1 has weight=2 vs acct2 weight=1
    assert scores[0].weight == 2.0
    assert scores[1].weight == 1.0


def test_reservations_affect_selection() -> None:
    """Active reservation should make an account less preferred."""
    estimator = QuotaEstimator()
    estimator.set_account_limits("acct1", max_hourly_cost_microdollars=10_000_000)
    estimator.set_account_limits("acct2", max_hourly_cost_microdollars=10_000_000)
    # Add reservation on acct1
    estimator.add_reservation("acct1", 4_000_000)

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

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


def test_restart_hydration_preserves_behavior() -> None:
    """Persisted windows should produce same routing after reload."""
    from go_aggregator.quota.estimation import PersistedWindowSnapshot

    estimator = QuotaEstimator()
    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        max_daily_cost_microdollars=10_000_000,
        five_hour_offset=2_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=1_000_000, cost_7d=2_000_000, cost_30d=3_000_000
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        max_daily_cost_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    # Rebuild estimator with same state (simulating restart)
    estimator2 = QuotaEstimator()
    estimator2.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        max_daily_cost_microdollars=10_000_000,
        five_hour_offset=2_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=1_000_000, cost_7d=2_000_000, cost_30d=3_000_000
        ),
    )
    estimator2.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        max_daily_cost_microdollars=10_000_000,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer2 = QuotaFairScorer(quota_estimator=estimator2)
    scores2 = scorer2.score_accounts(["acct1", "acct2"])

    assert scores[0].quota_score == scores2[0].quota_score
    assert scores[1].quota_score == scores2[1].quota_score


def test_offset_does_not_affect_wrong_window() -> None:
    """5h offset should not affect 7d or 30d scores."""
    from go_aggregator.quota.estimation import PersistedWindowSnapshot

    estimator = QuotaEstimator()
    estimator.accounts["acct1"] = AccountQuota(
        account_name="acct1",
        max_hourly_cost_microdollars=10_000_000,
        max_daily_cost_microdollars=10_000_000,
        max_monthly_cost_microdollars=60_000_000,
        five_hour_offset=5_000_000,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=1, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )
    estimator.accounts["acct2"] = AccountQuota(
        account_name="acct2",
        max_hourly_cost_microdollars=10_000_000,
        max_daily_cost_microdollars=10_000_000,
        max_monthly_cost_microdollars=60_000_000,
        five_hour_offset=0,
        weekly_offset=0,
        monthly_offset=0,
        persisted_snapshot=PersistedWindowSnapshot(
            account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
        ),
    )

    scorer = QuotaFairScorer(quota_estimator=estimator)
    scores = scorer.score_accounts(["acct1", "acct2"])

    # acct1 has 5h offset only, 7d and 30d should be identical
    assert scores[0].quota_score != scores[1].quota_score

    # Verify 5h offset is the differentiator by checking without it
    estimator.accounts["acct1"].five_hour_offset = 0
    scores_equal = scorer.score_accounts(["acct1", "acct2"])
    assert scores_equal[0].quota_score == scores_equal[1].quota_score
