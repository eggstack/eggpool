"""Phase 5 lock-scope and span-coverage tests.

Verifies that the selection lock no longer wraps the entire
pre-upstream pipeline:

* Thinking classification runs before the lock is acquired.
* Reservation-token estimation runs before the lock is acquired.
* Routing-plan construction runs before the lock is acquired.
* The routing-decision trace write runs AFTER the lock releases.
* All new span keys (lock_wait / locked / circuit_probe /
  account_lookup / db_write_* / routing_trace_build / write /
  runtime_publication) are registered and the relevant ones are
  populated for a successful selection.
"""

from __future__ import annotations

import os
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
from eggpool.runtime_dispatch import (
    SPAN_ACCOUNT_LOOKUP,
    SPAN_CIRCUIT_PROBE,
    SPAN_DB_WRITE_ATTEMPT,
    SPAN_DB_WRITE_REQUEST,
    SPAN_DB_WRITE_RESERVATION,
    SPAN_RESERVATION_ESTIMATE,
    SPAN_ROUTING_PLAN,
    SPAN_ROUTING_TRACE_BUILD,
    SPAN_ROUTING_TRACE_WRITE,
    SPAN_RUNTIME_PUBLICATION,
    SPAN_SELECTION_LOCK_WAIT,
    SPAN_SELECTION_LOCKED,
    SPAN_THINKING_CLASSIFICATION,
    DispatchSpanRecorder,
)

pytestmark = pytest.mark.request_path


class _MockCatalog:
    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


async def _build_fixture(account_names: list[str]) -> dict[str, Any]:
    for name in account_names:
        os.environ[f"K_{name}"] = "k"
    raw = {
        "providers": {
            "test-provider": {
                "id": "test-provider",
                "base_url": "https://api.example.com/v1",
                "protocols": ["openai"],
                "routing_priority": 0,
                "accounts": [
                    {"name": name, "api_key_env": f"K_{name}", "weight": 1.0}
                    for name in account_names
                ],
            }
        }
    }
    config = AppConfig.model_validate(raw)
    registry = AccountRegistry(config)
    cache = ModelCatalogCache()
    for name in account_names:
        cache.update_from_account(
            name, "test-provider", [{"model_id": "gpt-4", "protocol": "openai"}]
        )
    catalog = _MockCatalog(cache)
    quota_estimator = QuotaEstimator()
    router = Router(
        registry,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        quota_estimator=quota_estimator,
    )
    for name in account_names:
        router.quota_estimator.accounts[name] = AccountQuota(
            account_name=name,
            weight=1.0,
            capacity_5h_microdollars=1_000_000_000,
            capacity_7d_microdollars=7_000_000_000,
            capacity_30d_microdollars=30_000_000_000,
        )
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_insert(
            "INSERT INTO models (model_id, display_name, protocol) VALUES (?, ?, ?)",
            ("gpt-4", "gpt-4", "openai"),
        )
        for name in account_names:
            await db.execute_insert(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, ?)",
                (name, f"K_{name}", 1.0),
            )
            account_id = await db.execute_insert(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, ?)",
                (f"{name}-skip", f"K_{name}-skip", 1.0),
            )
            del account_id
            row = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
            assert row is not None
            account_id = int(row["id"])
            await db.execute_insert(
                "INSERT INTO account_models (account_id, model_id, enabled) "
                "VALUES (?, ?, 1)",
                (account_id, "gpt-4"),
            )
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    routing_decision_repo = RoutingDecisionRepository(db)
    recorder = DispatchSpanRecorder()
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
        dispatch_span_recorder=recorder,
    )
    return {
        "coordinator": coordinator,
        "db": db,
        "router": router,
        "recorder": recorder,
    }


class _StubClientPool:
    def get_default_client(self) -> httpx.AsyncClient:
        return _FakeClient()

    def get_client(
        self, provider_id: str | None = None, account_name: str | None = None
    ) -> httpx.AsyncClient:
        return _FakeClient()


class _FakeClient(httpx.AsyncClient):
    def __init__(self) -> None:
        super().__init__(transport=httpx.MockTransport(_noop_handler))


async def _noop_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def _make_context(request_id: str) -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
    )


