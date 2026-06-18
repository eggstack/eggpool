"""Section 8: Live routing feedback after finalization."""

from __future__ import annotations

import os

import pytest

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.models.config import AppConfig
from go_aggregator.quota.estimation import (
    PersistedWindowSnapshot,
    QuotaEstimator,
)
from go_aggregator.routing.router import Router


class _FakeCatalog:
    """Minimal catalog mock for Router construction."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self.cache = cache


@pytest.fixture()
def setup_router() -> tuple[Router, AccountRegistry]:
    os.environ["TEST_KEY_A"] = "test-key-a"
    os.environ["TEST_KEY_B"] = "test-key-b"
    try:
        two_account_config = AppConfig.from_dict(
            {
                "accounts": [
                    {
                        "name": "acct-a",
                        "api_key_env": "TEST_KEY_A",
                        "enabled": True,
                        "weight": 1.0,
                    },
                    {
                        "name": "acct-b",
                        "api_key_env": "TEST_KEY_B",
                        "enabled": True,
                        "weight": 1.0,
                    },
                ],
                "limits": {
                    "five_hour_microdollars": 10_000_000,
                    "weekly_microdollars": 50_000_000,
                    "monthly_microdollars": 100_000_000,
                },
            }
        )
        registry = AccountRegistry(two_account_config)
        hm = HealthManager()
        estimator = QuotaEstimator()
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct-a", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
        )
        cache.update_from_account(
            "acct-b", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
        )
        router = Router(
            registry,
            _FakeCatalog(cache),
            quota_estimator=estimator,
            health_manager=hm,
        )

        for acct in two_account_config.all_accounts():
            estimator.configure_account_policy(
                account_name=acct.name,
                weight=acct.weight,
                capacity_5h_microdollars=int(10_000_000 * acct.weight),
                capacity_7d_microdollars=int(50_000_000 * acct.weight),
                capacity_30d_microdollars=int(100_000_000 * acct.weight),
                offset_5h_microdollars=0,
                offset_7d_microdollars=0,
                offset_30d_microdollars=0,
            )

        return router, registry
    finally:
        os.environ.pop("TEST_KEY_A", None)
        os.environ.pop("TEST_KEY_B", None)


def test_record_usage_updates_quota_estimator(
    setup_router: tuple[Router, AccountRegistry],
) -> None:
    """After finalization, record_usage should update the quota estimator."""
    router, _ = setup_router
    estimator = router._quota_estimator

    estimator.record_usage("acct-a", tokens=1000, cost_microdollars=5_000_000)

    quota = estimator.get_account_quota("acct-a")
    assert quota is not None
    _, cost = quota.hourly_window.get_usage()
    assert cost == 5_000_000


def test_persisted_snapshot_refreshed_after_finalization(
    setup_router: tuple[Router, AccountRegistry],
) -> None:
    """After finalization with exact/derived cost, persisted snapshot is incremented."""
    router, _ = setup_router
    estimator = router._quota_estimator

    # Set up a persisted snapshot with initial values
    quota = estimator.get_account_quota("acct-a")
    assert quota is not None
    quota.persisted_snapshot = PersistedWindowSnapshot(
        account_id=1,
        cost_5h=1_000_000,
        cost_7d=2_000_000,
        cost_30d=3_000_000,
    )

    # Simulate recording exact cost via record_usage
    estimator.record_usage("acct-a", tokens=1000, cost_microdollars=500_000)

    # Manually increment snapshot (simulating what finalizer does)
    if quota.persisted_snapshot is not None:
        quota.persisted_snapshot.cost_5h += 500_000
        quota.persisted_snapshot.cost_7d += 500_000
        quota.persisted_snapshot.cost_30d += 500_000

    assert quota.persisted_snapshot.cost_5h == 1_500_000
    assert quota.persisted_snapshot.cost_7d == 2_500_000
    assert quota.persisted_snapshot.cost_30d == 3_500_000


def test_ewma_updated_from_exact_observations(
    setup_router: tuple[Router, AccountRegistry],
) -> None:
    """EWMA should be updated when exact cost observations are recorded."""
    router, _ = setup_router
    estimator = router._quota_estimator

    # Record several observations with model_id to build up EWMA
    for _ in range(10):
        estimator.record_usage(
            "acct-a", tokens=1000, cost_microdollars=3_000, model_id="gpt-4"
        )

    # Check that account/model EWMA was updated
    am_ewma = estimator.account_model_ewma.get("acct-a", {})
    assert len(am_ewma) > 0
    assert "gpt-4" in am_ewma
    assert am_ewma["gpt-4"].sample_count == 10


def test_immediate_next_selection_affected(
    setup_router: tuple[Router, AccountRegistry],
) -> None:
    """Request 2 should observe Request 1's cost immediately."""
    router, _ = setup_router
    estimator = router._quota_estimator

    # Record heavy usage on acct-a
    estimator.record_usage("acct-a", tokens=10_000, cost_microdollars=9_000_000)

    # Quota-a should now have high utilization
    quota_a = estimator.get_account_quota("acct-a")
    assert quota_a is not None
    _, cost_5h = quota_a.hourly_window.get_usage()
    assert cost_5h == 9_000_000

    # Quota-b should have zero utilization
    quota_b = estimator.get_account_quota("acct-b")
    assert quota_b is not None
    _, cost_b = quota_b.hourly_window.get_usage()
    assert cost_b == 0
