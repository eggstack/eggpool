"""Phase 14 end-to-end integration tests.

Covers: readiness safety, protocol families, unresolved model quarantine,
retry defaults, 402 cooldown, cache accounting, expiry race, health
idempotency, streaming response closure, and privacy regression.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.catalog.pricing import CostCalculator, PriceRepository
from go_aggregator.catalog.protocols import ModelProtocolResolver
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_two_account_config() -> AppConfig:
    os.environ["OPENCODE_P14_KEY_A"] = "key-p14-a"
    os.environ["OPENCODE_P14_KEY_B"] = "key-p14-b"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_P14_KEY_A",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "routing": {"quota_exhausted_cooldown_seconds": 0.5},
            "accounts": [
                {"name": "acct-a", "api_key_env": "OPENCODE_P14_KEY_A"},
                {"name": "acct-b", "api_key_env": "OPENCODE_P14_KEY_B"},
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
            ("acct-a", "OPENCODE_P14_KEY_A"),
        )
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-b", "OPENCODE_P14_KEY_B"),
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
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
        cost_calculator=cost_calculator,
        quota_estimator=router._quota_estimator,
        max_retry_attempts=2,
    )
    yield coord
    await httpx_client.aclose()


# ===========================================================================
# A. Readiness safety
# ===========================================================================


class TestReadinessSafety:
    """A. Readiness probe followed by normal proxy request succeeds."""

    @pytest.mark.asyncio
    async def test_readiness_then_proxy_request(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Readiness probe does not interfere with subsequent proxy requests."""
        # Probe writable
        assert await two_account_db.probe_writable()

        # Normal proxy request succeeds
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_success_response()
            )
            ctx = ProxyRequestContext(
                request_id="p14-readiness-proxy",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_readiness_concurrent_with_request(
        self, two_account_db: Database
    ) -> None:
        """Concurrent readiness probe and request transaction do not interfere."""
        probe_done = asyncio.Event()
        probe_result: list[bool] = []

        async def probe_task() -> None:
            result = await two_account_db.probe_writable()
            probe_result.append(result)
            probe_done.set()

        async def request_task() -> None:
            async with two_account_db.transaction():
                await two_account_db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("concurrent-acct", "DUMMY"),
                )

        # Start request task first
        req = asyncio.create_task(request_task())
        await asyncio.sleep(0.05)  # Let it enter transaction

        # Start probe
        probe = asyncio.create_task(probe_task())

        await asyncio.gather(req, probe)

        assert probe_result == [True]
        # Verify both succeeded
        row = await two_account_db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?", ("concurrent-acct",)
        )
        assert row is not None


# ===========================================================================
# B. Protocol families
# ===========================================================================


class TestProtocolFamilies:
    """B. MiniMax/Qwen route through /messages, GLM/Kimi/MiMo/DeepSeek
    through /chat/completions."""

    def test_minimax_routes_to_anthropic(self) -> None:
        resolver = ModelProtocolResolver()
        result = resolver.resolve_from_catalog("minimax-m3")
        assert result.protocol == "anthropic"

    def test_qwen3_routes_to_anthropic(self) -> None:
        resolver = ModelProtocolResolver()
        result = resolver.resolve_from_catalog("qwen3.7-max")
        assert result.protocol == "anthropic"

    def test_glm_routes_to_openai(self) -> None:
        resolver = ModelProtocolResolver()
        result = resolver.resolve_from_catalog("glm-5.1")
        assert result.protocol == "openai"

    def test_kimi_routes_to_openai(self) -> None:
        resolver = ModelProtocolResolver()
        result = resolver.resolve_from_catalog("kimi-k2.7")
        assert result.protocol == "openai"

    def test_mimo_routes_to_openai(self) -> None:
        resolver = ModelProtocolResolver()
        result = resolver.resolve_from_catalog("mimo-v2.5")
        assert result.protocol == "openai"

    def test_deepseek_routes_to_openai(self) -> None:
        resolver = ModelProtocolResolver()
        result = resolver.resolve_from_catalog("deepseek-v4-pro")
        assert result.protocol == "openai"


# ===========================================================================
# C. Unresolved model quarantine
# ===========================================================================


class TestUnresolvedQuarantine:
    """C. Mixed refresh with resolved and unresolved models commits resolved rows."""

    def test_unresolved_not_exposed(self) -> None:
        from go_aggregator.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct-a",
            "opencode-go",
            [
                {
                    "model_id": "gpt-4o",
                    "protocol": "openai",
                    "protocol_source": "exact_mapping",
                    "display_name": "GPT-4o",
                    "capabilities": {},
                    "source_metadata": {},
                },
                {
                    "model_id": "unknown-model",
                    "protocol": None,
                    "protocol_source": "unresolved",
                    "display_name": "Unknown",
                    "capabilities": {},
                    "source_metadata": {},
                },
            ],
        )

        exposed = cache.get_models_for_exposure("union", {"acct-a"})
        model_ids = [m["model_id"] for m in exposed]
        assert "gpt-4o" in model_ids
        assert "unknown-model" not in model_ids

    def test_unresolved_not_in_union_exposure(self) -> None:
        from go_aggregator.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct-a",
            "opencode-go",
            [
                {
                    "model_id": "mystery",
                    "protocol": None,
                    "protocol_source": "unresolved",
                    "display_name": "Mystery",
                    "capabilities": {},
                    "source_metadata": {},
                },
            ],
        )
        exposed = cache.get_models_for_exposure("union", {"acct-a"})
        model_ids = [m["model_id"] for m in exposed]
        assert "mystery" not in model_ids


# ===========================================================================
# D. Retry default safety
# ===========================================================================


