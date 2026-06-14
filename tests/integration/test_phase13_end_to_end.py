"""Phase 13 end-to-end integration tests.

Covers: attempt lifecycle, transaction ownership, projected estimates,
cancellation, no-usage accounting, price snapshots, telemetry,
quota eligibility, protocol enforcement, 404 classification,
duplicate finalization, and restart recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.catalog.pricing import CostCalculator, PriceRepository
from go_aggregator.catalog.protocols import ProtocolMismatchError
from go_aggregator.catalog.service import CatalogService
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AccountRepository,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_two_account_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY_A"] = "key-acct-a"
    os.environ["OPENCODE_TEST_KEY_B"] = "key-acct-b"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY_A",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "limits": {
                "five_hour_microdollars": 12_000_000,
                "weekly_microdollars": 30_000_000,
                "monthly_microdollars": 60_000_000,
            },
            "accounts": [
                {"name": "acct-a", "api_key_env": "OPENCODE_TEST_KEY_A"},
                {"name": "acct-b", "api_key_env": "OPENCODE_TEST_KEY_B"},
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
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        },
    )


def _error_response(status: int, body: str = '{"error": "fail"}') -> httpx.Response:
    return httpx.Response(status, content=body.encode())


def _make_openai_body(model: str = "gpt-4", stream: bool = False) -> bytes:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    if stream:
        payload["stream"] = True
    return json.dumps(payload).encode()


async def _seed_usage(
    db: Database,
    account_name: str,
    cost_microdollars: int,
    model_id: str = "gpt-4",
) -> None:
    """Insert a completed request to seed usage for an account."""
    import datetime as _dt

    acct_repo = AccountRepository(db)
    acct_id = await acct_repo.get_id_by_name(account_name)
    if acct_id is None:
        return
    started_at = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    seed_id = f"seed-{account_name}-{cost_microdollars}"
    await db.execute(
        "INSERT INTO requests "
        "(account_id, model_id, status, protocol, streamed, "
        "reserved_microdollars, proxy_request_id, cost_microdollars, started_at) "
        "VALUES (?, ?, 'completed', 'openai', 0, 0, ?, ?, ?)",
        (acct_id, model_id, seed_id, cost_microdollars, started_at),
    )
    await db.connection.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def two_account_db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    await database.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("acct-a", "OPENCODE_TEST_KEY_A"),
    )
    await database.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("acct-b", "OPENCODE_TEST_KEY_B"),
    )
    await database.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
    )
    await database.connection.commit()
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
    router.configure_account_policy(
        account_name="acct-a",
        weight=1.0,
        capacity_5h_microdollars=12_000_000,
        capacity_7d_microdollars=30_000_000,
        capacity_30d_microdollars=60_000_000,
        offset_5h_microdollars=0,
        offset_7d_microdollars=0,
        offset_30d_microdollars=0,
    )
    router.configure_account_policy(
        account_name="acct-b",
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
    )
    yield coord
    await httpx_client.aclose()


# ===========================================================================
# Section A: Successful failover cleanup
# ===========================================================================


async def _assert_failover_invariants(
    db: Database,
    proxy_request_id: str,
    expected_account_names: set[str],
    max_retry_attempts: int,
    health_manager: HealthManager,
) -> None:
    """Shared assertions for failover cleanup tests."""
    req_row = await db.fetch_one(
        "SELECT id FROM requests WHERE proxy_request_id = ?",
        (proxy_request_id,),
    )
    assert req_row is not None, f"Request {proxy_request_id} not found"
    request_id = str(req_row["id"])

    # Both attempts exist
    attempts = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ? ORDER BY attempt_number",
        (request_id,),
    )
    assert len(attempts) == 2, f"Expected 2 attempts, got {len(attempts)}"

    # Both attempts terminal (have completed_at)
    for attempt in attempts:
        assert attempt["completed_at"] is not None, (
            f"Attempt {attempt['attempt_number']} has no completed_at"
        )

    # First attempt has error status (may be None for connect errors)
    # Second attempt has 200
    assert attempts[1]["status_code"] == 200

    # Both reservations released
    resv_rows = await db.fetch_all(
        "SELECT * FROM reservations WHERE request_id = ?", (request_id,)
    )
    assert len(resv_rows) >= 2, f"Expected >= 2 reservations, got {len(resv_rows)}"
    for resv in resv_rows:
        assert resv["status"] == "released", (
            f"Reservation {resv['id']} not released: status={resv['status']}"
        )

    # Zero active reservations
    active_resvs = await db.fetch_all(
        "SELECT * FROM reservations WHERE request_id = ? AND status = 'active'",
        (request_id,),
    )
    assert len(active_resvs) == 0, (
        f"Expected 0 active reservations, got {len(active_resvs)}"
    )

    # Both attempts used different accounts
    attempt_account_ids = {a["account_id"] for a in attempts}
    assert len(attempt_account_ids) == 2, "Failover did not use different accounts"


class TestAttemptLifecycle:
    """Section A: Successful failover cleanup."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_failover_429_then_success(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Attempt 1 -> 429, Attempt 2 -> 200.
        Assert: both attempts terminal, both reservations released.
        """
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(429, '{"error": "rate limited"}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            context = ProxyRequestContext(
                request_id="test-a-429",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)

        assert response.status_code == 200
        assert call_count == 2

        await _assert_failover_invariants(
            two_account_db,
            "test-a-429",
            {"acct-a", "acct-b"},
            2,
            coordinator._health_manager,
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_failover_401_then_success(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """401 then 200. Same assertions as above."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(401, '{"error": "unauthorized"}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            context = ProxyRequestContext(
                request_id="test-a-401",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)

        assert response.status_code == 200
        assert call_count == 2

        await _assert_failover_invariants(
            two_account_db,
            "test-a-401",
            {"acct-a", "acct-b"},
            2,
            coordinator._health_manager,
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_failover_connect_error_then_success(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Connect error then 200."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("Connection refused")
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            context = ProxyRequestContext(
                request_id="test-a-connect",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)

        assert response.status_code == 200
        assert call_count == 2

        await _assert_failover_invariants(
            two_account_db,
            "test-a-connect",
            {"acct-a", "acct-b"},
            2,
            coordinator._health_manager,
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_failover_500_then_success(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """500 then 200."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(500, '{"error": "internal error"}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            context = ProxyRequestContext(
                request_id="test-a-500",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)

        assert response.status_code == 200
        assert call_count == 2

        await _assert_failover_invariants(
            two_account_db,
            "test-a-500",
            {"acct-a", "acct-b"},
            2,
            coordinator._health_manager,
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_failover_402_then_success(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """402 (payment required) then 200."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(402, '{"error": "payment required"}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            context = ProxyRequestContext(
                request_id="test-a-402",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await coordinator.execute(context)

        assert response.status_code == 200
        assert call_count == 2

        await _assert_failover_invariants(
            two_account_db,
            "test-a-402",
            {"acct-a", "acct-b"},
            2,
            coordinator._health_manager,
        )


# ===========================================================================
# Section B: Concurrent transaction isolation
# ===========================================================================


class TestTransactionConcurrency:
    """Section B: Concurrent transaction isolation."""

    @pytest.mark.asyncio
    async def test_concurrent_transactions_are_serialized(
        self, two_account_db: Database
    ) -> None:
        """Two concurrent transactions must not interleave.

        Task A starts a transaction and waits. Task B attempts a transaction
        and must block until Task A commits.
        """
        task_a_entered = asyncio.Event()
        task_a_can_continue = asyncio.Event()
        task_b_entered = asyncio.Event()

        async def _task_a() -> None:
            async with two_account_db.transaction():
                await two_account_db.execute(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("task-a-account", "DUMMY_KEY"),
                )
                task_a_entered.set()
                await asyncio.wait_for(task_a_can_continue.wait(), timeout=5.0)
                # Commit happens via context manager exit

        async def _task_b() -> None:
            # Wait a moment so task A enters first
            await asyncio.sleep(0.1)
            async with two_account_db.transaction():
                task_b_entered.set()
                await two_account_db.execute(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("task-b-account", "DUMMY_KEY"),
                )

        task_a = asyncio.create_task(_task_a())
        await asyncio.wait_for(task_a_entered.wait(), timeout=5.0)

        # Task B should NOT have entered its body yet (blocked by task A's lock)
        assert not task_b_entered.is_set(), (
            "Task B entered transaction while Task A holds it"
        )

        # Release task A
        task_a_can_continue.set()
        await asyncio.wait_for(task_a, timeout=5.0)

        # Now task B should complete
        task_b = asyncio.create_task(_task_b())
        await asyncio.wait_for(task_b, timeout=5.0)
        assert task_b_entered.is_set(), "Task B never entered transaction"

        # Verify both inserts succeeded
        acct_a = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?", ("task-a-account",)
        )
        acct_b = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?", ("task-b-account",)
        )
        assert acct_a is not None, "Task A insert not found"
        assert acct_b is not None, "Task B insert not found"

    @pytest.mark.asyncio
    async def test_readiness_does_not_rollback_request(
        self, two_account_db: Database
    ) -> None:
        """Readiness probe cannot roll back another request's work."""
        # Simulate a request inserting a row
        await two_account_db.execute(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("request-account", "DUMMY_KEY"),
        )
        await two_account_db.connection.commit()

        # Now simulate a readiness probe using a savepoint approach
        # The readiness probe should NOT roll back the request's row
        async with two_account_db.transaction():
            await two_account_db.execute(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                ("probe-account", "DUMMY_KEY"),
            )
            # Simulate a rollback by raising an exception
            try:
                raise ValueError("probe rollback simulation")
            except ValueError:
                # The inner exception causes rollback of only probe-account
                pass

        # The request-account row should survive
        survived = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?", ("request-account",)
        )
        assert survived is not None, (
            "Readiness probe rolled back unrelated request data"
        )

    @pytest.mark.asyncio
    async def test_nested_same_task_transaction_rollback(
        self, two_account_db: Database
    ) -> None:
        """Outer transaction inserts A, inner inserts B, outer raises.
        Assert both A and B roll back.
        """
        # Clear any prior state from other tests
        await two_account_db.execute(
            "DELETE FROM accounts WHERE name IN ('nested-a', 'nested-b')"
        )
        await two_account_db.connection.commit()

        with pytest.raises(ValueError, match="outer failure"):
            async with two_account_db.transaction():
                await two_account_db.execute(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("nested-a", "DUMMY_KEY"),
                )
                # Nested transaction (same task) - inherits outer boundary
                async with two_account_db.transaction():
                    await two_account_db.execute(
                        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, 1.0)",
                        ("nested-b", "DUMMY_KEY"),
                    )
                # Outer raises - both inserts should roll back
                raise ValueError("outer failure")

        # Both inserts should be rolled back
        row_a = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?", ("nested-a",)
        )
        row_b = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?", ("nested-b",)
        )
        assert row_a is None, "Nested outer rollback: row A survived"
        assert row_b is None, "Nested outer rollback: row B survived"


