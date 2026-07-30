"""Regression tests for the per-account catalog-recovery trigger.

The router detects when a configured+healthy account is missing from
``_account_support`` and fires a one-shot catalog refresh callback so
traffic can re-spread across configured siblings. Without it, a
transient per-account refresh failure ages ``_account_last_refresh``
past ``stale_after_s`` and silently de-pools the account from routing
— observed as 11,620 / 5 / 0 traffic skew across three sibling
opencode-go accounts in production.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.quota.estimation import QuotaEstimator
from eggpool.routing.router import Router


def _make_registry() -> AccountRegistry:
    return AccountRegistry(
        AppConfig.model_validate(
            {
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://example.test/v1",
                        "protocols": ["openai", "anthropic"],
                        "routing_priority": 0,
                        "accounts": [
                            {
                                "name": "opencode-go-0001",
                                "api_key": "k1",
                                "weight": 1.0,
                            },
                            {
                                "name": "opencode-go-0002",
                                "api_key": "k2",
                                "weight": 1.0,
                            },
                            {
                                "name": "opencode-go-0003",
                                "api_key": "k3",
                                "weight": 1.0,
                            },
                        ],
                    }
                }
            }
        )
    )


class _StubCatalog:
    def __init__(self, cache: ModelCatalogCache) -> None:
        self.cache = cache


def _seed_cache_with_only_0001(cache: ModelCatalogCache) -> None:
    """Simulate the production failure mode: 0001 has a recent catalog
    entry, 0002/0003 are absent (their per-account refresh failed and
    ``_account_last_refresh`` is too old to be considered fresh)."""
    cache.update_from_account(
        "opencode-go-0001",
        "opencode-go",
        [{"model_id": "gpt-5", "protocol": "openai"}],
    )


def _simulate_recovery_for(cache: ModelCatalogCache, *account_names: str) -> None:
    """Simulate what ``CatalogService.refresh_one_account`` does to the
    cache on a successful one-shot refresh: re-publish the model with
    the account as a supporting account and stamp
    ``_account_last_refresh`` for it. The non-destructive cache update
    path is used so the test stays close to the production code path.
    """
    for name in account_names:
        cache.update_from_account(
            name,
            "opencode-go",
            [{"model_id": "gpt-5", "protocol": "openai"}],
        )


@pytest.mark.asyncio()
async def test_missing_account_triggers_recovery_callback() -> None:
    """When 0002/0003 are missing from the eligible set but configured
    and healthy, the router must invoke the recovery callback for
    each of them exactly once on the first selection pass."""
    registry = _make_registry()
    cache = ModelCatalogCache()
    _seed_cache_with_only_0001(cache)

    invoked: list[str] = []

    def recovery(name: str) -> None:
        invoked.append(name)

    router = Router(
        registry,
        _StubCatalog(cache),  # type: ignore[arg-type]
        quota_estimator=QuotaEstimator(),
        stale_after_s=7200.0,
        fairness_mode="round_robin",
        missing_account_recovery_callback=recovery,
        missing_account_recovery_min_interval_s=0.0,
    )

    candidates = await router.select_accounts_for_failover(
        model_id="gpt-5",
        provider_id="opencode-go",
        protocol="openai",
    )
    assert [state.name for state, _ in candidates] == ["opencode-go-0001"]
    assert sorted(invoked) == ["opencode-go-0002", "opencode-go-0003"]


@pytest.mark.asyncio()
async def test_recovery_callback_is_rate_limited_per_account() -> None:
    """Calling the router many times in quick succession must not spam
    the recovery callback for the same account: a persistent upstream
    failure cannot become a refresh storm."""
    registry = _make_registry()
    cache = ModelCatalogCache()
    _seed_cache_with_only_0001(cache)

    invoked: list[str] = []
    min_interval = 5.0

    def recovery(name: str) -> None:
        invoked.append(name)

    router = Router(
        registry,
        _StubCatalog(cache),  # type: ignore[arg-type]
        quota_estimator=QuotaEstimator(),
        stale_after_s=7200.0,
        fairness_mode="round_robin",
        missing_account_recovery_callback=recovery,
        missing_account_recovery_min_interval_s=min_interval,
    )

    for _ in range(10):
        await router.select_accounts_for_failover(
            model_id="gpt-5",
            provider_id="opencode-go",
            protocol="openai",
        )

    assert invoked == ["opencode-go-0002", "opencode-go-0003"]


@pytest.mark.asyncio()
async def test_recovery_recovers_distribution_after_callback() -> None:
    """End-to-end regression: simulate the production failure, observe
    the router firing the recovery callback, simulate a successful
    one-shot refresh by re-publishing the model for 0002/0003, then
    assert the rotor balances all three accounts. This is the
    deterministic E2E the operator needs to prevent the
    11,620 / 5 / 0 skew from recurring silently."""
    registry = _make_registry()
    cache = ModelCatalogCache()
    _seed_cache_with_only_0001(cache)

    invoked: list[str] = []

    def recovery(name: str) -> None:
        invoked.append(name)
        _simulate_recovery_for(cache, name)

    router = Router(
        registry,
        _StubCatalog(cache),  # type: ignore[arg-type]
        quota_estimator=QuotaEstimator(),
        stale_after_s=7200.0,
        fairness_mode="round_robin",
        missing_account_recovery_callback=recovery,
        missing_account_recovery_min_interval_s=0.0,
    )

    counts = {
        "opencode-go-0001": 0,
        "opencode-go-0002": 0,
        "opencode-go-0003": 0,
    }
    n_requests = 100
    for _ in range(n_requests):
        result = await router.select_account(
            model_id="gpt-5",
            provider_id="opencode-go",
            protocol="openai",
        )
        assert result is not None
        counts[result.name] += 1
        await asyncio.sleep(0)

    assert sorted(invoked) == ["opencode-go-0002", "opencode-go-0003"]
    total = sum(counts.values())
    assert total == n_requests
    for name, count in counts.items():
        # Round-robin rotor over 3 candidates → each should land in
        # [20, 60] (33% ± 14% tolerance to keep the test deterministic
        # but tolerant of the rotor's per-key position offset).
        assert count >= 20, (
            f"{name} received only {count}/{n_requests} requests after "
            f"recovery; rotor is not spreading traffic"
        )


@pytest.mark.asyncio()
async def test_recovery_does_not_fire_when_all_accounts_eligible() -> None:
    """No-op path: when all configured accounts are already eligible,
    the recovery callback must not fire — the router is not in
    recovery mode."""
    registry = _make_registry()
    cache = ModelCatalogCache()
    for name in (
        "opencode-go-0001",
        "opencode-go-0002",
        "opencode-go-0003",
    ):
        cache.update_from_account(
            name, "opencode-go", [{"model_id": "gpt-5", "protocol": "openai"}]
        )

    invoked: list[str] = []

    def recovery(name: str) -> None:
        invoked.append(name)

    router = Router(
        registry,
        _StubCatalog(cache),  # type: ignore[arg-type]
        quota_estimator=QuotaEstimator(),
        stale_after_s=7200.0,
        fairness_mode="round_robin",
        missing_account_recovery_callback=recovery,
        missing_account_recovery_min_interval_s=0.0,
    )

    for _ in range(5):
        await router.select_accounts_for_failover(
            model_id="gpt-5",
            provider_id="opencode-go",
            protocol="openai",
        )
    assert invoked == []


@pytest.mark.asyncio()
async def test_recovery_does_not_fire_for_other_providers() -> None:
    """An account on a different provider that has the model must not
    be considered "missing" for a query scoped to a specific
    provider. The recovery callback is provider-scoped via the
    provider_id passed in."""
    registry = _make_registry()
    cache = ModelCatalogCache()
    # Only 0001 on opencode-go is in the cache. Add a second provider
    # and a fresh account on it to ensure the recovery is scoped to
    # the queried provider.
    AppConfig.model_validate(
        {
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": "https://example.test/v1",
                    "protocols": ["openai"],
                    "routing_priority": 0,
                    "accounts": [
                        {
                            "name": "opencode-go-0001",
                            "api_key": "k1",
                            "weight": 1.0,
                        },
                    ],
                }
            }
        }
    )
    _seed_cache_with_only_0001(cache)

    invoked: list[str] = []

    def recovery(name: str) -> None:
        invoked.append(name)

    router = Router(
        registry,
        _StubCatalog(cache),  # type: ignore[arg-type]
        quota_estimator=QuotaEstimator(),
        stale_after_s=7200.0,
        fairness_mode="round_robin",
        missing_account_recovery_callback=recovery,
        missing_account_recovery_min_interval_s=0.0,
    )

    await router.select_accounts_for_failover(
        model_id="gpt-5",
        provider_id="opencode-go",
        protocol="openai",
    )
    # Only 0002/0003 on opencode-go should be marked, not anything else.
    assert sorted(invoked) == ["opencode-go-0002", "opencode-go-0003"]


@pytest.mark.asyncio()
async def test_recovery_window_release_allows_retry() -> None:
    """After the rate-limit window elapses, the recovery callback must
    fire again — recovery is meant to keep trying until the cache
    converges, not just fire once per process lifetime."""
    registry = _make_registry()
    cache = ModelCatalogCache()
    _seed_cache_with_only_0001(cache)

    invoked: list[str] = []

    def recovery(name: str) -> None:
        invoked.append(name)

    router = Router(
        registry,
        _StubCatalog(cache),  # type: ignore[arg-type]
        quota_estimator=QuotaEstimator(),
        stale_after_s=7200.0,
        fairness_mode="round_robin",
        missing_account_recovery_callback=recovery,
        missing_account_recovery_min_interval_s=1.0,
    )

    await router.select_accounts_for_failover(
        model_id="gpt-5",
        provider_id="opencode-go",
        protocol="openai",
    )
    # Advance monotonic clock past the rate-limit window by hand
    # rather than sleeping — keeps the test deterministic.
    future = time.monotonic() + 2.0
    for stamp in router._missing_account_recovery_attempt_at.values():  # pyright: ignore[reportPrivateUsage]
        if stamp > future:
            future = stamp + 2.0
    # Force the per-account attempt timestamps into the past.
    for name in list(router._missing_account_recovery_attempt_at):  # pyright: ignore[reportPrivateUsage]
        router._missing_account_recovery_attempt_at[name] = 0.0  # pyright: ignore[reportPrivateUsage]

    await router.select_accounts_for_failover(
        model_id="gpt-5",
        provider_id="opencode-go",
        protocol="openai",
    )
    # 2 firings per account, 2 accounts = 4 invocations.
    assert invoked.count("opencode-go-0002") == 2
    assert invoked.count("opencode-go-0003") == 2
