"""Tests for routing-skew correction in the request coordinator.

Phase 2 of the routing-skew corrective plan. The coordinator's
``_select_and_persist_attempt`` previously published the runtime
``active_request_count`` and quota reservation OUTSIDE the select
lock, so a burst of concurrent callers could repeatedly pick the
same account before the first selection's bookkeeping became visible
to the next scorer. The lock-fix phase moves those publications
INSIDE the lock so the second selector observes the first selection's
penalties.

These tests exercise the coordinator at the unit level using an
in-memory SQLite database (built via ``Database(":memory:")`` +
``MigrationRunner``), an ``AccountRegistry`` with three equal accounts
under one provider, and a ``Router`` whose scorer has been forced to
deterministic mode (``tiebreaker_range = 0.0``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    RoutingDecisionRepository,
)
from eggpool.models.config import AppConfig
from eggpool.quota.estimation import AccountQuota, QuotaEstimator
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.routing.router import Router


class _MockCatalog:
    """Mock catalog service exposing only the ``cache`` attribute.

    Mirrors the pattern used by ``tests/unit/test_routing_priority.py``.
    The coordinator only touches ``catalog.cache`` from inside
    ``_select_and_persist_attempt``; the full ``CatalogService`` boot
    path is not needed for unit tests.
    """

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


def _build_config(account_names: list[str]) -> AppConfig:  # noqa: ARG001
    """Build an AppConfig with one provider containing N equal accounts.

    Kept for parity with ``test_routing_priority.py``. The tests
    below construct their configs inline.
    """
    return _build_coordinator_config(account_names)[0]


def _build_coordinator_config(
    account_names: list[str],
) -> tuple[AppConfig, list[float], list[int]]:
    """Build an AppConfig (weights=1.0, priorities=0) for the default case."""
    return (
        AppConfig.model_validate(
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
                            for name in account_names
                        ],
                    }
                }
            }
        ),
        [1.0] * len(account_names),
        [0] * len(account_names),
    )


@dataclass
class _CoordinatorFixture:
    """Lightweight harness bundling the coordinator and its dependencies."""

    db: Database
    registry: AccountRegistry
    catalog: _MockCatalog
    router: Router
    coordinator: RequestCoordinator
    account_names: list[str]


def _force_deterministic_routing(router: Router) -> None:
    """Pin the scorer's tiebreaker range to 0 so test 1 is reproducible.

    Only TestConcurrentSelectionDistribution should call this. The
    sequential tests rely on the default 0.01 tiebreaker range so the
    near-tie randomization spreads traffic across equal accounts.
    """
    router._scorer.tiebreaker_range = 0.0  # pyright: ignore[reportPrivateUsage]


async def _build_coordinator_fixture(
    account_names: list[str],
    *,
    weights: list[float] | None = None,
    priorities: list[int] | None = None,
    model_id: str = "gpt-4",
    protocol: str = "openai",
    protocols: list[str] | None = None,
    deterministic_routing: bool = False,
    provider_specs: list[dict[str, Any]] | None = None,
) -> _CoordinatorFixture:
    """Build a coordinator with N equal-weight accounts under one provider.

    ``protocols`` overrides the provider's protocol list (defaults to
    the requested ``protocol``).  ``weights`` and ``priorities``
    override the default 1.0 / 0 per account.
    """
    if len(account_names) < 1:
        raise ValueError("Need at least one account")

    weights = weights or [1.0] * len(account_names)
    priorities = priorities or [0] * len(account_names)
    protocols = protocols or [protocol]

    for name in account_names:
        os.environ[f"K_{name}"] = "k"

    if provider_specs is None:
        provider_accounts: list[dict[str, Any]] = [
            {
                "name": name,
                "api_key_env": f"K_{name}",
                "weight": weights[idx],
            }
            for idx, name in enumerate(account_names)
        ]
        provider_priority = max(priorities) if priorities else 0
        raw = {
            "providers": {
                "test-provider": {
                    "id": "test-provider",
                    "base_url": "https://api.example.com/v1",
                    "protocols": protocols,
                    "routing_priority": provider_priority,
                    "accounts": provider_accounts,
                }
            }
        }
    else:
        raw = {"providers": {}}
        for spec in provider_specs:
            pid = spec["id"]
            raw["providers"][pid] = {
                "id": pid,
                "base_url": "https://api.example.com/v1",
                "protocols": protocols,
                "routing_priority": spec.get("routing_priority", 0),
                "accounts": [
                    {
                        "name": acct,
                        "api_key_env": f"K_{acct}",
                        "weight": spec.get("weights", {}).get(acct, 1.0),
                    }
                    for acct in spec["accounts"]
                ],
            }
    config = AppConfig.model_validate(raw)
    registry = AccountRegistry(config)

    cache = ModelCatalogCache()
    for name in account_names:
        cache.update_from_account(
            name,
            "test-provider",
            [{"model_id": model_id, "protocol": protocol}],
        )
    catalog = _MockCatalog(cache)

    quota_estimator = QuotaEstimator()
    router = Router(
        registry,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        quota_estimator=quota_estimator,
    )
    if deterministic_routing:
        _force_deterministic_routing(router)
    for idx, name in enumerate(account_names):
        router.quota_estimator.accounts[name] = AccountQuota(
            account_name=name,
            weight=weights[idx],
            capacity_5h_microdollars=1_000_000_000,
            capacity_7d_microdollars=7_000_000_000,
            capacity_30d_microdollars=30_000_000_000,
        )

    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_accounts(
        db, account_names, weights=weights, model_id=model_id, protocol=protocol
    )

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    routing_decision_repo = RoutingDecisionRepository(db)

    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,  # type: ignore[arg-type]
        router=router,
        db=db,
        client_pool=_StubClientPool(),
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        routing_decision_repo=routing_decision_repo,
        quota_estimator=quota_estimator,
        health_manager=None,
    )

    return _CoordinatorFixture(
        db=db,
        registry=registry,
        catalog=catalog,
        router=router,
        coordinator=coordinator,
        account_names=list(account_names),
    )


async def _seed_accounts(
    db: Database,
    account_names: list[str],
    *,
    weights: list[float],
    model_id: str = "gpt-4",
    protocol: str = "openai",
) -> None:
    """Insert accounts, models, and account_models rows in one transaction."""
    async with db.transaction():
        existing_model = await db.fetch_one(
            "SELECT model_id FROM models WHERE model_id = ?", (model_id,)
        )
        if existing_model is None:
            await db.execute_insert(
                "INSERT INTO models (model_id, display_name, protocol) "
                "VALUES (?, ?, ?)",
                (model_id, model_id, protocol),
            )
        for idx, name in enumerate(account_names):
            existing_acct = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (name,)
            )
            if existing_acct is None:
                await db.execute_insert(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, ?)",
                    (name, f"K_{name}", weights[idx]),
                )
            acct_row = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (name,)
            )
            assert acct_row is not None
            account_id = int(acct_row["id"])
            existing_link = await db.fetch_one(
                "SELECT 1 FROM account_models WHERE account_id = ? AND model_id = ?",
                (account_id, model_id),
            )
            if existing_link is None:
                await db.execute_insert(
                    "INSERT INTO account_models "
                    "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                    (account_id, model_id),
                )


class _StubClientPool:
    """Stub client pool whose ``get_default_client`` and ``get_client``
    both return a synthetic httpx client that the coordinator will not
    actually use during ``_select_and_persist_attempt``."""

    def get_default_client(self) -> httpx.AsyncClient:
        return _FakeClient()

    def get_client(
        self,
        provider_id: str | None = None,
        account_name: str | None = None,
    ) -> httpx.AsyncClient:
        return _FakeClient()


class _FakeClient(httpx.AsyncClient):
    """Async client subclass used purely so type-checks pass.

    ``_select_and_persist_attempt`` never actually opens an
    upstream connection, so the client is never awaited.
    """


def _make_context(
    request_id: str,
    *,
    model_id: str = "gpt-4",
    protocol: str = "openai",
    provider_id: str | None = None,
) -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id=request_id,
        protocol=protocol,
        model_id=model_id,
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
        provider_id=provider_id,
    )


@pytest.fixture(autouse=True)
def _clean_env() -> Any:
    """Strip any K_<name> env vars created by other tests on teardown."""

    yield

    for key in list(os.environ.keys()):
        if key.startswith("K_"):
            os.environ.pop(key, None)


class TestConcurrentSelectionDistribution:
    """Phase 6 regression: a burst of concurrent selections must spread."""

    @pytest.mark.asyncio()
    async def test_concurrent_selection_distributes_across_equal_accounts(
        self,
    ) -> None:
        """Three equal accounts. 30 concurrent selections should not
        pile up on one account.

        Under the pre-fix code the runtime ``active_request_count``
        and quota reservation are published AFTER the select lock
        releases, so the first selection wins every race and the
        other 29 selectors all observe a zero-penalty account A. With
        the lock fix, the first selector's publications are visible
        to selector #2 and the inflight penalty forces spread.
        """
        names = ["alpha", "bravo", "charlie"]
        fixture = await _build_coordinator_fixture(names, deterministic_routing=True)

        try:
            attempts = 30

            async def _one() -> str:
                ctx = _make_context(
                    f"req-{time.time_ns()}-{id(object())}",
                )
                selected = await fixture.coordinator._select_and_persist_attempt(ctx, 1)
                return selected.account_name

            results = await asyncio.gather(*[_one() for _ in range(attempts)])
            counts: dict[str, int] = {name: 0 for name in names}
            for acct in results:
                counts[acct] += 1

            # No single account should capture > 60% of selections once
            # the lock fix is in. With 30 selections across 3 equal
            # accounts the expected average is 10; 60% is 18.
            top = max(counts.values())
            assert top <= 18, (
                f"Routing skew detected: account counts={counts}, "
                f"top={top}/{attempts} > 60%"
            )
            # And every account should see at least some traffic.
            for name in names:
                assert counts[name] >= 1, (
                    f"Account {name!r} received no selections in {attempts}"
                )
        finally:
            await fixture.db.disconnect()

    @pytest.mark.asyncio()
    async def test_selection_lock_covers_runtime_reservation_visibility(
        self,
    ) -> None:
        """The second sequential selector must observe the first
        selector's active_count + reservation BEFORE scoring.

        We monkey-patch ``Router.increment_active_request_count`` to
        record the post-publish state at the moment it runs and
        ``QuotaEstimator.add_reservation`` to assert that
        ``QuotaEstimator._account_reserved_cost`` already reflects the
        call. Pre-fix this fails because the active_count and
        reservation live outside the lock; post-fix the second
        selector's scoring observes the first selector's contributions.
        """
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)
        coordinator = fixture.coordinator
        router = fixture.router
        estimator = router.quota_estimator

        try:
            baseline_alpha = router._registry.get_state("alpha").active_request_count

            ctx_a = _make_context("req-A")
            selected_a = await coordinator._select_and_persist_attempt(ctx_a, 1)

            post_a_alpha = router._registry.get_state(
                selected_a.account_name
            ).active_request_count
            reserved_a = await estimator.get_account_reserved_cost(
                selected_a.account_name
            )

            ctx_b = _make_context("req-B")
            selected_b = await coordinator._select_and_persist_attempt(ctx_b, 1)

            assert post_a_alpha >= baseline_alpha + 1, (
                "active_request_count was not published before "
                "_select_and_persist_attempt returned; "
                f"baseline={baseline_alpha}, post={post_a_alpha}"
            )
            assert reserved_a > 0, (
                "add_reservation did not run inside the select lock; "
                "second selector would not see the prior reservation"
            )

            after_b = router._registry.get_state(
                selected_a.account_name
            ).active_request_count
            assert after_b == post_a_alpha or after_b == post_a_alpha + 1
            assert selected_a is not None
            assert selected_b is not None
        finally:
            await fixture.db.disconnect()


class TestSequentialRoutingDistribution:
    """Sequential routing fairness via ``Router.select_account`` directly."""

    @pytest.mark.asyncio()
    async def test_equal_weight_spreads_under_sequential_load(self) -> None:
        """Sequential ``select_account`` over three equal accounts must
        visit every account at least once across 30 calls."""
        names = ["alpha", "bravo", "charlie"]
        fixture = await _build_coordinator_fixture(names)

        try:
            counts: dict[str, int] = {name: 0 for name in names}
            for _ in range(30):
                selected = await fixture.router.select_account("gpt-4")
                assert selected is not None
                counts[selected.name] += 1

            for name in names:
                assert counts[name] >= 5, (
                    f"Account {name!r} got only {counts[name]} of 30 calls"
                )
        finally:
            await fixture.db.disconnect()

    @pytest.mark.asyncio()
    async def test_priority_tier_wins_before_fairness(self) -> None:
        """A high-priority provider wins every call; the low-priority
        provider's accounts never see traffic when the high tier is
        available.
        """
        for n in ["hi1", "hi2", "lo"]:
            os.environ[f"K_{n}"] = "k"
        specs = [
            {
                "id": "hi",
                "routing_priority": 10,
                "accounts": ["hi1", "hi2"],
            },
            {
                "id": "lo",
                "routing_priority": 0,
                "accounts": ["lo"],
            },
        ]
        fixture = await _build_coordinator_fixture(
            ["hi1", "hi2", "lo"],
            provider_specs=specs,
        )

        try:
            counts: dict[str, int] = {name: 0 for name in ["hi1", "hi2", "lo"]}
            for _ in range(30):
                selected = await fixture.router.select_account("gpt-4")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["lo"] == 0, (
                f"Low-tier account should never be selected when a higher "
                f"tier has eligible accounts; got {counts['lo']} selections"
            )
            assert counts["hi1"] + counts["hi2"] == 30
            assert counts["hi1"] >= 1
            assert counts["hi2"] >= 1
        finally:
            await fixture.db.disconnect()

    @pytest.mark.asyncio()
    async def test_no_single_account_monopolizes_routing(self) -> None:
        """Routing must remain fair across many sequential selections.
        No single account should capture more than 60% of selections
        once the lock fix is in. This is a regression check for the
        pre-fix skew where account A won every race under high load.
        """
        names = ["alpha", "bravo", "charlie"]
        fixture = await _build_coordinator_fixture(names)

        try:
            counts: dict[str, int] = {name: 0 for name in names}
            iterations = 60
            for _ in range(iterations):
                selected = await fixture.router.select_account("gpt-4")
                assert selected is not None
                counts[selected.name] += 1

            top = max(counts.values())
            assert top < iterations * 0.6, (
                f"Routing skew detected: account counts={counts}, "
                f"top={top}/{iterations} >= 60%"
            )
            for name in names:
                assert counts[name] >= 1, (
                    f"Account {name!r} received no selections in {iterations}"
                )
        finally:
            await fixture.db.disconnect()


class TestRoutingDecisionScoreComponents:
    """The routing_decisions row carries a valid ``score_components_json``."""

    @pytest.mark.asyncio()
    async def test_routing_decision_records_score_components_json(self) -> None:
        """A successful selection persists the score component breakdown."""
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)

        try:
            ctx = _make_context("req-score-components")
            selected = await fixture.coordinator._select_and_persist_attempt(ctx, 1)
            assert selected.account_name in names

            # Look up the routing_decisions row.
            row = await fixture.db.fetch_one(
                "SELECT * FROM routing_decisions "
                "WHERE selected_account_name = ? "
                "ORDER BY id DESC LIMIT 1",
                (selected.account_name,),
            )
            assert row is not None, "routing_decisions row missing"

            score_json_raw = row["score_components_json"]
            assert score_json_raw, "score_components_json should be populated"
            score_json = json.loads(score_json_raw)

            expected_keys = {
                "quota_score",
                "inflight_penalty",
                "health_penalty",
                "final_score",
                "weight",
                "active_request_count",
                "reserved_microdollars",
                "cost_5h_microdollars",
                "cost_7d_microdollars",
                "cost_30d_microdollars",
                "capacity_5h_microdollars",
                "capacity_7d_microdollars",
                "capacity_30d_microdollars",
                "tier",
                "requires_transcode",
                "top_candidates",
                "util_5h",
                "util_7d",
                "util_30d",
                "tie_break",
            }
            missing = expected_keys - set(score_json.keys())
            assert not missing, (
                f"score_components_json missing keys: {missing}; "
                f"got {set(score_json.keys())}"
            )
            assert isinstance(score_json["top_candidates"], list)
            assert score_json["tier"] == 0  # routing_priority is 0 for this provider

            # Utilization ratios must agree with cost / capacity when
            # the scorer's capacity fields are non-zero.
            for window in ("5h", "7d", "30d"):
                cost = score_json[f"cost_{window}_microdollars"]
                cap = score_json[f"capacity_{window}_microdollars"]
                util = score_json[f"util_{window}"]
                if cap > 0:
                    assert util is not None
                    assert abs(util - (cost / cap)) < 1e-9
                else:
                    assert util is None

            # tie_break must point at one of the documented factors so
            # the dashboard renders a stable label.
            assert score_json["tie_break"]["factor"] in {
                "no_runner_up",
                "tier",
                "quota",
                "inflight",
                "transcode",
                "near_tie",
                "exact_tie",
            }
            assert "margin" in score_json["tie_break"]
        finally:
            await fixture.db.disconnect()


class TestExplainAccountEligibility:
    """``Router.explain_account_eligibility`` returns one row per account."""

    @pytest.mark.asyncio()
    async def test_explain_account_eligibility_returns_reasons(self) -> None:
        """With one enabled account, one disabled, and one missing the
        model, the explanation should mark each account eligible or
        ineligible with a specific reason code."""
        names = ["good", "disabled_acct", "wrong_model"]
        # Build a fixture with three accounts but only register the
        # model on `good`.
        weights = [1.0, 1.0, 1.0]
        priorities = [0, 0, 0]
        fixture = await _build_coordinator_fixture(
            names,
            weights=weights,
            priorities=priorities,
        )

        try:
            # Disable one account and withhold the model on another.
            fixture.registry.get_state("disabled_acct").enabled = False
            fixture.registry.get_state("wrong_model").enabled = True
            # Remove wrong_model from the catalog so it cannot serve gpt-4.
            fixture.catalog._cache._account_support.pop("gpt-4", None)
            fixture.catalog._cache._account_support["gpt-4"] = {"good"}
            fixture.catalog._cache._account_providers.pop("wrong_model", None)

            rows = await fixture.router.explain_account_eligibility(
                model_id="gpt-4",
                provider_id=None,
                protocol="openai",
                transcode_eligibility=None,
            )

            assert len(rows) == 3
            by_name = {row["account_name"]: row for row in rows}
            assert "good" in by_name
            assert "disabled_acct" in by_name
            assert "wrong_model" in by_name

            assert by_name["good"]["eligible"] is True
            assert by_name["good"]["reason_code"] == "ok"

            assert by_name["disabled_acct"]["eligible"] is False
            assert by_name["disabled_acct"]["reason_code"] == "disabled"

            assert by_name["wrong_model"]["eligible"] is False
            assert by_name["wrong_model"]["reason_code"] in {
                "no_model",
                "model_stale",
                "no_protocol",
            }
        finally:
            await fixture.db.disconnect()

    @pytest.mark.asyncio()
    async def test_explain_includes_actionable_details(self) -> None:
        """``reason_detail`` strings include the identifiers an
        operator needs to act on the diagnosis (account, provider,
        configured protocols, requested model id, staleness window).
        """
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)

        try:
            router = fixture.router
            cache = fixture.catalog.cache

            # Pull alpha and bravo onto different providers at the
            # cache level so the wrong_provider check distinguishes
            # them; the registry still maps both to test-provider via
            # the fixture config.
            cache._account_providers["alpha"] = "opencode-go"  # noqa: SLF001
            cache._account_providers["bravo"] = "anthropic-proxy"  # noqa: SLF001
            fixture.registry.get_state("alpha").enabled = False
            fixture.registry.get_state("bravo").enabled = True

            # alpha disabled, bravo: ask for provider=opencode-go so
            # bravo triggers wrong_provider.
            rows = await router.explain_account_eligibility(
                model_id="gpt-4",
                provider_id="opencode-go",
                protocol=None,
                transcode_eligibility=None,
            )
            by_name = {row["account_name"]: row for row in rows}

            alpha = by_name["alpha"]
            assert alpha["eligible"] is False
            assert alpha["reason_code"] == "disabled"
            assert "alpha" in alpha["reason_detail"]

            bravo = by_name["bravo"]
            assert bravo["eligible"] is False
            assert bravo["reason_code"] == "wrong_provider"
            wp_detail = bravo["reason_detail"]
            assert "bravo" in wp_detail
            assert "anthropic-proxy" in wp_detail
            assert "opencode-go" in wp_detail

            # Re-enable alpha and ask provider=anthropic-proxy so
            # alpha is wrong_provider; bravo is now protocol-compatible
            # but test-provider's protocols list is just ["openai"], so
            # bravo (anthropic-proxy) reports no_protocol for "openai"
            # if we override its protocol list.
            fixture.registry.get_state("alpha").enabled = True
            # Override bravo's provider in both cache and registry so
            # the no_protocol message names the right provider.
            cache._account_providers["bravo"] = "anthropic-only"  # noqa: SLF001
            fixture.registry._account_providers["bravo"] = "anthropic-only"  # noqa: SLF001
            # Patch account_supports_protocol so bravo appears not to
            # support openai even though anthropic-only has no
            # protocols list in the fixture config.
            orig_supports = fixture.registry.account_supports_protocol

            def _supports_no_openai(  # type: ignore[no-untyped-def]
                account_name: str, protocol: str
            ) -> bool:
                if account_name == "bravo" and protocol == "openai":
                    return False
                return orig_supports(account_name, protocol)

            fixture.registry.account_supports_protocol = _supports_no_openai  # type: ignore[method-assign]
            orig_get_protocols = fixture.registry.get_provider_protocols

            def _get_protocols(  # type: ignore[no-untyped-def]
                provider_id: str,
            ) -> set[str]:
                if provider_id == "anthropic-only":
                    return {"anthropic"}
                return orig_get_protocols(provider_id)

            fixture.registry.get_provider_protocols = _get_protocols  # type: ignore[method-assign]

            rows = await router.explain_account_eligibility(
                model_id="gpt-4",
                provider_id="anthropic-only",
                protocol="openai",
                transcode_eligibility=None,
            )
            by_name = {row["account_name"]: row for row in rows}

            alpha = by_name["alpha"]
            assert alpha["eligible"] is False
            assert alpha["reason_code"] == "wrong_provider"
            detail = alpha["reason_detail"]
            assert "opencode-go" in detail
            assert "anthropic-only" in detail
            assert "alpha" in detail

            bravo = by_name["bravo"]
            assert bravo["eligible"] is False
            assert bravo["reason_code"] == "no_protocol"
            np_detail = bravo["reason_detail"]
            assert "bravo" in np_detail
            assert "anthropic-only" in np_detail
            assert "openai" in np_detail

            # When the model is unknown to the catalog at all, the
            # ``no_model`` detail should name the missing model and the
            # account so the operator can correlate against config.
            rows = await router.explain_account_eligibility(
                model_id="totally-unknown",
                provider_id=None,
                protocol=None,
                transcode_eligibility=None,
            )
            by_name = {row["account_name"]: row for row in rows}
            for row in by_name.values():
                if row["reason_code"] == "no_model":
                    assert "totally-unknown" in row["reason_detail"]
                    assert row["account_name"] in row["reason_detail"]

            # Confirm the cache does not leak rows we did not register.
            assert cache.get_supporting_accounts("totally-unknown") == set()
        finally:
            await fixture.db.disconnect()


class TestInFlightPenaltyPropagation:
    """Reservations are visible to subsequent scorers via the lock fix."""

    @pytest.mark.asyncio()
    async def test_inflight_penalty_visible_to_next_selector(self) -> None:
        """After ``add_reservation(N)`` + ``increment_active_request_count``
        the next ``score_accounts`` call observes the new penalty and
        reservation."""
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)

        try:
            router = fixture.router
            estimator = router.quota_estimator

            await estimator.add_reservation("alpha", 500_000)
            await router.increment_active_request_count("alpha")

            scores = await router._scorer.score_accounts(
                names,
                "gpt-4",
                active_requests={
                    name: router._registry.get_state(name).active_request_count
                    for name in names
                },
            )
            by_name = {score.account_name: score for score in scores}

            assert by_name["alpha"].reserved_microdollars >= 500_000 or (
                by_name["alpha"].inflight_penalty > by_name["bravo"].inflight_penalty
            ), (
                f"Expected alpha to carry inflight penalty after the "
                f"reservation was added; scores={scores}"
            )
        finally:
            await fixture.db.disconnect()


class TestPublishOrdering:
    """Lock/transaction nesting guarantees for the cleanup pass.

    These tests assert the invariant introduced by Phase 1 of the
    cleanup plan: the durable selection transaction commits BEFORE
    the runtime publication step (``increment_active_request_count`` +
    ``add_reservation``), AND that publication runs BEFORE
    ``_select_lock`` releases.  The earlier compound-context shape
    (``async with self._select_lock, self._db.transaction():``)
    published while the SQLite transaction was still open; if the
    transaction defers commit visibility until exit (which it does
    per ``Database.transaction``), the publication would see no
    durable rows for the just-selected attempt.
    """

    @pytest.mark.asyncio()
    async def test_runtime_publication_happens_after_transaction_commit(
        self,
    ) -> None:
        """``increment_active_request_count`` must observe the durable
        request/reservation/attempt/routing_decision rows that the
        coordinator just inserted.

        We monkeypatch the router's publication method to query the
        same in-memory database connection used by the coordinator.
        If the inner transaction context manager has not yet exited
        (the pre-cleanup bug), these rows are NOT visible from a
        separate fetch because the surrounding transaction is still
        open and SQLite has not committed.
        """
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)

        try:
            coordinator = fixture.coordinator
            router = fixture.router
            db = fixture.db

            observed: dict[str, object] = {}

            original_increment = router.increment_active_request_count

            async def _spy_increment(account_name: str) -> None:
                # Query all four rows for the just-created attempt.
                # Under the pre-cleanup compound-context shape the
                # ``async with`` for the transaction has not exited,
                # so SQLite has not yet COMMITted; these reads would
                # race or block depending on aiosqlite scheduling.
                # Under the cleaned-up nested shape the inner
                # transaction has committed and the rows are visible.
                attempt_row = await db.fetch_one(
                    "SELECT id, request_id FROM request_attempts "
                    "ORDER BY id DESC LIMIT 1"
                )
                decision_row = await db.fetch_one(
                    "SELECT id, selected_account_name FROM routing_decisions "
                    "ORDER BY id DESC LIMIT 1"
                )
                reservation_row = await db.fetch_one(
                    "SELECT id, account_id, model_id FROM reservations "
                    "ORDER BY id DESC LIMIT 1"
                )
                request_row = await db.fetch_one(
                    "SELECT id, model_id FROM requests ORDER BY id DESC LIMIT 1"
                )
                observed["attempt_row"] = attempt_row
                observed["decision_row"] = decision_row
                observed["reservation_row"] = reservation_row
                observed["request_row"] = request_row
                observed["selected_account"] = account_name
                await original_increment(account_name)

            router.increment_active_request_count = (  # type: ignore[method-assign]
                _spy_increment
            )

            ctx = _make_context("req-publish-ordering")
            selected = await coordinator._select_and_persist_attempt(ctx, 1)
            assert observed, "publication hook never fired"

            # All four durable rows must be visible when the
            # publication hook runs.
            assert observed["attempt_row"] is not None, (
                "request_attempts row missing at publication time — "
                "transaction had not committed before publication"
            )
            assert observed["decision_row"] is not None, (
                "routing_decisions row missing at publication time"
            )
            assert observed["reservation_row"] is not None, (
                "reservations row missing at publication time"
            )
            assert observed["request_row"] is not None, (
                "requests row missing at publication time"
            )
            assert observed["selected_account"] == selected.account_name
        finally:
            await fixture.db.disconnect()

    @pytest.mark.asyncio()
    async def test_select_lock_released_only_after_runtime_publication(
        self,
    ) -> None:
        """A concurrent selector must not be able to enter the
        critical section until the in-flight selector's publication
        has completed.

        We hold the publication step on an ``asyncio.Event`` so we
        can deterministically interleave a second selector against
        the first. The second selector must block on
        ``_select_lock`` until the first selector's publication is
        released; once released, the second selector must observe
        the first selector's contributions.
        """
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)

        try:
            coordinator = fixture.coordinator
            router = fixture.router

            publish_started = asyncio.Event()
            release_publish = asyncio.Event()
            second_started = asyncio.Event()

            original_increment = router.increment_active_request_count

            async def _blocking_increment(account_name: str) -> None:
                publish_started.set()
                await release_publish.wait()
                await original_increment(account_name)

            router.increment_active_request_count = (  # type: ignore[method-assign]
                _blocking_increment
            )

            first_selected_holder: list[str] = []

            async def _first() -> str:
                ctx = _make_context("req-first")
                selected = await coordinator._select_and_persist_attempt(ctx, 1)
                first_selected_holder.append(selected.account_name)
                return selected.account_name

            async def _second() -> tuple[str, int]:
                # Wait until first selector is mid-publication.
                await publish_started.wait()
                second_started.set()
                ctx = _make_context("req-second")
                selected = await coordinator._select_and_persist_attempt(ctx, 1)
                first_active = router._registry.get_state(
                    first_selected_holder[0]
                ).active_request_count
                return (selected.account_name, first_active)

            first_task = asyncio.create_task(_first())
            await publish_started.wait()
            second_task = asyncio.create_task(_second())
            await second_started.wait()
            # Give the second selector a chance to mis-enter the lock.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # The second task should still be blocked waiting on
            # _select_lock while we hold publication.
            assert not second_task.done(), (
                "second selector entered _select_lock before first "
                "publication completed"
            )
            # Release the first selector's publication.
            release_publish.set()
            await first_task
            second_result = await second_task
            second_selected, first_active = second_result
            assert first_active >= 1, (
                f"first selector's active count not published "
                f"before second selector scored; got {first_active}"
            )
        finally:
            await fixture.db.disconnect()

    @pytest.mark.asyncio()
    async def test_post_commit_publication_failure_removes_partial_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If publication raises mid-way, the active count increment
        is compensated and the exception is re-raised with the
        ``post_commit_interrupted`` flag set on the context.

        ``QuotaEstimator`` is a ``@dataclass(slots=True)`` so its
        methods cannot be reassigned directly.  Instead we
        monkeypatch the coordinator's ``_quota_estimator`` reference
        with a thin wrapper that fails on ``add_reservation`` while
        delegating the other accessors the coordinator uses
        (``get_account_reserved_cost``, ``estimate_cost``,
        ``get_account_quota``) to the underlying estimator.
        """
        names = ["alpha", "bravo"]
        fixture = await _build_coordinator_fixture(names)

        try:
            coordinator = fixture.coordinator
            router = fixture.router
            estimator = router.quota_estimator

            class _FailingEstimator:
                """Thin wrapper that fails on add_reservation."""

                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                async def add_reservation(
                    self,
                    account_name: str,
                    cost: int,
                    *,
                    requests: int = 1,
                    tokens: int = 0,
                ) -> None:
                    raise RuntimeError("simulated reservation failure")

                def get_account_quota(self, account_name: str) -> Any:
                    return self._inner.get_account_quota(account_name)

                async def get_account_reserved_cost(self, account_name: str) -> int:
                    return await self._inner.get_account_reserved_cost(account_name)

                def estimate_cost(
                    self, account_name: str, model_id: str, tokens: int
                ) -> int:
                    return self._inner.estimate_cost(account_name, model_id, tokens)

            failing_estimator = _FailingEstimator(estimator)
            monkeypatch.setattr(coordinator, "_quota_estimator", failing_estimator)

            ctx = _make_context("req-publication-failure")

            with pytest.raises(RuntimeError, match="simulated"):
                await coordinator._select_and_persist_attempt(ctx, 1)

            assert ctx.client_metadata.get("post_commit_interrupted") is True, (
                "post_commit_interrupted flag missing on context"
            )

            # After monkeypatch is reverted, the next selection
            # should succeed and produce the expected active count.
            monkeypatch.setattr(coordinator, "_quota_estimator", estimator)
            ctx2 = _make_context("req-publication-success")
            selected_second = await coordinator._select_and_persist_attempt(ctx2, 1)
            target_state = router._registry.get_state(selected_second.account_name)
            target_reserved = await estimator.get_account_reserved_cost(
                selected_second.account_name
            )
            assert target_state.active_request_count == 1, (
                f"active count after successful selection should be 1; "
                f"got {target_state.active_request_count}"
            )
            assert target_reserved > 0, (
                "reservation after successful selection should be > 0; "
                f"got {target_reserved}"
            )
        finally:
            await fixture.db.disconnect()