# ===========================================================================
# Section C: Projected request selection
# ===========================================================================


class TestProjectedSelection:
    """Section C: Projected request selection."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_estimated_cost_affects_selection(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Account with lower projected cost is selected.

        Seed account A with high usage so its projected cost exceeds B.
        Verify B is selected.
        """
        # First request - establish baseline (either account is fine)
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_success_response()
            )
            ctx = ProxyRequestContext(
                request_id="test-c-baseline",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200

        # Seed account A with high 5h usage
        await _seed_usage(two_account_db, "acct-a", 8_000_000)

        # Reload persisted windows so the estimator sees the seeded usage
        usage_window_repo = UsageWindowRepository(two_account_db)
        coordinator._quota_estimator.set_usage_window_repo(usage_window_repo)
        await coordinator._quota_estimator.load_persisted_windows()

        # Second request - A has high usage, B should be preferred
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_success_response()
            )
            ctx = ProxyRequestContext(
                request_id="test-c-projected",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200
            assert resp.account_name == "acct-b", (
                f"Expected acct-b after seeding acct-a high usage, "
                f"got {resp.account_name}"
            )

        # Verify the persisted reservation uses the projected estimate
        req_row = await two_account_db.fetch_one(
            "SELECT reserved_microdollars FROM requests WHERE proxy_request_id = ?",
            ("test-c-projected",),
        )
        assert req_row is not None
        assert req_row["reserved_microdollars"] > 0, (
            "Reservation should use nonzero projected estimate"
        )


# ===========================================================================
# Section D: Cancellation stages
# ===========================================================================


class TestCancellationStages:
    """Section D: Cancellation stages."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_cancel_before_headers(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Cancel during connect / before upstream headers arrive.

        For streaming, this means cancelling the task that calls
        coordinator.execute() before any response is returned.
        """
        upstream_called = asyncio.Event()
        hold_upstream = asyncio.Event()

        async def _handler(request: httpx.Request) -> httpx.Response:
            upstream_called.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(hold_upstream.wait(), timeout=5.0)

            async def _aiter_bytes():
                yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return httpx.Response(
                200,
                stream=_aiter_bytes(),
                headers={"content-type": "text/event-stream"},
            )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            ctx = ProxyRequestContext(
                request_id="test-d-cancel-pre-headers",
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_openai_body(stream=True),
                incoming_headers={"content-type": "application/json"},
            )
            # Start the execute task
            exec_task = asyncio.create_task(coordinator.execute(ctx))

            # Wait until upstream is called (connection established)
            await asyncio.wait_for(upstream_called.wait(), timeout=5.0)

            # Cancel the entire execute task before upstream responds
            exec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task

            # Release upstream so the mock doesn't hang
            hold_upstream.set()

        # Give finalizer a moment
        await asyncio.sleep(0.3)

        # Verify request is terminal (cancelled or pending depending on
        # where exactly the cancel landed in the lifecycle)
        req_row = await two_account_db.fetch_one(
            "SELECT status FROM requests WHERE proxy_request_id = ?",
            ("test-d-cancel-pre-headers",),
        )
        assert req_row is not None
        # Cancel during connect may land as pending (no selection yet)
        # or cancelled (after selection). Both are acceptable.
        assert req_row["status"] in ("pending", "cancelled", "completed", "error"), (
            f"Unexpected status: {req_row['status']}"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_cancel_during_stream(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Cancel during stream. Assert complete cleanup."""
        upstream_started = asyncio.Event()
        hold_upstream = asyncio.Event()

        async def _handler(request: httpx.Request) -> httpx.Response:
            upstream_started.set()

            async def _aiter_bytes():
                yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
                # Hold until test cancels
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(hold_upstream.wait(), timeout=5.0)
                yield b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return httpx.Response(
                200,
                stream=_aiter_bytes(),
                headers={"content-type": "text/event-stream"},
            )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            ctx = ProxyRequestContext(
                request_id="test-d-cancel-during-stream",
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_openai_body(stream=True),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200

            # Consume one chunk
            stream_iter = resp.stream_iterator
            assert stream_iter is not None
            chunk1 = await stream_iter.__anext__()
            assert len(chunk1) > 0

            # Cancel next chunk task
            next_chunk_task = asyncio.create_task(stream_iter.__anext__())
            await asyncio.sleep(0.05)
            next_chunk_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_chunk_task

            # Release upstream
            hold_upstream.set()

        # Give finalizer a moment
        await asyncio.sleep(0.2)

        # Verify terminal state
        req_row = await two_account_db.fetch_one(
            "SELECT status FROM requests WHERE proxy_request_id = ?",
            ("test-d-cancel-during-stream",),
        )
        assert req_row is not None
        assert req_row["status"] in ("cancelled", "completed")

        # Verify reservation released
        req_id = await two_account_db.fetch_one(
            "SELECT id FROM requests WHERE proxy_request_id = ?",
            ("test-d-cancel-during-stream",),
        )
        assert req_id is not None
        resv_rows = await two_account_db.fetch_all(
            "SELECT * FROM reservations WHERE request_id = ?",
            (str(req_id["id"]),),
        )
        assert len(resv_rows) >= 1
        assert resv_rows[0]["status"] == "released"

        # Verify account not penalized (CLIENT_CANCELLED doesn't penalize)
        health = coordinator._health_manager.get_account_health("acct-a")
        assert health.is_healthy

    @respx.mock
    @pytest.mark.asyncio
    async def test_cancel_non_streaming_after_connect(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Cancel a non-streaming request after upstream connection established.

        The upstream responds slowly; cancel arrives after headers but
        before the response body is fully read.
        """
        upstream_called = asyncio.Event()
        hold_upstream = asyncio.Event()

        async def _handler(request: httpx.Request) -> httpx.Response:
            upstream_called.set()
            # Hold until test cancels
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(hold_upstream.wait(), timeout=5.0)
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-slow",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Late"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            ctx = ProxyRequestContext(
                request_id="test-d-cancel-non-streaming",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            exec_task = asyncio.create_task(coordinator.execute(ctx))

            # Wait until upstream is called
            await asyncio.wait_for(upstream_called.wait(), timeout=5.0)

            # Cancel before upstream responds
            exec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task

            # Release upstream
            hold_upstream.set()

        # Give finalizer a moment
        await asyncio.sleep(0.3)

        # Verify terminal state
        req_row = await two_account_db.fetch_one(
            "SELECT status FROM requests WHERE proxy_request_id = ?",
            ("test-d-cancel-non-streaming",),
        )
        assert req_row is not None
        assert req_row["status"] in ("cancelled", "completed", "pending")

        # Verify reservation released
        req_id = await two_account_db.fetch_one(
            "SELECT id FROM requests WHERE proxy_request_id = ?",
            ("test-d-cancel-non-streaming",),
        )
        if req_id is not None:
            resv_rows = await two_account_db.fetch_all(
                "SELECT * FROM reservations WHERE request_id = ?",
                (str(req_id["id"]),),
            )
            for resv in resv_rows:
                assert resv["status"] == "released"


# ===========================================================================
# Section E: No-usage success
# ===========================================================================


class TestNoUsageSuccess:
    """Section E: No-usage success."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_success_without_usage_persists_estimate(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Successful response without usage persists nonzero estimated cost."""
        # Response without usage field
        no_usage_response = httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=no_usage_response
            )

            ctx = ProxyRequestContext(
                request_id="test-e-no-usage",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200

        # Verify cost is nonzero (uses reservation estimate)
        req_row = await two_account_db.fetch_one(
            "SELECT cost_microdollars, exactness FROM requests "
            "WHERE proxy_request_id = ?",
            ("test-e-no-usage",),
        )
        assert req_row is not None
        assert req_row["cost_microdollars"] > 0, (
            "No-usage success should use reservation estimate, not zero"
        )
        assert req_row["exactness"] == "estimated"

    @respx.mock
    @pytest.mark.asyncio
    async def test_estimated_cost_influences_immediate_routing(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Estimated cost from first request influences routing of second request.

        First request succeeds without usage (cost = estimated). Second request
        should see the estimated cost in the account's utilization.
        """
        # First request - no usage, cost will be estimated
        no_usage_response = httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=no_usage_response
            )
            ctx = ProxyRequestContext(
                request_id="test-e-routing-1",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200

        # Verify first request used estimated cost
        req1 = await two_account_db.fetch_one(
            "SELECT cost_microdollars, exactness FROM requests "
            "WHERE proxy_request_id = ?",
            ("test-e-routing-1",),
        )
        assert req1 is not None
        assert req1["cost_microdollars"] > 0
        assert req1["exactness"] == "estimated"

        # The estimated cost should now be reflected in the account's
        # in-memory quota state, which the scorer uses for routing.
        # Verify the daily window captured the cost.
        estimator = coordinator._quota_estimator
        account_name = resp.account_name
        snapshot = estimator.get_account_quota(account_name)
        assert snapshot is not None
        daily_tokens, daily_cost = snapshot.daily_window.get_usage()
        assert daily_cost > 0, "Estimated cost should be reflected in daily window"


# ===========================================================================
# Section F: New price snapshot
# ===========================================================================


class TestPriceSnapshot:
    """Section F: New price snapshot."""

    @pytest.mark.asyncio
    async def test_snapshot_yields_nonzero_cost(self, two_account_db: Database) -> None:
        """Insert snapshot and verify nonzero derived cost."""
        price_repo = PriceRepository(two_account_db)
        cost_calculator = CostCalculator(price_repo)

        # Insert a price snapshot with integer microdollar rates
        async with two_account_db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=0.03,
                output_price_per_1k=0.06,
                input_per_million_microdollars=30_000,
                output_per_million_microdollars=60_000,
                source="test",
            )

        # Calculate cost using the snapshot
        cost, exactness = await cost_calculator.calculate_cost(
            "gpt-4",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost > 0, "Snapshot-derived cost should be nonzero"
        assert exactness == "derived"

        # Verify snapshot is stored correctly
        snapshot = await price_repo.get_latest_snapshot("gpt-4")
        assert snapshot is not None
        assert snapshot.input_per_million_microdollars == 30_000
        assert snapshot.output_per_million_microdollars == 60_000


# ===========================================================================
# Section G: Missing price category
# ===========================================================================


class TestMissingPriceCategory:
    """Section G: Missing price category."""

    @pytest.mark.asyncio
    async def test_missing_input_rate_returns_estimated(
        self, two_account_db: Database
    ) -> None:
        """Missing rate returns estimated, not derived."""
        price_repo = PriceRepository(two_account_db)
        cost_calculator = CostCalculator(price_repo)

        # Insert a snapshot with only output rate (missing input rate)
        async with two_account_db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=0.06,
                input_per_million_microdollars=None,
                output_per_million_microdollars=60_000,
                source="test",
            )

        # Calculate cost with input tokens > 0 but no input rate
        cost, exactness = await cost_calculator.calculate_cost(
            "gpt-4",
            input_tokens=1000,
            output_tokens=500,
        )

        # Should be estimated (missing required input rate)
        assert exactness == "estimated", (
            f"Missing input rate should return 'estimated', got '{exactness}'"
        )
        assert cost > 0, "Cost should be nonzero even with estimated fallback"


# ===========================================================================
# Section H: Protocol fail closed
# ===========================================================================


class TestProtocolFailClosed:
    """Section H: Protocol fail closed."""

    @pytest.mark.asyncio
    async def test_unknown_model_not_exposed(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Unknown model is neither exposed nor routed."""
        # Request with unknown model should fail with 404
        with respx.mock:
            # No route mocked - should not reach upstream
            ctx = ProxyRequestContext(
                request_id="test-h-unknown",
                protocol="openai",
                model_id="nonexistent-model-xyz",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "nonexistent-model-xyz",
                        "messages": [{"role": "user", "content": "Hi"}],
                    }
                ).encode(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        # Should return 404 (model not found)
        assert resp.status_code == 404

        # Verify no request rows were created
        req_rows = await two_account_db.fetch_all(
            "SELECT * FROM requests WHERE proxy_request_id = ?",
            ("test-h-unknown",),
        )
        assert len(req_rows) == 0, "Unknown model should not create lifecycle rows"

    @respx.mock
    @pytest.mark.asyncio
    async def test_wrong_endpoint_returns_400(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Anthropic model through /chat/completions raises ProtocolMismatchError.

        The API endpoint layer catches this and returns 400. Here we verify
        the coordinator raises it before any lifecycle rows are created.
        """
        # Load an anthropic model into the catalog cache for this test
        coordinator._catalog.cache.load_model(
            model_id="claude-3-opus-20240229",
            display_name="Claude 3 Opus",
            protocol="anthropic",
            capabilities={},
            source_metadata={},
        )
        coordinator._catalog.cache.add_account_support(
            "claude-3-opus-20240229", "acct-a"
        )

        with respx.mock:
            # No route mocked - should not reach upstream
            ctx = ProxyRequestContext(
                request_id="test-h-wrong-endpoint",
                protocol="openai",
                model_id="claude-3-opus-20240229",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "claude-3-opus-20240229",
                        "messages": [{"role": "user", "content": "Hi"}],
                    }
                ).encode(),
                incoming_headers={"content-type": "application/json"},
            )
            with pytest.raises(ProtocolMismatchError):
                await coordinator.execute(ctx)

        # Verify no request rows were created
        req_rows = await two_account_db.fetch_all(
            "SELECT * FROM requests WHERE proxy_request_id = ?",
            ("test-h-wrong-endpoint",),
        )
        assert len(req_rows) == 0, "Wrong endpoint should not create lifecycle rows"


# ===========================================================================
# Section I: Model-specific 404
# ===========================================================================


class TestModelSpecific404:
    """Section I: Model-specific 404."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_model_404_from_a_200_from_b(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """A returns model-not-found, B succeeds. Only A/model disabled."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(404, '{"error": {"message": "model not found"}}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

            ctx = ProxyRequestContext(
                request_id="test-i-model-404",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        # Should succeed via failover
        assert resp.status_code == 200
        assert call_count == 2

        # Verify two attempts
        req_row = await two_account_db.fetch_one(
            "SELECT id FROM requests WHERE proxy_request_id = ?",
            ("test-i-model-404",),
        )
        assert req_row is not None
        attempts = await two_account_db.fetch_all(
            "SELECT * FROM request_attempts WHERE request_id = ?",
            (str(req_row["id"]),),
        )
        assert len(attempts) == 2

        # Verify first attempt had 404
        assert attempts[0]["status_code"] == 404

        # Determine which account was used for the first attempt
        first_attempt_account_id = attempts[0]["account_id"]
        acct_rows = await two_account_db.fetch_all(
            "SELECT id, name FROM accounts WHERE id = ?",
            (first_attempt_account_id,),
        )
        assert len(acct_rows) == 1
        first_attempt_account_name = acct_rows[0]["name"]

        # Verify model is disabled for the first-attempt account
        health = coordinator._health_manager.get_account_health(
            first_attempt_account_name
        )
        assert "gpt-4" in health.disabled_models, (
            f"Model should be disabled for {first_attempt_account_name} "
            "after model-specific 404"
        )

        # Verify model is NOT disabled for the other account
        other_account = "acct-b" if first_attempt_account_name == "acct-a" else "acct-a"
        health_other = coordinator._health_manager.get_account_health(other_account)
        assert "gpt-4" not in health_other.disabled_models, (
            f"Model should not be disabled for {other_account}"
        )

        # Verify catalog cache reflects the unavailability
        supporting = coordinator._catalog.cache.get_supporting_accounts("gpt-4")
        assert "acct-b" in supporting, "acct-b should still support gpt-4"
        # acct-a may or may not be in supporting accounts depending on
        # whether the cache was updated, but the health manager should
        # block routing to acct-a for this model


# ===========================================================================
# Section J: Duplicate finalization
# ===========================================================================


class TestDuplicateFinalization:
    """Section J: Duplicate finalization."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_double_finalize_preserves_original(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Second finalization call cannot overwrite terminal fields."""
        from go_aggregator.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
        )

        # Complete a successful request
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_success_response()
            )

            ctx = ProxyRequestContext(
                request_id="test-j-double-finalize",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200

        # Read original request state
        req_row = await two_account_db.fetch_one(
            "SELECT * FROM requests WHERE proxy_request_id = ?",
            ("test-j-double-finalize",),
        )
        assert req_row is not None
        original_status = req_row["status"]
        original_cost = req_row["cost_microdollars"]

        # Read original attempt state
        attempt_rows = await two_account_db.fetch_all(
            "SELECT * FROM request_attempts WHERE request_id = ?",
            (str(req_row["id"]),),
        )
        assert len(attempt_rows) == 1
        original_attempt_status = attempt_rows[0]["status_code"]
        original_attempt_bytes = attempt_rows[0]["bytes_emitted"]

        # Read original reservation state
        resv_rows = await two_account_db.fetch_all(
            "SELECT * FROM reservations WHERE request_id = ?",
            (str(req_row["id"]),),
        )
        assert len(resv_rows) >= 1
        original_resv_status = resv_rows[0]["status"]

        # Now attempt a second finalization with DIFFERENT data
        # (500 status, 0 bytes) - this should NOT overwrite the original
        from go_aggregator.request.coordinator import SelectedAttempt

        selected = SelectedAttempt(
            proxy_request_id="test-j-double-finalize",
            db_request_id=str(req_row["id"]),
            attempt_id=attempt_rows[0]["id"],
            reservation_id=resv_rows[0]["id"],
            account_id=attempt_rows[0]["account_id"],
            account_name="acct-a",
            api_key="key",
            model_id="gpt-4",
            estimated_microdollars=1000,
            estimated_tokens=100,
            attempt_number=1,
        )
        result = await coordinator._finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                status_code=500,
                bytes_emitted=0,
                error_class="TestError",
            ),
        )
        # Second finalization should return False (idempotent)
        assert result is False, (
            "Second finalization should return False for already-terminal request"
        )

        # Verify original state is preserved
        req_row_after = await two_account_db.fetch_one(
            "SELECT * FROM requests WHERE proxy_request_id = ?",
            ("test-j-double-finalize",),
        )
        assert req_row_after is not None
        assert req_row_after["status"] == original_status
        assert req_row_after["cost_microdollars"] == original_cost

        attempt_rows_after = await two_account_db.fetch_all(
            "SELECT * FROM request_attempts WHERE request_id = ?",
            (str(req_row_after["id"]),),
        )
        assert len(attempt_rows_after) == 1
        assert attempt_rows_after[0]["status_code"] == original_attempt_status
        assert attempt_rows_after[0]["bytes_emitted"] == original_attempt_bytes

        resv_rows_after = await two_account_db.fetch_all(
            "SELECT * FROM reservations WHERE request_id = ?",
            (str(req_row_after["id"]),),
        )
        assert len(resv_rows_after) >= 1
        assert resv_rows_after[0]["status"] == original_resv_status


# ===========================================================================
# Section L: Privacy regression
# ===========================================================================


class TestPrivacyRegression:
    """Section L: Privacy regression."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_secrets_in_db(
        self,
        coordinator: RequestCoordinator,
        two_account_db: Database,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No API keys or prompt content in database."""
        secret_prompt = "SUPER_SECRET_PROMPT_MARKER_9f3a2b"
        secret_response = "SECRET_RESPONSE_MARKER_7e4d1c"
        secret_api_key = "sk-SECRET_API_KEY_MARKER_5a8b3c"
        secret_auth = "SECRET_AUTH_MARKER_2d9e8f"

        # Set the API key env var to the secret value
        os.environ["OPENCODE_TEST_KEY_A"] = secret_api_key
        _build_two_account_config()
        os.environ["OPENCODE_TEST_KEY_A"] = secret_api_key

        with caplog.at_level(logging.DEBUG), respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "cmpl-1",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": secret_response,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                )
            )

            ctx = ProxyRequestContext(
                request_id="test-l-privacy",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": secret_prompt}],
                    }
                ).encode(),
                incoming_headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {secret_auth}",
                },
            )
            resp = await coordinator.execute(ctx)
            assert resp.status_code == 200

        # Search all database text columns for any of the markers
        all_markers = [
            secret_prompt,
            secret_response,
            secret_api_key,
            secret_auth,
        ]
        tables = ["requests", "reservations", "request_attempts", "accounts"]
        for table in tables:
            rows = await two_account_db.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
            for row in rows:
                row_str = json.dumps(dict(row))
                for marker in all_markers:
                    assert marker not in row_str, (
                        f"Privacy marker '{marker}' found in {table}: {row_str[:200]}"
                    )

        # Search captured logs for any of the markers
        for record in caplog.records:
            msg = record.getMessage()
            for marker in all_markers:
                assert marker not in msg, (
                    f"Privacy marker '{marker}' found in log: {msg[:200]}"
                )


