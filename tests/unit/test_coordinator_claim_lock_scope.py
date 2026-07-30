"""Milestone B selection-claim lock-scope tests.

Verifies the new ``_selection_claim_lock`` semantics:

* Database I/O happens OUTSIDE the claim lock (the canonical
  SQLite transaction for request / reservation / attempt rows does
  not overlap with the lock-held window).
* Runtime publication re-acquires the claim lock a second time so a
  concurrent selector entering the claim phase next observes this
  attempt's runtime state.
* New span keys ``SPAN_SELECTION_CLAIM_HELD``,
  ``SPAN_SELECTION_CLAIM_WAIT``, ``SPAN_SELECTION_REVALIDATION``,
  ``SPAN_DISPATCH_PERSISTENCE_*``, ``SPAN_POST_COMMIT_PUBLICATION``
  are populated for a successful selection.
* The diagnostics module records ``claims_committed`` and
  ``claims_published`` exactly once per attempt.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
from eggpool.request.selection_claim_diagnostics import (
    SelectionClaimDiagnostics,
)
from eggpool.routing.router import Router
from eggpool.runtime_dispatch import (
    SPAN_ACCOUNT_LOOKUP,
    SPAN_CIRCUIT_PROBE,
    SPAN_CLAIM_ROLLBACK,  # noqa: F401  (registered; populated by compensation path)
    SPAN_DB_WRITE_ATTEMPT,
    SPAN_DB_WRITE_REQUEST,
    SPAN_DB_WRITE_RESERVATION,
    SPAN_DISPATCH_PERSISTENCE_COMMIT,
    SPAN_DISPATCH_PERSISTENCE_TRANSACTION,
    SPAN_DISPATCH_PERSISTENCE_WAIT,
    SPAN_POST_COMMIT_COMPENSATION,  # noqa: F401  (registered; populated by compensation path)
    SPAN_POST_COMMIT_PUBLICATION,
    SPAN_RUNTIME_PUBLICATION,
    SPAN_SELECTION_CLAIM_HELD,
    SPAN_SELECTION_CLAIM_WAIT,
    SPAN_SELECTION_REVALIDATION,
    DispatchSpanRecorder,
)


class _MockCatalog:
    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


class _StubClientPool:
    def get_default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_noop_handler))

    def get_client(
        self, provider_id: str | None = None, account_name: str | None = None
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_noop_handler))


async def _noop_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


async def _build_fixture(account_names: list[str]) -> dict[str, Any]:
    for name in account_names:
        os.environ[f"K_MB_{name}"] = "k"
    raw = {
        "providers": {
            "test-provider": {
                "id": "test-provider",
                "base_url": "https://api.example.com/v1",
                "protocols": ["openai"],
                "routing_priority": 0,
                "accounts": [
                    {
                        "name": name,
                        "api_key_env": f"K_MB_{name}",
                        "weight": 1.0,
                    }
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
                (name, f"K_MB_{name}", 1.0),
            )
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
    diagnostics = SelectionClaimDiagnostics()
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
        selection_claim_diagnostics=diagnostics,
    )
    return {
        "coordinator": coordinator,
        "db": db,
        "router": router,
        "recorder": recorder,
        "diagnostics": diagnostics,
    }


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
async def test_claim_lock_held_spans_populated_per_attempt() -> None:
    """Successful selection populates the new Milestone B claim spans."""
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        ctx = _make_context("req-claim-held")
        await fixture["coordinator"]._select_and_persist_attempt(ctx, 1)
        recorder = fixture["recorder"]
        recorded = set(recorder.spans())
        assert SPAN_SELECTION_CLAIM_HELD in recorded
        assert SPAN_SELECTION_CLAIM_WAIT in recorded
        assert SPAN_SELECTION_REVALIDATION in recorded
        assert SPAN_CIRCUIT_PROBE in recorded
        assert SPAN_ACCOUNT_LOOKUP in recorded
        assert SPAN_DISPATCH_PERSISTENCE_WAIT in recorded
        assert SPAN_DISPATCH_PERSISTENCE_TRANSACTION in recorded
        assert SPAN_DISPATCH_PERSISTENCE_COMMIT in recorded
        assert SPAN_POST_COMMIT_PUBLICATION in recorded
        assert SPAN_RUNTIME_PUBLICATION in recorded
        # Legacy DB-write spans are still populated via the helper.
        assert SPAN_DB_WRITE_REQUEST in recorded
        assert SPAN_DB_WRITE_RESERVATION in recorded
        assert SPAN_DB_WRITE_ATTEMPT in recorded
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_claim_diagnostics_record_committed_and_published() -> None:
    """Each attempt increments committed + published counters exactly once.

    The diagnostics module is the runtime-side mirror of the
    SQLite durable rows: a successful attempt publishes both
    counters exactly once.  The compensation counters stay at zero
    in the happy path.
    """
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        ctx = _make_context("req-claim-diag")
        await fixture["coordinator"]._select_and_persist_attempt(ctx, 1)
        snap = fixture["diagnostics"].snapshot()
        assert snap["claims_committed"] == 1
        assert snap["claims_published"] == 1
        assert snap["claims_rolled_back_before_persistence"] == 0
        assert snap["post_commit_publication_failures"] == 0
        assert snap["compensation_successes"] == 0
        assert snap["compensation_failures"] == 0
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_db_io_runs_outside_selection_claim_lock() -> None:
    """Milestone B acceptance criterion #1: no DB op under the claim lock.

    The DB transaction commits AFTER the first claim lock releases,
    so a contention snapshot can distinguish the two critical
    sections.  We install a probe around the DB transaction to
    verify it never overlaps the lock held window.
    """
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        coord = fixture["coordinator"]
        # Track lock-held / DB-open windows to assert they never overlap.
        claim_lock_held = asyncio.Event()
        db_open = asyncio.Event()
        overlap = {"overlap": False}

        original_tx = coord._db.transaction

        @asynccontextmanager
        async def instrumented_transaction() -> AsyncIterator[None]:
            if claim_lock_held.is_set():
                overlap["overlap"] = True
            db_open.set()
            try:
                async with original_tx():
                    yield None
            finally:
                db_open.clear()

        coord._db.transaction = instrumented_transaction  # type: ignore[method-assign]

        # Wrap the lock so we can observe claim_held during the DB window.
        original_lock = coord._selection_claim_lock

        class _ProbeLock:
            def __init__(self, inner: asyncio.Lock) -> None:
                self._inner = inner
                self._depth = 0

            async def __aenter__(self) -> None:
                self._depth += 1
                await self._inner.acquire()
                claim_lock_held.set()

            async def __aexit__(self, *_args: object) -> None:
                claim_lock_held.clear()
                self._inner.release()
                self._depth -= 1

        coord._selection_claim_lock = _ProbeLock(original_lock)  # type: ignore[assignment]

        ctx = _make_context("req-no-db-under-claim-lock")
        await coord._select_and_persist_attempt(ctx, 1)
        assert not overlap["overlap"], (
            "DB transaction overlapped _selection_claim_lock held window"
        )
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_publish_reacquires_claim_lock() -> None:
    """Publication runs under a second acquisition of the claim lock.

    The dispatch-stability plan requires that a concurrent selector
    entering the claim phase next observes this attempt's runtime
    state; the only way to guarantee that without holding the lock
    across the entire DB transaction is to re-acquire the lock for
    publication.
    """
    fixture = await _build_fixture(["alpha", "bravo"])
    try:
        coord = fixture["coordinator"]
        acquisitions: list[float] = []

        original_lock = coord._selection_claim_lock

        class _CountingLock:
            def __init__(self, inner: asyncio.Lock) -> None:
                self._inner = inner

            async def __aenter__(self) -> None:
                import time as _time

                acquisitions.append(_time.perf_counter_ns())
                await self._inner.acquire()

            async def __aexit__(self, *_args: object) -> None:
                self._inner.release()

        coord._selection_claim_lock = _CountingLock(original_lock)  # type: ignore[assignment]
        ctx = _make_context("req-reacquire")
        await coord._select_and_persist_attempt(ctx, 1)
        # Two acquisitions per successful attempt: claim + publish.
        assert len(acquisitions) == 2, (
            f"expected two lock acquisitions, got {len(acquisitions)}"
        )
    finally:
        await fixture["db"].disconnect()
