"""Tests for routing and eligibility."""

from __future__ import annotations

import os

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.accounts.state import AccountRuntimeState
from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.models.config import AppConfig
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
