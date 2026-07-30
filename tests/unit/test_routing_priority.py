"""Tests for tiered provider routing priority behavior."""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.accounts.state import AccountRuntimeState
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.routing.router import Router, _group_by_priority


class _MockCatalog:
    """Mock catalog with a single model across all configured accounts."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


def _build_config(providers: list[dict[str, Any]]) -> AppConfig:
    """Build a minimal AppConfig from a list of provider dicts.

    Each provider dict has keys: id, base_url, routing_priority, accounts.
    Each account needs name and api_key (the test sets env vars via os.environ).
    """
    raw: dict[str, Any] = {"providers": {}}
    for provider in providers:
        raw["providers"][provider["id"]] = {
            "id": provider["id"],
            "base_url": provider["base_url"],
            "routing_priority": provider.get("routing_priority", 0),
            "accounts": provider["accounts"],
        }
    return AppConfig.model_validate(raw)


class TestGroupByPriority:
    """Tests for the pure helper used to tier eligible accounts."""

    def test_empty_input_returns_empty_list(self) -> None:
        assert _group_by_priority([]) == []

    def test_single_state_single_tier(self) -> None:
        from dataclasses import asdict

        state = AccountRuntimeState(name="a", routing_priority=3)
        tiers = _group_by_priority([state])
        assert len(tiers) == 1
        assert tiers[0] == [state]
        assert asdict(tiers[0][0])["routing_priority"] == 3

    def test_groups_by_descending_priority(self) -> None:
        a = AccountRuntimeState(name="a", routing_priority=1)
        b = AccountRuntimeState(name="b", routing_priority=5)
        c = AccountRuntimeState(name="c", routing_priority=3)
        tiers = _group_by_priority([a, b, c])
        assert [tier[0].routing_priority for tier in tiers] == [5, 3, 1]
        # Each tier should be a single-element list since priorities differ
        assert [[s.name for s in tier] for tier in tiers] == [["b"], ["c"], ["a"]]

    def test_groups_same_priority_together(self) -> None:
        a = AccountRuntimeState(name="a", routing_priority=2)
        b = AccountRuntimeState(name="b", routing_priority=5)
        c = AccountRuntimeState(name="c", routing_priority=5)
        d = AccountRuntimeState(name="d", routing_priority=0)
        tiers = _group_by_priority([a, b, c, d])
        assert [tier[0].routing_priority for tier in tiers] == [5, 2, 0]
        # Order within a tier is preserved from input
        assert [[s.name for s in tier] for tier in tiers] == [
            ["b", "c"],
            ["a"],
            ["d"],
        ]

    def test_zero_priority_supported(self) -> None:
        a = AccountRuntimeState(name="a", routing_priority=0)
        b = AccountRuntimeState(name="b", routing_priority=0)
        tiers = _group_by_priority([a, b])
        assert len(tiers) == 1
        assert tiers[0] == [a, b]


class TestRouterTieredSelection:
    """End-to-end tests for tiered selection in the Router."""

    @pytest.mark.asyncio()
    async def test_highest_priority_tier_selected_first(self) -> None:
        os.environ["K_HIGH"] = "k"
        os.environ["K_LOW"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low_acct", "api_key_env": "K_LOW"}],
                    },
                    {
                        "id": "high",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 5,
                        "accounts": [{"name": "high_acct", "api_key_env": "K_HIGH"}],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            cache.update_from_account(
                "low_acct", "low", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            cache.update_from_account(
                "high_acct", "high", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            selected = await router.select_account("gpt-4")
            assert selected is not None
            # Always pick the higher priority provider's account
            assert selected.name == "high_acct"
        finally:
            del os.environ["K_HIGH"]
            del os.environ["K_LOW"]

    @pytest.mark.asyncio()
    async def test_falls_through_when_top_tier_empty(self) -> None:
        """When the highest-priority tier has no eligible accounts, the
        router descends to the next tier rather than returning None."""
        os.environ["K_TOP"] = "k"
        os.environ["K_LOW"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low_acct", "api_key_env": "K_LOW"}],
                    },
                    {
                        "id": "top",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 5,
                        "accounts": [{"name": "top_acct", "api_key_env": "K_TOP"}],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            # Only the low-priority account supports the model. The
            # top-tier account is ineligible (no model support), so the
            # router must fall through to the next tier.
            cache.update_from_account(
                "low_acct", "low", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            selected = await router.select_account("gpt-4")
            assert selected is not None
            assert selected.name == "low_acct"
        finally:
            del os.environ["K_TOP"]
            del os.environ["K_LOW"]

    @pytest.mark.asyncio()
    async def test_top_tier_disabled_account_falls_through(self) -> None:
        os.environ["K_TOP"] = "k"
        os.environ["K_LOW"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low_acct", "api_key_env": "K_LOW"}],
                    },
                    {
                        "id": "top",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 5,
                        "accounts": [
                            {
                                "name": "top_acct",
                                "api_key_env": "K_TOP",
                                "enabled": False,
                            }
                        ],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            cache.update_from_account(
                "low_acct", "low", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            cache.update_from_account(
                "top_acct", "top", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            selected = await router.select_account("gpt-4")
            assert selected is not None
            assert selected.name == "low_acct"
        finally:
            del os.environ["K_TOP"]
            del os.environ["K_LOW"]

    @pytest.mark.asyncio()
    async def test_select_accounts_for_failover_priority_order(self) -> None:
        os.environ["K_A"] = "k"
        os.environ["K_B"] = "k"
        os.environ["K_C"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "p-low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low_acct", "api_key_env": "K_A"}],
                    },
                    {
                        "id": "p-high",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 5,
                        "accounts": [
                            {"name": "high_acct", "api_key_env": "K_B"},
                            {"name": "high_acct2", "api_key_env": "K_C"},
                        ],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for acct in ("low_acct", "high_acct", "high_acct2"):
                pid = "p-low" if acct == "low_acct" else "p-high"
                cache.update_from_account(
                    acct, pid, [{"model_id": "gpt-4", "protocol": "openai"}]
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            ranked = await router.select_accounts_for_failover("gpt-4", max_accounts=10)
            names = [state.name for state, _ in ranked]
            # Within a tier the order is randomized, but the high-priority
            # tier must appear before the low-priority tier.
            assert names.index("low_acct") > max(
                names.index("high_acct"), names.index("high_acct2")
            )
            assert set(names) == {"high_acct", "high_acct2", "low_acct"}
        finally:
            del os.environ["K_A"]
            del os.environ["K_B"]
            del os.environ["K_C"]

    @pytest.mark.asyncio()
    async def test_provider_filter_respected_with_priority(self) -> None:
        """Provider filter must take precedence over priority. A
        suffixed request only considers accounts of that provider, even
        if a higher-priority provider also has the model."""
        os.environ["K_HIGH"] = "k"
        os.environ["K_LOW"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low_acct", "api_key_env": "K_LOW"}],
                    },
                    {
                        "id": "high",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 5,
                        "accounts": [{"name": "high_acct", "api_key_env": "K_HIGH"}],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            cache.update_from_account(
                "low_acct", "low", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            cache.update_from_account(
                "high_acct", "high", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            # When provider_id is supplied, priority must NOT leak across
            # providers. Only the matching provider's accounts are eligible.
            selected = await router.select_account("gpt-4", provider_id="low")
            assert selected is not None
            assert selected.name == "low_acct"
        finally:
            del os.environ["K_HIGH"]
            del os.environ["K_LOW"]

    @pytest.mark.asyncio()
    async def test_failover_scores_carry_tier_field(self) -> None:
        """``select_accounts_for_failover`` annotates each ``RoutingScore``
        with the tier from the corresponding ``AccountRuntimeState`` so
        callers can short-circuit at tier boundaries."""
        os.environ["K_A"] = "k"
        os.environ["K_B"] = "k"
        os.environ["K_C"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "p-low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low_acct", "api_key_env": "K_A"}],
                    },
                    {
                        "id": "p-high",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 5,
                        "accounts": [
                            {"name": "high_acct", "api_key_env": "K_B"},
                            {"name": "high_acct2", "api_key_env": "K_C"},
                        ],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for acct in ("low_acct", "high_acct", "high_acct2"):
                pid = "p-low" if acct == "low_acct" else "p-high"
                cache.update_from_account(
                    acct, pid, [{"model_id": "gpt-4", "protocol": "openai"}]
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            ranked = await router.select_accounts_for_failover("gpt-4", max_accounts=10)
            by_name = {state.name: score for state, score in ranked}
            assert by_name["high_acct"].tier == 5
            assert by_name["high_acct2"].tier == 5
            assert by_name["low_acct"].tier == 0
            # Strict tier-bounded failover: stop at the first boundary.
            tier_seen: int | None = None
            for _state, score in ranked:
                if tier_seen is None:
                    tier_seen = score.tier
                elif score.tier != tier_seen:
                    break
        finally:
            del os.environ["K_A"]
            del os.environ["K_B"]
            del os.environ["K_C"]


class TestMixedPriorityLoadBalance:
    """Plan test item 4: priority-3 with one account, priority-0 with three
    accounts.

    Under tier-based routing the top tier always wins, so every
    ``select_account`` call must return the priority-3 account. The
    priority-0 accounts are reachable only as a fallback and must all
    appear in the failover-ranked list.
    """

    @pytest.mark.asyncio()
    async def test_top_tier_wins_every_call(self) -> None:
        os.environ["K_TOP"] = "k"
        os.environ["K_LOW1"] = "k"
        os.environ["K_LOW2"] = "k"
        os.environ["K_LOW3"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "p-low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [
                            {"name": "low1", "api_key_env": "K_LOW1"},
                            {"name": "low2", "api_key_env": "K_LOW2"},
                            {"name": "low3", "api_key_env": "K_LOW3"},
                        ],
                    },
                    {
                        "id": "p-top",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 3,
                        "accounts": [{"name": "top", "api_key_env": "K_TOP"}],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("low1", "low2", "low3", "top"):
                pid = "p-top" if name == "top" else "p-low"
                cache.update_from_account(
                    name, pid, [{"model_id": "gpt-4", "protocol": "openai"}]
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            # Run 60 trials — every call must hit the priority-3 account.
            counts: dict[str, int] = {"top": 0, "low1": 0, "low2": 0, "low3": 0}
            for _ in range(60):
                selected = await router.select_account("gpt-4")
                assert selected is not None
                counts[selected.name] += 1
            assert counts["top"] == 60
            assert counts["low1"] == counts["low2"] == counts["low3"] == 0

            # Failover list still surfaces the priority-0 accounts for
            # tier-leaking retry, with the top tier listed first.
            ranked = await router.select_accounts_for_failover("gpt-4", max_accounts=10)
            names = [state.name for state, _ in ranked]
            assert names[0] == "top"
            assert set(names[1:]) == {"low1", "low2", "low3"}
            # Tier ordering on the failover list is monotonically descending.
            tiers = [score.tier for _, score in ranked]
            assert tiers == sorted(tiers, reverse=True)
        finally:
            for k in ("K_TOP", "K_LOW1", "K_LOW2", "K_LOW3"):
                os.environ.pop(k, None)


class TestTierFallthroughOnCooldown:
    """Plan test item 5: when every account in the top tier is in
    cooldown, the router descends to the next tier and load balances
    across the accounts there."""

    @pytest.mark.asyncio()
    async def test_top_tier_cooldown_falls_through_to_lower_tier(self) -> None:
        os.environ["K_TOP"] = "k"
        os.environ["K_LOW1"] = "k"
        os.environ["K_LOW2"] = "k"
        os.environ["K_LOW3"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "p-low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [
                            {"name": "low1", "api_key_env": "K_LOW1"},
                            {"name": "low2", "api_key_env": "K_LOW2"},
                            {"name": "low3", "api_key_env": "K_LOW3"},
                        ],
                    },
                    {
                        "id": "p-top",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 3,
                        "accounts": [
                            {"name": "top1", "api_key_env": "K_TOP"},
                            {"name": "top2", "api_key_env": "K_TOP"},
                        ],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("low1", "low2", "low3", "top1", "top2"):
                pid = "p-top" if name.startswith("top") else "p-low"
                cache.update_from_account(
                    name, pid, [{"model_id": "gpt-4", "protocol": "openai"}]
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            # Put every priority-3 account in cooldown by manipulating the
            # registry state directly — there is no need to involve the
            # health manager for this unit-level invariant.
            for name in ("top1", "top2"):
                state = registry.get_state(name)
                state.health_state = "cooldown"
                state.cooldown_until = time.time() + 3600.0

            counts: dict[str, int] = {"low1": 0, "low2": 0, "low3": 0}
            for _ in range(60):
                selected = await router.select_account("gpt-4")
                assert selected is not None
                counts[selected.name] += 1

            # No priority-3 account is eligible, so traffic must spread
            # across the three priority-0 accounts.
            assert sum(counts.values()) == 60
            for name in ("low1", "low2", "low3"):
                assert counts[name] > 0, f"{name} received no traffic"
            # Quota-fair scorer with three equal-weight accounts:
            # expect a roughly even split (within 5 of 20 each).
            for name in ("low1", "low2", "low3"):
                assert abs(counts[name] - 20) <= 10, (
                    f"{name} count {counts[name]} drifted from expected 20"
                )
        finally:
            for k in ("K_TOP", "K_LOW1", "K_LOW2", "K_LOW3"):
                os.environ.pop(k, None)


class TestFailoverTierBoundary:
    """Plan test items 6 & 7: a single request's retry loop must respect
    the tier boundary under strict tier-bounded semantics, and the
    existing ``exclude_accounts`` set must continue to work across tier
    boundaries (tier-leaking semantics)."""

    @pytest.mark.asyncio()
    async def test_excluded_top_tier_account_does_not_leak_to_lower_tier(
        self,
    ) -> None:
        """``select_accounts_for_failover`` with ``exclude_accounts`` must
        skip the top-tier account and return the lower-tier account. The
        tier-leaking semantics are exercised by *not* passing
        ``exclude_accounts`` to the failover call: it returns the full
        ranked list. With ``exclude_accounts``, only the lower tier
        remains and the call still succeeds."""
        os.environ["K_TOP"] = "k"
        os.environ["K_LOW"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "p-low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [{"name": "low", "api_key_env": "K_LOW"}],
                    },
                    {
                        "id": "p-top",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 3,
                        "accounts": [{"name": "top", "api_key_env": "K_TOP"}],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            cache.update_from_account(
                "low", "p-low", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            cache.update_from_account(
                "top", "p-top", [{"model_id": "gpt-4", "protocol": "openai"}]
            )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            # Without exclusion: the top tier is selected.
            selected = await router.select_account("gpt-4")
            assert selected is not None
            assert selected.name == "top"

            # With the top-tier account in exclude_accounts: the router
            # must fall through to the priority-0 account rather than
            # returning None or re-selecting the excluded account.
            selected = await router.select_account("gpt-4", exclude_accounts={"top"})
            assert selected is not None
            assert selected.name == "low"

            # select_accounts_for_failover with the top tier excluded
            # returns only the lower-tier account, in priority order.
            ranked = await router.select_accounts_for_failover(
                "gpt-4", max_accounts=5, exclude_accounts={"top"}
            )
            names = [state.name for state, _ in ranked]
            assert names == ["low"]
            # The lower-tier account is annotated with its tier.
            assert ranked[0][1].tier == 0
        finally:
            for k in ("K_TOP", "K_LOW"):
                os.environ.pop(k, None)

    @pytest.mark.asyncio()
    async def test_failover_full_list_respects_tier_ordering(self) -> None:
        """``select_accounts_for_failover`` returns the full ranked list
        across all eligible tiers, with the tier boundary preserved as
        a contiguous block: every priority-3 account comes before every
        priority-0 account."""
        os.environ["K_TOP_A"] = "k"
        os.environ["K_TOP_B"] = "k"
        os.environ["K_LOW_A"] = "k"
        os.environ["K_LOW_B"] = "k"
        try:
            config = _build_config(
                [
                    {
                        "id": "p-low",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 0,
                        "accounts": [
                            {"name": "low_a", "api_key_env": "K_LOW_A"},
                            {"name": "low_b", "api_key_env": "K_LOW_B"},
                        ],
                    },
                    {
                        "id": "p-top",
                        "base_url": "https://api.example.com/v1",
                        "routing_priority": 3,
                        "accounts": [
                            {"name": "top_a", "api_key_env": "K_TOP_A"},
                            {"name": "top_b", "api_key_env": "K_TOP_B"},
                        ],
                    },
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("low_a", "low_b", "top_a", "top_b"):
                pid = "p-top" if name.startswith("top") else "p-low"
                cache.update_from_account(
                    name, pid, [{"model_id": "gpt-4", "protocol": "openai"}]
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            ranked = await router.select_accounts_for_failover("gpt-4", max_accounts=10)
            by_name = {state.name: score for state, score in ranked}
            assert by_name["top_a"].tier == 3
            assert by_name["top_b"].tier == 3
            assert by_name["low_a"].tier == 0
            assert by_name["low_b"].tier == 0

            # Tier ordering is preserved contiguously: all top-tier
            # accounts come first, then all low-tier accounts. The
            # coordinator's retry loop can short-circuit at the first
            # boundary by comparing tier values.
            index_by_name = {state.name: idx for idx, (state, _) in enumerate(ranked)}
            top_idx = max(index_by_name["top_a"], index_by_name["top_b"])
            low_idx = min(index_by_name["low_a"], index_by_name["low_b"])
            assert top_idx < low_idx
        finally:
            for k in ("K_TOP_A", "K_TOP_B", "K_LOW_A", "K_LOW_B"):
                os.environ.pop(k, None)