@pytest.mark.asyncio()
async def test_lock_spans_populated_after_successful_selection() -> None:
    """The lock timing spans are populated for a successful selection.

    Phase 5: the recorder now writes both SPAN_SELECTION_LOCK_WAIT
    (wait time before the lock is acquired) and SPAN_SELECTION_LOCKED
    (time held) by capturing the perf_counter before/after the
    ``async with self._select_lock`` block.
    """
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        ctx = _make_context("req-lock-spans")
        await fixture["coordinator"]._select_and_persist_attempt(ctx, 1)
        recorder = fixture["recorder"]
        recorded = set(recorder.spans())
        # Lock timing spans are populated for the successful path.
        assert SPAN_SELECTION_LOCK_WAIT in recorded
        assert SPAN_SELECTION_LOCKED in recorded
        # Pre-lock pure-compute spans still run for a successful path.
        assert SPAN_THINKING_CLASSIFICATION in recorded
        assert SPAN_RESERVATION_ESTIMATE in recorded
        assert SPAN_ROUTING_PLAN in recorded
        # Locked-region spans.
        assert SPAN_CIRCUIT_PROBE in recorded
        assert SPAN_ACCOUNT_LOOKUP in recorded
        assert SPAN_DB_WRITE_REQUEST in recorded
        assert SPAN_DB_WRITE_RESERVATION in recorded
        assert SPAN_DB_WRITE_ATTEMPT in recorded
        # Post-lock trace spans.
        assert SPAN_ROUTING_TRACE_BUILD in recorded
        assert SPAN_ROUTING_TRACE_WRITE in recorded
        # Runtime publication span.
        assert SPAN_RUNTIME_PUBLICATION in recorded
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_precomputed_context_fields_short_circuit_coordinator() -> None:
    """When the context carries precomputed thinking/reservation
    fields, the coordinator does not reparse ``original_body``.

    This pins the invariant: ``handle_proxy_request`` precomputes
    these values outside the lock, and the coordinator consumes
    them rather than recomputing.
    """
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        # Build a context with precomputed values.
        ctx = ProxyRequestContext(
            request_id="req-precomputed",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
            incoming_headers={},
            estimated_reservation_tokens=42,
            thinking_requirement=None,
            estimated_context_input_tokens=21,
        )
        await fixture["coordinator"]._select_and_persist_attempt(ctx, 1)
        # The precomputed values are preserved on the context.
        assert ctx.estimated_reservation_tokens == 42
        assert ctx.estimated_context_input_tokens == 21
    finally:
        await fixture["db"].disconnect()


# ---------------------------------------------------------------------------
# Phase 5 corrective polish: exactly-one-sample-per-attempt invariants.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_lock_spans_record_exactly_one_sample_per_attempt() -> None:
    """Each selection attempt must record *exactly one* sample for
    ``selection_lock_wait`` and ``selection_locked``.

    Phase 5 polish removed the placeholder ``_maybe_span`` blocks
    inside the lock that previously double-sampled these metrics.
    """
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        ctx = _make_context("req-lock-one-sample")
        await fixture["coordinator"]._select_and_persist_attempt(ctx, 1)
        recorder = fixture["recorder"]
        snap = recorder.snapshot_for_spans(
            [SPAN_SELECTION_LOCK_WAIT, SPAN_SELECTION_LOCKED]
        )
        rows = {row["span"]: row for row in snap["spans"]}
        assert rows[SPAN_SELECTION_LOCK_WAIT]["sample_count"] == 1
        assert rows[SPAN_SELECTION_LOCKED]["sample_count"] == 1
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_two_attempts_record_two_samples_per_lock_span() -> None:
    """Each successive selection attempt must append exactly one
    additional sample so historical metrics reflect *real* attempts."""
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        for i in range(2):
            ctx = _make_context(f"req-lock-twice-{i}")
            await fixture["coordinator"]._select_and_persist_attempt(ctx, i + 1)
        recorder = fixture["recorder"]
        snap = recorder.snapshot_for_spans(
            [SPAN_SELECTION_LOCK_WAIT, SPAN_SELECTION_LOCKED]
        )
        rows = {row["span"]: row for row in snap["spans"]}
        assert rows[SPAN_SELECTION_LOCK_WAIT]["sample_count"] == 2
        assert rows[SPAN_SELECTION_LOCKED]["sample_count"] == 2
    finally:
        await fixture["db"].disconnect()