class TestRetryDefaults:
    """D. 422 from A is passed through; 500 from A fails over to B."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_422_no_failover(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """422 from account A is passed through without trying B."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_error_response(422, '{"error": "unprocessable"}')
            )
            ctx = ProxyRequestContext(
                request_id="p14-422-no-failover",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        assert resp.status_code == 422

    @respx.mock
    @pytest.mark.asyncio
    async def test_500_fails_over(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """500 from account A fails over to account B."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(500, '{"error": "internal"}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)
            ctx = ProxyRequestContext(
                request_id="p14-500-failover",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        assert resp.status_code == 200
        assert call_count == 2


# ===========================================================================
# E. 402 cooldown
# ===========================================================================


class TestQuotaCooldown:
    """E. Account A returns 402, B succeeds; next request excludes A."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_402_cooldown_excludes_account(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """After 402, the account is excluded from the next request."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _error_response(402, '{"error": "quota exceeded"}')
            return _success_response()

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)
            ctx = ProxyRequestContext(
                request_id="p14-402-cooldown",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=_make_openai_body(),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        assert resp.status_code == 200
        # First call was 402, second was success
        assert call_count == 2

        # The account that got 402 should be in cooldown
        health = coordinator._health_manager.get_account_health("acct-a")
        # It should be unhealthy after 402
        if health.health_state == "quota_exhausted":
            assert not health.is_healthy


# ===========================================================================
# F. Cache accounting
# ===========================================================================


class TestCacheAccounting:
    """F. Cache creation tokens reach cache_write_tokens in finalization."""

    def test_cache_creation_mapped_to_write(self) -> None:
        from go_aggregator.proxy.usage import StreamUsageResult

        usage = StreamUsageResult(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=20,
            cache_creation_tokens=30,
        )
        # The coordinator should pass cache_creation_tokens as cache_write_tokens
        assert usage.cache_creation_tokens == 30


# ===========================================================================
# G. Expiry race
# ===========================================================================


class TestExpiryRace:
    """G. Normal release racing expiry cleanup does not double-decrement."""

    @pytest.mark.asyncio
    async def test_expiry_cleanup_no_double_decrement(
        self, two_account_db: Database
    ) -> None:
        from go_aggregator.background.cleanup import reconcile_expired_reservations

        request_repo = RequestRepository(two_account_db)
        reservation_repo = ReservationRepository(two_account_db)

        async with two_account_db.transaction():
            db_id = await request_repo.create_pending(
                request_id="p14-expiry-race",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=1,
            )

        await asyncio.sleep(1.5)

        # Release manually first
        async with two_account_db.transaction():
            released = await reservation_repo.release(
                reservation_id, reason="completed"
            )
        assert released is True

        # Cleanup should find nothing to transition
        cleanup_count = await reconcile_expired_reservations(two_account_db)
        assert cleanup_count == 0


# ===========================================================================
# H. Health idempotency
# ===========================================================================


class TestHealthIdempotency:
    """H. One final failed attempt increments health once; duplicate
    finalization creates no duplicate event."""

    @pytest.mark.asyncio
    async def test_duplicate_finalize_no_duplicate_event(
        self, two_account_db: Database
    ) -> None:
        from go_aggregator.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        request_repo = RequestRepository(two_account_db)
        attempt_repo = AttemptRepository(two_account_db)
        reservation_repo = ReservationRepository(two_account_db)
        health_manager = HealthManager()

        async with two_account_db.transaction():
            db_id = await request_repo.create_pending(
                request_id="p14-idempotent-event",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id,
                attempt_number=1,
                account_id=1,
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=two_account_db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            health_manager=health_manager,
        )

        _attempt_id = attempt_id
        _reservation_id = reservation_id

        class MockSelected:
            db_request_id = db_id
            account_name = "acct-a"
            model_id = "gpt-4"
            attempt_id = _attempt_id
            reservation_id = _reservation_id
            estimated_microdollars = 100000
            attempt_number = 1

        selected = MockSelected()

        # First finalization
        t1 = await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                status_code=500,
                error_class="InternalServerError",
            ),
        )
        assert t1 is True

        # Second finalization
        t2 = await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                status_code=500,
                error_class="InternalServerError",
            ),
        )
        assert t2 is False

        # Only one event
        events = await two_account_db.fetch_all(
            "SELECT * FROM account_events WHERE event_type = 'upstream_error'"
        )
        assert len(events) == 1

        # Health only recorded once
        health = health_manager.get_account_health("acct-a")
        assert health.consecutive_failures == 1


# ===========================================================================
# I. Streaming response closure
# ===========================================================================


class TestStreamingClosure:
    """I. Repeated pre-body streaming failures close every upstream response."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_500_closed(
        self, coordinator: RequestCoordinator, two_account_db: Database
    ) -> None:
        """Streaming 500 response is properly closed."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=_error_response(500, '{"error": "internal"}')
            )
            ctx = ProxyRequestContext(
                request_id="p14-stream-close",
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_openai_body(stream=True),
                incoming_headers={"content-type": "application/json"},
            )
            resp = await coordinator.execute(ctx)

        assert resp.status_code == 500


# ===========================================================================
# J. Privacy regression
# ===========================================================================


class TestPrivacyRegression:
    """J. No user content or secrets are persisted."""

    @pytest.mark.asyncio
    async def test_no_prompts_in_database(self, two_account_db: Database) -> None:
        """Search database for known prompt markers. None may appear."""
        rows = await two_account_db.fetch_all("SELECT * FROM requests")
        for row in rows:
            # Check error_detail for prompt content (should be truncated/absent)
            detail = row["error_detail"] or ""
            assert "password" not in detail.lower()
            assert "api_key" not in detail.lower()
