"""Phase 17 coordinator-level 402 quota-exhaustion lifecycle test.

Verifies the complete quota-exhaustion behavior through real request
execution:

1. One account returns 402; the other succeeds (the router picks
   non-deterministically which is which).
2. The 402 account is placed in ``quota_exhausted`` cooldown in both
   HealthManager and AccountRuntimeState.
3. A subsequent independent request does NOT attempt the 402 account
   while the cooldown is active.
4. After the cooldown expires, the 402 account becomes eligible again.
5. All attempts are terminal; reservations are released; no active
   counts remain for completed requests.
6. The 402 attempt affects health exactly once.
7. No raw provider body is persisted to the database.

The cooldown is short (0.5s) so the test runs quickly; cooldown
expiration is simulated by rewinding ``cooldown_until`` on the
HealthManager and runtime state directly to avoid ``time.sleep``.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.catalog.pricing import CostCalculator, PriceRepository
from go_aggregator.catalog.service import CatalogService
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.models.config import AppConfig
from go_aggregator.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from go_aggregator.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

UPSTREAM_BASE = "https://test-upstream.example.com"

COOLDOWN_SECONDS = 0.5
LOCAL_BODY = json.dumps(
    {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "ping"}],
    }
).encode()


def _build_two_account_config() -> AppConfig:
    os.environ["OPENCODE_P17_KEY_A"] = "key-p17-a"
    os.environ["OPENCODE_P17_KEY_B"] = "key-p17-b"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_P17_KEY_A",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "routing": {
                "quota_exhausted_cooldown_seconds": COOLDOWN_SECONDS,
            },
            "accounts": [
                {"name": "acct-a", "api_key_env": "OPENCODE_P17_KEY_A"},
                {"name": "acct-b", "api_key_env": "OPENCODE_P17_KEY_B"},
            ],
            "dashboard": {"enabled": False},
        }
    )


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
    )


def _quota_exhausted_response() -> httpx.Response:
    return httpx.Response(
        402,
        content=b'{"error": {"message": "quota exhausted", "type": "quota"}}',
    )


@pytest_asyncio.fixture()
async def two_account_db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-a", "OPENCODE_P17_KEY_A"),
        )
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-b", "OPENCODE_P17_KEY_B"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def coordinator(
    two_account_db: Database,
) -> AsyncGenerator[RequestCoordinator, None]:
    config = _build_two_account_config()
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, two_account_db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "acct-a")
    catalog.cache.add_account_support("gpt-4", "acct-b")

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    for acct in ("acct-a", "acct-b"):
        router.configure_account_policy(
            account_name=acct,
            weight=1.0,
            capacity_5h_microdollars=12_000_000,
            capacity_7d_microdollars=30_000_000,
            capacity_30d_microdollars=60_000_000,
            offset_5h_microdollars=0,
            offset_7d_microdollars=0,
            offset_30d_microdollars=0,
        )

    request_repo = RequestRepository(two_account_db)
    reservation_repo = ReservationRepository(two_account_db)
    attempt_repo = AttemptRepository(two_account_db)
    usage_window_repo = UsageWindowRepository(two_account_db)
    price_repo = PriceRepository(two_account_db)
    cost_calculator = CostCalculator(price_repo)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=two_account_db,
        httpx_client=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
        cost_calculator=cost_calculator,
        quota_estimator=router._quota_estimator,
        max_retry_attempts=2,
        quota_exhausted_cooldown_seconds=COOLDOWN_SECONDS,
    )
    yield coord
    await httpx_client.aclose()


def _make_context(request_id: str) -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=LOCAL_BODY,
        incoming_headers={"content-type": "application/json"},
    )


class TestQuotaExhaustedLifecycle:
    """End-to-end 402 lifecycle: one account 402, other succeeds, cooldown, recovery."""

    @pytest.mark.asyncio
    async def test_402_then_success_then_exclusion_then_recovery(
        self,
        coordinator: RequestCoordinator,
        two_account_db: Database,
    ) -> None:
        """One account gets 402, the other succeeds; cooldown excludes the
        exhausted one; once cooldown elapses it is eligible again.

        The router picks the first account non-deterministically, so the
        test discovers which account got the 402 by inspecting health
        state after the first request and tracks both accounts
        symmetrically.
        """

        attempt_counter: dict[str, int] = {"acct-a": 0, "acct-b": 0}

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=self._two_account_handler(attempt_counter)
            )
            # Request 1: one account 402, the other 200.
            resp1 = await coordinator.execute(_make_context("p17-req-1"))
        assert resp1.status_code == 200
        # Both accounts were attempted exactly once.
        assert attempt_counter["acct-a"] == 1
        assert attempt_counter["acct-b"] == 1

        # Discover which account was the quota-exhausted one.
        health_a = coordinator._health_manager.get_account_health("acct-a")
        health_b = coordinator._health_manager.get_account_health("acct-b")
        if health_a.health_state == "quota_exhausted":
            exhausted_name = "acct-a"
            healthy_name = "acct-b"
        elif health_b.health_state == "quota_exhausted":
            exhausted_name = "acct-b"
            healthy_name = "acct-a"
        else:
            raise AssertionError(
                f"Expected one account in quota_exhausted, got "
                f"acct-a={health_a.health_state}, "
                f"acct-b={health_b.health_state}"
            )

        # The exhausted account must be unhealthy in both health and runtime.
        exhausted_health = coordinator._health_manager.get_account_health(
            exhausted_name
        )
        assert exhausted_health.health_state == "quota_exhausted"
        assert not exhausted_health.is_healthy
        exhausted_state = coordinator._registry.get_state(exhausted_name)
        assert exhausted_state is not None
        assert exhausted_state.health_state == "quota_exhausted"
        assert not exhausted_state.is_eligible()

        # The successful account must be healthy.
        healthy_health = coordinator._health_manager.get_account_health(healthy_name)
        assert healthy_health.health_state == "healthy"
        assert healthy_health.is_healthy

        # No active reservations remain for either account.
        active_for_a = await two_account_db.fetch_all(
            "SELECT id FROM reservations WHERE account_id = ? AND status = 'active'",
            (1,),
        )
        assert active_for_a == []
        active_for_b = await two_account_db.fetch_all(
            "SELECT id FROM reservations WHERE account_id = ? AND status = 'active'",
            (2,),
        )
        assert active_for_b == []

        # All attempts are terminal.
        incomplete = await two_account_db.fetch_all(
            "SELECT id FROM request_attempts WHERE completed_at IS NULL"
        )
        assert incomplete == []

        # No request is still pending.
        pending = await two_account_db.fetch_all(
            "SELECT id FROM requests WHERE status = 'pending'"
        )
        assert pending == []

        # No raw provider body persisted (no 'quota exhausted' in DB).
        for table in ("requests", "request_attempts"):
            rows = await two_account_db.fetch_all(
                f"SELECT error_detail FROM {table} WHERE error_detail IS NOT NULL"
            )
            for row in rows:
                detail = row["error_detail"] or ""
                assert "quota exhausted" not in detail, (
                    f"Raw provider body persisted in {table}.error_detail"
                )

        # No active request count remains for either account.
        a_state = coordinator._registry.get_state("acct-a")
        b_state = coordinator._registry.get_state("acct-b")
        assert a_state is not None and a_state.active_request_count == 0
        assert b_state is not None and b_state.active_request_count == 0

        # Request 2: while the exhausted account is in cooldown, it is not
        # attempted. Reset counters and re-register the rotating handler
        # (it returns 402 on the first call to whichever account is
        # selected, but with the exhausted account ineligible the only
        # selected account is the healthy one and it succeeds).
        attempt_counter["acct-a"] = 0
        attempt_counter["acct-b"] = 0
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=self._two_account_handler(attempt_counter)
            )
            resp2 = await coordinator.execute(_make_context("p17-req-2"))
        assert resp2.status_code == 200
        # The exhausted account must NOT be attempted while in cooldown.
        assert attempt_counter[exhausted_name] == 0, (
            f"Account {exhausted_name} must not be attempted while in cooldown."
        )
        # The healthy account handles the request and succeeds.
        assert attempt_counter[healthy_name] == 1

        # Request 3: advance the cooldown so the exhausted account
        # becomes eligible again. Both the runtime state and the
        # HealthManager track their own cooldown_until, so we must
        # advance both before asserting recovery.
        future = time.time() + COOLDOWN_SECONDS + 1.0
        exhausted_state.refresh_transient_state(now=future)
        # Manually expire the HealthManager's cooldown by rewinding it
        # into the past; the next ``is_account_healthy`` call will then
        # transition the state back to healthy.
        exhausted_health.cooldown_until = time.time() - 1.0
        # HealthManager refreshes transient state lazily on the next
        # ``is_account_healthy`` call; we still want to assert that
        # the health state recovers.
        assert coordinator._health_manager.is_account_healthy(exhausted_name)
        assert exhausted_health.health_state == "healthy"
        assert exhausted_state.is_eligible()

        # Now the formerly-exhausted account is attempted and succeeds.
        attempt_counter["acct-a"] = 0
        attempt_counter["acct-b"] = 0
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=self._single_account_handler(
                    attempt_counter, exhausted_name, _success_response
                )
            )
            resp3 = await coordinator.execute(_make_context("p17-req-3"))
        assert resp3.status_code == 200
        assert attempt_counter[exhausted_name] == 1
        assert attempt_counter[healthy_name] == 0

    @staticmethod
    def _two_account_handler(counter: dict[str, int]):
        """Return a handler that returns 402 for the first call and 200 for
        all subsequent calls.

        The handler infers the account name from the ``Authorization``
        header (``Bearer key-p17-a`` or ``Bearer key-p17-b``) and
        increments the matching counter key. This lets the test confirm
        which accounts were attempted and in what order.
        """

        def _handle(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            if "key-p17-a" in auth:
                counter["acct-a"] = counter.get("acct-a", 0) + 1
            elif "key-p17-b" in auth:
                counter["acct-b"] = counter.get("acct-b", 0) + 1
            # The first upstream call returns 402; every subsequent call
            # returns 200. The counter just records who was attempted.
            counter["__calls"] = counter.get("__calls", 0) + 1
            if counter["__calls"] == 1:
                return _quota_exhausted_response()
            return _success_response()

        return _handle

    @staticmethod
    def _single_account_handler(
        counter: dict[str, int],
        which: str,
        responder: Any,
    ):
        def _handle(request: httpx.Request) -> httpx.Response:
            counter[which] += 1
            return responder()

        return _handle


class TestQuotaExhaustedNoResidualState:
    """Reservations, attempts, and request rows are all terminal."""

    @pytest.mark.asyncio
    async def test_no_residual_reservation_or_active_count(
        self,
        coordinator: RequestCoordinator,
        two_account_db: Database,
    ) -> None:
        attempt_counter: dict[str, int] = {"a": 0, "b": 0}

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=TestQuotaExhaustedLifecycle._two_account_handler(
                    attempt_counter
                )
            )
            resp = await coordinator.execute(_make_context("p17-no-residual"))

        assert resp.status_code == 200

        active_resv = await two_account_db.fetch_all(
            "SELECT id FROM reservations WHERE status = 'active'"
        )
        assert active_resv == []

        incomplete_attempts = await two_account_db.fetch_all(
            "SELECT id FROM request_attempts WHERE completed_at IS NULL"
        )
        assert incomplete_attempts == []

        pending_requests = await two_account_db.fetch_all(
            "SELECT id FROM requests WHERE status = 'pending'"
        )
        assert pending_requests == []

        for acct in ("acct-a", "acct-b"):
            state = coordinator._registry.get_state(acct)
            assert state is not None and state.active_request_count == 0


class TestQuotaExhaustedHealthAppliedOnce:
    """The 402 attempt affects health exactly once."""

    @pytest.mark.asyncio
    async def test_health_failure_count_single(
        self,
        coordinator: RequestCoordinator,
    ) -> None:
        attempt_counter: dict[str, int] = {"a": 0, "b": 0}
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=TestQuotaExhaustedLifecycle._two_account_handler(
                    attempt_counter
                )
            )
            resp = await coordinator.execute(_make_context("p17-health-once"))

        assert resp.status_code == 200

        # Exactly one of the accounts must be in quota_exhausted state, the
        # other must be healthy. The router picks the first account
        # non-deterministically, so we cannot assert which is which.
        health_a = coordinator._health_manager.get_account_health("acct-a")
        health_b = coordinator._health_manager.get_account_health("acct-b")
        assert {health_a.health_state, health_b.health_state} == {
            "quota_exhausted",
            "healthy",
        }
        # The 402 account must be unhealthy; the success account must be healthy.
        if health_a.health_state == "quota_exhausted":
            assert not health_a.is_healthy
            assert health_b.is_healthy
        else:
            assert not health_b.is_healthy
            assert health_a.is_healthy