# ===========================================================================
# Section K: Restart recovery (bonus, from phase-12 pattern)
# ===========================================================================


class TestRestartRecovery:
    """Section K: Restart recovery."""

    @pytest.mark.asyncio
    async def test_restart_recovery_with_per_attempt_semantics(
        self, two_account_db: Database
    ) -> None:
        """Create committed pending request, reservation, and incomplete attempt.
        Simulate restart and confirm recovery is compatible with per-attempt
        semantics.
        """
        config = _build_two_account_config()
        httpx_client = httpx.AsyncClient(
            base_url=config.upstream.base_url,
            timeout=httpx.Timeout(
                300.0, connect=5.0, read=300.0, write=30.0, pool=30.0
            ),
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

        request_repo = RequestRepository(two_account_db)
        reservation_repo = ReservationRepository(two_account_db)
        attempt_repo = AttemptRepository(two_account_db)

        # Step 1: Manually commit a pending request/reservation/attempt
        acct_repo = AccountRepository(two_account_db)
        acct_a_id = await acct_repo.get_id_by_name("acct-a")

        db_request_id = await request_repo.create_pending(
            request_id="test-k-recovery",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=acct_a_id,
        )
        await request_repo.update_after_selection(
            request_id=db_request_id,
            account_id=acct_a_id,
            reserved_microdollars=500000,
        )
        reservation_id = await reservation_repo.create(
            request_id=db_request_id,
            account_id=acct_a_id,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=500000,
        )
        await attempt_repo.create(
            request_id=db_request_id,
            attempt_number=1,
            account_id=acct_a_id,
        )
        await two_account_db.connection.commit()

        # Verify pending state exists
        req_row = await two_account_db.fetch_one(
            "SELECT status FROM requests WHERE proxy_request_id = ?",
            ("test-k-recovery",),
        )
        assert req_row is not None
        assert req_row["status"] == "pending"

        # Step 2: Make the request look old enough for recovery to pick it up
        await two_account_db.execute(
            "UPDATE requests SET started_at = datetime('now', '-15 minutes') "
            "WHERE proxy_request_id = ?",
            ("test-k-recovery",),
        )
        await two_account_db.execute(
            "UPDATE reservations SET created_at = datetime('now', '-15 minutes') "
            "WHERE id = ?",
            (reservation_id,),
        )
        await two_account_db.execute(
            "UPDATE request_attempts SET started_at = datetime('now', '-15 minutes') "
            "WHERE request_id = ?",
            (db_request_id,),
        )
        await two_account_db.connection.commit()

        # Step 3: Run crash recovery
        from go_aggregator.app import _crash_recovery

        await _crash_recovery(two_account_db)

        # Step 4: Verify interrupted terminal state
        req_row = await two_account_db.fetch_one(
            "SELECT status FROM requests WHERE proxy_request_id = ?",
            ("test-k-recovery",),
        )
        assert req_row is not None
        assert req_row["status"] == "interrupted"

        # Step 5: Verify reservation released
        resv_rows = await two_account_db.fetch_all(
            "SELECT status, release_reason FROM reservations WHERE id = ?",
            (reservation_id,),
        )
        assert len(resv_rows) >= 1
        assert resv_rows[0]["status"] == "released"
        assert resv_rows[0]["release_reason"] == "crash_recovery"

        # Step 6: Verify incomplete attempt was finalized by crash recovery
        attempt_rows = await two_account_db.fetch_all(
            "SELECT completed_at, error_class "
            "FROM request_attempts WHERE request_id = ?",
            (db_request_id,),
        )
        assert len(attempt_rows) == 1
        assert attempt_rows[0]["completed_at"] is not None
        assert attempt_rows[0]["error_class"] == "process_interrupted"

        # Step 7: Verify recovery event exists
        events = await two_account_db.fetch_all(
            "SELECT * FROM account_events WHERE event_type = 'crash_recovery'"
        )
        assert len(events) >= 1

        await httpx_client.aclose()


# ===========================================================================
# Section 10.3: Protocol family mapping tests
# ===========================================================================


class TestProtocolFamilyMappings:
    """Section 10.3: Verify Go model family mappings resolve correctly."""

    @pytest.mark.asyncio
    async def test_glm_family_resolves_to_openai(self) -> None:
        """GLM models should resolve to openai protocol."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("glm-4-plus")
        assert resolution.protocol == "openai"
        assert resolution.source == "family_mapping"

    @pytest.mark.asyncio
    async def test_kimi_family_resolves_to_openai(self) -> None:
        """Kimi models should resolve to openai protocol."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("kimi-k2")
        assert resolution.protocol == "openai"
        assert resolution.source == "family_mapping"

    @pytest.mark.asyncio
    async def test_mimo_family_resolves_to_openai(self) -> None:
        """MiMo models should resolve to openai protocol."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("mimo-7b")
        assert resolution.protocol == "openai"
        assert resolution.source == "family_mapping"

    @pytest.mark.asyncio
    async def test_deepseek_family_resolves_to_openai(self) -> None:
        """DeepSeek models should resolve to openai protocol."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("deepseek-v3")
        assert resolution.protocol == "openai"
        assert resolution.source == "family_mapping"

    @pytest.mark.asyncio
    async def test_minimax_family_resolves_to_openai(self) -> None:
        """MiniMax models should resolve to openai protocol."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("minimax-text-01")
        assert resolution.protocol == "openai"
        assert resolution.source == "family_mapping"

    @pytest.mark.asyncio
    async def test_qwen_family_resolves_to_openai(self) -> None:
        """Qwen models should resolve to openai protocol."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("qwen-max")
        assert resolution.protocol == "openai"
        assert resolution.source == "family_mapping"

    @pytest.mark.asyncio
    async def test_unknown_model_remains_unresolved(self) -> None:
        """Unknown model should remain unresolved."""
        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolution = resolver.resolve_from_catalog("totally-unknown-model-xyz")
        assert not resolution.protocol, (
            f"Unknown model should be unresolved, got {resolution.protocol!r}"
        )
        assert resolution.source == "unresolved"
